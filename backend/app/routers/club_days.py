"""Club Day CRUD, plan approval and lifecycle transitions.

Lifecycle: planned -> approved -> announced -> open -> submissions -> evaluating -> closed
- planned -> approved happens ONLY through the advisor approving the plan.
- every other step goes through POST /{id}/transition (rules in core/state_machines.py).
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from datetime import date

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
    body: str = Field(min_length=10)


class PlanDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


class TransitionIn(BaseModel):
    target: Literal["announced", "open", "submissions", "evaluating", "closed"]


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
    await assert_club_access(conn, user, body.club_id)
    row = await conn.fetchrow(
        """INSERT INTO club_days (club_id, day_date, title, submission_deadline, created_by)
           VALUES ($1,$2,$3,$4,$5) RETURNING *""",
        body.club_id, body.day_date, body.title, body.submission_deadline, user["id"],
    )
    return dict(row)


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
        f"""SELECT d.*, c.name AS club_name FROM club_days d
            JOIN clubs c ON c.id = d.club_id
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
        """INSERT INTO activity_plans (club_day_id, body, status)
           VALUES ($1, $2, 'pending')
           ON CONFLICT (club_day_id)
           DO UPDATE SET body = EXCLUDED.body, status = 'pending', approved_by = NULL
           RETURNING *""",
        club_day_id, body.body,
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