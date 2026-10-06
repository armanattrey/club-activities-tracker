"""Dashboards, monthly report and CSV exports (read-only).

Every number is computed by SQL over the raw tables (through two rollup views),
so a dashboard can never disagree with the raw data. The monthly report is
computed twice, per club and as raw totals, and says whether they reconcile.
Everything is scoped by role in SQL, not just hidden in the UI.
"""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.permissions import require_role
from app.db import get_conn

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

STAFF = ("coordinator", "advisor", "campus_admin", "super_admin")
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
MAX_EXPORT = 20000


# ------------------------------------------------------------------ scoping
def club_scope(user: dict):
    """SQL condition on clubs alias c, written against $1, plus the value for $1."""
    role = user["role"]
    if role == "super_admin":
        return "$1::bigint IS NOT NULL", 0
    if role == "campus_admin":
        return "c.campus_id = $1::bigint", user["campus_id"]
    if role == "coordinator":
        return "c.id IN (SELECT club_id FROM club_coordinators WHERE user_id = $1::bigint)", user["id"]
    return "c.advisor_id = $1::bigint", user["id"]  # advisor


def student_scope(user: dict):
    """SQL condition on the v_student_stats alias v, written against $1."""
    role = user["role"]
    if role == "super_admin":
        return "$1::bigint IS NOT NULL", 0
    if role == "campus_admin":
        return "v.campus_id = $1::bigint", user["campus_id"]
    frag, val = club_scope(user)
    return (
        "v.student_id IN (SELECT m.student_id FROM club_memberships m "
        "JOIN clubs c ON c.id = m.club_id "
        f"WHERE m.status = 'approved' AND {frag})"
    ), val


async def _club_in_scope(conn, user: dict, club_id: int):
    frag, val = club_scope(user)
    ok = await conn.fetchval(f"SELECT 1 FROM clubs c WHERE c.id = $2::int AND {frag}", val, club_id)
    if not ok:
        raise HTTPException(404, "Club not found")


# --------------------------------------------------------------- data helpers
async def _club_rows(conn, user: dict) -> list[dict]:
    frag, val = club_scope(user)
    rows = await conn.fetch(
        f"""SELECT v.club_id, v.club_name, v.members, v.capacity, v.club_days, v.last_club_day,
                   v.attendance_records, v.submissions, v.avg_score, v.certificates
            FROM v_club_stats v JOIN clubs c ON c.id = v.club_id
            WHERE {frag} ORDER BY v.club_name""",
        val,
    )
    return [dict(r) for r in rows]


async def _student_rows(conn, user: dict, *, only_zero=False, club_id=None, limit=100, offset=0):
    frag, val = student_scope(user)
    where, params = [frag], [val]
    if only_zero:
        where.append("v.attendance_records = 0 AND v.submissions = 0 AND v.event_registrations = 0")
    if club_id is not None:
        params.append(club_id)
        where.append(
            "v.student_id IN (SELECT m.student_id FROM club_memberships m "
            f"WHERE m.club_id = ${len(params)}::int AND m.status = 'approved')"
        )
    sql = (
        "SELECT v.student_id, v.name, v.student_code, v.attendance_records, v.submissions, "
        "v.event_registrations, v.certificates FROM v_student_stats v WHERE "
        + " AND ".join(where) + " ORDER BY v.name, v.student_id"
    )
    if limit is not None:
        params += [limit, offset]
        sql += f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    return [dict(r) for r in await conn.fetch(sql, *params)]


def _month_range(month: str):
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


