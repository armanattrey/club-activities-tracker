"""Join-request logic. Rules enforced here and by DB constraints:
- 1 primary + 1 secondary club per student (partial unique index)
- club capacity (checked under a row lock on the club)
- pending -> approved/rejected only (state machine)
"""
import asyncpg
from fastapi import HTTPException

from app.core import audit
from app.core.permissions import assert_club_access
from app.core.state_machines import ENROLMENT, transition


async def request_join(conn, student: dict, club_id: int, mtype: str):
    async with conn.transaction():
        # Lock the club row so concurrent requests/approvals serialize on it.
        club = await conn.fetchrow(
            "SELECT id, campus_id, capacity FROM clubs WHERE id=$1 FOR UPDATE", club_id
        )
        if club is None:
            raise HTTPException(404, "Club not found")
        if club["campus_id"] != student["campus_id"]:
            raise HTTPException(403, "You can only join clubs on your own campus")

        approved = await conn.fetchval(
            "SELECT count(*) FROM club_memberships WHERE club_id=$1 AND status='approved'",
            club_id,
        )
        if approved >= club["capacity"]:
            raise HTTPException(409, "Club is full")

        try:
            row = await conn.fetchrow(
                """INSERT INTO club_memberships (student_id, club_id, type)
                   VALUES ($1, $2, $3) RETURNING *""",
                student["id"], club_id, mtype,
            )
        except asyncpg.UniqueViolationError as e:
            # The DB is the final authority; translate its constraint to a message.
            if e.constraint_name == "uq_membership_student_club":
                raise HTTPException(409, "You already have a pending/approved request for this club")
            raise HTTPException(
                409, f"You already have a pending/approved {mtype} club (limit: 1 primary + 1 secondary)"
            )
        return dict(row)


async def decide(conn, actor: dict, membership_id: int, decision: str, reason: str | None):
    target = "approved" if decision == "approve" else "rejected"
    async with conn.transaction():
        m0 = await conn.fetchrow(
            "SELECT club_id FROM club_memberships WHERE id=$1", membership_id
        )
        if m0 is None:
            raise HTTPException(404, "Membership not found")

        # Lock order: club first, then membership (same order as request_join).
        club = await conn.fetchrow(
            "SELECT * FROM clubs WHERE id=$1 FOR UPDATE", m0["club_id"]
        )
        await assert_club_access(conn, actor, m0["club_id"])
        m = await conn.fetchrow(
            "SELECT * FROM club_memberships WHERE id=$1 FOR UPDATE", membership_id
        )

        transition(ENROLMENT, m["status"], target)  # raises InvalidTransition -> 409

        if target == "approved":
            approved = await conn.fetchval(
                "SELECT count(*) FROM club_memberships WHERE club_id=$1 AND status='approved'",
                club["id"],
            )
            if approved >= club["capacity"]:
                raise HTTPException(409, "Club is full")

        row = await conn.fetchrow(
            """UPDATE club_memberships
               SET status=$2, decided_by=$3, decided_at=now()
               WHERE id=$1 RETURNING *""",
            membership_id, target, actor["id"],
        )
        await audit.log(
            conn, actor_id=actor["id"], action=f"membership.{target}",
            entity="club_memberships", entity_id=membership_id,
            before={"status": m["status"]}, after={"status": target}, reason=reason,
        )
        return dict(row)