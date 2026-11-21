"""Legacy RomM APIs backed by the one native sync and exact-identity writer."""

import json
import re
from urllib.parse import urlparse

from app.database import get_db, get_setting
from app.services import media_groups, romm_client, romm_records, romm_sync

PAGE_SIZE = romm_client.PAGE_SIZE
PAGE_RETRIES = romm_client.PAGE_RETRIES
PAGE_RETRY_BACKOFF = romm_client.PAGE_RETRY_BACKOFF
DB_BATCH_SIZE = 100
COVER_BATCH_SIZE = 8
COVER_WALL_TIMEOUT = 15.0
_headers = romm_client._headers
_publish_year = romm_client._publish_year
_publisher = romm_client._publisher
_normal_title = media_groups.normalise_title


def get_excluded_platforms() -> set[str]:
    return romm_sync.configuration()["excluded"]


def get_browser_url(romm_url: str, romm_id: str) -> str:
    with get_db() as db:
        public_url = get_setting(db, "romm_public_url")
    return romm_client.browser_rom_url(romm_url, str(romm_id), public_url=public_url or None)


def _platform_icon_slug(platform: dict) -> str | None:
    raw = str(platform.get("slug") or platform.get("fs_slug") or "").strip().lower()
    return raw if re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw) else None


def get_platform_icon_urls(romm_url: str) -> dict[str, str]:
    with get_db() as db:
        base = (get_setting(db, "romm_public_url") or romm_url or "").rstrip("/")
        raw = get_setting(db, "romm_platform_icon_slugs")
    if urlparse(base).scheme.lower() != "https":
        return {}
    try:
        mapping = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(mapping, dict):
        return {}
    return {key: f"{base}/assets/platforms/{value}.ico" for key, value in mapping.items()
            if isinstance(key, str) and isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", value)}


_HANDHELD_PLATFORMS = frozenset({
    "gb", "gbc", "gameboy", "gba", "nds", "3ds", "switch", "psp", "vita",
    "gamegear", "lynx", "ngp", "ngpc", "wonderswan", "wonderswancolor",
})
_COMPUTER_PLATFORMS = frozenset({
    "pc", "windows", "linux", "mac", "macos", "dos", "amiga", "c64",
    "c128", "msx", "spectrum", "acpc", "atarist", "x68000", "pc98",
})


def get_platform_icon_kind(platform: str | None) -> str:
    slug = (platform or "").casefold().replace("-", "").replace("_", "")
    if slug in _HANDHELD_PLATFORMS:
        return "handheld"
    if slug in _COMPUTER_PLATFORMS:
        return "computer"
    if "arcade" in slug or slug in {"mame", "fbneo", "finalburnneo"}:
        return "arcade"
    return "gamepad"


def _slugify(value: str) -> str:
    return romm_client._compact_slug(value)


def _platform_slug(db, platform: dict) -> str:
    return romm_records.ensure_platform(db, romm_client.shelf_platform_slug(platform),
                                      platform.get("display_name") or platform.get("name"))


def _load_adoption_candidates(shelf_platform: str) -> dict:
    """Title-only adoption is intentionally retired; it is not identity evidence."""
    return {}


def _lookup_existing_by_romm_ids(db, romm_ids: list[str]) -> dict:
    return {key: row for key in dict.fromkeys(romm_ids) if key
            and (row := romm_records._attachment(db, key)) is not None}


def _import_rom_batch(db, romm_url, roms, shelf_platform, platform_id, adoption_candidates, stats):
    """Historical batch API, using the same transaction adapter as native sync."""
    outcomes, cover_jobs = [], []
    platform = {"id": str(platform_id), "shelf_platform": shelf_platform, "name": shelf_platform}
    for rom in roms:
        candidate = romm_client.normalise_rom(rom, platform)
        if candidate is None:
            stats["skipped"] = stats.get("skipped", 0) + 1
            outcomes.append(("Unknown ROM", "skipped"))
            continue
        result = romm_records.persist_candidate(db, candidate)
        action = result["action"]
        status = "added" if action == "created" else action
        stats[status] = stats.get(status, 0) + 1
        row = db.execute("SELECT cover_path FROM items_live WHERE id = ?", (result["item_id"],)).fetchone()
        cover_url = romm_sync._cover_target(romm_url, candidate.get("cover_url"))
        if row and not row["cover_path"] and cover_url and action != "in_trash":
            cover_jobs.append((cover_url, result["item_id"]))
        outcomes.append((candidate["title"], status))
    return outcomes, cover_jobs


async def _fetch_platform_page(client, romm_url, token, platform_id, offset, *, with_total):
    try:
        page, total = await romm_client._fetch_page(
            client, romm_url, _headers(token), str(platform_id), offset, with_total=with_total,
        )
        return page, total, True
    except romm_client.RomMError:
        return None, None, False


def _cover_url(romm_url: str, rom: dict) -> str | None:
    return romm_sync._cover_target(romm_url, rom.get("path_cover_large") or rom.get("path_cover_small") or rom.get("url_cover"))


def _same_host(a: str, b: str) -> bool:
    aa, bb = urlparse(a), urlparse(b)
    return (aa.scheme, aa.hostname, aa.port) == (bb.scheme, bb.hostname, bb.port)


async def _download_cover(client, romm_url, token, cover_url, item_id) -> str:
    return "downloaded" if await romm_sync._ingest_cover(client, romm_url, token, item_id, cover_url) else "missing"


_download_cover_with_deadline = _download_cover


async def _drain_cover_batch(batch: list[tuple], stats: dict) -> None:
    jobs = [(client, url, token, item_id, cover_url) for client, url, token, cover_url, item_id in batch]
    batch.clear()
    await romm_sync._drain_cover_batch(jobs, stats)


async def sync(romm_url: str, token: str, on_progress=None) -> dict:
    async def legacy_progress(current, total, title, status):
        await on_progress(current, total, title, "added" if status == "created" else status)

    try:
        result = await romm_sync.sync(legacy_progress if on_progress is not None else None,
                                      url=romm_url, token=token)
    except romm_client.RomMError as exc:
        return {"error": str(exc)}
    if result.get("error"):
        return result
    result = dict(result)
    result["added"] = result.pop("created", 0)
    if not result.get("in_trash"):
        result.pop("in_trash", None)
    return result


def _auto_link_items() -> None:
    with get_db() as db:
        media_groups.auto_link_family(db, "game")
