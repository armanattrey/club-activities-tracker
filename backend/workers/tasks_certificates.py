"""PDF generation runs in the background, never inside a web request.

Safety properties
- claiming is atomic (UPDATE ... WHERE status='pending'), so two workers can
  never render the same certificate
- the record's HMAC is re-checked before rendering; a tampered row is never turned
  into a genuine-looking PDF
- the PDF is written to a .part file and renamed, so a crash never leaves a
  half-written file that looks real
- a sweeper retries anything stuck (queue lost, worker crashed mid-render)
"""
import asyncio
import hashlib
import logging
import os

import asyncpg

from workers.task_runtime import celery

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


async def _generate_one(conn, cert_id: str) -> str:
    # Imported here so lightweight processes only load PDF support when used.
    from app.services import certificates as certs
    from app.services.storage import get_storage

    row = await conn.fetchrow(
        """UPDATE certificates
           SET status='generating', status_changed_at=now(), attempts=attempts+1
           WHERE id=$1::uuid AND status='pending' RETURNING *""",
        cert_id,
    )
    if row is None:
        return "skipped"  # someone else has it, or it is already done

    if not certs.record_intact(row):
        log.error("certificate %s failed its integrity check; not rendering", cert_id)
        await conn.execute(
            "UPDATE certificates SET status='failed', status_changed_at=now() WHERE id=$1::uuid",
            cert_id,
        )
        return "failed"

    try:
        pdf = await asyncio.to_thread(
            certs.render_pdf,
            cert_id=str(row["id"]), kind=row["kind"],
            snapshot=certs.as_dict(row["snapshot"]), issued_at=row["issued_at"],
        )
        key = f"certificates/{row['id']}.pdf"
        dest = get_storage().path_for(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(pdf)
        tmp.rename(dest)
        await conn.execute(
            """UPDATE certificates
               SET status='ready', pdf_sha256=$2, object_key=$3, status_changed_at=now()
               WHERE id=$1::uuid""",
            cert_id, hashlib.sha256(pdf).hexdigest(), key,
        )
        return "ready"
    except Exception:
        log.exception("certificate %s: rendering failed", cert_id)
        nxt = "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
        await conn.execute(
            "UPDATE certificates SET status=$2, status_changed_at=now() WHERE id=$1::uuid",
            cert_id, nxt,
        )
        return nxt


async def _with_conn(fn, *args):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return await fn(conn, *args)
    finally:
        await conn.close()


@celery.task(name="workers.tasks_certificates.generate_certificate")
def generate_certificate(cert_id: str):
    result = asyncio.run(_with_conn(_generate_one, cert_id))
    log.info("certificate %s: %s", cert_id, result)
    return result


async def _sweep(conn) -> int:
    # rows stuck in 'generating' (worker died mid-render) go back to the queue
    await conn.execute(
        """UPDATE certificates SET status='pending', status_changed_at=now()
           WHERE status='generating' AND status_changed_at < now() - interval '5 minutes'"""
    )
    ids = await conn.fetch(
        "SELECT id FROM certificates WHERE status='pending' ORDER BY issued_at LIMIT 50"
    )
    done = 0
    for r in ids:
        if await _generate_one(conn, str(r["id"])) == "ready":
            done += 1
    return done


@celery.task(name="workers.tasks_certificates.sweep_pending_certificates")
def sweep_pending_certificates():
    n = asyncio.run(_with_conn(_sweep))
    if n:
        log.info("sweeper generated %s certificate(s)", n)
    return n
