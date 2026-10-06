"""Builds the load-test section of TEST_REPORT.md from loadtest/results/*.
Run from the repo root (loadtest/run.sh does this)."""
import json
import os
import re

R = "loadtest/results"


def load(n):
    try:
        with open("%s/level-%d.json" % (R, n)) as f:
            return json.load(f)
    except Exception:
        return None


def dbrows(n):
    try:
        with open("%s/db-%d.txt" % (R, n)) as f:
            return int(f.read().strip())
    except Exception:
        return None


hw = []
if os.path.exists(R + "/hw.txt"):
    hw = open(R + "/hw.txt").read().strip().split("\n")
docker_line = "unknown resources"
if hw and len(hw[0].split()) == 2:
    ncpu, mem = hw[0].split()
    docker_line = "%s CPUs and %.1f GB RAM available to Docker" % (ncpu, int(mem) / 2 ** 30)
host = hw[1] if len(hw) > 1 else "a laptop"

rows, verdicts = [], []
for n in (100, 300, 500):
    d, db = load(n), dbrows(n)
    if not d:
        rows.append("| %d | no result | | | | | | | | | | FAIL |" % n)
        verdicts.append(False)
        continue
    errors = d["other_errors"] + d["already_409"]
    ok = d["p95_ms"] < 1000 and errors == 0 and db == d["created_201"] and d["created_201"] == n
    verdicts.append(ok)
    rows.append("| %d | %d | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
        n, d["requests"], d["created_201"], errors, db, d["avg_ms"], d["med_ms"],
        d["p95_ms"], d["p99_ms"], d["max_ms"], d["burst_seconds"], "PASS" if ok else "FAIL"))

result = ("All three levels met the target (p95 under 1 second, no errors, database rows equal successful requests)."
          if all(verdicts) else
          "At least one level missed the target. See the FAIL rows; raw output is in loadtest/results/.")

sec = """## Load test: student check-in (POST /checkin/scan)

Measured with k6 (`loadtest/checkin.js`, driver `loadtest/run.sh`); raw output is in `loadtest/results/`.
Machine: %s, %s. The API ran with 4 uvicorn workers, and k6 ran on the same machine, so the load
generator and the system under test shared the CPU (real servers should do at least as well).

How: N distinct students, all approved members of one club, each scan once, all starting at the same moment.
That is a burst, stricter than the brief's 500 check-ins spread over 5 minutes. Tokens are pre-issued and the
rotating QR code is computed by the load generator from the session secret, so this measures the student scan
endpoint only (not login, and not the coordinator's QR screen). A 50-student warm-up runs first and is not reported.

Target from the brief: p95 under 1 second. Latencies are in milliseconds. "Rows in DB" is the number of attendance
rows found for that session afterwards; it must equal "Created (201)" (nothing lost, nothing duplicated).

| Concurrent students | Requests | Created (201) | Errors | Rows in DB | avg | median | p95 | p99 | max | Burst (s) | Target |
|---|---|---|---|---|---|---|---|---|---|---|---|
%s

%s

Not measured here: the breaking point above 500 students, sustained load over minutes, and speech or PDF load.
""" % (host, docker_line, "\n".join(rows), result)

p = "TEST_REPORT.md"
s = open(p).read()
s = re.sub(r"## Load test: student check-in.*?(?=\n## )", "", s, flags=re.S)
s = re.sub(r"^- Load test \(.*\n", "", s, flags=re.M)
if "## Not yet done" in s:
    s = s.replace("## Not yet done", sec + "\n## Not yet done", 1)
else:
    s = s.rstrip() + "\n\n" + sec
open(p, "w").write(s)
print("TEST_REPORT.md updated")
print("\n".join(rows))
