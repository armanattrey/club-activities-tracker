"""Time-based jobs. Celery tasks are sync, so each one runs a short async
function with its own asyncpg connection.

My reading of the brief's deadlines (confirm or correct me):
  7-day  -> remind coordinators who have no approved plan
  3-day  -> auto-announce (IMPLEMENTED below)
  48-hr  -> remind members before the club day
  5-day  -> remind students of upcoming submission deadlines
"""
import asyncio
import logging
import os

import asyncpg

from workers.celery_app import celery

log = logging.getLogger(__name__)


async def _with_conn(fn):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def _announce(conn) -> int:
    # NOTE: current_date uses the DB server's timezone (UTC). Fine for a skeleton;
    # revisit if a club day near midnight IST matters.
    async with conn.transaction():
        days = await conn.fetch(
            """UPDATE club_days SET status = 'announced'
               WHERE status = 'approved' AND day_date <= current_date + 3
               RETURNING id, club_id"""
        )
        for d in days:
            await conn.execute(
                """INSERT INTO notifications (user_id, kind, payload)
                   SELECT student_id, 'club_day.announced',
                          jsonb_build_object('club_day_id', $1::bigint)
                   FROM club_memberships
                   WHERE club_id = $2 AND status = 'approved'""",
                d["id"], d["club_id"],
            )
            await conn.execute(
                """INSERT INTO audit_log (actor_id, action, entity, entity_id, after_state)
                   VALUES (NULL, 'club_day.auto_announce', 'club_days', $1::text,
                           jsonb_build_object('status', 'announced'))""",
                str(d["id"]),
            )
    return len(days)


@celery.task(name="workers.tasks_schedule.announce_upcoming_club_days")
def announce_upcoming_club_days():
    n = asyncio.run(_with_conn(_announce))
    log.info("announced %s club day(s)", n)
    return n


async def _count_pending(conn) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM notifications WHERE sent_at IS NULL AND send_at <= now()"
    )


@celery.task(name="workers.tasks_schedule.send_pending_notifications")
def send_pending_notifications():
    """STUB: only logs the backlog. TODO: deliver (in-app/email) and set sent_at."""
    n = asyncio.run(_with_conn(_count_pending))
    log.info("pending notifications: %s", n)
    return n


@celery.task(name="workers.tasks_schedule.remind_plan_7d")
def remind_plan_7d():
    """STUB. TODO: club days 7 days away with no approved plan -> notify coordinators."""
    log.info("remind_plan_7d: TODO")


@celery.task(name="workers.tasks_schedule.remind_48h")
def remind_48h():
    """STUB. TODO: club days within 48 hours -> notify approved members."""
    log.info("remind_48h: TODO")


@celery.task(name="workers.tasks_schedule.remind_deadline_5d")
def remind_deadline_5d():
    """STUB. TODO: submission deadlines within 5 days -> notify students without a submission."""
    log.info("remind_deadline_5d: TODO")


@celery.task(name="workers.tasks_schedule.monthly_report")
def monthly_report():
    """STUB. TODO: only run if tomorrow is the 1st; build the report from the
    same SQL views as the dashboards so the figures reconcile with raw data."""
    log.info("monthly_report: TODO")