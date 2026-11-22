"""Audiobookshelf sync, exact identity reconciliation and related-book adapters."""

import asyncio
import json
import logging
import threading
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException

from app.database import get_db
from app.features import feature_enabled
from app.services import catalogue_security, covers, holdings, isbn as isbn_svc, libraries, media_groups, series_memberships as series_memberships_svc
from app.services.catalogue_security import require_item_role
from app.services.item_write import IdentifierInTrash, ItemValueError, _write_scope, insert_item, update_item_fields

logger = logging.getLogger(__name__)
_sync_lock = threading.Lock()
_series_enricher = None


def set_series_enricher(callback) -> None:
    global _series_enricher
    _series_enricher = callback


def validate_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return "URL must use http:// or https://"
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return "Invalid URL"
        parsed.port  # Validate malformed ports before any request.
    except (ValueError, TypeError):
        return "Invalid URL"
    return None


def get_excluded_libraries() -> set[str]:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = 'abs_excluded_libraries'").fetchone()
    try:
        values = json.loads(row["value"]) if row and row["value"] else []
    except (ValueError, TypeError):
        values = []
    return {str(value) for value in values} if isinstance(values, list) else set()


def _normalise_publish_year(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _series_memberships(metadata: dict) -> list[dict]:
    return series_memberships_svc.normalise(metadata.get("series")) or series_memberships_svc.normalise(metadata.get("seriesName"))


def _attachment(db, abs_id: str):
    # Physical identity reads deliberately include Trash/hidden attachments.
    rows = db.execute(
        "SELECT id FROM items WHERE abs_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'audiobookshelf' AND external_id = ?",
        (abs_id, abs_id),
    ).fetchall()
    if len(rows) > 1:
        raise ItemValueError("Audiobookshelf identity has conflicting catalogue attachments")
    if not rows:
        return None
    item = db.execute("SELECT * FROM items WHERE id = ?", (rows[0][0],)).fetchone()
    if item is None:
        raise ItemValueError("Audiobookshelf identity has an incomplete attachment")
    if item["deleted_at"] is None:
        require_item_role(db, item["id"], "editor")
    return item


def persist_candidate(db, fields: dict, memberships=()) -> dict:
    """One transaction owns the retained ABS inline ID and digital holding."""
    with _write_scope(db):
        return _persist_candidate(db, fields, memberships)


def _persist_candidate(db, fields: dict, memberships) -> dict:
    fields = dict(fields)
    abs_id, library_id = fields.get("abs_id"), fields.get("abs_library_id")
    if not abs_id or not library_id or not fields.get("title"):
        raise ItemValueError("Audiobookshelf candidate needs its id, library and title")
    existing = _attachment(db, str(abs_id))
    if existing is not None and existing["deleted_at"] is not None:
        return {"item_id": existing["id"], "action": "in_trash"}
    if existing is not None:
        # Exact historical attachment wins over new format evidence. No
        # physical edition is detached or reclassified by a machine migration.
        fields["media_type"] = existing["media_type"]
    elif fields.get("media_type") not in {"audiobook", "ebook"}:
        raise ItemValueError("Audiobookshelf item needs explicit audio or ebook format evidence")
    if existing is None and fields.get("isbn"):
        matches = db.execute(
            "SELECT * FROM items_live WHERE isbn = ? AND media_type = ?",
            (fields["isbn"], fields["media_type"]),
        ).fetchall()
        if len(matches) > 1:
            raise ItemValueError("Audiobookshelf ISBN matches more than one edition")
        if matches:
            existing = matches[0]
            require_item_role(db, existing["id"], "editor")
            claimed = db.execute(
                "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider = 'audiobookshelf' LIMIT 1",
                (existing["id"],),
            ).fetchone()
            if existing["abs_id"] or claimed:
                return {"item_id": existing["id"], "action": "skipped",
                        "reason": "ISBN conflict: edition already has an Audiobookshelf attachment"}
    if existing is None:
        item_id = insert_item(db, {**fields, "series_memberships": list(memberships), "source": "audiobookshelf"},
                              restore_trashed=False)
        action = "added"
        fetch_cover = True
    else:
        item_id = existing["id"]
        # An inline field cannot represent every holding. Keep its original
        # representative when this exact ID came from a plural holding instead.
        other_holding = db.execute(
            "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider = 'audiobookshelf' "
            "AND external_id != ? LIMIT 1", (item_id, str(abs_id)),
        ).fetchone()
        if ((existing["abs_id"] and existing["abs_id"] != abs_id)
                or (not existing["abs_id"] and other_holding)):
            fields.pop("abs_id", None)
            fields.pop("abs_library_id", None)
        if fields.get("asin") is None:
            fields.pop("asin", None)
        changed = {key: value for key, value in fields.items() if existing[key] != value}
        if changed:
            update_item_fields(db, item_id, changed)
        series_memberships_svc.add_metadata_memberships(db, item_id, memberships)
        action = "updated" if changed else "unchanged"
        fetch_cover = not existing["cover_path"]
    # Preserve the exact holding even when a different inline representative
    # already exists. No delete/recreate: local holding IDs and metadata survive.
    db.execute(
        "INSERT INTO digital_holdings (item_id, provider, external_id, library_id) VALUES (?, 'audiobookshelf', ?, ?) "
        "ON CONFLICT(item_id, provider, external_id) DO UPDATE SET library_id = excluded.library_id",
        (item_id, str(abs_id), str(library_id)),
    )
    holdings.sync_item(db, item_id)
    return {"item_id": item_id, "action": action, "fetch_cover": fetch_cover}


async def fetch_library_items(client, abs_url: str, abs_token: str, library_id: str) -> list[dict]:
    """Complete, bounded discovery shared by sync and explicit library cleanup."""
    if validate_url(abs_url) or not str(library_id or "").strip():
        raise ValueError("Invalid Audiobookshelf library endpoint")
    rows, seen = [], set()
    for page in range(10000):
        response = await client.get(
            f"{abs_url.rstrip('/')}/api/libraries/{quote(str(library_id), safe='')}/items",
            headers={"Authorization": f"Bearer {abs_token}"}, params={"limit": 10000, "page": page},
        )
        if response.status_code != 200:
            raise ValueError("ABS library request failed")
        payload = response.json()
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("ABS library returned invalid items")
        fresh = [row for row in results if isinstance(row, dict) and str(row.get("id") or "") not in seen]
        if results and not fresh:
            raise ValueError("ABS pagination made no progress")
        rows.extend(fresh)
        seen.update(str(row.get("id") or "") for row in fresh)
        total = payload.get("total")
        if type(total) is int and len(rows) < total:
            if not results:
                raise ValueError("ABS library listing is incomplete")
            continue
        if type(total) is not int and len(results) >= 10000:
            continue
        return rows
    raise ValueError("ABS pagination exceeded the safe bound")


def _cover_binding(db, item_id: int, abs_id: str):
    item = _attachment(db, str(abs_id))
    if item is None or item["id"] != item_id or item["deleted_at"] is not None:
        raise ItemValueError("Audiobookshelf cover target is no longer available")
    principal = catalogue_security.current_actor()
    if principal is not None:
        account = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (principal["id"],)).fetchone()
        if (account is None or account["role"] != principal.get("account_role", principal.get("role"))
                or ("username" in principal and str(principal["username"]).casefold() != account["username"].casefold())
                or ("token_version" in principal and account["token_version"] != principal["token_version"])):
            raise ItemValueError("Audiobookshelf cover authority is no longer available")
    if item["cover_path"]:
        return None
    holding = db.execute(
        "SELECT library_id FROM digital_holdings WHERE item_id = ? AND provider = 'audiobookshelf' AND external_id = ?",
        (item_id, str(abs_id)),
    ).fetchone()
    return (libraries.item_library_id(db, item_id), (item["abs_id"], item["abs_library_id"]),
            tuple(holding) if holding is not None else None)


