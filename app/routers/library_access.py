"""Shared Browse projection, without endpoint replacement or router mutation."""

from app.services import browse_grouping, lists, user_state_browse


def fetch_page(db, user_id, where, params, order_clause, *, limit, offset, values):
    # Old grouping services deliberately accept a visibility predicate. Anchor
    # every node/edge and the page itself in the authoritative live request view
    # even before those services' raw-table reads have been modernized.
    visible = "i.id IN (SELECT id FROM items_live)"
    where = (where + " AND " if where else "WHERE ") + visible
    rows, total, display_total = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=limit, offset=offset,
        values=values, visibility_sql=visible,
    )
    user_state_browse.overlay_items(db, user_id, rows)
    if rows:
        ids = [row["id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        membership = {
            row["id"]: bool(row["wishlisted"]) for row in db.execute(
                f"SELECT i.id, {lists.WISHLISTED_SQL} AS wishlisted "
                f"FROM items_live i WHERE i.id IN ({marks})", ids,
            )
        }
        for row in rows:
            row["wishlisted"] = membership[row["id"]]
    return rows, total, display_total
