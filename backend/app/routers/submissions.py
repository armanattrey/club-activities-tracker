"""Submissions (file / photo / link) and rubric evaluation.

Rules (also described in the Stage 3 notes):
- students submit only to club days of clubs where they are APPROVED members
- accepted while the club day is open/submissions; after the deadline they are
  still accepted but flagged is_late (server clock)
- scoring allowed while the day is submissions/evaluating
- students see scores only once the day is 'closed'
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import AnyHttpUrl, BaseModel, Field

from app import db
from app.core import audit
from app.core.permissions import (
    CLUB_MANAGER_ROLES, assert_club_access, current_user, require_role,
)
from app.db import get_conn
from app.services.storage import get_storage

router = APIRouter(tags=["submissions"])

VIEW_ROLES = ("coordinator", "advisor", "campus_admin", "super_admin")
OPEN_FOR_SUBMISSION = ("open", "submissions")
OPEN_FOR_SCORING = ("submissions", "evaluating")
SCORES_VISIBLE_TO_STUDENT = ("closed",)  # change here to publish earlier
HIDDEN_FROM_STUDENTS = ("planned", "approved")


class LinkIn(BaseModel):
    url: AnyHttpUrl  # http/https only: rejects javascript:, file:, etc.


class EvaluationIn(BaseModel):
    scores: dict[str, int]
    comment: str | None = Field(default=None, max_length=2000)


def _public(row) -> dict:
    """What we return about a submission (never the internal storage key)."""
    keys = ("id", "club_day_id", "student_id", "kind", "url", "original_filename",
            "size_bytes", "sha256", "submitted_at", "is_late")
    return {k: row[k] for k in keys}


async def _check_student_can_submit(student: dict, club_day_id: int):
    """Short-lived connection: we do NOT hold a pooled connection while a
    large file streams in (150 students uploading at once would drain the pool)."""
    async with db.pool.acquire() as conn:
        day = await conn.fetchrow("SELECT * FROM club_days WHERE id=$1", club_day_id)
        if day is None or day["status"] in HIDDEN_FROM_STUDENTS:
            raise HTTPException(404, "Club day not found")
        member = await conn.fetchval(
            """SELECT 1 FROM club_memberships
               WHERE student_id=$1 AND club_id=$2 AND status='approved'""",
            student["id"], day["club_id"],
        )
        if not member:
            raise HTTPException(404, "Club day not found")
    if day["status"] not in OPEN_FOR_SUBMISSION:
        raise HTTPException(409, f"Submissions are not open (club day is '{day['status']}')")
    return day


def _is_late(day) -> bool:
    d = day["submission_deadline"]
    return d is not None and datetime.now(timezone.utc) > d


_INSERT = """INSERT INTO submissions
               (club_day_id, student_id, kind, object_key, url,
                original_filename, content_type, size_bytes, sha256, is_late)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *"""


@router.get("/rubric")
async def rubric(user: dict = Depends(current_user), conn=Depends(get_conn)):
    rows = await conn.fetch("SELECT key, max_score FROM rubric_criteria ORDER BY id")
    return [dict(r) for r in rows]


@router.post("/club-days/{club_day_id}/submissions/file", status_code=201)
async def submit_file(
    club_day_id: int,
    kind: Literal["file", "photo"] = Form("file"),
    file: UploadFile = File(...),
    user: dict = Depends(require_role("student")),
):
    day = await _check_student_can_submit(user, club_day_id)
    store = get_storage()
    saved = await store.save_upload(file, kind, f"submissions/{club_day_id}/{user['id']}")
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                _INSERT, club_day_id, user["id"], kind, saved["object_key"], None,
                saved["original_filename"], saved["content_type"],
                saved["size_bytes"], saved["sha256"], _is_late(day),
            )
    except Exception:
        store.delete(saved["object_key"])  # never leave an orphan file behind
        raise
    return _public(row)


@router.post("/club-days/{club_day_id}/submissions/link", status_code=201)
async def submit_link(
    club_day_id: int,
    body: LinkIn,
    user: dict = Depends(require_role("student")),
):
    url = str(body.url)
    if len(url) > 2000:
        raise HTTPException(422, "URL is too long")
    day = await _check_student_can_submit(user, club_day_id)
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT, club_day_id, user["id"], "link", None, url,
            None, None, None, None, _is_late(day),
        )
    return _public(row)


@router.get("/club-days/{club_day_id}/submissions")
async def list_submissions(
    club_day_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)
):
    day = await conn.fetchrow("SELECT * FROM club_days WHERE id=$1", club_day_id)
    if day is None:
        raise HTTPException(404, "Club day not found")

    if user["role"] == "student":
        rows = await conn.fetch(
            """SELECT s.*, u.name AS student_name, u.student_code
               FROM submissions s JOIN users u ON u.id = s.student_id
               WHERE s.club_day_id=$1 AND s.student_id=$2 ORDER BY s.submitted_at""",
            club_day_id, user["id"],
        )
        show_scores = day["status"] in SCORES_VISIBLE_TO_STUDENT
    else:
        await assert_club_access(conn, user, day["club_id"], allow=VIEW_ROLES)
        rows = await conn.fetch(
            """SELECT s.*, u.name AS student_name, u.student_code
               FROM submissions s JOIN users u ON u.id = s.student_id
               WHERE s.club_day_id=$1 ORDER BY s.submitted_at""",
            club_day_id,
        )
        show_scores = True

    evals: dict[int, dict] = {}
    if show_scores and rows:
        rubric_rows = await conn.fetch("SELECT max_score FROM rubric_criteria")
        max_total = sum(r["max_score"] for r in rubric_rows)
        erows = await conn.fetch(
            """SELECT e.*, u.name AS evaluator_name FROM evaluations e
               JOIN users u ON u.id = e.evaluator_id
               WHERE e.submission_id = ANY($1::bigint[]) ORDER BY e.created_at""",
            [r["id"] for r in rows],
        )
        for e in erows:  # one evaluator per club day in practice; latest wins
            evals[e["submission_id"]] = {
                "scores": e["scores"], "total": sum(e["scores"].values()),
                "max_total": max_total, "comment": e["comment"],
                "evaluator": e["evaluator_name"],
            }

    out = []
    for r in rows:
        item = _public(r)
        item["student_name"], item["student_code"] = r["student_name"], r["student_code"]
        item["evaluation"] = evals.get(r["id"])
        out.append(item)
    return out


@router.get("/submissions/{submission_id}/download")
async def download(
    submission_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)
):
    sub = await conn.fetchrow(
        """SELECT s.*, d.club_id FROM submissions s
           JOIN club_days d ON d.id = s.club_day_id WHERE s.id=$1""",
        submission_id,
    )
    if sub is None:
        raise HTTPException(404, "Submission not found")
    if user["role"] == "student":
        if sub["student_id"] != user["id"]:
            raise HTTPException(404, "Submission not found")  # don't reveal it exists
    else:
        await assert_club_access(conn, user, sub["club_id"], allow=VIEW_ROLES)
    if sub["kind"] == "link":
        raise HTTPException(422, "This submission is a link, not a file")

    path = get_storage().path_for(sub["object_key"])
    if not path.exists():
        raise HTTPException(404, "File is missing from storage")
    return FileResponse(
        path,
        filename=sub["original_filename"] or path.name,  # forces download, not inline
        media_type=sub["content_type"] or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.put("/submissions/{submission_id}/evaluation")
async def evaluate(
    submission_id: int,
    body: EvaluationIn,
    user: dict = Depends(require_role(*CLUB_MANAGER_ROLES)),
    conn=Depends(get_conn),
):
    async with conn.transaction():
        sub = await conn.fetchrow(
            """SELECT s.id, d.club_id, d.status AS day_status
               FROM submissions s JOIN club_days d ON d.id = s.club_day_id
               WHERE s.id=$1 FOR UPDATE OF s""",
            submission_id,
        )
        if sub is None:
            raise HTTPException(404, "Submission not found")
        await assert_club_access(conn, user, sub["club_id"])
        if sub["day_status"] not in OPEN_FOR_SCORING:
            raise HTTPException(
                409, f"Scoring is only allowed while the club day is "
                     f"{' or '.join(OPEN_FOR_SCORING)} (it is '{sub['day_status']}')")

        rubric = {r["key"]: r["max_score"]
                  for r in await conn.fetch("SELECT key, max_score FROM rubric_criteria")}
        if set(body.scores) != set(rubric):
            raise HTTPException(422, f"Give a score for exactly these criteria: {sorted(rubric)}")
        for key, val in body.scores.items():
            if not 0 <= val <= rubric[key]:
                raise HTTPException(422, f"'{key}' must be between 0 and {rubric[key]}")

        existing = await conn.fetchrow(
            "SELECT * FROM evaluations WHERE submission_id=$1 AND evaluator_id=$2 FOR UPDATE",
            submission_id, user["id"],
        )
        row = await conn.fetchrow(
            """INSERT INTO evaluations (submission_id, evaluator_id, scores, comment)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (submission_id, evaluator_id)
               DO UPDATE SET scores = EXCLUDED.scores, comment = EXCLUDED.comment
               RETURNING *""",
            submission_id, user["id"], body.scores, body.comment,
        )
        await audit.log(
            conn, actor_id=user["id"], action="evaluation.upsert",
            entity="evaluations", entity_id=row["id"],
            before=({"scores": existing["scores"], "comment": existing["comment"]}
                    if existing else None),
            after={"scores": row["scores"], "comment": row["comment"]},
        )
    return {
        "submission_id": submission_id, "scores": row["scores"],
        "total": sum(row["scores"].values()), "max_total": sum(rubric.values()),
        "comment": row["comment"],
    }