"""Events: registration, teams, check-in, results.

Concurrency rule used everywhere: every mutating operation starts by locking the
EVENT row (SELECT ... FOR UPDATE), then the team row if needed. One fixed lock
order means no deadlocks, and the lock is what makes the capacity check atomic:
two students cannot both take the last seat. DB unique constraints are the
second line of defence.
"""
from datetime import datetime, timezone
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.core import audit
from app.core.permissions import ADMIN_ROLES, current_user, require_role
from app.db import get_conn

router = APIRouter(prefix="/events", tags=["events"])


class EventIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    type: Literal["gambade", "hackathon", "cross_road", "international_conference", "other"]
    capacity: int = Field(gt=0, le=100000)
    team_size: int | None = Field(default=None, ge=1, le=50)
    starts_at: AwareDatetime | None = None
    campus_id: int | None = None  # super admin only
    is_inter_college: bool = False


class TeamIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class CheckinIn(BaseModel):
    student_id: int


class ResultItem(BaseModel):
    team_id: int | None = None
    student_id: int | None = None
    rank: int = Field(ge=1, le=10000)
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.team_id is None) == (self.student_id is None):
            raise ValueError("give exactly one of team_id or student_id")
        return self


class ResultsIn(BaseModel):
    results: list[ResultItem] = Field(max_length=500)


def _event_out(r, registered=None) -> dict:
    s = r["starts_at"]
    return {
        "id": r["id"], "campus_id": r["campus_id"], "type": r["type"], "name": r["name"],
        "capacity": r["capacity"], "team_size": r["team_size"], "starts_at": s,
        "is_inter_college": r["is_inter_college"],
        "approved_at": r["approved_at"],
        "registered": r["registered"] if registered is None else registered,
        "registration_open": s is None or datetime.now(timezone.utc) < s,
    }


async def _event_for(conn, user: dict, event_id: int, lock: bool = False):
    """Load an event the user may see. Other campuses get 404 (we don't reveal it exists)."""
    ev = await conn.fetchrow(
        f"SELECT * FROM events WHERE id=$1 {'FOR UPDATE' if lock else ''}", event_id
    )
    cross_campus_student = user["role"] == "student" and ev is not None and ev["is_inter_college"] and ev["approved_at"] is not None
    if ev is None or (user["role"] != "super_admin" and ev["campus_id"] != user["campus_id"] and not cross_campus_student):
        raise HTTPException(404, "Event not found")
    return ev


async def _require_editable(conn, ev):
    s = ev["starts_at"]
    if s is not None and datetime.now(timezone.utc) >= s:
        raise HTTPException(409, "Registration and teams are closed (the event has started)")
    if await conn.fetchval("SELECT 1 FROM event_results WHERE event_id=$1 LIMIT 1", ev["id"]):
        raise HTTPException(409, "Results are published; registration and teams are locked")


async def _require_registered(conn, event_id: int, student_id: int):
    ok = await conn.fetchval(
        "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, student_id
    )
    if not ok:
        raise HTTPException(403, "Register for the event first")


async def _remove_from_team(conn, event_id: int, student_id: int):
    """Remove a student from their team (if any); delete the team if it is now empty."""
    tm = await conn.fetchrow(
        "SELECT team_id FROM team_members WHERE event_id=$1 AND student_id=$2 FOR UPDATE",
        event_id, student_id,
    )
    if tm is None:
        return None
    await conn.execute(
        "DELETE FROM team_members WHERE team_id=$1 AND student_id=$2", tm["team_id"], student_id
    )
    left = await conn.fetchval("SELECT count(*) FROM team_members WHERE team_id=$1", tm["team_id"])
    if left == 0:
        await conn.execute("DELETE FROM teams WHERE id=$1", tm["team_id"])
    return tm["team_id"]


@router.post("", status_code=201)
async def create_event(
    body: EventIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)),
    conn=Depends(get_conn),
):
    campus_id = body.campus_id if user["role"] == "super_admin" else user["campus_id"]
    if campus_id is None:
        raise HTTPException(422, "campus_id is required")
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO events (campus_id, type, name, capacity, team_size, starts_at, is_inter_college)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
                campus_id, body.type, body.name.strip(), body.capacity,
                body.team_size, body.starts_at, body.is_inter_college,
            )
            await audit.log(
                conn, actor_id=user["id"], action="event.create", entity="events",
                entity_id=row["id"], after={"name": row["name"], "capacity": row["capacity"]},
            )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(422, "Unknown campus_id")
    return _event_out(row, 0)


