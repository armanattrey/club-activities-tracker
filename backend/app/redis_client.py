"""Cache adapter: process-local memory for demos or Redis for production."""
import asyncio
import time

from app.config import settings


class MemoryRedis:
    """Small async subset of Redis used by check-in paths in local mode."""

    def __init__(self):
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        async with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            value, expires_at = item
            if expires_at is not None and expires_at <= time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value, *, ex: int | None = None, nx: bool = False):
        async with self._lock:
            item = self._data.get(key)
            if item:
                _, expires_at = item
                if expires_at is not None and expires_at <= time.monotonic():
                    self._data.pop(key, None)
                    item = None
            if nx and item:
                return None
            expires_at = time.monotonic() + ex if ex is not None else None
            self._data[key] = (str(value), expires_at)
            return "OK"

    async def delete(self, key: str):
        async with self._lock:
            return int(self._data.pop(key, None) is not None)

    async def ping(self):
        return True

    async def aclose(self):
        async with self._lock:
            self._data.clear()


redis = None


async def init_redis():
    global redis
    if settings.app_mode == "local":
        redis = MemoryRedis()
        return
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required when APP_MODE=production")
    try:
        from redis.asyncio import from_url
    except ImportError as exc:
        raise RuntimeError("Install backend/requirements-production.txt for Redis support") from exc
    redis = from_url(settings.redis_url, decode_responses=True)


async def close_redis():
    if redis:
        await redis.aclose()
