"""Certificate integrity + PDF rendering.

Pure functions only (no DB pool, no FastAPI objects) so the API and the Celery
worker can both import this file.

Integrity model
- The HMAC covers the id, kind, student, ref, issuer, issue time AND a frozen
  `snapshot` (names, dates). Without the server secret nobody can edit the DB
  row and recompute a valid HMAC.
- The SHA-256 of the PDF bytes is stored separately, so an edited PDF fails
  even if the database is untouched.
"""
import hashlib
import hmac
import html
import json
from datetime import datetime, timezone
from string import Template

from app.config import settings


def _key() -> bytes:
    # Domain-separated so the JWT secret is never used directly as the HMAC key.
    return (settings.cert_secret or f"cert|{settings.jwt_secret}").encode()


def as_dict(value) -> dict:
    """asyncpg returns JSONB as a dict when the pool codec is set (the API) and
    as a str on a plain connection (the worker). Accept both."""
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical(*, cert_id, kind, ref_type, ref_id, student_id, issued_by,
              issued_at, snapshot) -> bytes:
    payload = {
        "id": str(cert_id), "kind": kind, "ref_type": ref_type, "ref_id": ref_id,
        "student_id": student_id, "issued_by": issued_by,
        "issued_at": _iso(issued_at), "snapshot": snapshot,
    }
    # sort_keys + fixed separators + ASCII escapes = identical bytes every time
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


def compute_hmac(**fields) -> str:
    return hmac.new(_key(), canonical(**fields), hashlib.sha256).hexdigest()


def sign_transcript(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(_key(), b"transcript|" + payload, hashlib.sha256).hexdigest()


def verify_transcript(snapshot: dict, signature: str) -> bool:
    return hmac.compare_digest(sign_transcript(snapshot), signature or "")


def fields_from_row(row) -> dict:
    return dict(
        cert_id=str(row["id"]), kind=row["kind"], ref_type=row["ref_type"],
        ref_id=row["ref_id"], student_id=row["student_id"],
        issued_by=row["issued_by"], issued_at=row["issued_at"],
        snapshot=as_dict(row["snapshot"]),
    )


def record_intact(row) -> bool:
    """Recompute the HMAC from what is in the DB right now and compare."""
    expected = compute_hmac(**fields_from_row(row))
    return hmac.compare_digest(expected, row["content_hmac"] or "")


def verify_url(cert_id) -> str:
    return f"{settings.public_base_url.rstrip('/')}/verify/{cert_id}"


# --------------------------------------------------------------------- PDF
_TEMPLATE = Template("""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page { size: A4 landscape; margin: 0; }
  body { margin: 0; font-family: 'DejaVu Sans', sans-serif; color: #1f2937; }
  .frame { margin: 12mm; height: 186mm; box-sizing: border-box; overflow: hidden;
           border: 3mm double #1e3a8a; padding: 12mm 18mm; text-align: center;
           position: relative; page-break-inside: avoid; }
  .org { font-size: 11pt; letter-spacing: 3px; text-transform: uppercase; color: #6b7280; }
  h1 { font-size: 30pt; margin: 6mm 0 2mm; color: #1e3a8a; }
  .lead { font-size: 13pt; margin: 4mm 0 2mm; }
  .name { font-size: 32pt; font-weight: bold; margin: 2mm 0 4mm;
          border-bottom: 0.5mm solid #9ca3af; display: inline-block; padding: 0 10mm 1mm; }
  .body { font-size: 13pt; line-height: 1.6; margin: 3mm 20mm; }
  .highlight { font-size: 17pt; font-weight: bold; color: #1e3a8a; }
  .footer { position: absolute; left: 18mm; right: 18mm; bottom: 10mm; }
  .left { position: absolute; left: 0; bottom: 0; text-align: left; font-size: 9pt; color: #4b5563; }
  .right { position: absolute; right: 0; bottom: 0; text-align: center; font-size: 8pt; color: #4b5563; }
  .right img { width: 28mm; height: 28mm; display: block; margin: 0 auto 1mm; }
  .mono { font-family: 'DejaVu Sans Mono', monospace; font-size: 8pt; }
</style></head><body>
<div class="frame">
  <div class="org">$campus</div>
  <h1>$heading</h1>
  <div class="lead">This is to certify that</div>
  <div class="name">$student</div>
  <div class="body">$body</div>
  <div class="footer">
    <div class="left">
      Issued on $issued<br>
      Certificate ID<br><span class="mono">$cert_id</span>
    </div>
    <div class="right">
      <img src="$qr" alt="QR code">
      Scan to verify
    </div>
  </div>
</div>
</body></html>""")


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def render_pdf(*, cert_id, kind, snapshot: dict, issued_at: datetime) -> bytes:
    # Imported here so merely importing this module never needs the PDF libraries.
    import segno
    from weasyprint import HTML

    student = _e(snapshot.get("student_name", ""))
    club = _e(snapshot.get("club_name", ""))
    event = _e(snapshot.get("event_name", ""))
    title = _e(snapshot.get("title", "Club Day"))
    day = _e(snapshot.get("day_date", ""))

    if event:
        if kind == "achievement":
            heading = "Certificate of Achievement"
            body = (f'has been recognised for<br><span class="highlight">{_e(snapshot.get("achievement", ""))}</span>'
                    f'<br>at {event} &middot; {day}')
        else:
            heading = "Certificate of Participation"
            body = f'has successfully participated in<br><span class="highlight">{event}</span><br>{day}'
    elif kind == "achievement":
        heading = "Certificate of Achievement"
        body = (f'has been recognised for<br>'
                f'<span class="highlight">{_e(snapshot.get("achievement", ""))}</span><br>'
                f'at {title}, {club} &middot; {day}')
    else:
        heading = "Certificate of Participation"
        body = (f'has successfully participated in<br>'
                f'<span class="highlight">{title}</span><br>'
                f'organised by {club} &middot; {day}')

    # micro=False: always a normal, widely scannable QR code
    qr = segno.make(verify_url(cert_id), error="m", micro=False)
    qr_uri = qr.svg_data_uri(scale=4, border=1)

    # Every dynamic value above is HTML-escaped, so user-supplied text can never
    # inject markup (and therefore can never make WeasyPrint fetch a URL).
    doc = _TEMPLATE.substitute(
        campus=_e(snapshot.get("campus_name", "")), heading=heading,
        student=student, body=body,
        issued=issued_at.astimezone(timezone.utc).strftime("%d %b %Y"),
        cert_id=_e(cert_id), qr=html.escape(qr_uri, quote=True),
    )
    return HTML(string=doc).write_pdf()
