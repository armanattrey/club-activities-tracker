"""Club Day CRUD, plan approval and lifecycle transitions.

Lifecycle: planned -> approved -> announced -> open -> submissions -> evaluating -> closed
- planned -> approved happens ONLY through the advisor approving the plan.
- every other step goes through POST /{id}/transition (rules in core/state_machines.py).
"""
import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from datetime import date, datetime

from app.core import audit
from app.core.permissions import (
    CLUB_MANAGER_ROLES, assert_club_access, current_user, require_role,
)
from app.core.state_machines import CLUB_DAY, transition
from app.db import get_conn
from app.services import checkin

router = APIRouter(prefix="/club-days", tags=["club-days"])

ADVISOR_ROLES = ("advisor", "campus_admin", "super_admin")
VIEW_ROLES = ("coordinator", "advisor", "campus_admin", "super_admin")
STUDENT_VISIBLE = ("announced", "open", "submissions", "evaluating", "closed")


class ClubDayIn(BaseModel):
    club_id: int
    day_date: date
    title: str | None = None
    # must include a timezone offset, e.g. 2026-10-20T17:00:00+05:30
    submission_deadline: AwareDatetime | None = None


class PlanIn(BaseModel):
    body: str | None = Field(default=None, min_length=10)
    topic: str | None = Field(default=None, min_length=2, max_length=240)
    format: str | None = Field(default=None, min_length=2, max_length=120)
    deliverable: str | None = Field(default=None, min_length=2, max_length=500)
    venue: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def require_plan_content(self):
        if self.body is None and not all((self.topic, self.format, self.deliverable)):
            raise ValueError("Provide body or all of topic, format and deliverable")
        return self


class PlanDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


class TransitionIn(BaseModel):
    target: Literal["announced", "open", "submissions", "evaluating", "closed"]


class PresenterIn(BaseModel):
    student_id: int
    title: str = Field(min_length=2, max_length=240)
    notes: str | None = Field(default=None, max_length=1000)


