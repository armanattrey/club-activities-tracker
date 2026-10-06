from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.permissions import CLUB_MANAGER_ROLES, assert_club_access, require_role
from app.db import get_conn
from app.services import enrolment

router = APIRouter(tags=["memberships"])


class JoinIn(BaseModel):
    type: Literal["primary", "secondary"]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


@router.post("/clubs/{club_id}/join", status_code=201)
async def join_club(
    club_id: int,
    body: JoinIn,
    user: dict = Depends(require_role("student")),
    conn=Depends(get_conn),
):
    return await enrolment.request_join(conn, user, club_id, body.type)


@router.get("/memberships/me")
async def my_memberships(
    user: dict = Depends(require_role("student")), conn=Depends(get_conn)
):
    rows = await conn.fetch(
        """SELECT m.*, c.name AS club_name FROM club_memberships m
           JOIN clubs c ON c.id = m.club_id
           WHERE m.student_id=$1 ORDER BY m.created_at DESC""",
        user["id"],
    )
    return [dict(r) for r in rows]


@router.get("/clubs/{club_id}/memberships")
async def club_memberships(
    club_id: int,
    status: str | None = None,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    await assert_club_access(conn, user, club_id)
    rows = await conn.fetch(
        """SELECT m.*, u.name AS student_name, u.student_code
           FROM club_memberships m JOIN users u ON u.id = m.student_id
           WHERE m.club_id=$1 AND ($2::text IS NULL OR m.status=$2)
           ORDER BY m.created_at""",
        club_id, status,
    )
    return [dict(r) for r in rows]


@router.post("/memberships/{membership_id}/decision")
async def decide_membership(
    membership_id: int,
    body: DecisionIn,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    return await enrolment.decide(conn, user, membership_id, body.decision, body.reason)