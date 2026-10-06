async def log(
    conn,
    *,
    actor_id: int,
    action: str,
    entity: str,
    entity_id,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
):
    """Append-only audit trail. Corrections must pass a non-empty reason
    (also enforced by a CHECK constraint in the DB)."""
    await conn.execute(
        """INSERT INTO audit_log
           (actor_id, action, entity, entity_id, before_state, after_state, reason)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        actor_id, action, entity, str(entity_id), before, after, reason,
    )