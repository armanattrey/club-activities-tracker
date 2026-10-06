from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.permissions import current_user
from app.db import get_conn

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(current_user),
    conn=Depends(get_conn),
):
    rows = await conn.fetch(
        """SELECT id, kind, payload, send_at, sent_at, read_at
           FROM notifications
           WHERE user_id=$1 AND ($2::boolean = false OR read_at IS NULL)
           ORDER BY send_at DESC, id DESC LIMIT $3""",
        user["id"], unread_only, limit,
    )
    unread = await conn.fetchval(
        "SELECT count(*) FROM notifications WHERE user_id=$1 AND read_at IS NULL",
        user["id"],
    )
    return {"unread_count": unread, "items": [dict(row) for row in rows]}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user: dict = Depends(current_user),
    conn=Depends(get_conn),
):
    row = await conn.fetchrow(
        """UPDATE notifications SET read_at=COALESCE(read_at, now())
           WHERE id=$1 AND user_id=$2
           RETURNING id, kind, payload, send_at, sent_at, read_at""",
        notification_id, user["id"],
    )
    if row is None:
        raise HTTPException(404, "Notification not found")
    return dict(row)


@router.post("/read-all")
async def mark_all_read(user: dict = Depends(current_user), conn=Depends(get_conn)):
    result = await conn.execute(
        "UPDATE notifications SET read_at=now() WHERE user_id=$1 AND read_at IS NULL",
        user["id"],
    )
    return {"updated": int(result.rsplit(" ", 1)[-1])}
