"""PUBLIC verification: no login required (that is the point of a QR code).

What a stranger can see: name, club, date, kind, status.
What they cannot see: student code, email, revoke reason.
If the record fails its integrity check we show NO details at all.
"""
import hashlib
import hmac
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.services import certificates as certs

router = APIRouter(tags=["verify (public)"])

MAX_CHECK_BYTES = 5 * 1024 * 1024  # certificates are small; cap the public upload


async def _load(conn, certificate_id: str):
    try:
        cid = uuid.UUID(certificate_id)
    except ValueError:
        raise HTTPException(404, "Certificate not found")
    row = await conn.fetchrow("SELECT * FROM certificates WHERE id=$1", cid)
    if row is None:
        raise HTTPException(404, "Certificate not found")
    return row


def _summary(row) -> dict:
    intact = certs.record_intact(row)
    revoked = row["revoked_at"] is not None
    if not intact:
        overall = "TAMPERED_RECORD"
    elif revoked:
        overall = "REVOKED"
    elif row["status"] != "ready":
        overall = "PENDING"
    else:
        overall = "VALID"

    snap = certs.as_dict(row["snapshot"])
    return {
        "overall": overall,
        "record_intact": intact,
        "revoked": revoked,
        "certificate": None if not intact else {
            "id": str(row["id"]), "kind": row["kind"],
            "student_name": snap.get("student_name"),
            "club_name": snap.get("club_name"),
            "campus_name": snap.get("campus_name"),
            "title": snap.get("title"), "day_date": snap.get("day_date"),
            "achievement": snap.get("achievement"),
            "issued_at": row["issued_at"].isoformat(),
        },
    }


@router.get("/api/verify/{certificate_id}")
async def verify_json(certificate_id: str, conn=Depends(get_conn)):
    return _summary(await _load(conn, certificate_id))


@router.post("/api/verify/{certificate_id}/file")
async def verify_file(
    certificate_id: str, file: UploadFile = File(...), conn=Depends(get_conn)
):
    """Compare an uploaded PDF with the original the server issued (SHA-256).
    Note: a re-saved or 'printed to PDF' copy has different bytes, so it will
    not match. That is intentional: only the original file is provable."""
    row = await _load(conn, certificate_id)
    digest = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_CHECK_BYTES:
            raise HTTPException(413, "File is too large to be a certificate")
        digest.update(chunk)
    uploaded = digest.hexdigest()
    stored = row["pdf_sha256"]
    matches = bool(stored) and hmac.compare_digest(uploaded, stored)
    out = _summary(row)
    out["file_matches"] = matches
    out["uploaded_sha256"] = uploaded
    return out


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verify certificate</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:620px;margin:2rem auto;padding:0 1rem;color:#1f2937;background:#f9fafb}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:1.25rem;margin-bottom:1rem}
 .banner{padding:1rem;border-radius:12px;font-weight:700;font-size:1.1rem;margin-bottom:1rem}
 .VALID{background:#dcfce7;color:#166534}
 .REVOKED,.TAMPERED_RECORD{background:#fee2e2;color:#991b1b}
 .PENDING,.NOTFOUND{background:#fef9c3;color:#854d0e}
 .row{display:flex;justify-content:space-between;gap:1rem;padding:.35rem 0;border-bottom:1px solid #f3f4f6}
 .row span{color:#6b7280}
 .result{margin-top:.75rem;font-weight:600}
 .ok{color:#166534}.bad{color:#991b1b}
 small{color:#6b7280}
</style></head><body>
<h1>Certificate verification</h1>
<div id="banner" class="banner PENDING">Checking...</div>
<div class="card" id="details" hidden></div>
<div class="card">
 <strong>Check a PDF file</strong><br>
 <small>Upload the certificate PDF you received. It is compared with the original the server issued.</small><br><br>
 <input type="file" id="pdf" accept="application/pdf,.pdf">
 <div id="fileResult" class="result"></div>
</div>
<script>
const id = "__CERT_ID__";
const MESSAGES = {
  VALID: "Valid certificate",
  REVOKED: "This certificate has been revoked",
  TAMPERED_RECORD: "The stored record failed its integrity check",
  PENDING: "This certificate is still being generated"
};
function addRow(parent, label, value) {
  if (value === null || value === undefined || value === "") return;
  const d = document.createElement("div"); d.className = "row";
  const a = document.createElement("span"); a.textContent = label;
  const b = document.createElement("strong"); b.textContent = value;
  d.append(a, b); parent.append(d);
}
async function load() {
  const banner = document.getElementById("banner");
  const r = await fetch("/api/verify/" + id);
  if (!r.ok) { banner.className = "banner NOTFOUND"; banner.textContent = "Certificate not found"; return; }
  const d = await r.json();
  banner.className = "banner " + d.overall;
  banner.textContent = MESSAGES[d.overall] || d.overall;
  if (d.certificate) {
    const c = d.certificate, box = document.getElementById("details");
    box.hidden = false;
    addRow(box, "Awarded to", c.student_name);
    addRow(box, "Type", c.kind === "achievement" ? "Achievement" : "Participation");
    addRow(box, "Achievement", c.achievement);
    addRow(box, "Activity", c.title);
    addRow(box, "Club", c.club_name);
    addRow(box, "Campus", c.campus_name);
    addRow(box, "Activity date", c.day_date);
    addRow(box, "Issued", new Date(c.issued_at).toLocaleString());
    addRow(box, "Certificate ID", c.id);
  }
}
document.getElementById("pdf").addEventListener("change", async (e) => {
  const out = document.getElementById("fileResult");
  if (!e.target.files.length) return;
  out.className = "result"; out.textContent = "Checking...";
  const fd = new FormData(); fd.append("file", e.target.files[0]);
  const r = await fetch("/api/verify/" + id + "/file", { method: "POST", body: fd });
  if (!r.ok) { out.className = "result bad"; out.textContent = "Could not check this file."; return; }
  const d = await r.json();
  out.className = "result " + (d.file_matches ? "ok" : "bad");
  out.textContent = d.file_matches
    ? "This file is identical to the original certificate."
    : "This file does NOT match the original. It may have been edited.";
});
load();
</script></body></html>"""


@router.get("/verify/{certificate_id}", response_class=HTMLResponse)
async def verify_page(certificate_id: str):
    """The page a QR code opens. The id is validated as a UUID first, and the page
    fetches everything else via the JSON API and writes it with textContent,
    so nothing from the database is ever pasted into HTML."""
    try:
        cid = uuid.UUID(certificate_id)
    except ValueError:
        return HTMLResponse("<h1>Certificate not found</h1>", status_code=404)
    return HTMLResponse(
        _PAGE.replace("__CERT_ID__", str(cid)),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )