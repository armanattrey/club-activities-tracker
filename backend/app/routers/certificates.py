"""Issuing, listing, downloading and revoking certificates.

PDF generation is NOT done here. We insert a 'pending' row and queue a Celery
task; the worker renders the PDF. Why: rendering is CPU-heavy and must never
sit in a web request path.
"""
import logging
import uuid
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core import audit
from app.core.permissions import (
    CLUB_MANAGER_ROLES, assert_club_access, current_user, require_role,
)
from app.db import get_conn
from app.services import certificates as certs
from app.services.storage import get_storage

log = logging.getLogger(__name__)
router = APIRouter(tags=["certificates"])

ADVISOR_ROLES = ("advisor", "campus_admin", "super_admin")
VIEW_ROLES = ("coordinator", "advisor", "campus_admin", "super_admin")

_CONTEXT_SQL = """
    SELECT u.name AS student_name, c.name AS club_name, ca.name AS campus_name,
           d.title, d.day_date, d.club_id
    FROM club_days d
    JOIN clubs c   ON c.id  = d.club_id
    JOIN campus ca ON ca.id = c.campus_id
    JOIN users u   ON u.id  = $1
    WHERE d.id = $2"""

_INSERT_SQL = """
    INSERT INTO certificates
      (id, student_id, kind, ref_type, ref_id, content_hmac,
       issued_by, issued_at, snapshot, status)
    VALUES ($1,$2,$3,'club_day',$4,$5,$6,$7,$8,'pending')
    ON CONFLICT (student_id, kind, ref_type, ref_id) DO NOTHING
    RETURNING *"""


class ParticipationIn(BaseModel):
    club_day_id: int


class AchievementIn(BaseModel):
    student_id: int
    club_day_id: int
    achievement: str = Field(min_length=3, max_length=120)


class RevokeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def _public(row) -> dict:
    snap = certs.as_dict(row["snapshot"])
    return {
        "id": str(row["id"]), "kind": row["kind"], "status": row["status"],
        "ref_type": row["ref_type"], "ref_id": row["ref_id"],
        "student_id": row["student_id"], "student_name": snap.get("student_name"),
        "club_name": snap.get("club_name"), "title": snap.get("title"),
        "day_date": snap.get("day_date"), "achievement": snap.get("achievement"),
        "issued_at": row["issued_at"], "revoked": row["revoked_at"] is not None,
        "pdf_sha256": row["pdf_sha256"],
    }


async def _enqueue(cert_ids: list):
    """Best effort: if this fails, the sweeper task picks the rows up within a minute."""
    def _send():
        from workers.celery_app import celery
        for cid in cert_ids:
            celery.send_task("workers.tasks_certificates.generate_certificate",
                             args=[str(cid)])
    try:
        await anyio.to_thread.run_sync(_send)
    except Exception:
        log.warning("could not queue certificate task; the sweeper will retry", exc_info=True)


async def _issue(conn, *, student_id: int, kind: str, club_day_id: int,
                 issued_by: int | None, achievement: str | None = None):
    """Insert a pending certificate. Returns (row, created). If one already exists
    for (student, kind, club day) the existing row comes back with created=False."""
    ctx = await conn.fetchrow(_CONTEXT_SQL, student_id, club_day_id)
    if ctx is None:
        raise HTTPException(404, "Club day or student not found")

    snapshot = {
        "student_name": ctx["student_name"], "club_name": ctx["club_name"],
        "campus_name": ctx["campus_name"], "title": ctx["title"] or "Club Day",
        "day_date": ctx["day_date"].isoformat(), "achievement": achievement,
    }
    cert_id = uuid.uuid4()
    issued_at = datetime.now(timezone.utc)
    mac = certs.compute_hmac(
        cert_id=str(cert_id), kind=kind, ref_type="club_day", ref_id=club_day_id,
        student_id=student_id, issued_by=issued_by, issued_at=issued_at, snapshot=snapshot,
    )
    row = await conn.fetchrow(
        _INSERT_SQL, cert_id, student_id, kind, club_day_id, mac,
        issued_by, issued_at, snapshot,
    )
    if row is None:
        existing = await conn.fetchrow(
            """SELECT * FROM certificates
               WHERE student_id=$1 AND kind=$2 AND ref_type='club_day' AND ref_id=$3""",
            student_id, kind, club_day_id,
        )
        return existing, False

    await audit.log(
        conn, actor_id=issued_by, action="certificate.issue",
        entity="certificates", entity_id=cert_id,
        after={"kind": kind, "student_id": student_id, "club_day_id": club_day_id},
    )
    return row, True


async def _club_of(conn, certificate_id: uuid.UUID, lock: bool = False):
    row = await conn.fetchrow(
        f"""SELECT c.*, d.club_id FROM certificates c
            JOIN club_days d ON d.id = c.ref_id
            WHERE c.id=$1 AND c.ref_type='club_day'
            {"FOR UPDATE OF c" if lock else ""}""",
        certificate_id,
    )
    if row is None:
        raise HTTPException(404, "Certificate not found")
    return row


