"""Cached catalogue-quality queues; reading this module never repairs anything."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import i18n
from app.config import MEDIA_TYPES, MUSIC_MEDIA_TYPES, PERIODICAL_MEDIA_TYPES
from app.i18n import N_
from app.services import connection_suggestions, legacy_sources
from app.services.source_connectors import PROVIDERS


STALE_AFTER_DAYS = 7
MAX_QUEUE_LIMIT = 100
CATEGORIES = (
    ("covers", N_("Missing covers")),
    ("creators", N_("Missing creators")),
    ("locations", N_("Unlocated physical copies")),
    ("periodicals", N_("Incomplete periodical issues")),
    ("sources", N_("Source checks")),
    ("unavailable", N_("Confirmed unavailable source records")),
    ("connections", N_("Review format connections")),
)
_ITEM_COLUMNS = "i.id AS item_id, i.title, i.media_type"


def _queue(db, relation: str, predicate: str, columns: str, order: str, limit: int):
    count = db.execute(f"SELECT COUNT(*) FROM {relation} WHERE {predicate}").fetchone()[0]
    rows = [dict(row) for row in db.execute(
        f"SELECT {columns} FROM {relation} WHERE {predicate} ORDER BY {order} LIMIT ?",
        (limit,),
    )] if limit else []
    return count, rows


def _timestamp(value) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _source_reasons(source, now) -> list[str]:
    reasons = []
    status = source["last_status"]
    success = _timestamp(source["last_sync_at"])
    if not source["last_attempt_at"] and status == "never":
        reasons.append(i18n.gettext("Never checked"))
    elif status == "error":
        reasons.append(i18n.gettext("Last attempt failed; cached records were retained"))
    elif status == "partial":
        reasons.append(i18n.gettext("Last attempt was partial; absence was not confirmed"))
    elif not success and status != "syncing":
        reasons.append(i18n.gettext("No successful complete check recorded"))
    if success and success < now - timedelta(days=STALE_AFTER_DAYS):
        reasons.append(i18n.gettext("Last successful complete check is over seven days old"))
    elif source["last_sync_at"] and success is None:
        reasons.append(i18n.gettext("Last-success timestamp needs review"))
    return reasons


def summary(db, limit: int = 50, *, category: str = "all") -> dict:
    """All Needs Attention context, with exact quality counts and bounded rows.

    ``category`` limits hydrated detail to one queue (or ``all``). Connection
    counts describe a bounded evidence scan, never an exact catalogue total.
    Physical location counts are copies, not owned or remotely held items.
    """
    if category not in {"all", *(key for key, _label in CATEGORIES)}:
        raise ValueError("Unknown attention category")
    limit = max(0, min(int(limit), MAX_QUEUE_LIMIT))
    queues, counts, more = {}, {}, {}

    def selected(key):
        return limit if category in ("all", key) else 0

    def query(key, relation, predicate, columns=_ITEM_COLUMNS, order="i.title COLLATE NOCASE, i.id"):
        counts[key], queues[key] = _queue(db, relation, predicate, columns, order, selected(key))
        more[key] = counts[key] > len(queues[key])

    query("covers", "items_live i",
          "(i.cover_path IS NULL OR TRIM(i.cover_path) = '') AND i.cover_review_dismissed = 0")

    # A missing magazine byline is normal, not an error. Music's release-level
    # artist credit is meaningful even if its generic authors field is empty.
    creator_rows = []
    for row in db.execute(
        f"SELECT {_ITEM_COLUMNS}, i.authors, m.artist_credit FROM items_live i "
        "LEFT JOIN music_releases m ON m.item_id = i.id ORDER BY i.title COLLATE NOCASE, i.id"
    ):
        media_type = row["media_type"]
        if media_type not in MEDIA_TYPES or media_type in PERIODICAL_MEDIA_TYPES:
            continue
        # A named editor/compiler is useful catalogue metadata even though a
        # shared contributor role alone is insufficient for format matching.
        keys = connection_suggestions.creator_keys(row["authors"], include_roles=True)
        if media_type in MUSIC_MEDIA_TYPES:
            keys |= connection_suggestions.creator_keys(row["artist_credit"], include_roles=True)
        if not keys:
            creator_rows.append(dict(row))
    counts["creators"] = len(creator_rows)
    queues["creators"] = creator_rows[:selected("creators")]
    more["creators"] = counts["creators"] > len(queues["creators"])

    query("locations", "copies_live c JOIN items_live i ON i.id = c.item_id",
          "c.location_id IS NULL", f"{_ITEM_COLUMNS}, c.id AS copy_id, c.copy_number",
          "i.title COLLATE NOCASE, i.id, c.copy_number, c.id")

    # Date-only and numbered-only issues are both legitimate. A publication
    # plus an issue number, issue date OR printed cover-date label identifies
    # an issue; volume and barcode supplements alone do not.
    periodical_types = ",".join("'" + value + "'" for value in sorted(PERIODICAL_MEDIA_TYPES))
    query("periodicals", "items_live i LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
          "LEFT JOIN periodical_publications p ON p.id = pi.publication_id",
          f"i.media_type IN ({periodical_types}) AND (pi.item_id IS NULL "
          "OR p.id IS NULL OR TRIM(p.title) = '' "
          "OR (TRIM(COALESCE(pi.issue_number, '')) = '' "
          "AND TRIM(COALESCE(pi.issue_date, '')) = '' "
          "AND TRIM(COALESCE(pi.cover_date_label, '')) = ''))",
          f"{_ITEM_COLUMNS}, p.title AS publication_title, pi.issue_number, pi.issue_date, pi.cover_date_label")

    source_rows = []
    now = datetime.now(timezone.utc)
    for row in db.execute(
        "SELECT id AS source_id, name, provider, last_status, last_sync_at, last_attempt_at "
        "FROM source_instances WHERE enabled = 1 ORDER BY name COLLATE NOCASE, id"
    ):
        source = dict(row)
        provider = PROVIDERS.get(source["provider"])
        if provider is not None and not provider.syncable:
            # Manual availability links have no enumeration lifecycle. Their
            # explicitly unavailable records still belong in the queue below.
            continue
        reasons = _source_reasons(source, now)
        if reasons:
            source.update(key=f"source:{source['source_id']}", legacy=False, reasons=reasons)
            source_rows.append(source)
    for status in legacy_sources.statuses(db):
        if not status["enabled"] or not status["configured"]:
            continue
        reasons = _source_reasons(status, now)
        if reasons:
            # Deliberate whitelist: the health DTO has safe error codes and a
            # settings_path, but neither error payloads nor URLs belong here.
            # Legacy sources are not multi-instance IDs; never invent one.
            source_rows.append({
                "key": f"legacy:{status['key']}", "legacy": True,
                **{field: status[field] for field in (
                    "provider", "name", "last_status", "last_sync_at", "last_attempt_at", "records_count",
                )},
                "reasons": reasons,
            })
    source_rows.sort(key=lambda source: (source["name"].casefold(), source["key"]))
    counts["sources"] = len(source_rows)
    queues["sources"] = source_rows[:selected("sources")]
    more["sources"] = counts["sources"] > len(queues["sources"])

    # Only explicit record absence counts. An error, timeout, partial run or
    # old successful snapshot must never turn a retained holding unavailable.
    query("unavailable", "source_records r JOIN items_live i ON i.id = r.item_id "
          "JOIN source_instances s ON s.id = r.source_id", "r.availability = 'unavailable'",
          f"{_ITEM_COLUMNS}, r.id AS record_id, r.source_id, s.name AS source_name, "
          "r.last_seen_at, s.last_sync_at, s.last_attempt_at",
          "s.name COLLATE NOCASE, s.id, i.title COLLATE NOCASE, i.id, r.id")

    review = connection_suggestions.review(db, connection_suggestions.MAX_SUGGESTIONS)
    counts["connections"] = len(review["suggestions"])
    queues["connections"] = review["suggestions"][:selected("connections")]
    more["connections"] = counts["connections"] > len(queues["connections"])
    dismissed = review["dismissed"][:selected("connections")]
    return {
        "category": category, "limit": limit, "counts": counts, "queues": queues,
        "has_more": more, "categories": CATEGORIES, "media_types": MEDIA_TYPES,
        "suggestions": queues["connections"], "dismissed_connections": dismissed,
        "dismissed_connection_count": len(review["dismissed"]),
        "connection_scan_limited": review["scan_limited"],
        "max_suggestions": connection_suggestions.MAX_SUGGESTIONS,
        "stale_after_days": STALE_AFTER_DAYS,
    }
