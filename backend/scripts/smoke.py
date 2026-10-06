"""Smoke test for Stages 1-2.
Run: docker compose exec api python -m scripts.smoke
Creates a fresh club day + check-in window each run. Uses the seeded demo users.
"""
import sys

import httpx

BASE = "http://localhost:8000"
PW = "password123"
results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))


def login(c: httpx.Client, email: str) -> dict:
    r = c.post("/auth/login", json={"identifier": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def fresh_token(c, coord, sid) -> str:
    return c.get(f"/checkin/sessions/{sid}/qr", headers=coord).json()["token"]


def main():
    c = httpx.Client(base_url=BASE, timeout=10)
    coord = login(c, "coord@demo.edu")
    advisor = login(c, "advisor@demo.edu")
    s1 = login(c, "s001@demo.edu")
    s3 = login(c, "s003@demo.edu")
    s1_id = c.get("/auth/me", headers=s1).json()["id"]

    # --- setup: make sure s001 is an approved member of club 1 ---
    c.post("/clubs/1/join", headers=s1, json={"type": "primary"})  # 409 if already requested: fine
    rows = c.get("/clubs/1/memberships", headers=coord).json()
    for m in rows:
        if m["student_code"] == "S001" and m["status"] == "pending":
            c.post(f"/memberships/{m['id']}/decision", headers=coord, json={"decision": "approve"})
    rows = c.get("/clubs/1/memberships", headers=coord).json()
    check("setup: s001 is an approved member of club 1",
          any(m["student_code"] == "S001" and m["status"] == "approved" for m in rows),
          "approve s001 in club 1 manually (club may be full)")

    # --- club day lifecycle ---
    r = c.post("/club-days", headers=coord, json={
        "club_id": 1, "day_date": "2026-10-10", "title": "Smoke test day",
        "submission_deadline": "2026-10-20T17:00:00+05:30"})
    check("coordinator creates club day (201, planned)",
          r.status_code == 201 and r.json()["status"] == "planned", r.text)
    day = r.json()["id"]

    r = c.post(f"/club-days/{day}/transition", headers=coord, json={"target": "open"})
    check("skipping planned->open is blocked (409)", r.status_code == 409, r.text)

    r = c.post("/club-days", headers=s1, json={"club_id": 1, "day_date": "2026-10-10"})
    check("student cannot create a club day (403)", r.status_code == 403, r.text)

    r = c.put(f"/club-days/{day}/plan", headers=coord, json={"body": "Robot demo and workshop"})
    check("coordinator submits plan (200)", r.status_code == 200, r.text)

    r = c.post(f"/club-days/{day}/plan/decision", headers=advisor, json={"decision": "approve"})
    check("advisor approves plan (200)", r.status_code == 200, r.text)
    r = c.get(f"/club-days/{day}", headers=coord)
    check("club day is now 'approved'", r.json().get("status") == "approved", r.text)

    for target in ("announced", "open"):
        r = c.post(f"/club-days/{day}/transition", headers=coord, json={"target": target})
        check(f"transition to '{target}' (200)", r.status_code == 200, r.text)

    r = c.get("/club-days", headers=s1)
    check("student sees the announced/open club day",
          r.status_code == 200 and any(d["id"] == day for d in r.json()), r.text)

    # --- check-in ---
    r = c.post(f"/checkin/club-days/{day}/open", headers=coord, json={})
    check("coordinator opens check-in window (201)", r.status_code == 201, r.text)
    sid = r.json()["id"]
    check("session secret is NOT leaked in the response", "secret" not in r.json())

    r = c.get(f"/checkin/sessions/{sid}/qr", headers=s1)
    check("student cannot fetch the QR (403)", r.status_code == 403, r.text)

    r = c.post("/checkin/scan", headers=s1,
               json={"session_id": sid, "token": fresh_token(c, coord, sid)})
    check("student scans a live QR (201, not late)",
          r.status_code == 201 and r.json()["is_late"] is False, r.text)

    r = c.post("/checkin/scan", headers=s1,
               json={"session_id": sid, "token": fresh_token(c, coord, sid)})
    check("double check-in is blocked (409)", r.status_code == 409, r.text)

    r = c.post("/checkin/scan", headers=s1,
               json={"session_id": sid, "token": "0000000000000000"})
    check("invalid/stale token is rejected (403)", r.status_code == 403, r.text)

    r = c.post("/checkin/scan", headers=s3,
               json={"session_id": sid, "token": fresh_token(c, coord, sid)})
    check("non-member cannot check in (403)", r.status_code == 403, r.text)

    r = c.get(f"/checkin/sessions/{sid}/attendance", headers=coord)
    check("coordinator sees s001 in attendance",
          r.status_code == 200 and any(a["student_id"] == s1_id for a in r.json()), r.text)

    r = c.get("/checkin/attendance/me", headers=s1)
    check("student sees own attendance", r.status_code == 200 and len(r.json()) >= 1, r.text)

    # --- corrections ---
    url = f"/checkin/sessions/{sid}/corrections"
    r = c.post(url, headers=coord, json={"student_id": s1_id, "action": "remove", "reason": "x"})
    check("correction with a weak reason is refused (422)", r.status_code == 422, r.text)

    r = c.post(url, headers=coord, json={
        "student_id": s1_id, "action": "remove", "reason": "Scanned for the wrong session"})
    check("correction: remove with a real reason (200)", r.status_code == 200, r.text)
    r = c.get(f"/checkin/sessions/{sid}/attendance", headers=coord)
    check("removed student is gone from attendance",
          not any(a["student_id"] == s1_id for a in r.json()), r.text)

    r = c.post(url, headers=coord, json={
        "student_id": s1_id, "action": "add", "reason": "Verified in person by coordinator"})
    check("correction: add with a real reason (200)", r.status_code == 200, r.text)
    r = c.get(f"/checkin/sessions/{sid}/attendance", headers=coord)
    check("re-added record is flagged 'corrected'",
          any(a["student_id"] == s1_id and a["corrected"] for a in r.json()), r.text)

    # --- close ---
    r = c.post(f"/checkin/sessions/{sid}/close", headers=coord)
    check("coordinator closes the window (200)", r.status_code == 200, r.text)
    r = c.get(f"/checkin/sessions/{sid}/qr", headers=coord)
    check("QR is refused after close (409)", r.status_code == 409, r.text)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


main()