async def _monthly(conn, user: dict, month: str) -> dict:
    start, end = _month_range(month)
    frag, val = club_scope(user)
    # Everything is attributed to the club day's date, so all figures use one rule.
    in_month = "d.status <> 'planned' AND d.day_date >= $2::date AND d.day_date < $3::date"

    per_club = await conn.fetch(
        f"""SELECT c.id AS club_id, c.name AS club_name,
              (SELECT count(*) FROM club_days d
                 WHERE d.club_id = c.id AND {in_month}) AS club_days,
              (SELECT count(*) FROM attendance a
                 JOIN checkin_sessions s ON s.id = a.session_id
                 JOIN club_days d ON d.id = s.club_day_id
                 WHERE d.club_id = c.id AND {in_month}) AS attendance_records,
              (SELECT count(DISTINCT a.student_id) FROM attendance a
                 JOIN checkin_sessions s ON s.id = a.session_id
                 JOIN club_days d ON d.id = s.club_day_id
                 WHERE d.club_id = c.id AND {in_month}) AS unique_attendees,
              (SELECT count(*) FROM submissions sb
                 JOIN club_days d ON d.id = sb.club_day_id
                 WHERE d.club_id = c.id AND {in_month}) AS submissions,
              (SELECT count(*) FROM certificates ce
                 JOIN club_days d ON d.id = ce.ref_id
                 WHERE ce.ref_type = 'club_day' AND ce.revoked_at IS NULL
                   AND d.club_id = c.id AND {in_month}) AS certificates
            FROM clubs c WHERE {frag} ORDER BY c.name""",
        val, start, end,
    )

    raw = await conn.fetchrow(
        f"""SELECT
              (SELECT count(*) FROM club_days d JOIN clubs c ON c.id = d.club_id
                 WHERE {frag} AND {in_month}) AS club_days,
              (SELECT count(*) FROM attendance a
                 JOIN checkin_sessions s ON s.id = a.session_id
                 JOIN club_days d ON d.id = s.club_day_id
                 JOIN clubs c ON c.id = d.club_id
                 WHERE {frag} AND {in_month}) AS attendance_records,
              (SELECT count(DISTINCT a.student_id) FROM attendance a
                 JOIN checkin_sessions s ON s.id = a.session_id
                 JOIN club_days d ON d.id = s.club_day_id
                 JOIN clubs c ON c.id = d.club_id
                 WHERE {frag} AND {in_month}) AS unique_attendees_overall,
              (SELECT count(*) FROM submissions sb
                 JOIN club_days d ON d.id = sb.club_day_id
                 JOIN clubs c ON c.id = d.club_id
                 WHERE {frag} AND {in_month}) AS submissions,
              (SELECT count(*) FROM certificates ce
                 JOIN club_days d ON d.id = ce.ref_id
                 JOIN clubs c ON c.id = d.club_id
                 WHERE ce.ref_type = 'club_day' AND ce.revoked_at IS NULL
                   AND {frag} AND {in_month}) AS certificates""",
        val, start, end,
    )

    keys = ("club_days", "attendance_records", "submissions", "certificates")
    mismatches = []
    for k in keys:
        total = sum(r[k] for r in per_club)
        if total != raw[k]:
            mismatches.append({"metric": k, "sum_of_clubs": total, "raw_total": raw[k]})
    totals = {k: raw[k] for k in keys}
    totals["unique_attendees_overall"] = raw["unique_attendees_overall"]
    return {
        "month": month,
        "clubs": [dict(r) for r in per_club],
        "totals": totals,
        "reconciled": not mismatches,
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------- CSV
def csv_safe(v) -> str:
    """Stop spreadsheet formula injection: a leading = + - @ is neutralised."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    out.write("\ufeff")  # BOM so Excel reads UTF-8 names correctly
    if rows:
        w = csv.writer(out)
        keys = list(rows[0].keys())
        w.writerow(keys)
        for r in rows:
            w.writerow([csv_safe(r[k]) for k in keys])
    return out.getvalue()


# ---------------------------------------------------------------- endpoints
@router.get("/summary")
async def summary(user: dict = Depends(require_role(*STAFF)), conn=Depends(get_conn)):
    clubs = await _club_rows(conn, user)
    frag, val = club_scope(user)
    by_status = await conn.fetch(
        f"""SELECT d.status, count(*) AS n FROM club_days d
            JOIN clubs c ON c.id = d.club_id WHERE {frag} GROUP BY d.status""",
        val,
    )
    if user["role"] == "super_admin":
        ev_frag, ev_val = "$1::bigint IS NOT NULL", 0
    else:
        ev_frag, ev_val = "e.campus_id = $1::bigint", user["campus_id"]
    events = await conn.fetchval(f"SELECT count(*) FROM events e WHERE {ev_frag}", ev_val)
    regs = await conn.fetchval(
        f"""SELECT count(*) FROM registrations r
            JOIN events e ON e.id = r.event_id WHERE {ev_frag}""",
        ev_val,
    )
    return {
        "clubs": len(clubs),
        "members": sum(c["members"] for c in clubs),
        "club_days_total": sum(r["n"] for r in by_status),
        "club_days_approved_or_later": sum(c["club_days"] for c in clubs),
        "club_days_by_status": {r["status"]: r["n"] for r in by_status},
        "attendance_records": sum(c["attendance_records"] for c in clubs),
        "submissions": sum(c["submissions"] for c in clubs),
        "certificates": sum(c["certificates"] for c in clubs),
        "events": events,
        "event_registrations": regs,
    }


@router.get("/clubs")
async def clubs(user: dict = Depends(require_role(*STAFF)), conn=Depends(get_conn)):
    return await _club_rows(conn, user)


@router.get("/inactive-clubs")
async def inactive_clubs(
    weeks: int = Query(4, ge=1, le=104),
    user: dict = Depends(require_role(*STAFF)),
    conn=Depends(get_conn),
):
    """Clubs with no club day past 'planned' in the last N weeks (future days count)."""
    frag, val = club_scope(user)
    rows = await conn.fetch(
        f"""SELECT v.club_id, v.club_name, v.members, v.last_club_day
            FROM v_club_stats v JOIN clubs c ON c.id = v.club_id
            WHERE {frag}
              AND NOT EXISTS (SELECT 1 FROM club_days d
                              WHERE d.club_id = c.id AND d.status <> 'planned'
                                AND d.day_date >= current_date - ($2::int * 7))
            ORDER BY v.last_club_day NULLS FIRST, v.club_name""",
        val, weeks,
    )
    return [dict(r) for r in rows]


@router.get("/zero-participation")
async def zero_participation(
    club_id: int | None = None,
    user: dict = Depends(require_role(*STAFF)),
    conn=Depends(get_conn),
):
    """Students with no attendance, no submissions and no event registrations."""
    if club_id is not None:
        await _club_in_scope(conn, user, club_id)
    return await _student_rows(conn, user, only_zero=True, club_id=club_id, limit=None)


@router.get("/students")
async def students(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role(*STAFF)),
    conn=Depends(get_conn),
):
    return await _student_rows(conn, user, limit=limit, offset=offset)


@router.get("/monthly-report")
async def monthly_report(
    month: str = Query(..., pattern=MONTH_RE),
    user: dict = Depends(require_role(*STAFF)),
    conn=Depends(get_conn),
):
    return await _monthly(conn, user, month)


@router.get("/export/{kind}")
async def export(
    kind: str,
    month: str | None = Query(None, pattern=MONTH_RE),
    user: dict = Depends(require_role(*STAFF)),
    conn=Depends(get_conn),
):
    if kind == "clubs":
        rows = await _club_rows(conn, user)
    elif kind == "students":
        rows = await _student_rows(conn, user, limit=MAX_EXPORT, offset=0)
    elif kind == "monthly-report":
        if not month:
            raise HTTPException(422, "month is required (YYYY-MM)")
        rows = (await _monthly(conn, user, month))["clubs"]
    else:
        raise HTTPException(404, "Unknown export")
    return Response(
        to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{kind}.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )
