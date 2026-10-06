from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.permissions import current_user
from app.core.security import create_token, verify_password
from app.db import get_conn

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    identifier: str  # email or student_code
    password: str


@router.post("/login")
async def login(body: LoginIn, conn=Depends(get_conn)):
    u = await conn.fetchrow(
        "SELECT * FROM users WHERE email=$1 OR student_code=$1", body.identifier
    )
    if u is None or not verify_password(body.password, u["pw_hash"]):
        raise HTTPException(401, "Invalid credentials")
    user = {"id": u["id"], "role": u["role"], "campus_id": u["campus_id"]}
    return {"access_token": create_token(user), "token_type": "bearer", "user": user}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user

# TODO: POST /users (admin creates accounts), password reset.