# ------------------------------------------------------------------ issuing
@router.post("/certificates/participation")
async def claim_participation(
    body: ParticipationIn,
    response: Response,
    user: dict = Depends(require_role("student")),
    conn=Depends(get_conn),
):
    """A student claims their participation certificate. Needs verified attendance."""
    async with conn.transaction():
        attended = await conn.fetchval(
            """SELECT 1 FROM attendance a
               JOIN checkin_sessions s ON s.id = a.session_id
               WHERE a.student_id=$1 AND s.club_day_id=$2 LIMIT 1""",
            user["id"], body.club_day_id,
        )
        if not attended:
            raise HTTPException(403, "You have no verified attendance for this club day")
        row, created = await _issue(
            conn, student_id=user["id"], kind="participation",
            club_day_id=body.club_day_id, issued_by=None,  # None = issued by the system
        )
    if created:
        await _enqueue([row["id"]])
        response.status_code = 201
    return {**_public(row), "already_issued": not created}


@router.post("/club-days/{club_day_id}/certificates/participation")
async def issue_participation_bulk(
    club_day_id: int,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    """Coordinator issues participation certificates for everyone who checked in."""
    day = await conn.fetchrow("SELECT club_id FROM club_days WHERE id=$1", club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")
    await assert_club_access(conn, user, day["club_id"])

    new_ids = []
    async with conn.transaction():
        students = await conn.fetch(
            """SELECT DISTINCT a.student_id FROM attendance a
               JOIN checkin_sessions s ON s.id = a.session_id
               WHERE s.club_day_id=$1""",
            club_day_id,
        )
        for s in students:
            row, created = await _issue(
                conn, student_id=s["student_id"], kind="participation",
                club_day_id=club_day_id, issued_by=user["id"],
            )
            if created:
                new_ids.append(row["id"])
    await _enqueue(new_ids)
    return {"attendees": len(students), "issued": len(new_ids),
            "already_had": len(students) - len(new_ids)}


@router.post("/certificates/achievement", status_code=201)
async def issue_achievement(
    body: AchievementIn,
    user: dict = Depends(require_role(*ADVISOR_ROLES)),
    conn=Depends(get_conn),
):
    """Only the club's advisor (or an admin) can issue an achievement certificate."""
    day = await conn.fetchrow("SELECT club_id FROM club_days WHERE id=$1", body.club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")
    await assert_club_access(conn, user, day["club_id"], allow=ADVISOR_ROLES)

    member = await conn.fetchval(
        """SELECT 1 FROM club_memberships
           WHERE student_id=$1 AND club_id=$2 AND status='approved'""",
        body.student_id, day["club_id"],
    )
    if not member:
        raise HTTPException(422, "Student is not an approved member of this club")

    async with conn.transaction():
        row, created = await _issue(
            conn, student_id=body.student_id, kind="achievement",
            club_day_id=body.club_day_id, issued_by=user["id"],
            achievement=body.achievement.strip(),
        )
    if not created:
        raise HTTPException(409, "This student already has an achievement certificate for this club day")
    await _enqueue([row["id"]])
    return _public(row)


# ------------------------------------------------------------------ reading
@router.get("/certificates/me")
async def my_certificates(
    user: dict = Depends(require_role("student")), conn=Depends(get_conn)
):
    rows = await conn.fetch(
        "SELECT * FROM certificates WHERE student_id=$1 ORDER BY issued_at DESC", user["id"]
    )
    return [_public(r) for r in rows]


@router.get("/club-days/{club_day_id}/certificates")
async def day_certificates(
    club_day_id: int,
    user: dict = Depends(require_role(*VIEW_ROLES)),
    conn=Depends(get_conn),
):
    day = await conn.fetchrow("SELECT club_id FROM club_days WHERE id=$1", club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")
    await assert_club_access(conn, user, day["club_id"], allow=VIEW_ROLES)
    rows = await conn.fetch(
        """SELECT * FROM certificates
           WHERE ref_type='club_day' AND ref_id=$1 ORDER BY issued_at""",
        club_day_id,
    )
    return [_public(r) for r in rows]


@router.get("/certificates/{certificate_id}/download")
async def download(
    certificate_id: uuid.UUID,
    user: dict = Depends(current_user),
    conn=Depends(get_conn),
):
    row = await _club_of(conn, certificate_id)
    if user["role"] == "student":
        if row["student_id"] != user["id"]:
            raise HTTPException(404, "Certificate not found")  # don't reveal it exists
    else:
        await assert_club_access(conn, user, row["club_id"], allow=VIEW_ROLES)

    if row["status"] != "ready":
        raise HTTPException(409, f"Certificate is not ready yet (status: {row['status']})")
    path = get_storage().path_for(row["object_key"])
    if not path.exists():
        raise HTTPException(404, "Certificate file is missing from storage")
    return FileResponse(
        path, media_type="application/pdf",
        filename=f"certificate-{certificate_id}.pdf",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# --------------------------------------------------------------- revocation
@router.post("/certificates/{certificate_id}/revoke")
async def revoke(
    certificate_id: uuid.UUID,
    body: RevokeIn,
    user: dict = Depends(require_role(*ADVISOR_ROLES)),
    conn=Depends(get_conn),
):
    async with conn.transaction():
        row = await _club_of(conn, certificate_id, lock=True)
        await assert_club_access(conn, user, row["club_id"], allow=ADVISOR_ROLES)
        if row["revoked_at"] is not None:
            raise HTTPException(409, "Certificate is already revoked")
        await conn.execute(
            "UPDATE certificates SET revoked_at=now(), revoke_reason=$2 WHERE id=$1",
            certificate_id, body.reason,
        )
        await audit.log(
            conn, actor_id=user["id"], action="certificate.revoke",
            entity="certificates", entity_id=certificate_id,
            before={"revoked": False}, after={"revoked": True}, reason=body.reason,
        )
    return {"id": str(certificate_id), "revoked": True}