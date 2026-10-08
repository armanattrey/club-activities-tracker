"""Optional WhatsApp Cloud API delivery for opted-in users.

Messages use a pre-approved template configured in Meta. This worker never
logs access tokens, phone numbers, message bodies, or provider response bodies.
"""
import asyncio
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

SUPPORTED_KINDS = {
    "club_day.announced", "club_day.plan_due", "club_day.upcoming",
    "submission.deadline_soon", "campus.monthly_report",
}
MAX_ATTEMPTS = 5
BATCH_SIZE = 50
SEND_CONCURRENCY = 5


def whatsapp_config():
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "").strip()
    template = os.getenv("WHATSAPP_TEMPLATE_NAME", "").strip()
    language = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en").strip()
    if not all((token, phone_id, version, template)):
        return None
    if not re.fullmatch(r"v[0-9]+(?:\.[0-9]+)?", version):
        log.error("WhatsApp delivery disabled: invalid Graph API version setting")
        return None
    if not re.fullmatch(r"[0-9]+", phone_id):
        log.error("WhatsApp delivery disabled: invalid phone number ID setting")
        return None
    return {
        "token": token,
        "url": f"https://graph.facebook.com/{version}/{phone_id}/messages",
        "template": template,
        "language": language,
    }


def template_parameters(kind: str, payload: dict) -> tuple[str, str]:
    """Return the two body parameters expected by the configured template."""
    title = {
        "club_day.announced": "A Club Day was announced",
        "club_day.plan_due": "A Club Day plan is due",
        "club_day.upcoming": "A Club Day is coming up",
        "submission.deadline_soon": "A submission deadline is approaching",
        "campus.monthly_report": "Your monthly campus report is ready",
    }.get(kind, "Club Activities Tracker update")
    detail = payload.get("title") or payload.get("club_name") or payload.get("month") or "Open the app to view details."
    day = payload.get("day_date")
    if day:
        detail = f"{detail} · {day}"
    # Bound each value to keep template content predictable and avoid accidental
    # multi-kilobyte payloads from user-entered activity fields.
    return str(title)[:60], str(detail)[:120]


def _provider_error_code(response: httpx.Response) -> str:
    """Extract only the stable provider error code; discard free-form details."""
    try:
        error = response.json().get("error", {})
        code = error.get("code")
        return f"http_{response.status_code}_meta_{int(code)}" if code is not None else f"http_{response.status_code}"
    except (ValueError, TypeError, AttributeError):
        return f"http_{response.status_code}"


async def _claim_batch(conn):
    async with conn.transaction():
        await conn.execute(
            """UPDATE notification_deliveries d SET status='failed',
                      last_error_code='outside_opt_in_period', updated_at=now()
               FROM notifications n, whatsapp_preferences p
               WHERE d.notification_id=n.id AND p.user_id=n.user_id
                 AND d.channel='whatsapp' AND d.status='pending'
                 AND p.opted_in AND n.send_at < p.opted_in_at"""
        )
        # Recover work abandoned by a worker process that stopped mid-request.
        await conn.execute(
            """UPDATE notification_deliveries SET status='pending', updated_at=now()
               WHERE channel='whatsapp' AND status='sending'
                 AND updated_at < now() - interval '5 minutes'"""
        )
        # Fan out only notifications already due for the in-app inbox and only
        # while the user has an active, explicit WhatsApp opt-in.
        await conn.execute(
            """INSERT INTO notification_deliveries (notification_id, channel)
               SELECT n.id, 'whatsapp'
               FROM notifications n
               JOIN whatsapp_preferences p ON p.user_id=n.user_id AND p.opted_in
               WHERE n.sent_at IS NOT NULL AND n.send_at >= p.opted_in_at
                 AND n.kind=ANY($1::text[])
               ON CONFLICT (notification_id, channel) DO NOTHING""",
            list(SUPPORTED_KINDS),
        )
        rows = await conn.fetch(
            """WITH picked AS (
                 SELECT d.id
                 FROM notification_deliveries d
                   JOIN notifications n ON n.id=d.notification_id
                 JOIN whatsapp_preferences p ON p.user_id=n.user_id AND p.opted_in
                 WHERE d.channel='whatsapp' AND d.status='pending'
                   AND d.next_attempt_at <= now() AND n.kind=ANY($1::text[])
                   AND n.send_at >= p.opted_in_at
                 ORDER BY d.next_attempt_at, d.id LIMIT $2
                 FOR UPDATE OF d SKIP LOCKED
               )
               UPDATE notification_deliveries d
               SET status='sending', attempts=d.attempts+1, updated_at=now()
               FROM picked
               WHERE d.id=picked.id
               RETURNING d.id, d.notification_id, d.attempts""",
            list(SUPPORTED_KINDS), BATCH_SIZE,
        )
        if not rows:
            return []
        details = await conn.fetch(
            """SELECT d.id, d.attempts, n.kind, n.payload, p.phone_e164
               FROM notification_deliveries d
               JOIN notifications n ON n.id=d.notification_id
               JOIN whatsapp_preferences p ON p.user_id=n.user_id
               WHERE d.id=ANY($1::bigint[]) AND p.opted_in""",
            [row["id"] for row in rows],
        )
        return [dict(row) for row in details]


