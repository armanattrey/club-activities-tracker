import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.permissions import ADMIN_ROLES, assert_club_access, current_user, require_role
from app.db import get_conn

router = APIRouter(prefix="/clubs", tags=["clubs"])


class ClubIn(BaseModel):
    name: str
    capacity: int = Field(gt=0)
    campus_id: int | None = None  # only used by super_admin
    advisor_id: int | None = None
    rules: dict = {}


class CoordinatorIn(BaseModel):
    user_id: int


@router.post("", status_code=201)
async def create_club(
    body: ClubIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)),
    conn=Depends(get_conn),
):
    campus_id = body.campus_id if user["role"] == "super_admin" else user["campus_id"]
    if campus_id is None:
        raise HTTPException(422, "campus_id is required")
    # TODO: validate that advisor_id belongs to an 'advisor' user on this campus.
    try:
        row = await conn.fetchrow(
            """INSERT INTO clubs (campus_id, name, capacity, advisor_id, rules)
               VALUES ($1,$2,$3,$4,$5) RETURNING *""",
            campus_id, body.name, body.capacity, body.advisor_id, body.rules,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "A club with this name already exists on that campus")
    return dict(row)


@router.get("")
async def list_clubs(user: dict = Depends(current_user), conn=Depends(get_conn)):
    """Row-level scoping by role."""
    base = """SELECT c.*,
                (SELECT count(*) FROM club_memberships m
                 WHERE m.club_id=c.id AND m.status='approved') AS approved_count
              FROM clubs c"""
    role = user["role"]
    if role == "super_admin":
        rows = await conn.fetch(base + " ORDER BY c.id")
    elif role in ("student", "campus_admin"):
        rows = await conn.fetch(base + " WHERE c.campus_id=$1 ORDER BY c.id", user["campus_id"])
    elif role == "coordinator":
        rows = await conn.fetch(
            base + " WHERE c.id IN (SELECT club_id FROM club_coordinators WHERE user_id=$1) ORDER BY c.id",
            user["id"],
        )
    else:  # advisor
        rows = await conn.fetch(base + " WHERE c.advisor_id=$1 ORDER BY c.id", user["id"])
    return [dict(r) for r in rows]


@router.get("/{club_id}")
async def get_club(club_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    club = await conn.fetchrow("SELECT * FROM clubs WHERE id=$1", club_id)
    if club is None:
        raise HTTPException(404, "Club not found")
    if user["role"] != "super_admin" and club["campus_id"] != user["campus_id"]:
        raise HTTPException(403, "Different campus")
    return dict(club)


@router.post("/{club_id}/coordinators", status_code=201)
async def add_coordinator(
    club_id: int,
    body: CoordinatorIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)),
    conn=Depends(get_conn),
):
    club = await assert_club_access(conn, user, club_id, allow=ADMIN_ROLES)
    target = await conn.fetchrow("SELECT role, campus_id FROM users WHERE id=$1", body.user_id)
    if target is None or target["role"] != "coordinator" or target["campus_id"] != club["campus_id"]:
        raise HTTPException(422, "User must be a coordinator on the club's campus")
    await conn.execute(
        "INSERT INTO club_coordinators (club_id, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        club_id, body.user_id,
    )
    return {"club_id": club_id, "user_id": body.user_id}