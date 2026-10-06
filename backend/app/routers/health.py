from fastapi import APIRouter

from app import db, redis_client

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    async with db.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await redis_client.redis.ping()
    return {"status": "ok"}