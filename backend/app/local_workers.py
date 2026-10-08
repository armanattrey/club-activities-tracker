"""In-process equivalents of the Celery queues used by the local demo mode."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
_certificate_queue: asyncio.Queue[str] | None = None
_tasks: list[asyncio.Task] = []


async def enqueue_certificate(certificate_id: str):
    if _certificate_queue is None:
        raise RuntimeError("Local background workers are not running")
    await _certificate_queue.put(certificate_id)


async def _certificate_worker():
    from workers.tasks_certificates import _generate_one, _with_conn

    while True:
        certificate_id = await _certificate_queue.get()
        try:
            result = await _with_conn(_generate_one, certificate_id)
            log.info("certificate %s: %s", certificate_id, result)
        except Exception:
            log.exception("local certificate task failed for %s", certificate_id)
        finally:
            _certificate_queue.task_done()


def _scheduled_jobs(now: datetime):
    minute = now.strftime("%Y-%m-%d %H:%M")
    jobs = []
    if now.second < 30:
        jobs.extend([
            ("send-pending-notifications", minute, "workers.tasks_schedule", "send_pending_notifications"),
            ("generate-pending-certificates", minute, "workers.tasks_certificates", "sweep_pending_certificates"),
        ])
        if now.minute == 0:
            jobs.append(("announce-upcoming-club-days", minute, "workers.tasks_schedule", "announce_upcoming_club_days"))
        if now.minute == 15:
            jobs.append(("reminder-48h", minute, "workers.tasks_schedule", "remind_48h"))
        if now.hour == 9 and now.minute == 0:
            jobs.append(("plan-reminder-7d", minute, "workers.tasks_schedule", "remind_plan_7d"))
        if now.hour == 9 and now.minute == 30:
            jobs.append(("deadline-reminder-5d", minute, "workers.tasks_schedule", "remind_deadline_5d"))
        if now.day in (28, 29, 30, 31) and now.hour == 23 and now.minute == 55:
            jobs.append(("monthly-report", minute, "workers.tasks_schedule", "monthly_report"))
    return jobs


async def _scheduler():
    last_run = {}
    while True:
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        for name, slot, module_name, task_name in _scheduled_jobs(now):
            if last_run.get(name) == slot:
                continue
            last_run[name] = slot
            try:
                module = __import__(module_name, fromlist=[task_name])
                running = asyncio.create_task(asyncio.to_thread(getattr(module, task_name)))
                try:
                    await asyncio.shield(running)
                except asyncio.CancelledError:
                    await running
                    raise
            except Exception:
                log.exception("local scheduled job %s failed", name)
        await asyncio.sleep(15)


async def start_local_workers():
    global _certificate_queue, _tasks
    _certificate_queue = asyncio.Queue()
    _tasks = [
        asyncio.create_task(_certificate_worker(), name="local-certificate-worker"),
        asyncio.create_task(_scheduler(), name="local-scheduler"),
    ]


async def stop_local_workers():
    global _certificate_queue, _tasks
    for task in _tasks:
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []
    _certificate_queue = None
