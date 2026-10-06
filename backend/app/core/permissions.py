"""Reusable access control. Endpoints never hand-roll role checks.

- require_role(...)       -> which roles may call an endpoint at all
- assert_club_access(...) -> row-level scoping: may THIS user touch THIS club?
"""
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token

bearer = HTTPBearer(auto_error=False)

ADMIN_ROLES = ("campus_admin", "super_admin")
CLUB_MANAGER_ROLES = ("coordinator", "campus_admin", "super_admin")


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    if creds is None:
        raise HTTPException(401, "Not authenticated")
    try:
        p = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    # Claims are trusted (no DB hit) to keep hot paths like check-in fast.
    return {"id": int(p["sub"]), "role": p["role"], "campus_id": p["campus_id"]}


def require_role(*roles: str):
    async def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "Your role cannot perform this action")
        return user

    return dep


async def assert_club_access(
    conn, user: dict, club_id: int, allow: tuple = CLUB_MANAGER_ROLES
):
    """Raises 404/403 unless `user` may act on this club. Returns the club row."""
    club = await conn.fetchrow("SELECT * FROM clubs WHERE id = $1", club_id)
    if club is None:
        raise HTTPException(404, "Club not found")

    role = user["role"]
    if role not in allow:
        raise HTTPException(403, "Your role cannot perform this action")

    if role == "super_admin":
        return club
    if role == "campus_admin" and club["campus_id"] == user["campus_id"]:
        return club
    if role == "coordinator":
        ok = await conn.fetchval(
            "SELECT 1 FROM club_coordinators WHERE club_id=$1 AND user_id=$2",
            club_id, user["id"],
        )
        if ok:
            return club
    if role == "advisor" and club["advisor_id"] == user["id"]:
        return club

    raise HTTPException(403, "You do not have access to this club")