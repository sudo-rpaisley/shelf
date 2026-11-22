"""RomM configuration, platform selection, sync and cover ingestion.

RomM is a digital availability provider.  A synced game therefore remains a
service-backed catalogue row and is never inferred to be the same object as a
physical cartridge/disc by title/platform similarity.
"""

from __future__ import annotations

import json
import logging
import asyncio
import threading
import re
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException

from app.config import HTTP_TIMEOUT
from app.crypto import decrypt_value, encrypt_value, get_encryption_key
from app import config as app_config
from app.database import get_db, get_setting
from app.features import feature_enabled
from app.services import catalogue_security, covers, item_write, romm_client, romm_records

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]
COVER_WALL_TIMEOUT = 15.0
_sync_lock = threading.Lock()


def clean_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise romm_client.RomMError("RomM URL must use http:// or https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise romm_client.RomMError("RomM URL is invalid")
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
        url = _read(db, "romm_url")
        public_url = _read(db, "romm_public_url")
        token = _read_secret(db, "romm_token")
        legacy_token = _read_secret(db, "romm_api_token")
        if token and legacy_token and token != legacy_token:
            raise romm_client.RomMError("Conflicting saved RomM tokens; save the selected token explicitly")
        token = token or legacy_token
        try:
            excluded_raw = json.loads(_read(db, "romm_excluded_platforms") or "[]")
        except (json.JSONDecodeError, TypeError):
            excluded_raw = []
    excluded = {
        str(value) for value in excluded_raw
        if isinstance(value, (str, int)) and str(value).strip()
    } if isinstance(excluded_raw, list) else set()
    return {
        "url": url,
        "public_url": public_url,
        "token": token,
        "token_saved": bool(token),
        "excluded": excluded,
    }


def save_configuration(
    *,
    url: str,
    public_url: str = "",
    token: str = "",
    clear_token: bool = False,
) -> None:
    clean_server = clean_url(url)
    clean_public = clean_url(public_url) if public_url else ""
    with get_db() as db:
        _write(db, "romm_url", clean_server)
        _write(db, "romm_public_url", clean_public)
        _write_secret(db, "romm_token", token.strip(), clear=clear_token)
        _write_secret(db, "romm_api_token", token.strip(), clear=clear_token)


def save_platform_selection(rows: list[dict[str, Any]]) -> None:
    excluded: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise romm_client.RomMError("Invalid RomM platform selection")
        platform_id = str(row.get("id") or "").strip()
        included = row.get("included")
        if not platform_id or platform_id in seen or not isinstance(included, bool):
            raise romm_client.RomMError("Invalid RomM platform selection")
        seen.add(platform_id)
        if not included:
            excluded.append(platform_id)
    with get_db() as db:
        _write(db, "romm_excluded_platforms", json.dumps(sorted(excluded)))


async def discover_platforms(
    *, url: str | None = None, token: str | None = None
) -> list[dict[str, Any]]:
    config = configuration()
    server = clean_url(url) if url else clean_url(config["url"])
    auth = str(token or config["token"] or "").strip()
    if not server or not auth:
        raise romm_client.RomMError("RomM URL and API token are required")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        platforms = await romm_client.fetch_platforms(client, server, auth)
    return [
        {**row, "included": row["id"] not in config["excluded"]}
        for row in platforms
    ]


def _cover_target(server: str, cover_url: str | None) -> str | None:
    raw = str(cover_url or "").strip()
    if not raw:
        return None
    try:
        base = clean_url(server)
        server_parts = urlsplit(base)
        target = urljoin(base + "/", raw)
        target_parts = urlsplit(target)
        # Reject ambiguous URL syntax before httpx can normalize it differently.
        if (not server_parts.hostname or not target_parts.hostname or "\\" in target
                or any(ord(char) < 33 for char in target)
                or target_parts.username is not None or target_parts.password is not None):
            return None
        # Provider metadata never grants authority for an external cover request,
        # even if it would be sent without the RomM token. Redirects stay disabled.
        if ((target_parts.scheme, target_parts.hostname, target_parts.port)
                != (server_parts.scheme, server_parts.hostname, server_parts.port)):
            return None
    except (ValueError, romm_client.RomMError):
        return None
    return target


class _CoverRefused(ValueError):
    """An in-flight cover no longer has its original catalogue authority."""


class RomMCoverError(RuntimeError):
    """An operational cover failure, distinct from an expected missing/refusal."""


def _cover_actor() -> dict | None:
    try:
        return catalogue_security.current_actor()
    except RuntimeError as exc:
        # Only the native actor lookup is translated here, not a writer's or
        # filesystem's RuntimeError. A copied expired scope must never fall
        # through to the trusted actor=None background-job path.
        raise _CoverRefused("RomM cover request scope has ended") from exc


def _cover_principal_matches(db, captured: dict | None) -> bool:
    if _cover_actor() != captured:
        return False
    if captured is None:
        return True  # Trusted background sync, not an HTTP bypass.
    account = db.execute(
        "SELECT username, role, token_version FROM users WHERE id = ?", (captured["id"],),
    ).fetchone()
    return bool(account is not None
                and captured.get("account_role", captured.get("role")) == account["role"]
                and ("username" not in captured or str(captured["username"]).casefold() == account["username"].casefold())
                and ("token_version" not in captured or captured["token_version"] == account["token_version"]))


async def _ingest_cover(
    client: httpx.AsyncClient,
    server: str,
    token: str,
    item_id: int,
    cover_url: str | None,
    romm_id: str | None = None,
    platform_id: str | None = None,
) -> bool:
    target = _cover_target(server, cover_url)
    if not target:
        return False

    def binding_for(db):
        try:
            return romm_records.cover_binding(db, item_id, romm_id, platform_id)
        except (HTTPException, romm_records.RomMPersistenceError) as exc:
            # Only exact-binding/authority reads are expected refusals. Do not
            # hide a canonical writer or publisher failure with this adapter.
            raise _CoverRefused("RomM cover identity is unavailable") from exc

    try:
        principal = _cover_actor()
        principal = dict(principal) if principal is not None else None
        with get_db() as db, item_write._write_scope(db):
            binding = binding_for(db)
            if binding is None or not _cover_principal_matches(db, principal):
                return False
        with covers.CoverStage(item_id) as stage:
            async with asyncio.timeout(COVER_WALL_TIMEOUT):
                response = await client.get(
                    target, headers={"Authorization": f"Bearer {token}", "Accept": "image/*"},
                    timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
                )
            if response.status_code == 404 or 300 <= response.status_code < 400:
                return False
            if response.status_code != 200:
                raise RomMCoverError(f"RomM cover request returned HTTP {response.status_code}")
            if len(response.content) > covers.MAX_COVER_SIZE:
                return False
            path = covers.stage_uploaded_cover(stage, response.content,
                                                content_type=response.headers.get("content-type"))
            if not path or not stage.owns(path):
                return False
            pending = stage.path_for(PurePosixPath(path).suffix)

            def authorize(db):
                if (not _cover_principal_matches(db, principal)
                        or binding_for(db) != binding):
                    raise _CoverRefused("RomM cover target changed")
                if (pending.is_symlink() or not pending.is_file()
                        or not covers.MIN_COVER_SIZE <= pending.stat().st_size <= covers.MAX_COVER_SIZE):
                    raise _CoverRefused("RomM cover stage is unavailable")
                content = pending.read_bytes()
                detected = covers._image_format(content)
                if (content != response.content or not covers.validate_uploaded_cover(content)
                        or detected is None or detected[0] != pending.suffix):
                    raise _CoverRefused("RomM cover stage is invalid")

            def store(db, published_path):
                item_write.update_item_fields(db, item_id, {"cover_path": published_path})

            # Expired request scopes also refuse before get_db installs its
            # actor-aware views. The callback repeats all checks under lock.
            if _cover_actor() != principal:
                raise _CoverRefused("RomM cover principal changed")
            with get_db() as db:
                covers.publish_staged_cover(db, stage, path, authorize=authorize, store=store)
        return True
    except _CoverRefused:
        return False


async def _drain_cover_batch(jobs: list[tuple], stats: dict) -> None:
    if not jobs:
        return
    batch = list(jobs)
    jobs.clear()
    results = await asyncio.gather(*(_ingest_cover(*job) for job in batch), return_exceptions=True)
    stats["covers"] = stats.get("covers", 0) + sum(result is True for result in results)
    stats["cover_errors"] = stats.get("cover_errors", 0) + sum(isinstance(result, BaseException) for result in results)
    for job, result in zip(batch, results):
        if isinstance(result, BaseException):
            # Transport/IO exceptions can contain credential-bearing URLs or
            # server text. Keep the diagnostic useful without logging either.
            logger.warning("RomM cover ingestion failed for item %s (%s)", job[3], type(result).__name__)


async def sync(on_progress: ProgressCallback | None = None, *, url: str | None = None,
               token: str | None = None) -> dict:
    """Only active RomM importer; the old public sync API delegates here."""
    if not feature_enabled("romm"):
        return {"error": "RomM is disabled"}
    if not _sync_lock.acquire(blocking=False):
        return {"error": "RomM sync is already running"}
    try:
        return await _sync(on_progress, url=url, token=token)
    finally:
        _sync_lock.release()


async def _sync(on_progress: ProgressCallback | None = None, *, url: str | None = None,
                token: str | None = None) -> dict[str, int]:
    config = configuration()
    server = clean_url(url if url is not None else config["url"])
    token = str(token if token is not None else config["token"] or "").strip()
    if not server or not token:
        raise romm_client.RomMError("RomM URL and API token are required")

    stats = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "in_trash": 0,
             "errors": 0, "incomplete_platforms": 0, "covers": 0, "cover_errors": 0}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        platforms = await romm_client.fetch_platforms(client, server, token)
        selected = [row for row in platforms if row["id"] not in config["excluded"]]
        total = sum(
            row["rom_count"] for row in selected
            if isinstance(row.get("rom_count"), int) and row["rom_count"] >= 0
        )
        current = 0
        cover_jobs = []
        icon_slugs = {row["shelf_platform"]: row["romm_slug"] for row in selected
                      if re.fullmatch(r"[a-z0-9][a-z0-9-]*", row.get("romm_slug") or "")}
        with get_db() as db:
            _write(db, "romm_platform_icon_slugs", json.dumps(icon_slugs, sort_keys=True))
        if on_progress:
            await on_progress(0, total, "Syncing RomM library…", "discovering")
        for platform in selected:
            try:
                if on_progress:
                    await on_progress(current, max(total, current), f"Fetching {platform['name']}", "discovering")
                async for candidate in romm_client.iter_rom_candidates(
                    client, server, token, platform
                ):
                    current += 1
                    title = str(candidate.get("title") or "RomM game")
                    try:
                        with get_db() as db:
                            result = romm_records.persist_candidate(db, candidate)
                        action = result["action"]
                        stats[action if action in stats else "updated"] += 1
                        # No cover for a candidate left in Trash (G85) —
                        # same reasoning as komga_sync.
                        if action != "in_trash":
                            cover_jobs.append((client, server, token, result["item_id"], candidate.get("cover_url"),
                                               candidate["romm_id"], candidate["romm_platform_id"]))
                        status = action
                    except (romm_records.RomMPersistenceError, ValueError):
                        logger.warning("Skipping unsafe RomM candidate %r", candidate.get("romm_id"), exc_info=True)
                        stats["skipped"] += 1
                        status = "skipped"
                    except Exception:
                        logger.exception("Failed to persist RomM candidate %r", candidate.get("romm_id"))
                        stats["errors"] += 1
                        status = "error"
                    if on_progress is not None:
                        await on_progress(current, max(total, current), title, status)
                    if len(cover_jobs) >= 8:
                        await _drain_cover_batch(cover_jobs, stats)
            except romm_client.RomMError:
                logger.exception("Could not read RomM platform %s", platform["id"])
                stats["errors"] += 1
                stats["incomplete_platforms"] += 1
        await _drain_cover_batch(cover_jobs, stats)
    from app.services import media_groups
    with get_db() as db:
        media_groups.auto_link_family(db, "game")
    stats["added"] = stats["created"]
    return stats


def item_action(item_id: int) -> str | None:
    if not feature_enabled("romm"):
        return None
    config = configuration()
    if not config["url"]:
        return None
    with get_db() as db:
        records = romm_records.records_for_item(db, item_id)
        row = db.execute("SELECT romm_id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        selected = row["romm_id"] if row and row["romm_id"] else None
        if not selected and len(records) == 1:
            selected = records[0]["romm_id"]
        if selected:
            # Physical identity lookup only, to refuse hidden collisions.
            attachments = db.execute(
                "SELECT id AS item_id FROM items WHERE romm_id = ? UNION "
                "SELECT item_id FROM romm_records WHERE romm_id = ? UNION "
                "SELECT item_id FROM digital_holdings WHERE provider = 'romm' AND external_id = ?",
                (selected, selected, selected),
            ).fetchall()
            if any(record["item_id"] != item_id for record in attachments):
                return None
    if not selected:
        return None
    return romm_client.browser_rom_url(
        config["url"], selected, public_url=config["public_url"] or None
    )