async def _record_success(conn, delivery_id: int, message_id: str | None):
    await conn.execute(
        """UPDATE notification_deliveries SET status='sent', sent_at=now(),
           provider_message_id=$2, last_error_code=NULL, updated_at=now() WHERE id=$1""",
        delivery_id, message_id,
    )


async def _record_failure(conn, row: dict, code: str):
    permanent_client_error = code.startswith("http_4") and not code.startswith("http_429")
    terminal = row["attempts"] >= MAX_ATTEMPTS or permanent_client_error
    delay_seconds = min(60 * (2 ** max(row["attempts"] - 1, 0)), 21600)
    await conn.execute(
        """UPDATE notification_deliveries SET status=$2,
           next_attempt_at=now() + ($3::int * interval '1 second'),
           last_error_code=$4, updated_at=now() WHERE id=$1""",
        row["id"], "failed" if terminal else "pending", delay_seconds, code[:80],
    )


async def _send_one(client: httpx.AsyncClient, config: dict, row: dict):
    title, detail = template_parameters(row["kind"], row["payload"] or {})
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": row["phone_e164"],
        "type": "template",
        "template": {
            "name": config["template"],
            "language": {"code": config["language"]},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": title},
                    {"type": "text", "text": detail},
                ],
            }],
        },
    }
    try:
        response = await client.post(
            config["url"], json=body,
            headers={"Authorization": f"Bearer {config['token']}"},
        )
        if response.is_success:
            try:
                messages = response.json().get("messages", [])
                message_id = messages[0].get("id") if messages else None
            except (ValueError, TypeError, AttributeError, IndexError):
                message_id = None
            return message_id, None
        return None, _provider_error_code(response)
    except httpx.TimeoutException:
        return None, "request_timeout"
    except httpx.RequestError:
        return None, "network_error"


async def deliver_whatsapp(conn) -> int:
    config = whatsapp_config()
    if not config:
        return 0
    rows = await _claim_batch(conn)
    if not rows:
        return 0
    ready = []
    for row in rows:
        current_preference = await conn.fetchrow(
            """SELECT p.opted_in, p.phone_e164 FROM notification_deliveries d
               JOIN notifications n ON n.id=d.notification_id
               JOIN whatsapp_preferences p ON p.user_id=n.user_id
               WHERE d.id=$1""",
            row["id"],
        )
        if not current_preference or not current_preference["opted_in"]:
            await conn.execute(
                """UPDATE notification_deliveries SET status='failed',
                   last_error_code='user_opted_out', updated_at=now() WHERE id=$1""",
                row["id"],
            )
            continue
        if current_preference["phone_e164"] != row["phone_e164"]:
            await conn.execute(
                """UPDATE notification_deliveries SET status='pending',
                   attempts=GREATEST(attempts-1,0), updated_at=now() WHERE id=$1""",
                row["id"],
            )
            continue
        ready.append(row)

    if not ready:
        return 0
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        semaphore = asyncio.Semaphore(SEND_CONCURRENCY)

        async def limited_send(row):
            async with semaphore:
                return await _send_one(client, config, row)

        outcomes = await asyncio.gather(*(limited_send(row) for row in ready))
        sent = 0
        for row, (message_id, error_code) in zip(ready, outcomes):
            if error_code:
                await _record_failure(conn, row, error_code)
                log.warning("WhatsApp delivery %s failed (%s), attempt %s", row["id"], error_code, row["attempts"])
            else:
                await _record_success(conn, row["id"], message_id)
                sent += 1
    return sent
