"""Issuing, listing, downloading and revoking certificates.

PDF rendering stays out of the request path. Production queues it through
Celery; local mode uses an in-process worker and the same database state.
"""
import logging
import uuid
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core import audit
from app.core.permissions import (
    ADMIN_ROLES, CLUB_MANAGER_ROLES, assert_club_access, current_user, require_role,
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
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending')
    ON CONFLICT (student_id, kind, ref_type, ref_id) DO NOTHING
    RETURNING *"""


class ParticipationIn(BaseModel):
    club_day_id: int


class AchievementIn(BaseModel):
    student_id: int
    club_day_id: int
    achievement: str = Field(min_length=3, max_length=120)


class EventAchievementIn(BaseModel):
    student_id: int
    achievement: str = Field(min_length=3, max_length=120)


class RevokeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def _public(row) -> dict:
    snap = certs.as_dict(row["snapshot"])
    return {
        "id": str(row["id"]), "kind": row["kind"], "status": row["status"],
        "ref_type": row["ref_type"], "ref_id": row["ref_id"],
        "student_id": row["student_id"], "student_name": snap.get("student_name"),
        "club_name": snap.get("club_name"), "event_name": snap.get("event_name"), "title": snap.get("title"),
        "day_date": snap.get("day_date"), "achievement": snap.get("achievement"),
        "issued_at": row["issued_at"], "revoked": row["revoked_at"] is not None,
        "pdf_sha256": row["pdf_sha256"],
    }


async def _enqueue(cert_ids: list):
    """Queue locally or in production; the persisted pending row is the fallback."""
    if settings.app_mode == "local":
        try:
            from app.local_workers import enqueue_certificate
            for cid in cert_ids:
                await enqueue_certificate(str(cid))
        except Exception:
            log.warning("could not queue local certificate task; the sweeper will retry", exc_info=True)
        return

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
        _INSERT_SQL, cert_id, student_id, kind, "club_day", club_day_id, mac,
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


async def _event_certificate_context(conn, event_id: int, student_id: int):
    return await conn.fetchrow("""SELECT u.name AS student_name, ca_student.name AS campus_name,
      e.name AS event_name, e.starts_at, e.campus_id, u.campus_id AS student_campus_id
      FROM events e JOIN users u ON u.id=$2 JOIN campus ca_student ON ca_student.id=u.campus_id
      WHERE e.id=$1 AND u.role='student'""", event_id, student_id)


async def _issue_event(conn, *, student_id: int, event_id: int, kind: str,
                       issued_by: int | None, achievement: str | None = None):
    ctx = await _event_certificate_context(conn, event_id, student_id)
    if ctx is None:
        raise HTTPException(404, "Event or student not found")
    snapshot = {"student_name": ctx["student_name"], "campus_name": ctx["campus_name"],
                "event_name": ctx["event_name"], "day_date": ctx["starts_at"].date().isoformat() if ctx["starts_at"] else "",
                "achievement": achievement}
    cert_id = uuid.uuid4()
    issued_at = datetime.now(timezone.utc)
    mac = certs.compute_hmac(cert_id=str(cert_id), kind=kind, ref_type="event", ref_id=event_id,
      student_id=student_id, issued_by=issued_by, issued_at=issued_at, snapshot=snapshot)
    row = await conn.fetchrow(_INSERT_SQL, cert_id, student_id, kind, "event", event_id, mac,
      issued_by, issued_at, snapshot)
    if row is None:
        existing = await conn.fetchrow("""SELECT * FROM certificates WHERE student_id=$1 AND kind=$2
          AND ref_type='event' AND ref_id=$3""", student_id, kind, event_id)
        return existing, False
    await audit.log(conn, actor_id=issued_by, action="certificate.issue", entity="certificates",
      entity_id=cert_id, after={"kind": kind, "student_id": student_id, "event_id": event_id})
    return row, True


async def _certificate_resource(conn, certificate_id: uuid.UUID, lock=False):
    row = await conn.fetchrow(f"SELECT * FROM certificates WHERE id=$1 {'FOR UPDATE' if lock else ''}", certificate_id)
    if row is None:
        raise HTTPException(404, "Certificate not found")
    if row["ref_type"] == "club_day":
        resource = await conn.fetchrow("SELECT club_id FROM club_days WHERE id=$1", row["ref_id"])
        row = dict(row)
        row["club_id"] = resource["club_id"] if resource else None
    elif row["ref_type"] == "event":
        resource = await conn.fetchrow("SELECT campus_id FROM events WHERE id=$1", row["ref_id"])
        row = dict(row)
        row["event_campus_id"] = resource["campus_id"] if resource else None
    return row


async def _assert_certificate_access(conn, user, row, allow_students=True):
    if user["role"] == "student":
        if allow_students and row["student_id"] == user["id"]:
            return
        raise HTTPException(404, "Certificate not found")
    if row["ref_type"] == "club_day":
        if row.get("club_id") is None:
            raise HTTPException(404, "Certificate not found")
        await assert_club_access(conn, user, row["club_id"], allow=VIEW_ROLES)
    elif row["ref_type"] == "event":
        if user["role"] == "super_admin":
            return
        if user["role"] != "campus_admin" or user["campus_id"] != row.get("event_campus_id"):
            raise HTTPException(404, "Certificate not found")
    else:
        raise HTTPException(404, "Certificate not found")


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


@router.post("/events/{event_id}/certificates/participation")
async def issue_event_participation(event_id: int,
    user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    event = await conn.fetchrow("SELECT campus_id FROM events WHERE id=$1", event_id)
    if event is None or (user["role"] != "super_admin" and event["campus_id"] != user["campus_id"]):
        raise HTTPException(404, "Event not found")
    new_ids = []
    async with conn.transaction():
        students = await conn.fetch("SELECT student_id FROM registrations WHERE event_id=$1 AND checked_in_at IS NOT NULL", event_id)
        for student in students:
            row, created = await _issue_event(conn, student_id=student["student_id"],
              event_id=event_id, kind="participation", issued_by=user["id"])
            if created:
                new_ids.append(row["id"])
    await _enqueue(new_ids)
    return {"attendees": len(students), "issued": len(new_ids), "already_had": len(students)-len(new_ids)}


@router.post("/events/{event_id}/certificates/participation/claim")
async def claim_event_participation(event_id: int, response: Response,
    user: dict = Depends(require_role("student")), conn=Depends(get_conn)):
    checked_in = await conn.fetchval("SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2 AND checked_in_at IS NOT NULL", event_id, user["id"])
    if not checked_in:
        raise HTTPException(403, "Verified event check-in is required")
    async with conn.transaction():
        row, created = await _issue_event(conn, student_id=user["id"], event_id=event_id, kind="participation", issued_by=None)
    if created:
        await _enqueue([row["id"]])
        response.status_code = 201
    return {**_public(row), "already_issued": not created}


@router.post("/events/{event_id}/certificates/achievement", status_code=201)
async def issue_event_achievement(event_id: int, body: EventAchievementIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    event = await conn.fetchrow("SELECT campus_id FROM events WHERE id=$1", event_id)
    if event is None or (user["role"] != "super_admin" and event["campus_id"] != user["campus_id"]):
        raise HTTPException(404, "Event not found")
    ranked = await conn.fetchval("""SELECT 1 FROM event_results r WHERE r.event_id=$1 AND
      (r.student_id=$2 OR r.team_id IN (SELECT team_id FROM team_members WHERE event_id=$1 AND student_id=$2))""", event_id, body.student_id)
    if not ranked:
        raise HTTPException(422, "Student must appear in published event results")
    async with conn.transaction():
        row, created = await _issue_event(conn, student_id=body.student_id, event_id=event_id,
          kind="achievement", issued_by=user["id"], achievement=body.achievement.strip())
    if not created:
        raise HTTPException(409, "This student already has an achievement certificate for this event")
    await _enqueue([row["id"]])
    return _public(row)


@router.get("/events/{event_id}/certificates")
async def event_certificates(event_id: int, user: dict = Depends(require_role(*VIEW_ROLES)), conn=Depends(get_conn)):
    event = await conn.fetchrow("SELECT campus_id FROM events WHERE id=$1", event_id)
    if event is None or (user["role"] != "super_admin" and event["campus_id"] != user["campus_id"]):
        raise HTTPException(404, "Event not found")
    rows = await conn.fetch("SELECT * FROM certificates WHERE ref_type='event' AND ref_id=$1 ORDER BY issued_at", event_id)
    return [_public(r) for r in rows]


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
    row = await _certificate_resource(conn, certificate_id)
    await _assert_certificate_access(conn, user, row)

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
        row = await _certificate_resource(conn, certificate_id, lock=True)
        await _assert_certificate_access(conn, user, row, allow_students=False)
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
