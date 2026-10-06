import redis.asyncio as aioredis

from app.config import settings

redis: aioredis.Redis | None = None


async def init_redis():
    global redis
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def close_redis():
    if redis:
        await redis.aclose()