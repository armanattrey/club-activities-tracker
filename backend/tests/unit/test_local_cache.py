import asyncio

from app.redis_client import MemoryRedis


def test_memory_cache_nx_and_delete_match_used_redis_contract():
    async def scenario():
        cache = MemoryRedis()
        assert await cache.set("checkin:done:1:2", 1, nx=True, ex=30) == "OK"
        assert await cache.set("checkin:done:1:2", 1, nx=True, ex=30) is None
        assert await cache.get("checkin:done:1:2") == "1"
        assert await cache.delete("checkin:done:1:2") == 1
        assert await cache.get("checkin:done:1:2") is None

    asyncio.run(scenario())


def test_memory_cache_expires_values():
    async def scenario():
        cache = MemoryRedis()
        await cache.set("temporary", "value", ex=0)
        assert await cache.get("temporary") is None

    asyncio.run(scenario())