async def _ingest_cover(client, abs_url: str, abs_token: str, item_id: int, abs_id: str) -> bool:
    """Publish only for the same live, authorized provider attachment after I/O."""
    async with asyncio.timeout(15):
        with get_db() as db, _write_scope(db):
            binding = _cover_binding(db, item_id, abs_id)
        if binding is None:
            return False
        with covers.CoverStage(item_id) as stage:
            response = await client.get(
                f"{abs_url}/api/items/{quote(abs_id, safe='')}/cover",
                headers={"Authorization": f"Bearer {abs_token}"},
                params={"width": 400}, follow_redirects=False,
            )
            if response.status_code != 200:
                return False
            path = covers.save_uploaded_cover(stage.download_id, response.content)
            if not path:
                return False

            def authorize(db):
                if _cover_binding(db, item_id, abs_id) != binding:
                    raise ItemValueError("Audiobookshelf cover attachment changed during download")

            def store(db, published):
                update_item_fields(db, item_id, {"cover_path": published})

            with get_db() as db:
                covers.publish_staged_cover(db, stage, path, authorize=authorize, store=store)
    return True


async def sync(abs_url: str, abs_token: str, on_progress=None) -> dict:
    if not feature_enabled("abs_sync"):
        return {"error": "Audiobookshelf sync is disabled"}
    if validate_url(abs_url):
        return {"error": "Invalid Audiobookshelf URL"}
    if not _sync_lock.acquire(blocking=False):
        return {"error": "Audiobookshelf sync is already running"}
    try:
        result = await _sync(abs_url.rstrip("/"), abs_token, on_progress)
        if not result.get("error") and _series_enricher is not None:
            try:
                await _series_enricher(abs_url.rstrip("/"), abs_token)
            except Exception:
                logger.warning("Audiobookshelf series enrichment failed", exc_info=True)
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("Audiobookshelf returned an incomplete response", exc_info=True)
        return {"error": "Audiobookshelf connection or response failed"}
    finally:
        _sync_lock.release()