@router.get("")
async def list_events(user: dict = Depends(current_user), conn=Depends(get_conn)):
    base = """SELECT e.*, (SELECT count(*) FROM registrations r WHERE r.event_id = e.id) AS registered
              FROM events e"""
    order = " ORDER BY e.starts_at NULLS LAST, e.id DESC"
    if user["role"] == "super_admin":
        rows = await conn.fetch(base + order)
    elif user["role"] == "student":
        rows = await conn.fetch(base + " WHERE e.campus_id=$1 OR (e.is_inter_college AND e.approved_at IS NOT NULL)" + order, user["campus_id"])
    else:
        rows = await conn.fetch(base + " WHERE e.campus_id=$1" + order, user["campus_id"])
    out = [_event_out(r) for r in rows]

    if user["role"] == "student":
        regs = {r["event_id"] for r in await conn.fetch(
            "SELECT event_id FROM registrations WHERE student_id=$1", user["id"])}
        teams = {r["event_id"]: r for r in await conn.fetch(
            """SELECT tm.event_id, t.id, t.name FROM team_members tm
               JOIN teams t ON t.id = tm.team_id WHERE tm.student_id=$1""", user["id"])}
        for o in out:
            o["my_registered"] = o["id"] in regs
            t = teams.get(o["id"])
            o["my_team"] = {"id": t["id"], "name": t["name"]} if t else None
    return out


