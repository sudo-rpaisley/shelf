"""Komga configuration, library selection, sync and cover ingestion.

This is the integration boundary between the provider-normalisation helpers and
Shelf persistence. New imports are digital; exact historical attachments retain
their edition and source. A physical edition is not adopted merely by ISBN.
"""

from __future__ import annotations

import json
import logging
import asyncio
import threading
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from app.config import HTTP_TIMEOUT
from app.crypto import decrypt_value, encrypt_value, get_encryption_key
from app import config as app_config
from app.database import get_db, get_setting
from app.features import feature_enabled
from app.services import catalogue_security, covers, komga_books, komga_libraries, komga_records, libraries
from app.services.item_write import _write_scope, update_item_fields

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]
_sync_lock = threading.Lock()
_series_enricher = None
COVER_WALL_TIMEOUT = 15.0


class KomgaCoverError(ValueError):
    """A thumbnail failed, as distinct from a provider's ordinary 404 miss."""


def set_series_enricher(callback) -> None:
    global _series_enricher
    _series_enricher = callback


def clean_url(value: str | None) -> str:
    """Validate an admin-configured Komga HTTP(S) root and strip a trailing slash."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise komga_libraries.KomgaError("Komga URL must use http:// or https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise komga_libraries.KomgaError("Komga URL is invalid")
    return text


def _read(db, key: str) -> str:
    return get_setting(db, key)


def _write(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _read_secret(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    raw = str(row["value"] or "") if row else ""
    plain = decrypt_value(raw, get_encryption_key(), key_name=key) if raw else ""
    return app_config.get_setting_value(key, plain)


def _write_secret(db, key: str, value: str, *, clear: bool = False) -> None:
    if clear:
        _write(db, key, "")
    elif value:
        _write(db, key, encrypt_value(value, get_encryption_key()))


def configuration() -> dict[str, Any]:
    with get_db() as db:
        url = _read(db, "komga_url")
        public_url = _read(db, "komga_public_url")
        api_key = _read_secret(db, "komga_api_key")
        kinds = komga_libraries.parse_library_kinds(_read(db, "komga_library_kinds"))
        try:
            legacy = json.loads(_read(db, "komga_library_media_types") or "{}")
        except (ValueError, TypeError):
            legacy = {}
        if not isinstance(legacy, dict):
            legacy = {}
        for library_id, media_type in legacy.items():
            kind = {"digital_comic": "comic", "digital_manga": "manga"}.get(media_type)
            if kind is None:
                continue
            if str(library_id) in kinds and kinds[str(library_id)] != kind:
                raise komga_libraries.KomgaError("Conflicting Komga library classifications; select the library type explicitly")
            kinds[str(library_id)] = kind
        try:
            excluded_raw = json.loads(_read(db, "komga_excluded_libraries") or "[]")
        except (json.JSONDecodeError, TypeError):
            excluded_raw = []
    excluded = {
        str(value) for value in excluded_raw
        if isinstance(value, (str, int)) and str(value).strip()
    } if isinstance(excluded_raw, list) else set()
    return {
        "url": url,
        "public_url": public_url,
        "api_key": api_key,
        "api_key_saved": bool(api_key),
        "kinds": kinds,
        "excluded": excluded,
    }


def save_configuration(
    *,
    url: str,
    public_url: str = "",
    api_key: str = "",
    clear_api_key: bool = False,
) -> None:
    clean_server = clean_url(url)
    clean_public = clean_url(public_url) if public_url else ""
    with get_db() as db:
        _write(db, "komga_url", clean_server)
        _write(db, "komga_public_url", clean_public)
        _write_secret(db, "komga_api_key", api_key.strip(), clear=clear_api_key)


def save_library_selection(rows: list[dict[str, Any]]) -> None:
    kinds: dict[str, str] = {}
    excluded: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise komga_libraries.KomgaError("Invalid Komga library selection")
        library_id = str(row.get("id") or "").strip()
        kind = str(row.get("kind") or "").strip().casefold()
        included = row.get("included")
        if not library_id or library_id in seen or kind not in komga_libraries.LIBRARY_KINDS:
            raise komga_libraries.KomgaError("Invalid Komga library selection")
        if not isinstance(included, bool):
            raise komga_libraries.KomgaError("Invalid Komga library selection")
        seen.add(library_id)
        kinds[library_id] = kind
        if not included:
            excluded.append(library_id)
    with get_db() as db:
        _write(db, "komga_library_kinds", komga_libraries.dump_library_kinds(kinds))
        _write(db, "komga_library_media_types", json.dumps({
            library_id: f"digital_{kind}" for library_id, kind in kinds.items()
        }, sort_keys=True))
        _write(db, "komga_excluded_libraries", json.dumps(sorted(excluded)))


async def discover_libraries(
    *, url: str | None = None, api_key: str | None = None
) -> list[dict[str, Any]]:
    config = configuration()
    server = clean_url(url) if url else clean_url(config["url"])
    key = str(api_key or config["api_key"] or "").strip()
    if not server or not key:
        raise komga_libraries.KomgaError("Komga URL and API key are required")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        libraries = await komga_libraries.fetch_libraries(
            client, server, key, configured=config["kinds"]
        )
    excluded = config["excluded"]
    return [
        {**row, "included": row["id"] not in excluded}
        for row in libraries
    ]


def _cover_binding(db, item_id: int, komga_id: str):
    item = komga_records._attachment(db, str(komga_id))
    if item is None or item["id"] != item_id or item["deleted_at"] is not None:
        raise KomgaCoverError("Komga cover target is no longer available")
    principal = catalogue_security.current_actor()
    if principal is not None:
        account = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (principal["id"],)).fetchone()
        if (account is None or account["role"] != principal.get("account_role", principal.get("role"))
                or ("username" in principal and str(principal["username"]).casefold() != account["username"].casefold())
                or ("token_version" in principal and account["token_version"] != principal["token_version"])):
            raise KomgaCoverError("Komga cover authority is no longer available")
    if item["cover_path"]:
        return None
    record = db.execute("SELECT library_id, series_id, kind FROM komga_records WHERE komga_id = ?",
                        (str(komga_id),)).fetchone()
    holding = db.execute(
        "SELECT library_id FROM digital_holdings WHERE item_id = ? AND provider = 'komga' AND external_id = ?",
        (item_id, str(komga_id)),
    ).fetchone()
    return (libraries.item_library_id(db, item_id),
            (item["komga_id"], item["komga_library_id"], item["komga_series_id"]),
            tuple(record) if record is not None else None,
            tuple(holding) if holding is not None else None)


async def _ingest_cover(
    client: httpx.AsyncClient,
    server: str,
    api_key: str,
    item_id: int,
    komga_id: str,
) -> bool:
    # Only 404/already-covered is a miss. Transport/deadline/validation failures
    # propagate to the batch's per-cover error counter, without aborting siblings.
    async with asyncio.timeout(COVER_WALL_TIMEOUT):
        with get_db() as db, _write_scope(db):
            binding = _cover_binding(db, item_id, komga_id)
        if binding is None:
            return False
        with covers.CoverStage(item_id) as stage:
            response = await client.get(
                f"{server}/api/v1/books/{quote(str(komga_id), safe='')}/thumbnail",
                headers={"X-API-Key": api_key, "Accept": "image/jpeg"},
                timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
            )
            if response.status_code == 404:
                return False
            if response.status_code != 200:
                raise KomgaCoverError("Komga thumbnail request failed")
            path = covers.save_uploaded_cover(stage.download_id, response.content)
            if not path:
                raise KomgaCoverError("Komga thumbnail was not a valid cover")

            def authorize(db):
                if _cover_binding(db, item_id, komga_id) != binding:
                    raise KomgaCoverError("Komga cover attachment changed during download")

            def store(db, published):
                update_item_fields(db, item_id, {"cover_path": published})

            with get_db() as db:
                covers.publish_staged_cover(db, stage, path, authorize=authorize, store=store)
    return True


async def _drain_cover_batch(jobs: list[tuple], stats: dict) -> None:
    if not jobs:
        return
    results = await asyncio.gather(*(_ingest_cover(*job) for job in jobs), return_exceptions=True)
    jobs.clear()
    stats["covers"] = stats.get("covers", 0) + sum(result is True for result in results)
    stats["cover_errors"] = stats.get("cover_errors", 0) + sum(isinstance(result, BaseException) for result in results)


async def sync(on_progress: ProgressCallback | None = None, *, url: str | None = None,
               api_key: str | None = None) -> dict:
    """Only active Komga importer, shared by native and legacy entrypoints."""
    if not feature_enabled("komga"):
        return {"error": "Komga is disabled"}
    if not _sync_lock.acquire(blocking=False):
        return {"error": "Komga sync is already running"}
    try:
        return await _sync(on_progress, url=url, api_key=api_key)
    finally:
        _sync_lock.release()


async def _sync(on_progress: ProgressCallback | None = None, *, url: str | None = None,
                api_key: str | None = None) -> dict[str, int]:
    """Synchronise all included Komga libraries into Shelf."""
    config = configuration()
    server = clean_url(url if url is not None else config["url"])
    key = str(api_key if api_key is not None else config["api_key"] or "").strip()
    if not server or not key:
        raise komga_libraries.KomgaError("Komga URL and API key are required")

    stats = {"created": 0, "adopted": 0, "updated": 0, "unchanged": 0, "skipped": 0,
             "in_trash": 0, "errors": 0, "covers": 0, "cover_errors": 0}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        libraries = await komga_libraries.fetch_libraries(
            client, server, key, configured=config["kinds"]
        )
        selected = [row for row in libraries if row["id"] not in config["excluded"] and row["explicit_kind"]]
        stats["skipped"] += sum(1 for row in libraries if row["id"] not in config["excluded"] and not row["explicit_kind"])

        batches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for library in selected:
            try:
                candidates = await komga_books.fetch_library_candidates(
                    client,
                    server,
                    key,
                    library_id=library["id"],
                    kind=library["kind"],
                )
                batches.append((library, candidates))
            except komga_libraries.KomgaError:
                logger.exception("Could not read Komga library %s", library["id"])
                stats["errors"] += 1

        total = sum(len(candidates) for _, candidates in batches)
        current = 0
        cover_jobs = []
        for _library, candidates in batches:
            for candidate in candidates:
                current += 1
                title = str(candidate.get("title") or "Komga item")
                try:
                    with get_db() as db:
                        result = komga_records.persist_candidate(db, candidate)
                    action = result["action"]
                    if action in stats:
                        stats[action] += 1
                    else:
                        stats["updated"] += 1
                    # A candidate left in Trash gets no cover either: the
                    # skip must land before the item's first write (G85),
                    # and a cover is a write to the row the sync is meant to
                    # leave exactly as the user left it.
                    if action != "in_trash":
                        cover_jobs.append((client, server, key, result["item_id"], candidate["komga_id"]))
                    status = action
                except (komga_records.KomgaPersistenceError, ValueError):
                    logger.warning("Skipping unsafe Komga candidate %r", candidate.get("komga_id"), exc_info=True)
                    stats["skipped"] += 1
                    status = "skipped"
                except Exception:
                    logger.exception("Failed to persist Komga candidate %r", candidate.get("komga_id"))
                    stats["errors"] += 1
                    status = "error"
                if on_progress is not None:
                    await on_progress(current, total, title, status)
                if len(cover_jobs) >= 8:
                    await _drain_cover_batch(cover_jobs, stats)
        await _drain_cover_batch(cover_jobs, stats)
    from app.services import media_groups
    with get_db() as db:
        media_groups.auto_link_family(db, "comic")
        media_groups.auto_link_family(db, "manga")
    if _series_enricher is not None:
        try:
            await _series_enricher(server, key)
        except Exception:
            logger.warning("Komga series enrichment failed", exc_info=True)
    stats["added"] = stats["created"]
    return stats


def item_action(item_id: int) -> str | None:
    if not feature_enabled("komga"):
        return None
    config = configuration()
    if not config["url"]:
        return None
    with get_db() as db:
        records = komga_records.records_for_item(db, item_id)
        row = db.execute("SELECT komga_id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        selected = row["komga_id"] if row and row["komga_id"] else None
        if not selected and len(records) == 1:
            selected = records[0]["komga_id"]
        if selected:
            # Identity collision check includes hidden/Trash attachments;
            # it refuses without exposing the conflicting row.
            attachments = db.execute(
                "SELECT id AS item_id FROM items WHERE komga_id = ? UNION "
                "SELECT item_id FROM komga_records WHERE komga_id = ? UNION "
                "SELECT item_id FROM digital_holdings WHERE provider = 'komga' AND external_id = ?",
                (selected, selected, selected),
            ).fetchall()
            if any(record["item_id"] != item_id for record in attachments):
                return None
    if not selected:
        return None
    return komga_libraries.browser_book_url(
        config["url"], selected, public_url=config["public_url"] or None
    )
