# Test Report: Club Activities Tracker

Generated 2026-10-06 06:37 UTC by running each script against the live stack. Raw output is in test-results/.

## Functional and integrity tests

| Script | What it covers | Result |
|---|---|---|
| scripts/smoke.py | Auth, memberships, club-day lifecycle, rotating-QR check-in, corrections | 26/26 checks passed |
| scripts/smoke3.py | Submissions, upload validation, rubric scoring, score visibility | 29/29 checks passed |
| scripts/smoke4.py | Certificates, PDF generation, public verify page, tamper detection | 35/35 checks passed |
| scripts/smoke5.py | Events, teams, capacity under concurrency, results | 38/38 checks passed |
| scripts/smoke6.py | Dashboards, monthly report reconciliation, CSV export | 44/44 checks passed |

## Load test: student check-in (POST /checkin/scan)

Measured with k6 (`loadtest/checkin.js`, driver `loadtest/run.sh`); raw output is in `loadtest/results/`.
Machine: Apple M5, 10 CPUs and 7.7 GB RAM available to Docker. The API ran with 4 uvicorn workers, and k6 ran on the same machine, so the load
generator and the system under test shared the CPU (real servers should do at least as well).

How: N distinct students, all approved members of one club, each scan once, all starting at the same moment.
That is a burst, stricter than the brief's 500 check-ins spread over 5 minutes. Tokens are pre-issued and the
rotating QR code is computed by the load generator from the session secret, so this measures the student scan
endpoint only (not login, and not the coordinator's QR screen). A 50-student warm-up runs first and is not reported.

Target from the brief: p95 under 1 second. Latencies are in milliseconds. "Rows in DB" is the number of attendance
rows found for that session afterwards; it must equal "Created (201)" (nothing lost, nothing duplicated).

| Concurrent students | Requests | Created (201) | Errors | Rows in DB | avg | median | p95 | p99 | max | Burst (s) | Target |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 100 | 100 | 0 | 100 | 11.5 | 7.4 | 42.2 | 43.8 | 43.9 | 0 | PASS |
| 300 | 300 | 300 | 0 | 300 | 33.1 | 30.2 | 54 | 55.5 | 57.4 | 0.1 | PASS |
| 500 | 500 | 500 | 0 | 500 | 57.4 | 51.6 | 100.1 | 106.3 | 110.6 | 0.1 | PASS |

All three levels met the target (p95 under 1 second, no errors, database rows equal successful requests).

Not measured here: the breaking point above 500 students, sustained load over minutes, and speech or PDF load.

## Coverage by risk area

| Risk area | Covered by | Not covered |
|---|---|---|
| Check-in integrity | smoke.py: live QR, duplicate scan (409), bad token (403), non-member (403), corrections need a reason; unit tests: token expiry, session binding, wrong secret | Late flag and geo flag after the window opens (no automated test) |
| Certificate verification | smoke4.py: public verify without login, edited database row detected, one extra byte in the PDF detected, revocation, no student code or email leaked | PDF visual layout (checked by eye only), failed and pending states |
| Access control | smoke.py role refusals, smoke3.py (other student cannot download), smoke5.py (other campus gets 404), smoke6.py (students refused on every dashboard, scoped views) | A full endpoint-by-role matrix |
| Reconciliation | smoke6.py compares dashboards, per-student figures, inactive clubs and the monthly report with independent counts straight from the database | |
| Concurrency and scale | smoke5.py: 3 simultaneous registrations for 1 seat, exactly one wins; load test: 100, 300, 500 students | Concurrent team joins; the same student scanning twice at the same moment under load |
| Robustness | | Network drops, restarting services mid-session |

## Remaining verification and stretch work

- A single end-to-end run across three clubs (plan through monthly report) is not recorded in this report.
- Migration 009 additions (club approval, calendar CSV import, activity venues, presenter records, extended event types, campus and top-contributor reports) have not received integration-test coverage yet. This update was checked with Python/JavaScript syntax and whitespace checks only; the existing test results above predate these additions.
- WhatsApp Cloud API template delivery is implemented behind user opt-in and optional Meta credentials; provider delivery has not been verified because credentials and an approved sender/template are not configured. Telegram remains unimplemented.
- Sustained-load and breaking-point testing above 500 students; testing on a cloud server.
- Robustness tests (network drop, service restart during a session).
- Cost sheet needs the real hosting price (docs/COST.md).

## Follow-up verification (2026-10-08)

- WhatsApp opt-in, template dispatch, retry tracking, and delivery migration 010 were added. The Meta API call was not sent during this change because no provider credentials or approved template are configured.
- Local automated verification in this environment was limited to Python AST parsing, inline JavaScript parsing, and `git diff --check`. The unit suite and live API/device runs could not start: `pytest` is not installed in the host Python environment and the Docker daemon is unavailable.
- The current browser tab uses `file://`, which is blocked for live browser inspection. A deployed HTTPS URL is needed to run and show device viewport checks.

## Local runtime follow-up

- Added a Docker-free launcher using native PostgreSQL, process-local cache, in-process certificate work, and an in-process schedule loop. PostgreSQL remains required because core queries depend on PostgreSQL JSONB, `RETURNING`, time-zone functions, and row locks; this runtime was not switched to SQLite.
- `bash -n scripts/run_local.sh`, Docker Compose configuration validation, Python syntax parsing, frontend inline-script parsing, and direct in-memory cache/schedule checks passed.
- The full local launch was not exercised: this host has no PostgreSQL binaries, no running Docker daemon, and no pytest installation. Therefore API startup, migrations, certificate rendering, scheduler operations, and the retained production Celery/Redis path still need an integration run on a machine with the required services.

## Known limitations

- No rate limiting on the public verify endpoints (only a 5 MB upload cap).
- CORS is open to all origins (development setting).
- Files are stored on a local Docker volume, not S3.
- The existing smoke and load results above do not cover the latest calendar, approval and reporting additions.
- Non-Latin names in certificates need an extra font package.