@router.get("/{event_id}")
async def get_event(event_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    ev = await _event_for(conn, user, event_id)
    registered = await conn.fetchval("SELECT count(*) FROM registrations WHERE event_id=$1", event_id)
    rows = await conn.fetch(
        """SELECT t.id AS team_id, t.name AS team_name, u.id AS student_id,
                  u.name AS student_name, u.student_code
           FROM teams t
           LEFT JOIN team_members tm ON tm.team_id = t.id
           LEFT JOIN users u ON u.id = tm.student_id
           WHERE t.event_id=$1 ORDER BY t.id, u.name""",
        event_id,
    )
    if user["role"] == "student":
        if not await conn.fetchval("SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, user["id"]):
            rows = []
    teams: dict = {}
    for r in rows:
        t = teams.setdefault(r["team_id"], {"id": r["team_id"], "name": r["team_name"], "members": []})
        if r["student_id"] is not None:
            m = {"student_id": r["student_id"], "student_name": r["student_name"]}
            if user["role"] != "student":
                m["student_code"] = r["student_code"]
            t["members"].append(m)
    out = _event_out(ev, registered)
    out["teams"] = list(teams.values())
    if user["role"] == "student":
        out["my_registered"] = bool(await conn.fetchval(
            "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, user["id"]))
        mt = await conn.fetchrow(
            """SELECT t.id, t.name FROM team_members tm JOIN teams t ON t.id = tm.team_id
               WHERE tm.event_id=$1 AND tm.student_id=$2""", event_id, user["id"])
        out["my_team"] = {"id": mt["id"], "name": mt["name"]} if mt else None
    return out


@router.post("/{event_id}/register", status_code=201)
async def register(
    event_id: int, user: dict = Depends(require_role("student")), conn=Depends(get_conn)
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)  # serialises seat-taking
        await _require_editable(conn, ev)
        already = await conn.fetchval(
            "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, user["id"])
        if already:
            raise HTTPException(409, "You are already registered for this event")
        taken = await conn.fetchval("SELECT count(*) FROM registrations WHERE event_id=$1", event_id)
        if taken >= ev["capacity"]:
            raise HTTPException(409, "Event is full")
        try:
            row = await conn.fetchrow(
                "INSERT INTO registrations (event_id, student_id) VALUES ($1,$2) RETURNING id, created_at",
                event_id, user["id"],
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "You are already registered for this event")
        if ev["is_inter_college"] and ev["approved_at"] is not None and user["campus_id"] != ev["campus_id"]:
            await conn.execute(
                """INSERT INTO duty_leave_requests (event_id, student_id)
                   VALUES ($1,$2) ON CONFLICT (event_id, student_id) DO NOTHING""",
                event_id, user["id"],
            )
    return {"event_id": event_id, "registered": True, "seats_left": ev["capacity"] - taken - 1,
            "registered_at": row["created_at"]}


@router.post("/{event_id}/approve")
async def approve_inter_college_event(
    event_id: int, user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn),
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)
        if not ev["is_inter_college"]:
            raise HTTPException(409, "Only inter-college events need approval")
        if ev["approved_at"] is not None:
            raise HTTPException(409, "Event is already approved")
        row = await conn.fetchrow(
            "UPDATE events SET approved_by=$2, approved_at=now() WHERE id=$1 RETURNING approved_at",
            event_id, user["id"],
        )
        await audit.log(conn, actor_id=user["id"], action="event.inter_college_approve",
                        entity="events", entity_id=event_id,
                        after={"approved": True, "approved_at": row["approved_at"]})
    return {"event_id": event_id, "approved": True, "approved_at": row["approved_at"]}


@router.delete("/{event_id}/register")
async def unregister(
    event_id: int, user: dict = Depends(require_role("student")), conn=Depends(get_conn)
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)
        await _require_editable(conn, ev)
        reg = await conn.fetchval(
            "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2 FOR UPDATE",
            event_id, user["id"])
        if not reg:
            raise HTTPException(404, "You are not registered for this event")
        await _remove_from_team(conn, event_id, user["id"])
        await conn.execute(
            "DELETE FROM registrations WHERE event_id=$1 AND student_id=$2", event_id, user["id"])
    return {"unregistered": True}


@router.post("/{event_id}/teams", status_code=201)
async def create_team(
    event_id: int, body: TeamIn,
    user: dict = Depends(require_role("student")), conn=Depends(get_conn),
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)
        await _require_editable(conn, ev)
        await _require_registered(conn, event_id, user["id"])
        if await conn.fetchval(
            "SELECT 1 FROM team_members WHERE event_id=$1 AND student_id=$2", event_id, user["id"]):
            raise HTTPException(409, "You are already in a team for this event")
        try:
            team = await conn.fetchrow(
                "INSERT INTO teams (event_id, name) VALUES ($1,$2) RETURNING id, name",
                event_id, body.name.strip(),
            )
            await conn.execute(
                "INSERT INTO team_members (team_id, event_id, student_id) VALUES ($1,$2,$3)",
                team["id"], event_id, user["id"],
            )
        except asyncpg.UniqueViolationError as e:
            if e.constraint_name == "uq_team_name_per_event":
                raise HTTPException(409, "A team with this name already exists")
            raise HTTPException(409, "You are already in a team for this event")
    return {"id": team["id"], "name": team["name"], "event_id": event_id}


@router.post("/{event_id}/teams/leave")
async def leave_team(
    event_id: int, user: dict = Depends(require_role("student")), conn=Depends(get_conn)
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)
        await _require_editable(conn, ev)
        if await _remove_from_team(conn, event_id, user["id"]) is None:
            raise HTTPException(404, "You are not in a team for this event")
    return {"left": True}


@router.post("/{event_id}/teams/{team_id}/join")
async def join_team(
    event_id: int, team_id: int,
    user: dict = Depends(require_role("student")), conn=Depends(get_conn),
):
    async with conn.transaction():
        ev = await _event_for(conn, user, event_id, lock=True)
        await _require_editable(conn, ev)
        await _require_registered(conn, event_id, user["id"])
        team = await conn.fetchrow(
            "SELECT id FROM teams WHERE id=$1 AND event_id=$2 FOR UPDATE", team_id, event_id)
        if team is None:
            raise HTTPException(404, "Team not found")
        if await conn.fetchval(
            "SELECT 1 FROM team_members WHERE event_id=$1 AND student_id=$2", event_id, user["id"]):
            raise HTTPException(409, "You are already in a team for this event")
        members = await conn.fetchval("SELECT count(*) FROM team_members WHERE team_id=$1", team_id)
        if ev["team_size"] is not None and members >= ev["team_size"]:
            raise HTTPException(409, "Team is full")
        try:
            await conn.execute(
                "INSERT INTO team_members (team_id, event_id, student_id) VALUES ($1,$2,$3)",
                team_id, event_id, user["id"],
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "You are already in a team for this event")
    return {"joined": True, "team_id": team_id}


@router.get("/{event_id}/registrations")
async def list_registrations(
    event_id: int, user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn)
):
    await _event_for(conn, user, event_id)
    rows = await conn.fetch(
        """SELECT r.student_id, u.name AS student_name, u.student_code,
                  r.created_at, r.checked_in_at, t.name AS team_name
           FROM registrations r
           JOIN users u ON u.id = r.student_id
           LEFT JOIN team_members tm ON tm.event_id = r.event_id AND tm.student_id = r.student_id
           LEFT JOIN teams t ON t.id = tm.team_id
           WHERE r.event_id=$1 ORDER BY r.created_at""",
        event_id,
    )
    return [dict(r) for r in rows]


@router.post("/{event_id}/checkin")
async def checkin_student(
    event_id: int, body: CheckinIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn),
):
    await _event_for(conn, user, event_id)
    async with conn.transaction():
        row = await conn.fetchrow(
            """UPDATE registrations SET checked_in_at = now()
               WHERE event_id=$1 AND student_id=$2 AND checked_in_at IS NULL
               RETURNING checked_in_at""",
            event_id, body.student_id,
        )
        if row is None:
            exists = await conn.fetchval(
                "SELECT 1 FROM registrations WHERE event_id=$1 AND student_id=$2",
                event_id, body.student_id)
            if exists:
                raise HTTPException(409, "Student is already checked in")
            raise HTTPException(404, "Student is not registered for this event")
        await audit.log(
            conn, actor_id=user["id"], action="event.checkin", entity="registrations",
            entity_id=f"{event_id}:{body.student_id}", after={"checked_in": True},
        )
    return {"student_id": body.student_id, "checked_in_at": row["checked_in_at"]}


@router.put("/{event_id}/results")
async def set_results(
    event_id: int, body: ResultsIn,
    user: dict = Depends(require_role(*ADMIN_ROLES)), conn=Depends(get_conn),
):
    """Replaces ALL results for the event in one transaction."""
    team_ids = [r.team_id for r in body.results if r.team_id is not None]
    student_ids = [r.student_id for r in body.results if r.student_id is not None]
    if len(set(team_ids)) != len(team_ids) or len(set(student_ids)) != len(student_ids):
        raise HTTPException(422, "A team or student appears more than once")

    async with conn.transaction():
        await _event_for(conn, user, event_id, lock=True)
        if team_ids:
            n = await conn.fetchval(
                "SELECT count(*) FROM teams WHERE event_id=$1 AND id = ANY($2::bigint[])",
                event_id, team_ids)
            if n != len(team_ids):
                raise HTTPException(422, "Every team_id must belong to this event")
        if student_ids:
            n = await conn.fetchval(
                """SELECT count(*) FROM registrations
                   WHERE event_id=$1 AND student_id = ANY($2::bigint[])""",
                event_id, student_ids)
            if n != len(student_ids):
                raise HTTPException(422, "Every student_id must be registered for this event")

        before = await conn.fetchval("SELECT count(*) FROM event_results WHERE event_id=$1", event_id)
        await conn.execute("DELETE FROM event_results WHERE event_id=$1", event_id)
        for r in body.results:
            await conn.execute(
                """INSERT INTO event_results (event_id, team_id, student_id, rank, notes)
                   VALUES ($1,$2,$3,$4,$5)""",
                event_id, r.team_id, r.student_id, r.rank, r.notes,
            )
        await audit.log(
            conn, actor_id=user["id"], action="event.results", entity="events",
            entity_id=event_id, before={"rows": before}, after={"rows": len(body.results)},
        )
    return {"event_id": event_id, "saved": len(body.results)}


@router.get("/{event_id}/results")
async def get_results(event_id: int, user: dict = Depends(current_user), conn=Depends(get_conn)):
    await _event_for(conn, user, event_id)
    rows = await conn.fetch(
        """SELECT r.rank, r.notes, r.team_id, t.name AS team_name,
                  r.student_id, u.name AS student_name
           FROM event_results r
           LEFT JOIN teams t ON t.id = r.team_id
           LEFT JOIN users u ON u.id = r.student_id
           WHERE r.event_id=$1 ORDER BY r.rank, t.name, u.name""",
        event_id,
    )
    return [dict(r) for r in rows]
