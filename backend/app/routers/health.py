from fastapi import APIRouter

from app import db, redis_client
from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    async with db.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await redis_client.redis.ping()
    return {
        "status": "ok",
        "mode": settings.app_mode,
        "cache": "memory" if settings.app_mode == "local" else "redis",
        "background_jobs": "in_process" if settings.app_mode == "local" else "celery",
    }
