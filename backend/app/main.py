from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db, redis_client
from app.config import settings
from app.core.state_machines import InvalidTransition
from app.routers import (
    admin, auth, certificates, checkin, club_days, clubs, health, memberships,
    dashboards, events, extras, notifications, submissions, verify,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    workers_started = False
    try:
        await redis_client.init_redis()
        if settings.app_mode == "local":
            from app.local_workers import start_local_workers
            await start_local_workers()
            workers_started = True
        yield
    finally:
        if workers_started:
            from app.local_workers import stop_local_workers
            await stop_local_workers()
        await redis_client.close_redis()
        await db.close_pool()


app = FastAPI(title="Club Activities Tracker", lifespan=lifespan)

# TODO: restrict origins to the real frontend URL before deploying.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(InvalidTransition)
async def invalid_transition_handler(request: Request, exc: InvalidTransition):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(clubs.router)
app.include_router(memberships.router)
app.include_router(club_days.router)
app.include_router(checkin.router)
app.include_router(submissions.router)
app.include_router(certificates.router)
app.include_router(verify.router)  # PUBLIC: no login
app.include_router(notifications.router)
app.include_router(events.router)
app.include_router(dashboards.router)
app.include_router(extras.router)
