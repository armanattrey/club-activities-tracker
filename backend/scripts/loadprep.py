"""Load-test data prep. Run inside the api container:
  docker compose exec -T api python -m scripts.loadprep --level 500 > loadtest/data.json
Creates (idempotently) a separate 'Load Test Campus' with 1000 student users and a club,
approves them as members, then creates a fresh open club day and check-in window through
the real API. Prints ONE JSON document (session id, HMAC secret, one JWT per student)
to stdout. Progress goes to stderr so stdout stays valid JSON.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import date

import asyncpg
import httpx

from app.core.security import create_token, hash_password

BASE = "http://localhost:8000"
MAX_USERS = 1000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


async def db_part(level):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("INSERT INTO campus (name) VALUES ('Load Test Campus') ON CONFLICT DO NOTHING")
        cid = await conn.fetchval("SELECT id FROM campus WHERE name = 'Load Test Campus'")
        await conn.execute(
            "INSERT INTO clubs (campus_id, name, capacity) VALUES ($1, 'Load Test Club', $2) ON CONFLICT DO NOTHING",
            cid, MAX_USERS)
        club_id = await conn.fetchval(
            "SELECT id FROM clubs WHERE campus_id = $1 AND name = 'Load Test Club'", cid)
        have = await conn.fetchval(
            "SELECT count(*) FROM users WHERE campus_id = $1 AND student_code LIKE 'LT%'", cid)
        if have < MAX_USERS:
            log("creating load-test students (first run only)...")
            pw = hash_password("password123")
            rows = [(cid, "LT%04d" % i, "lt%04d@load.test" % i, "Load Student %04d" % i, pw)
                    for i in range(1, MAX_USERS + 1)]
            await conn.executemany(
                """INSERT INTO users (role, campus_id, student_code, email, name, pw_hash)
                   VALUES ('student', $1, $2, $3, $4, $5) ON CONFLICT DO NOTHING""", rows)
        recs = await conn.fetch(
            """SELECT id FROM users WHERE campus_id = $1 AND student_code LIKE 'LT%'
               ORDER BY student_code LIMIT $2""", cid, level)
        ids = [r["id"] for r in recs]
        await conn.execute(
            """INSERT INTO club_memberships (student_id, club_id, type, status, decided_at)
               SELECT unnest($1::bigint[]), $2::int, 'primary', 'approved', now()
               ON CONFLICT DO NOTHING""", ids, club_id)
        return cid, club_id, ids
    finally:
        await conn.close()


def api_part(club_id):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/auth/login", json={"identifier": "super@demo.edu", "password": "password123"})
    r.raise_for_status()
    h = {"Authorization": "Bearer " + r.json()["access_token"]}

    def step(method, path, **kw):
        resp = c.request(method, path, headers=h, **kw)
        if resp.status_code >= 400:
            raise SystemExit("%s %s -> %s %s" % (method, path, resp.status_code, resp.text))
        return resp.json()

    day = step("POST", "/club-days", json={
        "club_id": club_id, "day_date": date.today().isoformat(), "title": "Load test"})["id"]
    step("PUT", "/club-days/%d/plan" % day, json={"body": "Load test plan for the check-in endpoint"})
    step("POST", "/club-days/%d/plan/decision" % day, json={"decision": "approve"})
    step("POST", "/club-days/%d/transition" % day, json={"target": "announced"})
    step("POST", "/club-days/%d/transition" % day, json={"target": "open"})
    sess = step("POST", "/checkin/club-days/%d/open" % day,
                json={"duration_minutes": 60, "late_after_minutes": 30})
    return sess["id"]


async def get_secret(session_id):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return await conn.fetchval("SELECT secret FROM checkin_sessions WHERE id = $1", session_id)
    finally:
        await conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=100)
    a = ap.parse_args()
    if not 1 <= a.level <= MAX_USERS:
        raise SystemExit("--level must be between 1 and %d" % MAX_USERS)
    log("preparing %d students..." % a.level)
    cid, club_id, ids = asyncio.run(db_part(a.level))
    sid = api_part(club_id)
    secret = asyncio.run(get_secret(sid))
    tokens = [create_token({"id": i, "role": "student", "campus_id": cid}) for i in ids]
    print(json.dumps({"session_id": sid, "secret": secret, "level": a.level, "tokens": tokens}))


main()
