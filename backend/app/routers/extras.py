"""Problem 5 extension APIs: budgets, duty leave, transcripts and event galleries."""
import csv
import io
import json
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core import audit
from app.core.permissions import ADMIN_ROLES, CLUB_MANAGER_ROLES, current_user, require_role
from app.db import get_conn
from app.services.storage import get_storage
from app.services import certificates as certs

router = APIRouter(tags=["extended modules"])
BUDGET_ROLES = (*CLUB_MANAGER_ROLES, "advisor")


class BudgetIn(BaseModel):
    club_id: int
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=4000)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3)


class DecisionIn(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=500)


class MediaConsentIn(BaseModel):
    consent_to_publish: bool


class TranscriptVerificationIn(BaseModel):
    snapshot: dict
    signature: str = Field(min_length=64, max_length=64)


async def _event_access(conn, user, event_id):
    event = await conn.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    if user["role"] == "super_admin":
        return event
    if event["campus_id"] == user["campus_id"]:
        return event
    if user["role"] == "student" and event["is_inter_college"] and event["approved_at"]:
        return event
    raise HTTPException(404, "Event not found")


@router.post("/budgets", status_code=201)
async def create_budget_request(body: BudgetIn, user: dict = Depends(require_role(*BUDGET_ROLES)), conn=Depends(get_conn)):
    club = await conn.fetchrow("SELECT * FROM clubs WHERE id=$1", body.club_id)
    if club is None:
        raise HTTPException(404, "Club not found")
    if user["role"] in ("coordinator", "advisor"):
        if user["role"] == "coordinator":
            allowed = await conn.fetchval("SELECT 1 FROM club_coordinators WHERE club_id=$1 AND user_id=$2", body.club_id, user["id"])
        else:
            allowed = await conn.fetchval("SELECT 1 FROM clubs WHERE id=$1 AND advisor_id=$2", body.club_id, user["id"])
    else:
        allowed = user["role"] == "super_admin" or club["campus_id"] == user["campus_id"]
    if not allowed:
        raise HTTPException(403, "You do not have access to this club")
    row = await conn.fetchrow(
        """INSERT INTO club_budget_requests (club_id, requester_id, title, description, amount, currency)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        body.club_id, user["id"], body.title.strip(), body.description.strip(), body.amount, body.currency.upper(),
    )
    await audit.log(conn, actor_id=user["id"], action="budget.create", entity="club_budget_requests",
                    entity_id=row["id"], after={"club_id": body.club_id, "amount": str(body.amount)})
    return dict(row)


@router.get("/budgets")
async def list_budget_requests(user: dict = Depends(require_role(*BUDGET_ROLES)), conn=Depends(get_conn)):
    if user["role"] == "super_admin":
        rows = await conn.fetch("""SELECT b.*, c.name AS club_name, ca.name AS campus_name
          FROM club_budget_requests b JOIN clubs c ON c.id=b.club_id JOIN campus ca ON ca.id=c.campus_id
          ORDER BY b.created_at DESC""")
    elif user["role"] == "campus_admin":
        rows = await conn.fetch("""SELECT b.*, c.name AS club_name, ca.name AS campus_name FROM club_budget_requests b
          JOIN clubs c ON c.id=b.club_id JOIN campus ca ON ca.id=c.campus_id
          WHERE c.campus_id=$1 ORDER BY b.created_at DESC""", user["campus_id"])
    elif user["role"] == "coordinator":
        rows = await conn.fetch("""SELECT b.*, c.name AS club_name, ca.name AS campus_name FROM club_budget_requests b
          JOIN clubs c ON c.id=b.club_id JOIN campus ca ON ca.id=c.campus_id
          WHERE b.club_id IN (SELECT club_id FROM club_coordinators WHERE user_id=$1)
          ORDER BY b.created_at DESC""", user["id"])
    else:
        rows = await conn.fetch("""SELECT b.*, c.name AS club_name, ca.name AS campus_name FROM club_budget_requests b
          JOIN clubs c ON c.id=b.club_id JOIN campus ca ON ca.id=c.campus_id
          WHERE c.advisor_id=$1 ORDER BY b.created_at DESC""", user["id"])
    return [dict(r) for r in rows]


@router.post("/budgets/{request_id}/decision")
async def decide_budget(request_id: int, body: DecisionIn,
                        user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    async with conn.transaction():
        row = await conn.fetchrow("""SELECT b.*, c.campus_id FROM club_budget_requests b
          JOIN clubs c ON c.id=b.club_id WHERE b.id=$1 FOR UPDATE OF b""", request_id)
        if row is None or (user["role"] != "super_admin" and row["campus_id"] != user["campus_id"]):
            raise HTTPException(404, "Budget request not found")
        if row["status"] != "pending":
            raise HTTPException(409, "Budget request has already been decided")
        status = "approved" if body.decision == "approve" else "rejected"
        updated = await conn.fetchrow("""UPDATE club_budget_requests SET status=$2, decided_by=$3,
           decided_at=now(), decision_reason=$4 WHERE id=$1 RETURNING *""",
           request_id, status, user["id"], body.reason)
        await audit.log(conn, actor_id=user["id"], action=f"budget.{body.decision}",
                        entity="club_budget_requests", entity_id=request_id,
                        before={"status": "pending"}, after={"status": status}, reason=body.reason)
    return dict(updated)


@router.get("/duty-leave/me")
async def my_duty_leave(user: dict = Depends(require_role("student")), conn=Depends(get_conn)):
    rows = await conn.fetch("""SELECT d.*, e.name AS event_name, e.starts_at, e.campus_id AS host_campus_id,
       ca.name AS host_campus FROM duty_leave_requests d JOIN events e ON e.id=d.event_id
       JOIN campus ca ON ca.id=e.campus_id WHERE d.student_id=$1 ORDER BY d.created_at DESC""", user["id"])
    return [dict(r) for r in rows]


@router.get("/duty-leave")
async def duty_leave_queue(user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    where = "" if user["role"] == "super_admin" else "WHERE u.campus_id=$1"
    rows = await conn.fetch(f"""SELECT d.*, e.name AS event_name, e.starts_at, u.name AS student_name,
       u.student_code, ca.name AS student_campus FROM duty_leave_requests d
       JOIN events e ON e.id=d.event_id JOIN users u ON u.id=d.student_id JOIN campus ca ON ca.id=u.campus_id
       {where} ORDER BY d.created_at DESC""", *([] if user["role"] == "super_admin" else [user["campus_id"]]))
    return [dict(r) for r in rows]


@router.post("/duty-leave/{request_id}/decision")
async def decide_duty_leave(request_id: int, body: DecisionIn,
                            user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    async with conn.transaction():
        row = await conn.fetchrow("""SELECT d.*, u.campus_id AS student_campus FROM duty_leave_requests d
          JOIN users u ON u.id=d.student_id WHERE d.id=$1 FOR UPDATE OF d""", request_id)
        if row is None or (user["role"] != "super_admin" and row["student_campus"] != user["campus_id"]):
            raise HTTPException(404, "Duty leave request not found")
        if row["status"] != "pending":
            raise HTTPException(409, "Duty leave request has already been decided")
        status = "approved" if body.decision == "approve" else "rejected"
        updated = await conn.fetchrow("""UPDATE duty_leave_requests SET status=$2, decided_by=$3,
          decided_at=now(), decision_reason=$4 WHERE id=$1 RETURNING *""", request_id, status, user["id"], body.reason)
        await audit.log(conn, actor_id=user["id"], action=f"duty_leave.{body.decision}",
          entity="duty_leave_requests", entity_id=request_id,
          before={"status": "pending"}, after={"status": status}, reason=body.reason)
    return dict(updated)


@router.get("/transcripts/me")
async def my_transcript(user: dict = Depends(require_role("student")), conn=Depends(get_conn)):
    return await _transcript(conn, user["id"])


async def _transcript(conn, student_id):
    student = await conn.fetchrow("SELECT id,name,student_code,campus_id FROM users WHERE id=$1", student_id)
    if student is None:
        raise HTTPException(404, "Student not found")
    campus = await conn.fetchval("SELECT name FROM campus WHERE id=$1", student["campus_id"])
    club_rows = await conn.fetch("""SELECT d.id, d.title, d.day_date, c.name AS club_name,
      count(DISTINCT a.id)::int AS attendance_count FROM attendance a
      JOIN checkin_sessions s ON s.id=a.session_id JOIN club_days d ON d.id=s.club_day_id
      JOIN clubs c ON c.id=d.club_id WHERE a.student_id=$1
      GROUP BY d.id,c.name ORDER BY d.day_date""", student_id)
    event_rows = await conn.fetch("""SELECT e.id,e.name,e.starts_at,r.checked_in_at,
      er.rank, er.notes FROM registrations r JOIN events e ON e.id=r.event_id
      LEFT JOIN event_results er ON er.event_id=e.id AND er.student_id=r.student_id
      WHERE r.student_id=$1 ORDER BY e.starts_at NULLS LAST,e.id""", student_id)
    return {"student": {"id": student["id"], "name": student["name"], "student_code": student["student_code"], "campus": campus},
      "club_days": [dict(r) for r in club_rows], "events": [dict(r) for r in event_rows]}


@router.get("/transcripts/me.csv")
async def transcript_csv(user: dict = Depends(require_role("student")), conn=Depends(get_conn)):
    data = await _transcript(conn, user["id"])
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["type", "title", "organization", "date", "attendance", "rank"])
    for row in data["club_days"]:
        writer.writerow(["Club day", row["title"] or "Club Day", row["club_name"], row["day_date"], "attended", ""])
    for row in data["events"]:
        writer.writerow(["Event", row["name"], "", row["starts_at"], "checked in" if row["checked_in_at"] else "registered", row["rank"] or ""])
    return StreamingResponse(iter([stream.getvalue()]), media_type="text/csv",
      headers={"Content-Disposition": "attachment; filename=student-transcript.csv"})


@router.get("/transcripts/me/download")
async def download_signed_transcript(user: dict = Depends(require_role("student")), conn=Depends(get_conn)):
    snapshot = jsonable_encoder({"schema": "club-tracker-transcript-v1", "transcript": await _transcript(conn, user["id"])})
    document = {**snapshot, "signature": certs.sign_transcript(snapshot)}
    return StreamingResponse(iter([json.dumps(document, ensure_ascii=False, separators=(",", ":"))]),
      media_type="application/json", headers={"Content-Disposition": "attachment; filename=verified-transcript.json"})


@router.post("/api/transcripts/verify")
async def verify_signed_transcript(body: TranscriptVerificationIn):
    valid = certs.verify_transcript(body.snapshot, body.signature)
    return {"valid": valid, "schema": body.snapshot.get("schema") if valid else None}


@router.get("/transcripts/{student_id}")
async def student_transcript(student_id: int, user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    student = await conn.fetchrow("SELECT id,campus_id,role FROM users WHERE id=$1", student_id)
    if student is None or student["role"] != "student" or (user["role"] != "super_admin" and student["campus_id"] != user["campus_id"]):
        raise HTTPException(404, "Student not found")
    return await _transcript(conn, student_id)


@router.post("/events/{event_id}/media", status_code=201)
async def upload_event_media(event_id: int, consent_to_publish: bool = Form(...), file: UploadFile = File(...),
                             user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_access(conn, user, event_id)
    if user["role"] == "student" and not await conn.fetchval(
        "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, user["id"]):
        raise HTTPException(403, "Register for this event before uploading media")
    saved = await get_storage().save_upload(file, "photo", f"events/{event_id}/gallery")
    try:
        row = await conn.fetchrow("""INSERT INTO event_media
          (event_id, uploaded_by, object_key, original_filename, content_type, size_bytes, sha256, consent_to_publish)
          VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id, moderation_status, consent_to_publish, created_at""",
          event_id, user["id"], saved["object_key"], saved["original_filename"], saved["content_type"],
          saved["size_bytes"], saved["sha256"], consent_to_publish)
    except BaseException:
        get_storage().delete(saved["object_key"])
        raise
    return {"id": row["id"], "event_id": event_id, "moderation_status": row["moderation_status"],
      "consent_to_publish": row["consent_to_publish"], "created_at": row["created_at"]}


@router.get("/events/{event_id}/media")
async def list_event_media(event_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_access(conn, user, event_id)
    rows = await conn.fetch("""SELECT m.id,m.event_id,m.uploaded_by,m.original_filename,m.content_type,
      m.size_bytes,m.created_at FROM event_media m WHERE m.event_id=$1 AND m.consent_to_publish
      AND m.moderation_status='approved' ORDER BY m.created_at DESC""", event_id)
    if user["role"] in ADMIN_ROLES:
        rows += await conn.fetch("""SELECT m.id,m.event_id,m.uploaded_by,m.original_filename,m.content_type,
          m.size_bytes,m.created_at FROM event_media m WHERE m.event_id=$1 AND m.moderation_status='pending'
          AND m.uploaded_by<>$2 ORDER BY m.created_at DESC""", event_id, user["id"])
    own = await conn.fetch("""SELECT m.id,m.event_id,m.uploaded_by,m.original_filename,m.content_type,
      m.size_bytes,m.created_at FROM event_media m WHERE m.event_id=$1 AND m.uploaded_by=$2
      AND NOT (m.consent_to_publish AND m.moderation_status='approved') ORDER BY m.created_at DESC""", event_id, user["id"])
    rows += own
    return [dict(r) for r in rows]


@router.get("/events/{event_id}/media/{media_id}")
async def download_event_media(event_id: int, media_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_access(conn, user, event_id)
    row = await conn.fetchrow("SELECT * FROM event_media WHERE id=$1 AND event_id=$2", media_id, event_id)
    if row is None:
        raise HTTPException(404, "Media not found")
    allowed = row["consent_to_publish"] and row["moderation_status"] == "approved"
    allowed = allowed or row["uploaded_by"] == user["id"] or user["role"] in ADMIN_ROLES
    if not allowed:
        raise HTTPException(404, "Media not found")
    path = get_storage().path_for(row["object_key"])
    if not path.exists():
        raise HTTPException(404, "Media file missing")
    return FileResponse(path, media_type=row["content_type"] or "application/octet-stream",
      filename=row["original_filename"], headers={"X-Content-Type-Options": "nosniff"})


@router.post("/events/{event_id}/media/{media_id}/moderate")
async def moderate_event_media(event_id: int, media_id: int, body: DecisionIn,
                               user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)):
    event = await _event_access(conn, user, event_id)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM event_media WHERE id=$1 AND event_id=$2 FOR UPDATE", media_id, event_id)
        if row is None:
            raise HTTPException(404, "Media not found")
        if row["moderation_status"] != "pending":
            raise HTTPException(409, "Media has already been moderated")
        status = "approved" if body.decision == "approve" else "rejected"
        await conn.execute("UPDATE event_media SET moderation_status=$2, moderation_reason=$3 WHERE id=$1", media_id, status, body.reason)
        await audit.log(conn, actor_id=user["id"], action=f"event_media.{body.decision}", entity="event_media",
          entity_id=media_id, before={"moderation_status": "pending"}, after={"moderation_status": status}, reason=body.reason)
    return {"id": media_id, "event_id": event["id"], "moderation_status": status}


@router.put("/events/{event_id}/media/{media_id}/consent")
async def update_event_media_consent(event_id: int, media_id: int, body: MediaConsentIn,
                                     user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_access(conn, user, event_id)
    row = await conn.fetchrow("SELECT * FROM event_media WHERE id=$1 AND event_id=$2", media_id, event_id)
    if row is None or row["uploaded_by"] != user["id"]:
        raise HTTPException(404, "Media not found")
    updated = await conn.fetchrow("""UPDATE event_media SET consent_to_publish=$2,
      moderation_status=CASE WHEN $2 THEN moderation_status ELSE 'pending' END
      WHERE id=$1 RETURNING id, consent_to_publish, moderation_status""", media_id, body.consent_to_publish)
    await audit.log(conn, actor_id=user["id"], action="event_media.consent", entity="event_media",
      entity_id=media_id, before={"consent_to_publish": row["consent_to_publish"]},
      after={"consent_to_publish": updated["consent_to_publish"]})
    return dict(updated)


@router.delete("/events/{event_id}/media/{media_id}")
async def delete_event_media(event_id: int, media_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_access(conn, user, event_id)
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM event_media WHERE id=$1 AND event_id=$2 FOR UPDATE", media_id, event_id)
        if row is None or (row["uploaded_by"] != user["id"] and user["role"] not in ADMIN_ROLES):
            raise HTTPException(404, "Media not found")
        await conn.execute("DELETE FROM event_media WHERE id=$1", media_id)
    get_storage().delete(row["object_key"])
    return {"deleted": True}
