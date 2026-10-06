"""Smoke test for Stage 5 (events, teams, check-in, results, concurrency).
Run: docker compose exec api python -m scripts.smoke5
Creates fresh events each run. Uses the seeded demo users.
"""
import concurrent.futures
import sys

import httpx

BASE = "http://localhost:8000"
PW = "password123"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(detail)[:300]))


def login(c, email):
    r = c.post("/auth/login", json={"identifier": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def new_event(c, h, **kw):
    body = {"name": "Smoke event", "type": "hackathon", "capacity": 3,
            "team_size": 2, "starts_at": "2099-01-01T00:00:00+00:00"}
    body.update(kw)
    return c.post("/events", headers=h, json=body)


def main():
    c = httpx.Client(base_url=BASE, timeout=20)
    admin = login(c, "admin@demo.edu")
    s1, s2, s3 = (login(c, "s00%d@demo.edu" % i) for i in (1, 2, 3))
    n1 = login(c, "n001@demo.edu")  # other campus
    uid = {k: c.get("/auth/me", headers=h).json()["id"] for k, h in (("s1", s1), ("s2", s2), ("s3", s3))}

    r = new_event(c, s1)
    check("student cannot create an event (403)", r.status_code == 403, r.text)
    r = new_event(c, admin, capacity=0)
    check("capacity 0 is rejected (422)", r.status_code == 422, r.text)
    r = new_event(c, admin)
    check("campus admin creates an event (201)", r.status_code == 201, r.text)
    a = r.json()["id"]
    past = new_event(c, admin, name="Old event", starts_at="2020-01-01T00:00:00+00:00").json()["id"]

    r = c.get("/events", headers=s1)
    mine = next((e for e in r.json() if e["id"] == a), None) if r.status_code == 200 else None
    check("student sees the event, 0 registered, not registered yet",
          mine is not None and mine["registered"] == 0 and mine["my_registered"] is False, r.text)
    r = c.get("/events", headers=n1)
    check("other campus does not see it", r.status_code == 200 and all(e["id"] != a for e in r.json()), r.text)
    r = c.get("/events/%d" % a, headers=n1)
    check("other campus cannot open it (404)", r.status_code == 404, r.text)
    r = c.post("/events/%d/register" % a, headers=n1)
    check("other campus cannot register (404)", r.status_code == 404, r.text)

    r = c.post("/events/%d/register" % past, headers=s1)
    check("registration closed once the event has started (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/register" % a, headers=s1)
    check("s001 registers (201)", r.status_code == 201, r.text)
    r = c.post("/events/%d/register" % a, headers=s1)
    check("double registration is blocked (409)", r.status_code == 409, r.text)
    for h in (s2, s3):
        c.post("/events/%d/register" % a, headers=h)

    b = new_event(c, admin, name="One seat", capacity=1, team_size=None).json()["id"]

    def reg(h):
        with httpx.Client(base_url=BASE, timeout=20) as cc:
            return cc.post("/events/%d/register" % b, headers=h).status_code

    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        codes = list(ex.map(reg, [s1, s2, s3]))
    check("3 simultaneous registrations for 1 seat: exactly one wins",
          sorted(codes) == [201, 409, 409], str(codes))
    r = c.get("/events/%d" % b, headers=admin)
    check("seat count is exactly 1 afterwards", r.json().get("registered") == 1, r.text)

    r = c.post("/events/%d/teams" % a, headers=s1, json={"name": "Alpha"})
    check("s001 creates team Alpha (201)", r.status_code == 201, r.text)
    alpha = r.json().get("id")
    r = c.post("/events/%d/teams" % a, headers=s1, json={"name": "Second"})
    check("a student cannot create a second team (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/teams" % a, headers=s2, json={"name": "ALPHA"})
    check("team names are unique ignoring case (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/teams/%d/join" % (a, alpha), headers=s2)
    check("s002 joins Alpha (200)", r.status_code == 200, r.text)
    r = c.post("/events/%d/teams/%d/join" % (a, alpha), headers=s3)
    check("full team rejects a third member (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/teams" % a, headers=s3, json={"name": "Beta"})
    check("s003 creates team Beta (201)", r.status_code == 201, r.text)
    beta = r.json().get("id")
    r = c.post("/events/%d/teams/%d/join" % (a, beta), headers=s2)
    check("a student already in a team cannot join another (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/teams/leave" % a, headers=s2)
    check("s002 leaves Alpha (200)", r.status_code == 200, r.text)
    r = c.post("/events/%d/teams/%d/join" % (a, beta), headers=s2)
    check("s002 joins Beta (200)", r.status_code == 200, r.text)
    r = c.post("/events/%d/teams/999999/join" % a, headers=s1)
    check("joining a team that does not exist (404 or 409)", r.status_code in (404, 409), r.text)

    r = c.delete("/events/%d/register" % a, headers=s2)
    check("s002 unregisters (200)", r.status_code == 200, r.text)
    d = c.get("/events/%d" % a, headers=admin).json()
    teams = {t["name"]: t for t in d.get("teams", [])}
    check("unregistering also removed s002 from Beta",
          d.get("registered") == 2 and len(teams.get("Beta", {}).get("members", [])) == 1, str(d))

    r = c.get("/events/%d/registrations" % a, headers=s1)
    check("student cannot list registrations (403)", r.status_code == 403, r.text)
    r = c.post("/events/%d/checkin" % a, headers=s1, json={"student_id": uid["s1"]})
    check("student cannot check people in (403)", r.status_code == 403, r.text)
    r = c.post("/events/%d/checkin" % a, headers=admin, json={"student_id": uid["s1"]})
    check("admin checks s001 in (200)", r.status_code == 200, r.text)
    r = c.post("/events/%d/checkin" % a, headers=admin, json={"student_id": uid["s1"]})
    check("checking in twice is blocked (409)", r.status_code == 409, r.text)
    r = c.post("/events/%d/checkin" % a, headers=admin, json={"student_id": uid["s2"]})
    check("checking in someone not registered (404)", r.status_code == 404, r.text)
    r = c.get("/events/%d/registrations" % a, headers=admin)
    rows = r.json() if r.status_code == 200 else []
    check("registration list shows 2 students, s001 checked in",
          len(rows) == 2 and any(x["student_id"] == uid["s1"] and x["checked_in_at"] for x in rows), r.text)

    good = {"results": [{"team_id": alpha, "rank": 1}, {"team_id": beta, "rank": 2}]}
    r = c.put("/events/%d/results" % a, headers=s1, json=good)
    check("student cannot set results (403)", r.status_code == 403, r.text)
    r = c.put("/events/%d/results" % a, headers=admin,
              json={"results": [{"team_id": alpha, "rank": 1}, {"team_id": alpha, "rank": 2}]})
    check("same team twice is rejected (422)", r.status_code == 422, r.text)
    r = c.put("/events/%d/results" % a, headers=admin, json={"results": [{"team_id": 999999, "rank": 1}]})
    check("a team from nowhere is rejected (422)", r.status_code == 422, r.text)
    r = c.put("/events/%d/results" % a, headers=admin,
              json={"results": [{"team_id": alpha, "student_id": uid["s1"], "rank": 1}]})
    check("a result must name a team OR a student, not both (422)", r.status_code == 422, r.text)
    r = c.put("/events/%d/results" % a, headers=admin, json=good)
    check("admin publishes results (200)", r.status_code == 200 and r.json()["saved"] == 2, r.text)
    r = c.get("/events/%d/results" % a, headers=s1)
    rs = r.json() if r.status_code == 200 else []
    check("student reads results, Alpha ranked first",
          len(rs) == 2 and rs[0]["rank"] == 1 and rs[0]["team_name"] == "Alpha", r.text)
    r = c.post("/events/%d/teams/leave" % a, headers=s3)
    check("teams are locked once results are published (409)", r.status_code == 409, r.text)
    r = c.delete("/events/%d/register" % a, headers=s3)
    check("unregistering is locked once results are published (409)", r.status_code == 409, r.text)

    passed = sum(results)
    print("\n%d/%d checks passed" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


main()
