"""Smoke test for Stage 6 (dashboards, monthly report, CSV exports).
Run: docker compose exec api python -m scripts.smoke6
Read-only. Every dashboard figure is compared with independent counts taken
straight from the database.
"""
import asyncio
import json
import os
import sys

import asyncpg
import httpx

BASE = "http://localhost:8000"
PW = "password123"
results: list = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(detail)[:300]))


def login(c, email):
    r = c.post("/auth/login", json={"identifier": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


async def _rows(sql, *args):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def q(sql, *args):
    return asyncio.run(_rows(sql, *args))


RAW_CLUB = """SELECT
 (SELECT count(*) FROM club_memberships WHERE club_id = $1 AND status = 'approved') AS mem,
 (SELECT count(*) FROM club_days WHERE club_id = $1 AND status <> 'planned') AS cd,
 (SELECT count(*) FROM attendance a JOIN checkin_sessions s ON s.id = a.session_id
    JOIN club_days d ON d.id = s.club_day_id WHERE d.club_id = $1) AS att,
 (SELECT count(*) FROM submissions sb JOIN club_days d ON d.id = sb.club_day_id
    WHERE d.club_id = $1) AS sub,
 (SELECT count(*) FROM certificates ce JOIN club_days d ON d.id = ce.ref_id
    WHERE ce.ref_type = 'club_day' AND ce.revoked_at IS NULL AND d.club_id = $1) AS cert"""

RAW_MONTH = """SELECT
 (SELECT count(*) FROM club_days d WHERE d.club_id = $1 AND d.status <> 'planned'
    AND d.day_date >= DATE '2026-10-01' AND d.day_date < DATE '2026-11-01') AS cd,
 (SELECT count(*) FROM attendance a JOIN checkin_sessions s ON s.id = a.session_id
    JOIN club_days d ON d.id = s.club_day_id WHERE d.club_id = $1 AND d.status <> 'planned'
    AND d.day_date >= DATE '2026-10-01' AND d.day_date < DATE '2026-11-01') AS att,
 (SELECT count(*) FROM submissions sb JOIN club_days d ON d.id = sb.club_day_id
    WHERE d.club_id = $1 AND d.status <> 'planned'
    AND d.day_date >= DATE '2026-10-01' AND d.day_date < DATE '2026-11-01') AS sub,
 (SELECT count(*) FROM certificates ce JOIN club_days d ON d.id = ce.ref_id
    WHERE ce.ref_type = 'club_day' AND ce.revoked_at IS NULL AND d.club_id = $1
    AND d.status <> 'planned'
    AND d.day_date >= DATE '2026-10-01' AND d.day_date < DATE '2026-11-01') AS cert"""


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    student = login(c, "s001@demo.edu")
    coord = login(c, "coord@demo.edu")
    advisor = login(c, "advisor@demo.edu")
    admin = login(c, "admin@demo.edu")
    sup = login(c, "super@demo.edu")
    campus = c.get("/auth/me", headers=admin).json()["campus_id"]
    sid = c.get("/auth/me", headers=student).json()["id"]

    # ---- access control ----
    r = c.get("/dashboards/summary")
    check("dashboards need login (401)", r.status_code == 401, r.text)
    for path in ("/dashboards/summary", "/dashboards/clubs", "/dashboards/students",
                 "/dashboards/zero-participation", "/dashboards/inactive-clubs",
                 "/dashboards/monthly-report?month=2026-10", "/dashboards/export/clubs"):
        r = c.get(path, headers=student)
        check("student is refused: " + path + " (403)", r.status_code == 403, r.status_code)

    # ---- summary vs raw ----
    sm = c.get("/dashboards/summary", headers=admin).json()
    raw = q("""SELECT (SELECT count(*) FROM club_days d JOIN clubs c ON c.id = d.club_id
                        WHERE c.campus_id = $1) AS days,
                      (SELECT count(*) FROM events WHERE campus_id = $1) AS events,
                      (SELECT count(*) FROM registrations r JOIN events e ON e.id = r.event_id
                        WHERE e.campus_id = $1) AS regs,
                      (SELECT count(*) FROM clubs WHERE campus_id = $1) AS clubs""", campus)[0]
    check("summary counts match the raw tables",
          sm.get("club_days_total") == raw["days"] and sm.get("events") == raw["events"]
          and sm.get("event_registrations") == raw["regs"] and sm.get("clubs") == raw["clubs"],
          str(sm) + " vs " + str(raw))
    check("summary status breakdown adds up to the total",
          sum(sm.get("club_days_by_status", {}).values()) == sm.get("club_days_total"), sm)

    # ---- per-club table vs raw ----
    rows = c.get("/dashboards/clubs", headers=admin).json()
    bad = []
    for row in rows:
        rw = q(RAW_CLUB, row["club_id"])[0]
        got = (row["members"], row["club_days"], row["attendance_records"],
               row["submissions"], row["certificates"])
        want = (rw["mem"], rw["cd"], rw["att"], rw["sub"], rw["cert"])
        if got != want:
            bad.append((row["club_name"], got, want))
    check("clubs table matches the raw tables for every club", len(rows) >= 2 and not bad, bad)

    ev = q("""SELECT e.scores FROM evaluations e JOIN submissions sb ON sb.id = e.submission_id
              JOIN club_days d ON d.id = sb.club_day_id WHERE d.club_id = 1""")
    robotics = next((x for x in rows if x["club_id"] == 1), {})
    if ev:
        totals = [sum(json.loads(e["scores"]).values()) for e in ev]
        want_avg = round(sum(totals) / len(totals), 2)
        check("average score matches the raw evaluations",
              robotics.get("avg_score") is not None
              and abs(float(robotics["avg_score"]) - want_avg) < 0.011,
              str(robotics.get("avg_score")) + " vs " + str(want_avg))
    else:
        check("average score is empty when nothing is scored", robotics.get("avg_score") is None, robotics)

    # ---- role scoping ----
    def names(h):
        return {x["club_name"] for x in c.get("/dashboards/clubs", headers=h).json()}
    check("coordinator sees only their own club", names(coord) == {"Robotics"}, names(coord))
    check("advisor sees the clubs they advise",
          {"Robotics", "Coding Club"} <= names(advisor), names(advisor))
    check("campus admin sees the whole campus", {"Robotics", "Coding Club"} <= names(admin), names(admin))
    check("super admin sees everything", {"Robotics", "Coding Club"} <= names(sup), names(sup))

    # ---- monthly report ----
    mr = c.get("/dashboards/monthly-report?month=2026-10", headers=admin).json()
    check("monthly report reconciles with the raw totals",
          mr.get("reconciled") is True and not mr.get("mismatches"), mr.get("mismatches"))
    row1 = next((x for x in mr.get("clubs", []) if x["club_id"] == 1), {})
    rw = q(RAW_MONTH, 1)[0]
    check("monthly figures for Robotics match the raw tables",
          (row1.get("club_days"), row1.get("attendance_records"), row1.get("submissions"),
           row1.get("certificates")) == (rw["cd"], rw["att"], rw["sub"], rw["cert"]),
          str(row1) + " vs " + str(rw))
    old = c.get("/dashboards/monthly-report?month=2020-01", headers=admin).json()
    check("an empty month is all zeros and still reconciles",
          old.get("reconciled") is True
          and all(x["club_days"] == 0 and x["attendance_records"] == 0 for x in old.get("clubs", [])), old)
    r = c.get("/dashboards/monthly-report?month=2026-12", headers=admin)
    check("December rolls over to the next year correctly (200)", r.status_code == 200, r.text)
    for bad_month in ("2026-13", "abc", "2026-1"):
        r = c.get("/dashboards/monthly-report?month=" + bad_month, headers=admin)
        check("bad month '" + bad_month + "' is rejected (422)", r.status_code == 422, r.status_code)
    r = c.get("/dashboards/monthly-report", headers=admin)
    check("month is required (422)", r.status_code == 422, r.status_code)
    cm = c.get("/dashboards/monthly-report?month=2026-10", headers=coord).json()
    check("coordinator's monthly report covers only their club",
          [x["club_name"] for x in cm.get("clubs", [])] == ["Robotics"], cm)

    # ---- inactive clubs ----
    for weeks in (1, 52):
        got = {x["club_id"] for x in c.get("/dashboards/inactive-clubs?weeks=" + str(weeks),
                                           headers=admin).json()}
        want = {x["id"] for x in q("""SELECT c.id FROM clubs c WHERE c.campus_id = $1 AND NOT EXISTS
            (SELECT 1 FROM club_days d WHERE d.club_id = c.id AND d.status <> 'planned'
             AND d.day_date >= current_date - ($2::int * 7))""", campus, weeks)}
        check("inactive clubs (" + str(weeks) + " weeks) match the raw tables", got == want, str(got) + " vs " + str(want))
    r = c.get("/dashboards/inactive-clubs?weeks=0", headers=admin)
    check("weeks=0 is rejected (422)", r.status_code == 422, r.status_code)

    # ---- zero participation ----
    zero_sql = """SELECT u.id FROM users u WHERE u.role = 'student' AND u.campus_id = $1
        AND NOT EXISTS (SELECT 1 FROM attendance a WHERE a.student_id = u.id)
        AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.student_id = u.id)
        AND NOT EXISTS (SELECT 1 FROM registrations r WHERE r.student_id = u.id)"""
    exp = {x["id"] for x in q(zero_sql, campus)}
    got = {x["student_id"] for x in c.get("/dashboards/zero-participation", headers=admin).json()}
    check("zero-participation list matches the raw tables", got == exp, str(got) + " vs " + str(exp))
    members = {x["student_id"] for x in q(
        "SELECT student_id FROM club_memberships WHERE club_id = 1 AND status = 'approved'")}
    got_c = {x["student_id"] for x in c.get("/dashboards/zero-participation", headers=coord).json()}
    check("coordinator sees zero-participation only among their members", got_c == (exp & members),
          str(got_c) + " vs " + str(exp & members))
    got_f = {x["student_id"] for x in c.get("/dashboards/zero-participation?club_id=1", headers=admin).json()}
    check("club filter works", got_f == (exp & members), str(got_f))
    r = c.get("/dashboards/zero-participation?club_id=999999", headers=admin)
    check("unknown club filter is 404", r.status_code == 404, r.status_code)
    r = c.get("/dashboards/zero-participation?club_id=2", headers=coord)
    check("a club outside the coordinator's scope is 404", r.status_code == 404, r.status_code)

    # ---- students ----
    allst = c.get("/dashboards/students?limit=1000", headers=admin).json()
    n_students = q("SELECT count(*) AS n FROM users WHERE role = 'student' AND campus_id = $1", campus)[0]["n"]
    check("students list covers every student on the campus", len(allst) == n_students, str(len(allst)) + " vs " + str(n_students))
    mine = next((x for x in allst if x["student_id"] == sid), {})
    rw = q("""SELECT (SELECT count(*) FROM attendance WHERE student_id = $1) AS att,
                     (SELECT count(*) FROM submissions WHERE student_id = $1) AS sub,
                     (SELECT count(*) FROM registrations WHERE student_id = $1) AS reg,
                     (SELECT count(*) FROM certificates WHERE student_id = $1 AND revoked_at IS NULL) AS cert""", sid)[0]
    check("one student's figures match the raw tables",
          (mine.get("attendance_records"), mine.get("submissions"), mine.get("event_registrations"),
           mine.get("certificates")) == (rw["att"], rw["sub"], rw["reg"], rw["cert"]), str(mine) + " vs " + str(rw))
    r = c.get("/dashboards/students?limit=1", headers=admin)
    check("pagination returns exactly one row", r.status_code == 200 and len(r.json()) == 1, r.text)
    r = c.get("/dashboards/students?limit=0", headers=admin)
    check("limit=0 is rejected (422)", r.status_code == 422, r.status_code)

    # ---- CSV exports ----
    r = c.get("/dashboards/export/clubs", headers=admin)
    lines = r.text.lstrip("\ufeff").splitlines()
    check("clubs CSV: right type, header, one row per club",
          r.status_code == 200 and "text/csv" in r.headers.get("content-type", "")
          and "club_name" in lines[0] and len(lines) - 1 == len(rows), (r.status_code, lines[:2]))
    r = c.get("/dashboards/export/students", headers=admin)
    lines = r.text.lstrip("\ufeff").splitlines()
    check("students CSV has one row per student", r.status_code == 200 and len(lines) - 1 == n_students, len(lines))
    r = c.get("/dashboards/export/monthly-report?month=2026-10", headers=admin)
    lines = r.text.lstrip("\ufeff").splitlines()
    check("monthly CSV has one row per club",
          r.status_code == 200 and len(lines) - 1 == len(mr.get("clubs", [])), len(lines))
    r = c.get("/dashboards/export/monthly-report", headers=admin)
    check("monthly CSV needs a month (422)", r.status_code == 422, r.status_code)
    r = c.get("/dashboards/export/nothing", headers=admin)
    check("unknown export is 404", r.status_code == 404, r.status_code)
    r = c.get("/dashboards/export/students", headers=student)
    check("student cannot export (403)", r.status_code == 403, r.status_code)

    from app.routers.dashboards import csv_safe
    check("CSV formula injection is neutralised",
          csv_safe("=HYPERLINK(1)").startswith("'=") and csv_safe("+1").startswith("'+")
          and csv_safe("@x").startswith("'@") and csv_safe("ok") == "ok" and csv_safe(None) == "")

    passed = sum(results)
    print("\n" + str(passed) + "/" + str(len(results)) + " checks passed")
    sys.exit(0 if passed == len(results) else 1)


main()
