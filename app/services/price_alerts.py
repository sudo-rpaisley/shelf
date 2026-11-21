"""Wishlist price alerts: nightly list-price lookups and the drop digest.

Helpers take the caller's connection. `run_pass` owns its connections: it
opens a short `get_db()` block for each read or write and never holds one
across an `await` (G3, G112). No module-level state (G13).
"""

import json
import logging
from datetime import datetime, timezone

import httpx

from app.database import get_db, get_setting
from app.services import isbn, isbndb, lists, notify

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 15
MAX_THRESHOLD_PCT = 100
DEFAULT_NIGHTLY_CAP = 100
MAX_NIGHTLY_CAP = 500
# ISBNdb refusing the key: the rest of the pass would be refused too.
KEY_REFUSED_STATUSES = (401, 403)


def _bounded_int(db, key: str, default: int, maximum: int) -> int:
    raw = get_setting(db, key)
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if 1 <= value <= maximum else default


def get_threshold_pct(db) -> int:
    """The drop threshold in percent; missing or out-of-range reads as the default."""
    return _bounded_int(db, "price_alert_threshold_pct", DEFAULT_THRESHOLD_PCT, MAX_THRESHOLD_PCT)


def get_nightly_cap(db) -> int:
    """Most lookups per pass; missing or out-of-range reads as the default."""
    return _bounded_int(db, "price_alert_nightly_cap", DEFAULT_NIGHTLY_CAP, MAX_NIGHTLY_CAP)


def _failure_reason(status) -> str:
    if status in KEY_REFUSED_STATUSES:
        return "ISBNdb refused the API key"
    if status == 429:
        return "ISBNdb rate limit"
    if status is None:
        return "no response from ISBNdb"
    return f"ISBNdb answered HTTP {status}"


def describe_last_run(stamp: str | None, summary: str | None) -> str:
    """The Settings "Last run" text: `YYYY-MM-DD HH:MM UTC — N checked[, M failed (why)]`,
    or `Never`. `summary` is the JSON `check_price_alerts` stores in
    `price_alert_last_summary`; a missing or unreadable one shows the time alone.
    """
    try:
        when = datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "Never"
    try:
        info = json.loads(summary)
        text = f"{when} — {int(info['looked_up'])} checked"
        if int(info["failed"]):
            text += f", {int(info['failed'])} failed ({_failure_reason(info.get('status'))})"
        return text
    except (TypeError, ValueError, KeyError):
        return when


def select_candidates(db, cap: int) -> list:
    """Live, wishlisted, ISBN-bearing items: never-observed first, then oldest observation.

    SQLite sorts NULL first under ASC, so a never-observed item leads.
    """
    return db.execute(
        "SELECT i.id, i.title, i.isbn, "
        "(SELECT MAX(ph.observed_at) FROM price_history ph WHERE ph.item_id = i.id) AS last_seen "
        "FROM items_live i "
        f"WHERE {lists.WISHLISTED_SQL} AND i.isbn IS NOT NULL AND i.isbn != '' "
        "ORDER BY last_seen ASC, i.id ASC "
        "LIMIT ?",
        (cap,),
    ).fetchall()


def record_observation(db, item_id: int, price: float | None) -> int:
    """Append one observation (NULL price = a miss) and return its row id."""
    cur = db.execute(
        "INSERT INTO price_history (item_id, price, source) VALUES (?, ?, 'isbndb')",
        (item_id, price),
    )
    return cur.lastrowid


def previous_price(db, item_id: int, before_id: int) -> float | None:
    """The most recent earlier non-null price for the item, or None."""
    row = db.execute(
        "SELECT price FROM price_history "
        "WHERE item_id = ? AND id < ? AND price IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (item_id, before_id),
    ).fetchone()
    return row["price"] if row else None


def drop_pct(previous: float | None, current: float | None) -> float | None:
    """Percent fallen from `previous` to `current`; None when not comparable.

    A rise returns a negative number. A NULL on either side, or a zero
    previous price, is not comparable.
    """
    if previous is None or current is None or previous <= 0:
        return None
    return (previous - current) * 100 / previous


