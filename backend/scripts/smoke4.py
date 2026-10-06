"""Smoke test for Stage 4 (certificates, PDF generation, public verification).
Run: docker compose exec api python -m scripts.smoke4
Creates a fresh club day each run. Uses the seeded demo users.
"""
import asyncio
import os
import sys
import time
import uuid

import asyncpg
import httpx

BASE = "http://localhost:8000"
PW = "password123"
results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))


def login(c, email):
    r = c.post("/auth/login", json={"identifier": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def fresh_token(c, coord, sid) -> str:
    return c.get(f"/checkin/sessions/{sid}/qr", headers=coord).json()["token"]


def make_open_day(c, coord, advisor):
    r = c.post("/club-days", headers=coord, json={
        "club_id": 1, "day_date": "2026-10-10", "title": "Stage 4 smoke",
        "submission_deadline": "2099-01-01T00:00:00+00:00"})
    day = r.json()["id"]
    c.put(f"/club-days/{day}/plan", headers=coord, json={"body": "Stage 4 smoke plan"})
    c.post(f"/club-days/{day}/plan/decision", headers=advisor, json={"decision": "approve"})
    for t in ("announced", "open"):
        c.post(f"/club-days/{day}/transition", headers=coord, json={"target": t})
    return day


def wait_ready(c, h, cert_id, timeout=90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        for item in c.get("/certificates/me", headers=h).json():
            if item["id"] == cert_id:
                if item["status"] == "ready":
                    return True
                if item["status"] == "failed":
                    return False
        time.sleep(2)
    return False


async def _exec(sql, *args):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


def db_exec(sql, *args):
    asyncio.run(_exec(sql, *args))


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    coord = login(c, "coord@demo.edu")
    advisor = login(c, "advisor@demo.edu")
    s1 = login(c, "s001@demo.edu")
    s3 = login(c, "s003@demo.edu")
    s1_id = c.get("/auth/me", headers=s1).json()["id"]
    s3_id = c.get("/auth/me", headers=s3).json()["id"]

    # setup: s001 must be an approved member of club 1
    c.post("/clubs/1/join", headers=s1, json={"type": "primary"})
    for m in c.get("/clubs/1/memberships", headers=coord).json():
        if m["student_code"] == "S001" and m["status"] == "pending":
            c.post(f"/memberships/{m['id']}/decision", headers=coord, json={"decision": "approve"})

    day = make_open_day(c, coord, advisor)
    sid = c.post(f"/checkin/club-days/{day}/open", headers=coord, json={}).json()["id"]

    # ---- participation: needs verified attendance ----
    r = c.post("/certificates/participation", headers=s1, json={"club_day_id": day})
    check("no certificate without attendance (403)", r.status_code == 403, r.text)

    r = c.post("/checkin/scan", headers=s1,
               json={"session_id": sid, "token": fresh_token(c, coord, sid)})
    check("setup: s001 checks in", r.status_code == 201, r.text)

    r = c.post("/certificates/participation", headers=s3, json={"club_day_id": day})
    check("student who did not attend cannot claim (403)", r.status_code == 403, r.text)

    r = c.post("/certificates/participation", headers=s1, json={"club_day_id": day})
    check("s001 claims a participation certificate (201)",
          r.status_code == 201 and r.json()["kind"] == "participation", r.text)
    cert = r.json()["id"]

    r = c.post("/certificates/participation", headers=s1, json={"club_day_id": day})
    check("claiming twice returns the same certificate (200, already_issued)",
          r.status_code == 200 and r.json()["already_issued"] is True
          and r.json()["id"] == cert, r.text)

    ready = wait_ready(c, s1, cert)
    check("worker generated the PDF (status ready)", ready,
          "check: docker compose logs worker --tail 40")
    if not ready:
        print("\nStopping: the PDF was never produced, so the remaining checks would be meaningless.")
        sys.exit(1)

    # ---- download permissions ----
    r = c.get(f"/certificates/{cert}/download", headers=s1)
    pdf = r.content
    check("student downloads own PDF (real PDF, not empty)",
          r.status_code == 200 and pdf.startswith(b"%PDF") and len(pdf) > 1000,
          f"{r.status_code} {len(pdf)} bytes")
    r = c.get(f"/certificates/{cert}/download", headers=s3)
    check("another student cannot download it (404)", r.status_code == 404, r.text)
    r = c.get(f"/certificates/{cert}/download", headers=coord)
    check("coordinator downloads the same bytes",
          r.status_code == 200 and r.content == pdf, str(r.status_code))
    r = c.get(f"/certificates/{cert}/download")
    check("download needs login (401)", r.status_code == 401, r.text)

    # ---- PUBLIC verification (no login header at all) ----
    r = c.get(f"/api/verify/{cert}")
    v = r.json() if r.status_code == 200 else {}
    check("public verify says VALID without logging in",
          r.status_code == 200 and v.get("overall") == "VALID" and v.get("record_intact") is True,
          r.text)
    cinfo = v.get("certificate") or {}
    check("verify shows the right club and a student name",
          cinfo.get("club_name") == "Robotics" and bool(cinfo.get("student_name")), str(cinfo))
    check("verify does not expose student code or email",
          "student_code" not in cinfo and "email" not in cinfo, str(cinfo))

    r = c.get(f"/verify/{cert}")
    check("QR landing page is an HTML page (200)",
          r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), str(r.status_code))
    r = c.get(f"/api/verify/{uuid.uuid4()}")
    check("unknown certificate id -> 404", r.status_code == 404, r.text)
    r = c.get("/api/verify/not-a-uuid")
    check("malformed id -> 404", r.status_code == 404, r.text)

    # ---- file tamper detection ----
    r = c.post(f"/api/verify/{cert}/file", files={"file": ("c.pdf", pdf, "application/pdf")})
    check("the original PDF matches", r.status_code == 200 and r.json()["file_matches"] is True, r.text)
    r = c.post(f"/api/verify/{cert}/file",
               files={"file": ("c.pdf", pdf + b"\n%edited", "application/pdf")})
    check("a PDF with one extra byte does NOT match",
          r.status_code == 200 and r.json()["file_matches"] is False, r.text)

    # ---- record tamper detection (edit the database directly) ----
    original_name = cinfo.get("student_name")
    db_exec("""UPDATE certificates SET snapshot = jsonb_set(snapshot, '{student_name}', '"Mallory"')
               WHERE id = $1::uuid""", cert)
    v2 = c.get(f"/api/verify/{cert}").json()
    check("editing the DB row is detected (TAMPERED_RECORD, no details shown)",
          v2.get("overall") == "TAMPERED_RECORD" and v2.get("certificate") is None, str(v2))
    db_exec("""UPDATE certificates SET snapshot = jsonb_set(snapshot, '{student_name}', to_jsonb($2::text))
               WHERE id = $1::uuid""", cert, original_name)
    v3 = c.get(f"/api/verify/{cert}").json()
    check("restoring the row makes it VALID again", v3.get("overall") == "VALID", str(v3))

    # ---- bulk issue ----
    r = c.post(f"/club-days/{day}/certificates/participation", headers=s1)
    check("student cannot bulk-issue (403)", r.status_code == 403, r.text)
    r = c.post(f"/club-days/{day}/certificates/participation", headers=coord)
    check("coordinator bulk-issue: nothing new, attendee already has one",
          r.status_code == 200 and r.json()["issued"] == 0 and r.json()["already_had"] == 1, r.text)

    # ---- achievement ----
    body = {"student_id": s1_id, "club_day_id": day, "achievement": "Best Project Award"}
    r = c.post("/certificates/achievement", headers=s1, json=body)
    check("student cannot issue achievement (403)", r.status_code == 403, r.text)
    r = c.post("/certificates/achievement", headers=coord, json=body)
    check("coordinator cannot issue achievement (403)", r.status_code == 403, r.text)
    r = c.post("/certificates/achievement", headers=advisor, json={**body, "student_id": s3_id})
    check("achievement for a non-member is refused (422)", r.status_code == 422, r.text)
    r = c.post("/certificates/achievement", headers=advisor, json=body)
    check("advisor issues an achievement certificate (201)",
          r.status_code == 201 and r.json()["kind"] == "achievement", r.text)
    ach = r.json().get("id")
    r = c.post("/certificates/achievement", headers=advisor, json=body)
    check("issuing the same achievement twice is blocked (409)", r.status_code == 409, r.text)

    check("achievement PDF is generated", wait_ready(c, s1, ach), "check worker logs")
    va = c.get(f"/api/verify/{ach}").json()
    check("verify shows the achievement text",
          va.get("overall") == "VALID"
          and (va.get("certificate") or {}).get("achievement") == "Best Project Award", str(va))

    r = c.get(f"/club-days/{day}/certificates", headers=coord)
    check("coordinator lists both certificates for the club day",
          r.status_code == 200 and len(r.json()) == 2, r.text)

    # ---- revocation ----
    r = c.post(f"/certificates/{ach}/revoke", headers=coord, json={"reason": "Issued by mistake"})
    check("coordinator cannot revoke (403)", r.status_code == 403, r.text)
    r = c.post(f"/certificates/{ach}/revoke", headers=advisor, json={"reason": "no"})
    check("revoke needs a real reason (422)", r.status_code == 422, r.text)
    r = c.post(f"/certificates/{ach}/revoke", headers=advisor, json={"reason": "Issued by mistake"})
    check("advisor revokes the certificate (200)", r.status_code == 200, r.text)
    vr = c.get(f"/api/verify/{ach}").json()
    check("verify now says REVOKED", vr.get("overall") == "REVOKED", str(vr))
    r = c.post(f"/certificates/{ach}/revoke", headers=advisor, json={"reason": "Issued by mistake"})
    check("revoking twice is blocked (409)", r.status_code == 409, r.text)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


main()