async def _visible_day(conn, user: dict, club_day_id: int):
    day = await conn.fetchrow("SELECT * FROM club_days WHERE id=$1", club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")
    if user["role"] == "student":
        member = await conn.fetchval(
            """SELECT 1 FROM club_memberships
               WHERE student_id=$1 AND club_id=$2 AND status='approved'""",
            user["id"], day["club_id"],
        )
        # students must not even learn that unannounced club days exist
        if not member or day["status"] not in STUDENT_VISIBLE:
            raise HTTPException(404, "Club day not found")
    else:
        await assert_club_access(conn, user, day["club_id"], allow=VIEW_ROLES)
    return day


@router.post("", status_code=201)
async def create_club_day(
    body: ClubDayIn,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    club = await assert_club_access(conn, user, body.club_id)
    if club["approval_status"] != "approved":
        raise HTTPException(409, "Club must be approved before scheduling a Club Day")
    row = await conn.fetchrow(
        """INSERT INTO club_days (club_id, day_date, title, submission_deadline, created_by)
           VALUES ($1,$2,$3,$4,$5) RETURNING *""",
        body.club_id, body.day_date, body.title, body.submission_deadline, user["id"],
    )
    return dict(row)


@router.post("/import", status_code=201)
async def import_club_days(
    file: UploadFile = File(...),
    club_ids: str = Form(..., description="Comma-separated club IDs"),
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    """Import academic-calendar dates from CSV for selected clubs.

    Required headers: day_date (YYYY-MM-DD). Optional: title, submission_deadline
    (ISO 8601 timestamp including timezone). One row is created per date and club.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(415, "Upload a .csv calendar file")
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Calendar file must be 2 MB or smaller")
    try:
        decoded = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        headers = {h.strip() for h in (reader.fieldnames or []) if h}
        if "day_date" not in headers:
            raise ValueError("CSV must include a day_date column")
        parsed = []
        for line, values in enumerate(reader, start=2):
            if len(parsed) >= 500:
                raise ValueError("Import is limited to 500 calendar rows")
            values = {str(k).strip(): (v or "").strip() for k, v in values.items() if k}
            if not values.get("day_date"):
                raise ValueError(f"Line {line}: day_date is required")
            deadline = None
            if values.get("submission_deadline"):
                deadline = datetime.fromisoformat(values["submission_deadline"].replace("Z", "+00:00"))
                if deadline.utcoffset() is None:
                    raise ValueError(f"Line {line}: submission_deadline must include a timezone")
            parsed.append((date.fromisoformat(values["day_date"]), values.get("title") or None, deadline))
        if not parsed:
            raise ValueError("CSV has no calendar rows")
        ids = sorted({int(piece.strip()) for piece in club_ids.split(",") if piece.strip()})
        if not ids or len(ids) > 50:
            raise ValueError("Select between 1 and 50 clubs")
    except (UnicodeDecodeError, csv.Error, ValueError, TypeError) as exc:
        raise HTTPException(422, f"Invalid calendar CSV: {exc}") from exc

    created = 0
    skipped = 0
    async with conn.transaction():
        clubs = {}
        for club_id in ids:
            club = await assert_club_access(conn, user, club_id)
            if club["approval_status"] != "approved":
                raise HTTPException(409, f"Club {club_id} must be approved before importing dates")
            await conn.fetchrow("SELECT id FROM clubs WHERE id=$1 FOR UPDATE", club_id)
            clubs[club_id] = club
        for club_id in ids:
            for day_date, title, deadline in parsed:
                exists = await conn.fetchval(
                    "SELECT 1 FROM club_days WHERE club_id=$1 AND day_date=$2 AND title IS NOT DISTINCT FROM $3",
                    club_id, day_date, title,
                )
                if exists:
                    skipped += 1
                    continue
                row = await conn.fetchrow(
                    """INSERT INTO club_days (club_id, day_date, title, submission_deadline, created_by)
                       VALUES ($1,$2,$3,$4,$5) RETURNING id""",
                    club_id, day_date, title, deadline, user["id"],
                )
                await audit.log(conn, actor_id=user["id"], action="club_day.calendar_import",
                                entity="club_days", entity_id=row["id"],
                                after={"club_id": club_id, "day_date": day_date.isoformat(), "title": title})
                created += 1
    return {"created": created, "skipped": skipped, "clubs": len(ids), "calendar_rows": len(parsed)}


@router.get("")
async def list_club_days(
    club_id: int | None = None,
    user: dict = Depends(current_user),
    conn=Depends(get_conn),
):
    """Row-level scoping by role."""
    role = user["role"]
    params: list = []
    if role == "student":
        params.append(user["id"])
        where = f"""WHERE d.status IN {STUDENT_VISIBLE}
                    AND d.club_id IN (SELECT club_id FROM club_memberships
                                      WHERE student_id=$1 AND status='approved')"""
    elif role == "coordinator":
        params.append(user["id"])
        where = "WHERE d.club_id IN (SELECT club_id FROM club_coordinators WHERE user_id=$1)"
    elif role == "advisor":
        params.append(user["id"])
        where = "WHERE c.advisor_id=$1"
    elif role == "campus_admin":
        params.append(user["campus_id"])
        where = "WHERE c.campus_id=$1"
    else:
        where = "WHERE TRUE"
    if club_id is not None:
        params.append(club_id)
        where += f" AND d.club_id=${len(params)}"
    rows = await conn.fetch(
        f"""SELECT d.*, c.name AS club_name, p.body AS plan_body,
                   p.topic AS plan_topic, p.format AS plan_format,
                   p.deliverable AS plan_deliverable, p.venue AS plan_venue, p.status AS plan_status
            FROM club_days d
            JOIN clubs c ON c.id = d.club_id
            LEFT JOIN activity_plans p ON p.club_day_id=d.id
            {where} ORDER BY d.day_date DESC""",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/{club_day_id}")
async def get_club_day(
    club_day_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)
):
    day = await _visible_day(conn, user, club_day_id)
    out = dict(day)
    if user["role"] != "student":
        plan = await conn.fetchrow(
            "SELECT * FROM activity_plans WHERE club_day_id=$1", club_day_id
        )
        out["plan"] = dict(plan) if plan else None
    return out


@router.get("/{club_day_id}/presenters")
async def list_presenters(club_day_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    day = await _visible_day(conn, user, club_day_id)
    if user["role"] == "student":
        raise HTTPException(403, "Presenter records are staff-only")
    rows = await conn.fetch(
        """SELECT p.*, u.name AS student_name, u.student_code
           FROM club_day_presenters p JOIN users u ON u.id=p.student_id
           WHERE p.club_day_id=$1 ORDER BY p.marked_at""", day["id"],
    )
    return [dict(row) for row in rows]


@router.post("/{club_day_id}/presenters", status_code=201)
async def mark_presenter(club_day_id: int, body: PresenterIn,
                         user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)), conn=Depends(get_conn)):
    async with conn.transaction():
        day = await conn.fetchrow("SELECT * FROM club_days WHERE id=$1 FOR UPDATE", club_day_id)
        if day is None:
            raise HTTPException(404, "Club day not found")
        await assert_club_access(conn, user, day["club_id"])
        if day["status"] not in ("open", "submissions", "evaluating", "closed"):
            raise HTTPException(409, "Presenters can be recorded once the Club Day is open")
        if not body.title.strip():
            raise HTTPException(422, "Presentation title cannot be blank")
        member = await conn.fetchval(
            "SELECT 1 FROM club_memberships WHERE club_id=$1 AND student_id=$2 AND status='approved'",
            day["club_id"], body.student_id,
        )
        if not member:
            raise HTTPException(422, "Presenter must be an approved member of this club")
        row = await conn.fetchrow(
            """INSERT INTO club_day_presenters (club_day_id, student_id, title, notes, marked_by)
               VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (club_day_id, student_id)
               DO UPDATE SET title=EXCLUDED.title, notes=EXCLUDED.notes,
                             marked_by=EXCLUDED.marked_by, marked_at=now()
               RETURNING *""",
            club_day_id, body.student_id, body.title.strip(), body.notes, user["id"],
        )
        await audit.log(conn, actor_id=user["id"], action="club_day.presenter_marked",
                        entity="club_day_presenters", entity_id=row["id"],
                        after={"student_id": body.student_id, "title": body.title.strip()})
    return dict(row)


@router.put("/{club_day_id}/plan")
async def submit_plan(
    club_day_id: int,
    body: PlanIn,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    """Submit or resubmit (after rejection) the activity plan."""
    day = await conn.fetchrow("SELECT * FROM club_days WHERE id=$1", club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")
    await assert_club_access(conn, user, day["club_id"])
    if day["status"] != "planned":
        raise HTTPException(409, "The plan can only be edited while the club day is 'planned'")
    row = await conn.fetchrow(
        """INSERT INTO activity_plans (club_day_id, body, topic, format, deliverable, venue, status)
           VALUES ($1, $2, $3, $4, $5, $6, 'pending')
           ON CONFLICT (club_day_id)
           DO UPDATE SET body = EXCLUDED.body, topic = EXCLUDED.topic,
                         format = EXCLUDED.format, deliverable = EXCLUDED.deliverable,
                         venue = EXCLUDED.venue,
                         status = 'pending', approved_by = NULL
           RETURNING *""",
        club_day_id,
        body.body or f"Topic: {body.topic}\nFormat: {body.format}\nDeliverable: {body.deliverable}",
        body.topic, body.format, body.deliverable, body.venue,
    )
    return dict(row)


@router.post("/{club_day_id}/plan/decision")
async def decide_plan(
    club_day_id: int,
    body: PlanDecisionIn,
    user: dict = Depends(require_role(*ADVISOR_ROLES)),
    conn=Depends(get_conn),
):
    """Advisor approves/rejects. Approving also moves the club day planned -> approved."""
    target = "approved" if body.decision == "approve" else "rejected"
    async with conn.transaction():
        day = await conn.fetchrow(
            "SELECT * FROM club_days WHERE id=$1 FOR UPDATE", club_day_id
        )
        if day is None:
            raise HTTPException(404, "Club day not found")
        await assert_club_access(conn, user, day["club_id"], allow=ADVISOR_ROLES)
        plan = await conn.fetchrow(
            "SELECT * FROM activity_plans WHERE club_day_id=$1 FOR UPDATE", club_day_id
        )
        if plan is None:
            raise HTTPException(404, "No plan has been submitted yet")
        if plan["status"] != "pending":
            raise HTTPException(409, f"Plan is already {plan['status']}")

        new_plan = await conn.fetchrow(
            "UPDATE activity_plans SET status=$2, approved_by=$3 WHERE id=$1 RETURNING *",
            plan["id"], target, user["id"],
        )
        if target == "approved":
            new_status = transition(CLUB_DAY, day["status"], "approved")
            await conn.execute(
                "UPDATE club_days SET status=$2 WHERE id=$1", club_day_id, new_status
            )
        await audit.log(
            conn, actor_id=user["id"], action=f"plan.{target}",
            entity="activity_plans", entity_id=plan["id"],
            before={"status": plan["status"]}, after={"status": target},
            reason=body.reason,
        )
    return dict(new_plan)


@router.post("/{club_day_id}/transition")
async def move_club_day(
    club_day_id: int,
    body: TransitionIn,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    closed_sessions: list[int] = []
    async with conn.transaction():
        day = await conn.fetchrow(
            "SELECT * FROM club_days WHERE id=$1 FOR UPDATE", club_day_id
        )
        if day is None:
            raise HTTPException(404, "Club day not found")
        await assert_club_access(conn, user, day["club_id"])

        new_status = transition(CLUB_DAY, day["status"], body.target)  # 409 if illegal

        if day["status"] == "open":
            # leaving 'open' ends any live check-in window
            rows = await conn.fetch(
                """UPDATE checkin_sessions SET closes_at = now()
                   WHERE club_day_id=$1 AND closes_at > now() RETURNING id""",
                club_day_id,
            )
            closed_sessions = [r["id"] for r in rows]

        row = await conn.fetchrow(
            "UPDATE club_days SET status=$2 WHERE id=$1 RETURNING *",
            club_day_id, new_status,
        )
        await audit.log(
            conn, actor_id=user["id"], action="club_day.transition",
            entity="club_days", entity_id=club_day_id,
            before={"status": day["status"]}, after={"status": new_status},
        )
    # drop cached copies AFTER commit so the scan path re-reads the new closes_at
    for sid in closed_sessions:
        await checkin.invalidate_session(sid)
    return dict(row)
