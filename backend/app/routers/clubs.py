import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.permissions import ADMIN_ROLES, assert_club_access, current_user, require_role
from app.db import get_conn

router = APIRouter(prefix="/clubs", tags=["clubs"])


class ClubIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    capacity: int = Field(gt=0)
    campus_id: int | None = None  # only used by super_admin
    advisor_id: int | None = None
    rules: dict = Field(default_factory=dict)


class ClubUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    capacity: int | None = Field(default=None, gt=0)
    advisor_id: int | None = None
    rules: dict | None = None


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
    if len(body.name.strip()) == 0:
        raise HTTPException(422, "Club name cannot be blank")
    if not await conn.fetchval("SELECT 1 FROM campus WHERE id=$1", campus_id):
        raise HTTPException(422, "Campus not found")
    if body.advisor_id is not None:
        await _validate_advisor(conn, body.advisor_id, campus_id)
    try:
        row = await conn.fetchrow(
            """INSERT INTO clubs (campus_id, name, description, capacity, advisor_id, rules)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
            campus_id, body.name.strip(), body.description.strip(), body.capacity,
            body.advisor_id, body.rules,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "A club with this name already exists on that campus")
    return dict(row)


async def _validate_advisor(conn, advisor_id: int | None, campus_id: int):
    if advisor_id is None:
        return
    target = await conn.fetchrow(
        "SELECT role, campus_id FROM users WHERE id=$1", advisor_id
    )
    if target is None or target["role"] != "advisor" or target["campus_id"] != campus_id:
        raise HTTPException(422, "Advisor must be an advisor on the club's campus")


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


@router.patch("/{club_id}")
async def update_club(
    club_id: int,
    body: ClubUpdateIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)),
    conn=Depends(get_conn),
):
    club = await assert_club_access(conn, user, club_id, allow=ADMIN_ROLES)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "Provide at least one field to update")
    for key in ("name", "description", "capacity", "rules"):
        if key in fields and fields[key] is None:
            raise HTTPException(422, f"{key} cannot be null")
    if "advisor_id" in fields:
        await _validate_advisor(conn, fields["advisor_id"], club["campus_id"])
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(422, "Club name cannot be blank")
    if "description" in fields and fields["description"] is not None:
        fields["description"] = fields["description"].strip()
    allowed = ("name", "description", "capacity", "advisor_id", "rules")
    values = [fields[k] for k in allowed if k in fields]
    assignments = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(k for k in allowed if k in fields))
    try:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT id FROM clubs WHERE id=$1 FOR UPDATE", club_id
            )
            if locked is None:
                raise HTTPException(404, "Club not found")
            if fields.get("capacity") is not None:
                members = await conn.fetchval(
                    "SELECT count(*) FROM club_memberships WHERE club_id=$1 AND status='approved'",
                    club_id,
                )
                if fields["capacity"] < members:
                    raise HTTPException(409, "Capacity cannot be lower than the approved member count")
            row = await conn.fetchrow(
                f"UPDATE clubs SET {assignments} WHERE id=$1 RETURNING *", club_id, *values
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "A club with this name already exists on that campus") from exc
    return dict(row)


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