async def _sync(abs_url: str, abs_token: str, on_progress=None) -> dict:
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    headers = {"Authorization": f"Bearer {abs_token}"}
    excluded = get_excluded_libraries()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{abs_url}/api/libraries", headers=headers)
        if response.status_code != 200:
            return {"error": f"Failed to connect: HTTP {response.status_code}"}
        libraries = response.json().get("libraries", [])
        if not isinstance(libraries, list):
            return {"error": "Audiobookshelf returned an invalid library list"}
        batches = []
        for library in libraries:
            if not isinstance(library, dict) or not library.get("id") or str(library["id"]) in excluded:
                continue
            try:
                rows = await fetch_library_items(client, abs_url, abs_token, str(library["id"]))
                batches.append((str(library["id"]), rows))
            except (httpx.HTTPError, ValueError, TypeError):
                stats["errors"] += 1
                logger.warning("Could not completely read Audiobookshelf library %s", library["id"], exc_info=True)
        total = sum(len(rows) for _, rows in batches)
        current = 0
        for library_id, rows in batches:
            for item in rows:
                current += 1
                media = item.get("media") if isinstance(item.get("media"), dict) else {}
                metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
                title = str(metadata.get("title") or "").strip()
                abs_id = str(item.get("id") or "").strip()
                if not title or not abs_id or item.get("libraryId") not in (None, library_id):
                    stats["skipped"] += 1
                    if on_progress:
                        await on_progress(current, total, title or abs_id or "Missing title or Audiobookshelf item ID", "skipped")
                    continue
                duration = media.get("duration")
                audio_count = media.get("numAudioFiles")
                has_audio = (type(duration) in (int, float) and duration > 0) or (type(audio_count) is int and audio_count > 0)
                media_type = "audiobook" if has_audio else "ebook" if media.get("ebookFormat") else None
                raw_isbn, raw_asin = metadata.get("isbn"), metadata.get("asin")
                pair = isbn_svc.canonical_isbn_pair(raw_isbn) if raw_isbn else None
                asin = isbn_svc.clean_asin(raw_asin)
                if raw_isbn and pair is None:
                    logger.warning("Audiobookshelf item %s: %r is not a valid ISBN — stored without one", abs_id, raw_isbn)
                if raw_asin and asin is None:
                    logger.warning("Audiobookshelf item %s: %r is not a valid ASIN — stored without one", abs_id, raw_asin)
                memberships = _series_memberships(metadata)
                fields = {
                    "title": title, "authors": metadata.get("authorName") or metadata.get("author"),
                    "narrator": metadata.get("narratorName"), "isbn": pair[0] if pair else None, "asin": asin,
                    "media_type": media_type, "publisher": metadata.get("publisher"),
                    "publish_year": _normalise_publish_year(metadata.get("publishedYear")),
                    "description": metadata.get("description"),
                    "series_name": memberships[0]["name"] if memberships else None,
                    "series_position": memberships[0]["position"] if memberships else None,
                    "duration_mins": int(duration / 60) if type(duration) in (int, float) and duration > 0 else None,
                    "abs_id": abs_id, "abs_library_id": library_id,
                }
                try:
                    with get_db() as db:
                        decision = persist_candidate(db, fields, memberships)
                    action = decision["action"]
                    stats[action] = stats.get(action, 0) + 1
                    if decision.get("reason"):
                        title = f"{title}: {decision['reason']}"
                except IdentifierInTrash:
                    stats["in_trash"] = stats.get("in_trash", 0) + 1
                    decision, action = {}, "in_trash"
                except ItemValueError as exc:
                    stats["errors"] += 1
                    decision, action = {}, "error"
                    title = f"{title}: {exc}"
                if on_progress:
                    await on_progress(current, total, title, action)
                if not decision.get("fetch_cover") or action == "in_trash":
                    continue
                try:
                    await _ingest_cover(client, abs_url, abs_token, decision["item_id"], abs_id)
                except (httpx.HTTPError, TimeoutError, OSError, ItemValueError, HTTPException):
                    logger.debug("Failed to download cover for ABS item %s", abs_id, exc_info=True)
    _auto_link_items()
    return stats


def get_playback_url(abs_url: str, abs_id: str, public_url: str | None = None) -> str:
    base = (public_url or abs_url).rstrip("/")
    return f"{base}/item/{quote(str(abs_id), safe='')}"


_normalize_title = media_groups.normalise_title
_authors_compatible = media_groups._authors_compatible


def auto_link(origin_ids) -> int:
    """Link origins only to different formats outside the given import set."""
    origins = frozenset(int(item_id) for item_id in origin_ids)
    with get_db() as db:
        return sum(media_groups.auto_link_item(db, item_id, strict_origin_ids=origins)
                   for item_id in sorted(origins))


def _auto_link_items() -> None:
    with get_db() as db:
        media_groups.auto_link_family(db, "book")
