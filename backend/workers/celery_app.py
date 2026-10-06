import os
import sys

from celery import Celery
from celery.schedules import crontab

# Celery only puts the project folder on the import path while it loads this file,
# so tasks that import `app.*` later fail with "No module named app".
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

celery = Celery(
    "clubtracker",
    broker=os.environ["REDIS_URL"],
    backend=os.environ["REDIS_URL"],
    include=["workers.tasks_schedule", "workers.tasks_certificates"],
)

# Assumption: campuses are in India, so schedules run on IST.
celery.conf.timezone = "Asia/Kolkata"

celery.conf.beat_schedule = {
    # 3-day rule: approved club days become 'announced' and members are notified
    "announce-upcoming-club-days": {
        "task": "workers.tasks_schedule.announce_upcoming_club_days",
        "schedule": crontab(minute=0),  # hourly
    },
    # outbox pattern: deliver rows from the `notifications` table
    "send-pending-notifications": {
        "task": "workers.tasks_schedule.send_pending_notifications",
        "schedule": 60.0,
    },
    # safety net: render any certificate whose queue message was lost
    "generate-pending-certificates": {
        "task": "workers.tasks_certificates.sweep_pending_certificates",
        "schedule": 60.0,
    },
    # stubs for the remaining reminders (see the task docstrings)
    "plan-reminder-7d": {
        "task": "workers.tasks_schedule.remind_plan_7d",
        "schedule": crontab(hour=9, minute=0),
    },
    "reminder-48h": {
        "task": "workers.tasks_schedule.remind_48h",
        "schedule": crontab(minute=15),
    },
    "deadline-reminder-5d": {
        "task": "workers.tasks_schedule.remind_deadline_5d",
        "schedule": crontab(hour=9, minute=30),
    },
    # runs on days 28-31; the task itself checks that tomorrow is the 1st
    "monthly-report": {
        "task": "workers.tasks_schedule.monthly_report",
        "schedule": crontab(hour=23, minute=55, day_of_month="28-31"),
    },
}


@celery.task
def ping():
    return "pong"