def build_digest(drops: list[dict]) -> tuple[str, str]:
    """Title and body for a drop digest.

    Each drop is `{"title", "previous", "price"}`. Money goes through
    `format_money`, which reads the display currency on its own connection,
    so call this outside any open write block.
    """
    from app.currency import format_money

    n = len(drops)
    title = f"Shelf: {n} wishlist price drop{'' if n == 1 else 's'}"
    lines = []
    for d in drops:
        pct = round(drop_pct(d["previous"], d["price"]))
        lines.append(
            f"{d['title']} — {format_money(d['previous'])} → "
            f"{format_money(d['price'])} (−{pct}%)"
        )
    return title, "\n".join(lines)


def price_line(db, item_id: int) -> dict | None:
    """The item-detail list-price line, or None with no priced observation.

    `current` is the latest non-null row. `was` is the most recent earlier
    non-null row whose price differs from `current`, else None. `stale` is
    True when a later lookup returned no price, so `current` is not the latest
    word and the line must date it.
    """
    cur = db.execute(
        "SELECT id, price, observed_at FROM price_history "
        "WHERE item_id = ? AND price IS NOT NULL ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if not cur:
        return None
    was = db.execute(
        "SELECT price, observed_at FROM price_history "
        "WHERE item_id = ? AND id < ? AND price IS NOT NULL AND price != ? "
        "ORDER BY id DESC LIMIT 1",
        (item_id, cur["id"], cur["price"]),
    ).fetchone()
    newest = db.execute(
        "SELECT MAX(id) FROM price_history WHERE item_id = ?", (item_id,)
    ).fetchone()[0]
    return {
        "current": cur["price"],
        "current_on": cur["observed_at"][:10],
        "stale": newest != cur["id"],
        "was": was["price"] if was else None,
        "was_on": was["observed_at"][:10] if was else None,
    }


async def run_pass(api_key: str, *, threshold: int, cap: int,
                   notify_url: str, notify_format: str) -> dict:
    """One nightly pass. Returns `{looked_up, failed, failed_status, drops, sent}`.

    `failed` counts lookups ISBNdb did not answer (`failed_status` is the last
    one's HTTP status, None for no response). A refused key stops the pass.
    """
    with get_db() as db:
        candidates = select_candidates(db, cap)

    looked_up = 0
    failed = 0
    failed_status = None
    drops: list[dict] = []
    cache = isbndb._load_cache()
    async with httpx.AsyncClient() as client:
        for row in candidates:
            isbn13 = isbn.to_isbn13(row["isbn"])
            if isbn13 is None:
                price = None
            else:
                looked_up += 1
                try:
                    data = await isbndb.lookup_price(isbn13, api_key, client, cache,
                                                     use_cache=False, raise_on_failure=True)
                except isbndb.LookupFailed as exc:
                    # No answer is not an observation: record nothing, so the
                    # book keeps its place at the front of the queue.
                    failed += 1
                    failed_status = exc.status
                    if exc.status in KEY_REFUSED_STATUSES:
                        break  # every further lookup would be refused too
                    continue
                price = isbndb.parse_price(data)
            with get_db() as db:
                obs_id = record_observation(db, row["id"], price)
                previous = previous_price(db, row["id"], obs_id)
            pct = drop_pct(previous, price)
            if pct is not None and pct >= threshold:
                drops.append({"title": row["title"], "previous": previous, "price": price})
    isbndb._save_cache(cache)

    sent = False
    if drops and notify_url:
        title, body = build_digest(drops)
        sent = await notify.send_notification(notify_url, title, body, notify_format)
        if not sent:
            logger.warning("Price-drop digest for %d item(s) was not accepted", len(drops))
    return {"looked_up": looked_up, "failed": failed, "failed_status": failed_status,
            "drops": len(drops), "sent": bool(sent)}
