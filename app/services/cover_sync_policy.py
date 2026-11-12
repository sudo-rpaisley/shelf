"""Durable, explicitly reversible suppression of automatic source artwork."""


def decision(db, item_id: int) -> bool | None:
    """True is blocked, False is explicit opt-in, None is a legacy/default state."""
    row = db.execute(
        "SELECT b.blocked FROM cover_sync_blocks b JOIN items_live i ON i.id = b.item_id WHERE b.item_id = ?",
        (item_id,),
    ).fetchone()
    return bool(row["blocked"]) if row is not None else None


def blocked(db, item_id: int) -> bool:
    return decision(db, item_id) is True


def block(db, item_id: int) -> None:
    """Caller holds the write lock and has already validated the live item."""
    db.execute("INSERT INTO cover_sync_blocks (item_id, blocked) VALUES (?, 1) "
               "ON CONFLICT(item_id) DO UPDATE SET blocked = 1", (item_id,))


def allow(db, item_id: int) -> bool:
    if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
        return False
    db.execute("INSERT INTO cover_sync_blocks (item_id, blocked) VALUES (?, 0) "
               "ON CONFLICT(item_id) DO UPDATE SET blocked = 0", (item_id,))
    from app.services import item_write
    item_write.update_item_fields(db, item_id, {"cover_review_dismissed": 0})
    return True
