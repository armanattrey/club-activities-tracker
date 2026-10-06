"""Smoke test for Stage 3 (submissions, files, rubric scoring).
Run: docker compose exec api python -m scripts.smoke3
Creates two fresh club days each run. Uses the seeded demo users.
"""
import sys

import httpx

BASE = "http://localhost:8000"
PW = "password123"
results: list[bool] = []

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def check(name: str, ok: bool, detail: str = ""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))


def login(c, email):
    r = c.post("/auth/login", json={"identifier": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_open_day(c, coord, advisor, deadline):
    r = c.post("/club-days", headers=coord, json={
        "club_id": 1, "day_date": "2026-10-10", "title": "Stage 3 smoke",
        "submission_deadline": deadline})
    day = r.json()["id"]
    c.put(f"/club-days/{day}/plan", headers=coord, json={"body": "Stage 3 smoke plan"})
    c.post(f"/club-days/{day}/plan/decision", headers=advisor, json={"decision": "approve"})
    for t in ("announced", "open"):
        c.post(f"/club-days/{day}/transition", headers=coord, json={"target": t})
    return day


def upload(c, h, day, name, data, ctype, kind="file"):
    return c.post(f"/club-days/{day}/submissions/file", headers=h,
                  files={"file": (name, data, ctype)}, data={"kind": kind})


def main():
    c = httpx.Client(base_url=BASE, timeout=15)
    coord = login(c, "coord@demo.edu")
    advisor = login(c, "advisor@demo.edu")
    s1 = login(c, "s001@demo.edu")
    s3 = login(c, "s003@demo.edu")

    # setup: s001 must be an approved member of club 1
    c.post("/clubs/1/join", headers=s1, json={"type": "primary"})
    for m in c.get("/clubs/1/memberships", headers=coord).json():
        if m["student_code"] == "S001" and m["status"] == "pending":
            c.post(f"/memberships/{m['id']}/decision", headers=coord, json={"decision": "approve"})

    r = c.get("/rubric", headers=s1)
    check("rubric lists 3 criteria", r.status_code == 200 and len(r.json()) == 3, r.text)

    day = make_open_day(c, coord, advisor, "2099-01-01T00:00:00+00:00")   # future deadline
    late_day = make_open_day(c, coord, advisor, "2020-01-01T00:00:00+00:00")  # already passed

    # ---- uploads ----
    r = upload(c, s1, day, "report.pdf", PDF, "application/pdf")
    check("student uploads a PDF (201, not late)",
          r.status_code == 201 and r.json()["is_late"] is False, r.text)
    sub = r.json().get("id")
    check("response does not leak the storage key", "object_key" not in r.json())

    r = upload(c, s1, day, "fake.pdf", b"this is not a pdf at all", "application/pdf")
    check("file whose content doesn't match its extension is rejected (422)",
          r.status_code == 422, r.text)
    r = upload(c, s1, day, "virus.exe", b"MZ" + b"\x00" * 20, "application/octet-stream")
    check("disallowed extension is rejected (422)", r.status_code == 422, r.text)
    r = upload(c, s1, day, "empty.pdf", b"", "application/pdf")
    check("empty file is rejected (422)", r.status_code == 422, r.text)
    r = upload(c, s1, day, "doc.pdf", PDF, "application/pdf", kind="photo")
    check("PDF sent as a 'photo' is rejected (422)", r.status_code == 422, r.text)

    r = upload(c, s1, day, "team.png", PNG, "image/png", kind="photo")
    check("student uploads a photo (201)", r.status_code == 201, r.text)

    r = c.post(f"/club-days/{day}/submissions/link", headers=s1,
               json={"url": "https://example.com/my-slides"})
    check("student submits a link (201)", r.status_code == 201, r.text)
    r = c.post(f"/club-days/{day}/submissions/link", headers=s1,
               json={"url": "javascript:alert(1)"})
    check("non-http link is rejected (422)", r.status_code == 422, r.text)

    r = upload(c, s1, late_day, "late.pdf", PDF, "application/pdf")
    check("upload after the deadline is accepted but flagged late",
          r.status_code == 201 and r.json()["is_late"] is True, r.text)

    r = upload(c, s3, day, "x.pdf", PDF, "application/pdf")
    check("non-member cannot submit (404)", r.status_code == 404, r.text)

    # ---- downloads ----
    r = c.get(f"/submissions/{sub}/download", headers=coord)
    check("coordinator downloads the file, bytes identical",
          r.status_code == 200 and r.content == PDF, f"{r.status_code}")
    r = c.get(f"/submissions/{sub}/download", headers=s1)
    check("student downloads their own file", r.status_code == 200, r.text)
    r = c.get(f"/submissions/{sub}/download", headers=s3)
    check("another student cannot download it (404)", r.status_code == 404, r.text)

    # ---- scoring ----
    good = {"content": 4, "presentation": 3, "participation": 5}
    r = c.put(f"/submissions/{sub}/evaluation", headers=s1, json={"scores": good})
    check("student cannot score (403)", r.status_code == 403, r.text)
    r = c.put(f"/submissions/{sub}/evaluation", headers=coord, json={"scores": good})
    check("scoring while day is 'open' is refused (409)", r.status_code == 409, r.text)

    r = c.post(f"/club-days/{day}/transition", headers=coord, json={"target": "submissions"})
    check("club day moves to 'submissions'", r.status_code == 200, r.text)

    r = c.put(f"/submissions/{sub}/evaluation", headers=coord,
              json={"scores": {"content": 4, "presentation": 3}})
    check("missing a criterion is rejected (422)", r.status_code == 422, r.text)
    r = c.put(f"/submissions/{sub}/evaluation", headers=coord,
              json={"scores": {"content": 9, "presentation": 3, "participation": 5}})
    check("score out of range is rejected (422)", r.status_code == 422, r.text)

    r = c.put(f"/submissions/{sub}/evaluation", headers=coord,
              json={"scores": good, "comment": "Solid demo"})
    check("coordinator scores the submission (total 12/15)",
          r.status_code == 200 and r.json()["total"] == 12 and r.json()["max_total"] == 15, r.text)
    r = c.put(f"/submissions/{sub}/evaluation", headers=coord,
              json={"scores": {"content": 5, "presentation": 4, "participation": 5}})
    check("re-scoring updates the score (total 14/15)",
          r.status_code == 200 and r.json()["total"] == 14, r.text)

    r = c.get(f"/club-days/{day}/submissions", headers=s1)
    check("student cannot see scores before the day is closed",
          r.status_code == 200 and all(s["evaluation"] is None for s in r.json()), r.text)
    r = c.get(f"/club-days/{day}/submissions", headers=coord)
    check("coordinator sees scores and all submissions",
          r.status_code == 200 and any(s["evaluation"] for s in r.json()), r.text)

    # ---- lifecycle gates ----
    r = c.post(f"/club-days/{day}/transition", headers=coord, json={"target": "evaluating"})
    check("club day moves to 'evaluating'", r.status_code == 200, r.text)
    r = upload(c, s1, day, "toolate.pdf", PDF, "application/pdf")
    check("uploads are refused once evaluating (409)", r.status_code == 409, r.text)

    r = c.post(f"/club-days/{day}/transition", headers=coord, json={"target": "closed"})
    check("club day moves to 'closed'", r.status_code == 200, r.text)
    r = c.get(f"/club-days/{day}/submissions", headers=s1)
    scored = [s for s in r.json() if s["evaluation"]]
    check("student now sees their score (14/15)",
          len(scored) == 1 and scored[0]["evaluation"]["total"] == 14, r.text)
    r = c.put(f"/submissions/{sub}/evaluation", headers=coord, json={"scores": good})
    check("scoring is frozen after close (409)", r.status_code == 409, r.text)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


main()