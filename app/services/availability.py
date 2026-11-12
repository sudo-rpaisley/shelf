"""Read-only, cached availability across physical and connected holdings.

Catalogue ownership is not evidence of a physical copy. Only ``copies_live``
supplies physical holdings, and only format edges connect equivalent editions.
No resolver performs a network request, refresh, backfill or catalogue write.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from app import features
from app.config import MEDIA_TYPES
from app.database import get_setting
from app.services import audiobookshelf, format_groups, komga_libraries, komga_sync, legacy_sources, romm_client, romm_sync


_STATES = frozenset({"available", "lent", "missing", "unavailable", "unknown"})
_FAILED_STATUSES = frozenset({
    "error", "failed", "failure", "partial", "offline", "unavailable", "unreachable",
    "unauthorized", "timeout",
})
_PROVIDER_FEATURES = {
    "abs": "abs_sync", "audiobookshelf": "abs_sync", "abs_sync": "abs_sync",
    "komga": "komga", "romm": "romm",
}
_ACTIONS = {
    "book": "Read", "ebook": "Read", "comic": "Read", "manga": "Read",
    "magazine": "Read", "audiobook": "Listen", "dvd": "Watch",
    "digital_video": "Watch", "tv_series": "Watch",
    "video_game": "Play", "vinyl": "Listen", "cassette": "Listen",
    "cd": "Listen", "digital_music": "Listen",
}


def _credential_query(value: str) -> bool:
    """Do not turn a stored authentication-bearing URL into a viewer link."""
    for key, _value in parse_qsl(value.replace(";", "&"), keep_blank_values=True):
        key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if key in {"key", "auth", "authentication", "authcode", "authkey", "jwt", "user",
                   "username", "login", "sig", "signature", "session"}:
            return True
        if any(part in key for part in (
            "token", "password", "passwd", "secret", "credential", "authorization", "apikey",
        )):
            return True
    return False


def _safe_url(value: str | None, *, base: str | None = None, root: bool = False) -> str | None:
    """Allow browser HTTP(S) URLs, not userinfo, credentials or unsafe schemes.

    LAN and loopback hosts are intentional for self-hosted integrations (G12).
    Relative record links are resolved only against a validated configured root.
    Harmless query parameters and SPA fragments remain usable.
    """
    text = str(value or "").strip()
    if not text or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text) or "\\" in text:
        return None
    try:
        parts = urlsplit(text)
        if not parts.scheme and not parts.netloc:
            if root or not base or text.startswith("//"):
                return None
            safe_base = _safe_url(base, root=True)
            if not safe_base:
                return None
            text = urljoin(safe_base.rstrip("/") + "/", text)
            parts = urlsplit(text)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        # Accessing port also validates malformed/out-of-range configured ports.
        parts.port
        if parts.username is not None or parts.password is not None:
            return None
        if root and (parts.query or parts.fragment):
            return None
        fragment_query = parts.fragment.partition("?")[2] if "?" in parts.fragment else parts.fragment
        if _credential_query(parts.query) or _credential_query(fragment_query):
            return None
    except ValueError:
        return None
    return text.rstrip("/") if root else text


def legacy_url(server, public, external_id, builder) -> str | None:
    """Credential-free, encoded browser URL shared by new and legacy panels."""
    root = _safe_url(public or server, root=True)
    if not root or not str(external_id or "").strip():
        return None
    # Existing builders interpolate the id as a path segment. Quote it before
    # calling them so provider metadata cannot inject a query or fragment.
    return _safe_url(builder(root, quote(str(external_id).strip(), safe="")))


def _row(item, *, source_key, source_name, kind, state, location=None,
         url=None, last_seen=None, detail=None, action=None) -> dict:
    return {
        "item_id": item["id"],
        "item_title": item["title"],
        "media_type": item["media_type"],
        "source_key": source_key,
        "source_name": source_name,
        "kind": kind,
        "state": state,
        "location": location,
        "url": url,
        "action": (action or _ACTIONS.get(item["media_type"], "Open")) if url else None,
        "last_seen": last_seen,
        "detail": detail,
    }


def _item_ids(values: Iterable[int]) -> list[int]:
    ids = []
    seen = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**63 - 1:
            raise ValueError("Item IDs must be positive integers")
        if value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _chunks(db, ids):
    size = min(400, db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for start in range(0, len(ids), size):
        chunk = ids[start:start + size]
        yield chunk, ",".join("?" for _ in chunk)


def format_item_ids(db, item_id: int) -> list[int]:
    """The live, undirected format-only component, including the starting item.

    A trashed intermediate edition cannot bridge two live editions. ``UNION``
    rather than ``UNION ALL`` terminates cycles without a depth/size truncation.
    """
    return format_groups.format_ids(db, item_id)


def _physical_rows(db, items, result) -> None:
    for ids, placeholders in _chunks(db, list(items)):
        copies = db.execute(
            f"""SELECT c.item_id, c.copy_number, c.missing_since, l.name AS location,
                       EXISTS (SELECT 1 FROM checkouts co
                               WHERE co.item_id = c.item_id AND co.checked_in IS NULL) AS lent
                FROM copies_live c LEFT JOIN locations l ON l.id = c.location_id
                WHERE c.item_id IN ({placeholders}) ORDER BY c.item_id, c.copy_number, c.id""",
            ids,
        ).fetchall()
        for copy in copies:
            state = "missing" if copy["missing_since"] else "lent" if copy["lent"] else "available"
            detail = f"Copy {copy['copy_number']}"
            if state == "missing":
                detail += f" · Missing since {copy['missing_since']}"
            elif state == "lent":
                # Checkouts are item-level, not copy-level. Do not claim that an
                # unidentified sibling is available while an item loan is open.
                detail += " · Item is on loan; the loan does not identify a copy."
            result[copy["item_id"]].append(_row(
                items[copy["item_id"]], source_key="physical", source_name="Physical copy",
                kind="physical", state=state, location=copy["location"], detail=detail,
            ))


def _existing_rows(db, items, result, enabled) -> None:
    health = {row["key"]: row for row in legacy_sources.statuses(db)}
    abs_items = [item for item in items.values() if item["abs_id"]]
    if enabled["abs_sync"] and abs_items:
        server = get_setting(db, "abs_url")
        public = get_setting(db, "abs_public_url")
        for item in abs_items:
            url = legacy_url(server, public, item["abs_id"], audiobookshelf.get_playback_url)
            result[item["id"]].append(_row(
                item, source_key="abs", source_name="Audiobookshelf", kind="digital",
                state="available" if url and health["abs"]["last_status"] not in {"error", "partial"} else "unknown", url=url,
                # abs_last_sync is a scheduler attempt time, not a successful
                # per-item observation. Neither it nor item edits prove last seen.
                detail=("Last source sync failed or was partial; cached holding retained."
                        if health["abs"]["last_status"] in {"error", "partial"}
                        else "Cached Audiobookshelf holding; no live connection check."),
            ))

    providers = (
        ("komga", "Komga", "komga_records", "komga_id", komga_sync.configuration,
         komga_libraries.browser_book_url),
        ("romm", "RomM", "romm_records", "romm_id", romm_sync.configuration,
         romm_client.browser_rom_url),
    )
    for key, name, table, id_column, configuration, builder in providers:
        if not enabled[key]:
            continue
        config = None
        for ids, placeholders in _chunks(db, list(items)):
            records = db.execute(
                f"SELECT r.item_id, r.{id_column} AS external_id, r.updated_at "
                f"FROM {table} r JOIN items_live i ON i.id = r.item_id "
                f"WHERE r.item_id IN ({placeholders}) ORDER BY r.item_id, r.{id_column}",
                ids,
            ).fetchall()
            if records and config is None:
                config = configuration()
            for record in records:
                url = legacy_url(config["url"], config["public_url"], record["external_id"], builder)
                result[record["item_id"]].append(_row(
                    items[record["item_id"]], source_key=key, source_name=name, kind="digital",
                    state="available" if url and health[key]["last_status"] not in {"error", "partial"} else "unknown", url=url,
                    last_seen=record["updated_at"],
                    detail=("Last source sync failed or was partial; cached holding retained."
                            if health[key]["last_status"] in {"error", "partial"}
                            else "Cached holding; no live connection check."),
                ))


def _source_rows(db, items, result, enabled) -> None:
    for ids, placeholders in _chunks(db, list(items)):
        records = db.execute(
            "SELECT r.item_id, r.source_id, r.open_url, r.availability, r.last_seen_at, "
            "r.media_type AS record_media_type, s.name AS source_name, s.provider, "
            "s.base_url, s.browser_url, s.enabled, s.last_status, s.last_error "
            "FROM source_records r JOIN source_instances s ON s.id = r.source_id "
            "JOIN items_live i ON i.id = r.item_id "
            f"WHERE r.item_id IN ({placeholders}) "
            "ORDER BY s.name COLLATE NOCASE, s.id, r.id",
            ids,
        ).fetchall()
        for record in records:
            feature = _PROVIDER_FEATURES.get(str(record["provider"] or "").strip().casefold())
            if feature and not enabled[feature]:
                continue
            state = record["availability"] if record["availability"] in _STATES else "unknown"
            detail = "Cached source record; no live connection check."
            failed = (str(record["last_status"] or "").strip().casefold() in _FAILED_STATUSES
                      or bool(record["last_error"]))
            if not record["enabled"] or failed:
                # An explicit absence remains an absence. A timeout or disabled
                # source is never evidence that a previously held record vanished.
                if state != "unavailable":
                    state = "unknown"
                detail = ("Source is disabled; cached record retained." if not record["enabled"]
                          else "Last source sync failed; cached record retained.")
            url = None
            if record["enabled"]:
                url = _safe_url(record["open_url"], base=record["browser_url"] or record["base_url"])
            result[record["item_id"]].append(_row(
                items[record["item_id"]], source_key=f"source:{record['source_id']}",
                source_name=record["source_name"], kind="digital", state=state, url=url,
                last_seen=record["last_seen_at"], detail=detail,
                action=_ACTIONS.get(record["record_media_type"]),
            ))


def availability_for_items(db, item_ids: Iterable[int]) -> dict[int, list[dict]]:
    """Resolve requested live items only, including live items with no holdings.

    All collection queries are bounded by SQLite's parameter limit. Returned
    dictionaries contain display metadata only, never source credentials, raw
    configuration, provider JSON or exception/error text.
    """
    ids = _item_ids(item_ids)
    items = {}
    for chunk, placeholders in _chunks(db, ids):
        rows = db.execute(
            f"SELECT id, title, media_type, abs_id FROM items_live WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        items.update((row["id"], dict(row)) for row in rows)
    result = {item_id: [] for item_id in ids if item_id in items}
    if not result:
        return result
    enabled = {key: features.feature_enabled(key) for key in ("abs_sync", "komga", "romm")}
    _physical_rows(db, items, result)
    _existing_rows(db, items, result, enabled)
    _source_rows(db, items, result, enabled)
    return result


def available_here(db, item_id: int) -> dict | None:
    """Template context for a live edition and all its format-only holdings."""
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    availability = availability_for_items(db, format_item_ids(db, item_id))
    rows = [row for holdings in availability.values() for row in holdings]
    rows.sort(key=lambda row: (
        row["item_id"] != item_id, row["item_title"].casefold(), row["media_type"],
        row["item_id"], row["kind"] != "physical", row["source_name"].casefold(),
    ))
    return {"item": dict(item), "rows": rows, "media_types": MEDIA_TYPES}
