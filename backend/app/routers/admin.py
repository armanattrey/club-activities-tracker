import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.permissions import current_user, require_role
from app.core.security import hash_password
from app.db import get_conn

router = APIRouter(tags=["administration"])
ADMIN_ROLES = ("campus_admin", "super_admin")
USER_ROLES = ("student", "coordinator", "advisor", "campus_admin", "super_admin")


class CampusIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10, max_length=128)
    role: str
    campus_id: int | None = None
    student_code: str | None = Field(default=None, max_length=80)


@router.get("/campuses")
async def list_campuses(user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    if user["role"] == "super_admin":
        rows = await conn.fetch("SELECT id, name FROM campus ORDER BY name")
    else:
        rows = await conn.fetch(
            "SELECT id, name FROM campus WHERE id=$1", user["campus_id"]
        )
    return [dict(row) for row in rows]


@router.post("/campuses", status_code=201)
async def create_campus(
    body: CampusIn,
    user: dict = Depends(require_role("super_admin")),
    conn=Depends(get_conn),
):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Campus name must contain at least 2 characters")
    try:
        row = await conn.fetchrow(
            "INSERT INTO campus (name) VALUES ($1) RETURNING id, name", name
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "A campus with this name already exists")
    return dict(row)


@router.get("/users")
async def list_users(
    user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)
):
    if user["role"] == "super_admin":
        rows = await conn.fetch(
            """SELECT id, campus_id, role, student_code, email, name, created_at
               FROM users ORDER BY campus_id NULLS FIRST, role, name"""
        )
    else:
        rows = await conn.fetch(
            """SELECT id, campus_id, role, student_code, email, name, created_at
               FROM users WHERE campus_id=$1 ORDER BY role, name""",
            user["campus_id"],
        )
    return [dict(row) for row in rows]


@router.post("/users", status_code=201)
async def create_user(
    body: UserIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)),
    conn=Depends(get_conn),
):
    if body.role not in USER_ROLES:
        raise HTTPException(422, "Unknown user role")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Name cannot be blank")
    if user["role"] == "campus_admin":
        if body.role in ("campus_admin", "super_admin"):
            raise HTTPException(403, "Campus admins cannot create admin accounts")
        campus_id = user["campus_id"]
    else:
        campus_id = body.campus_id
        if body.role != "super_admin" and campus_id is None:
            raise HTTPException(422, "campus_id is required for this role")
        if body.role == "super_admin":
            campus_id = None

    if campus_id is not None:
        exists = await conn.fetchval("SELECT 1 FROM campus WHERE id=$1", campus_id)
        if not exists:
            raise HTTPException(422, "Campus not found")

    student_code = body.student_code.strip() if body.student_code else None
    if body.role == "student" and not student_code:
        raise HTTPException(422, "student_code is required for students")
    if body.role != "student" and student_code:
        raise HTTPException(422, "student_code is only valid for students")

    try:
        row = await conn.fetchrow(
            """INSERT INTO users (campus_id, role, student_code, email, name, pw_hash)
               VALUES ($1,$2,$3,$4,$5,$6)
               RETURNING id, campus_id, role, student_code, email, name, created_at""",
            campus_id, body.role, student_code, body.email.strip().lower(),
            name, hash_password(body.password),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "Email or student code is already in use") from exc
    return dict(row)
