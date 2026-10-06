from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db, redis_client
from app.core.state_machines import InvalidTransition
from app.routers import (
    auth, certificates, checkin, club_days, clubs, health, memberships,
    submissions, verify,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    await redis_client.init_redis()
    yield
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
app.include_router(clubs.router)
app.include_router(memberships.router)
app.include_router(club_days.router)
app.include_router(checkin.router)
app.include_router(submissions.router)
app.include_router(certificates.router)
app.include_router(verify.router)  # PUBLIC: no login

# Later stages add routers here: events, dashboards.

from app.routers import dashboards  # noqa: E402

app.include_router(dashboards.router)  # Stage 6

from app.routers import events  # noqa: E402

app.include_router(events.router)  # Stage 5
