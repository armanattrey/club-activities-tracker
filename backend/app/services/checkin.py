"""Check-in logic shared by routers: rotating QR tokens, Redis session cache, geo math.

Design notes
- The QR token is HMAC(secret, "session_id:step"). It is computed, never stored,
  so rotating it costs zero DB/Redis writes.
- We accept the current AND previous step, so a student who scans right as the
  code rotates is not rejected. A screenshot is therefore valid for at most
  2 * STEP_SECONDS.
"""
import hashlib
import hmac
import json
import math
import time

from app import db, redis_client

STEP_SECONDS = 25


# ---------- rotating token ----------
def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def seconds_left_in_step(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(STEP_SECONDS - (now % STEP_SECONDS))


def make_token(secret: str, session_id: int, step: int) -> str:
    msg = f"{session_id}:{step}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:16]


def token_valid(secret: str, session_id: int, token: str, now: float | None = None) -> bool:
    step = current_step(now)
    supplied = token.encode()
    # compare_digest avoids timing leaks; we check the current and previous step
    return any(
        hmac.compare_digest(make_token(secret, session_id, s).encode(), supplied)
        for s in (step, step - 1)
    )


# ---------- geo ----------
def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in metres."""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- Redis session cache ----------
def _session_key(session_id: int) -> str:
    return f"checkin:session:{session_id}"


def done_key(session_id: int, student_id: int) -> str:
    return f"checkin:done:{session_id}:{student_id}"


async def cache_session(row, club_id: int) -> dict:
    data = {
        "id": row["id"],
        "club_day_id": row["club_day_id"],
        "club_id": club_id,
        "secret": row["secret"],
        # epoch seconds so the hot path never parses datetimes
        "opens_at": row["opens_at"].timestamp(),
        "closes_at": row["closes_at"].timestamp(),
        "late_after": row["late_after"].timestamp(),
        "geo_lat": row["geo_lat"],
        "geo_lng": row["geo_lng"],
        "geo_radius_m": row["geo_radius_m"],
    }
    ttl = max(int(data["closes_at"] - time.time()) + 300, 60)
    await redis_client.redis.set(_session_key(row["id"]), json.dumps(data), ex=ttl)
    return data


async def load_session(session_id: int) -> dict | None:
    """Redis first; on a miss, load from Postgres and re-cache.
    Acquires its own DB connection only on a miss, to keep the hot path light."""
    raw = await redis_client.redis.get(_session_key(session_id))
    if raw:
        return json.loads(raw)
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.*, d.club_id FROM checkin_sessions s
               JOIN club_days d ON d.id = s.club_day_id
               WHERE s.id = $1""",
            session_id,
        )
    if row is None:
        return None
    return await cache_session(row, row["club_id"])


async def invalidate_session(session_id: int):
    await redis_client.redis.delete(_session_key(session_id))