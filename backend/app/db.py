import json

import asyncpg

from app.config import settings

pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection):
    # Make JSONB columns read/write as Python dicts instead of strings.
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v, default=str),
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_pool():
    global pool
    # Pool size matters for the 500-check-in load test: tune min/max per worker.
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=5, max_size=20, init=_init_conn
    )


async def close_pool():
    if pool:
        await pool.close()


async def get_conn():
    """FastAPI dependency: one pooled connection per request."""
    async with pool.acquire() as conn:
        yield conn