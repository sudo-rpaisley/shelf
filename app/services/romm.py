"""RomM library sync.

RomM is authoritative for the digital copy: Shelf imports RomM metadata and
artwork as ``digital_game`` items, while physical ``video_game`` rows stay
separate and are linked by conservative title+platform matching.
"""

import asyncio
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.database import get_db, get_setting
from app.services import covers
from app.services.igdb import PLATFORM_IDS
from app.services.item_write import insert_item

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
DB_BATCH_SIZE = 100
PAGE_RETRIES = 3
PAGE_RETRY_BACKOFF = 1.0
COVER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
COVER_RETRIES = 1
COVER_BATCH_SIZE = 8
COVER_WALL_TIMEOUT = 15.0

_IGDB_TO_SHELF = {value: key for key, value in PLATFORM_IDS.items()}
_PLATFORM_ALIASES = {
    "atari-2600": "atari2600",
    "atari-5200": "atari5200",
    "atari-7800": "atari7800",
    "nintendo-entertainment-system": "nes",
    "super-nintendo-entertainment-system": "snes",
    "nintendo-64": "n64",
    "nintendo-gamecube": "gamecube",
    "nintendo-wii": "wii",
    "nintendo-wii-u": "wiiu",
    "nintendo-switch": "switch",
    "game-boy": "gameboy",
    "game-boy-advance": "gba",
    "nintendo-ds": "nds",
    "nintendo-3ds": "3ds",
    "mega-drive": "genesis",
    "megadrive": "genesis",
    "sega-genesis": "genesis",
    "sega-saturn": "saturn",
    "sega-dreamcast": "dreamcast",
    "playstation": "ps1",
    "playstation-1": "ps1",
    "psx": "ps1",
    "playstation-2": "ps2",
    "playstation-3": "ps3",
    "playstation-4": "ps4",
    "playstation-5": "ps5",
    "playstation-portable": "psp",
    "playstation-vita": "vita",
    "xbox-360": "xbox360",
    "xbox-one": "xboxone",
    "xbox-series-x-s": "xboxsx",
    "xbox-series": "xboxsx",
    "windows": "pc",
}


def get_excluded_platforms() -> set[str]:
    """RomM platform IDs the user has opted out of syncing."""
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'romm_excluded_platforms'"
        ).fetchone()
    if not row or not row["value"]:
        return set()
    try:
        values = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def get_browser_url(romm_url: str, romm_id: str) -> str:
    """Construct the browser-facing RomM game-detail URL."""
    with get_db() as db:
        public_url = get_setting(db, "romm_public_url")
    base = (public_url or romm_url).rstrip("/")
    return f"{base}/rom/{romm_id}"


def _platform_icon_slug(platform: dict) -> str | None:
    """RomM's icon filename stem for a platform, if it is path-safe."""
    raw = str(platform.get("slug") or platform.get("fs_slug") or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw):
        return raw
    return None


def get_platform_icon_urls(romm_url: str) -> dict[str, str]:
    """Browser-safe RomM platform icon URLs keyed by Shelf platform slug.

    RomM serves built-in and custom platform icons from /assets/platforms.
    Shelf only uses an HTTPS browser-facing root here: internal Docker/LAN HTTP
    URLs would be blocked as mixed content by Shelf's CSP and by modern browsers.
    """
    with get_db() as db:
        public_url = get_setting(db, "romm_public_url")
        raw_map = get_setting(db, "romm_platform_icon_slugs")
    base = (public_url or romm_url or "").rstrip("/")
    if urlparse(base).scheme.lower() != "https":
        return {}
    try:
        mapping = json.loads(raw_map or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(mapping, dict):
        return {}

    urls: dict[str, str] = {}
    for shelf_slug, icon_slug in mapping.items():
        if not isinstance(shelf_slug, str) or not isinstance(icon_slug, str):
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", icon_slug):
            continue
        urls[shelf_slug] = f"{base}/assets/platforms/{icon_slug}.ico"
    return urls


_HANDHELD_PLATFORMS = frozenset({
    "gb", "gbc", "gameboy", "gba", "nds", "3ds", "switch", "psp", "vita",
    "gamegear", "lynx", "ngp", "ngpc", "wonderswan", "wonderswancolor",
})
_COMPUTER_PLATFORMS = frozenset({
    "pc", "windows", "linux", "mac", "macos", "dos", "amiga", "c64",
    "c128", "msx", "spectrum", "acpc", "atarist", "x68000", "pc98",
})


def get_platform_icon_kind(platform: str | None) -> str:
    """Local fallback glyph when no RomM-specific icon can be displayed."""
    slug = (platform or "").casefold().replace("-", "").replace("_", "")
    if slug in {value.replace("-", "").replace("_", "") for value in _HANDHELD_PLATFORMS}:
        return "handheld"
    if slug in {value.replace("-", "").replace("_", "") for value in _COMPUTER_PLATFORMS}:
        return "computer"
    if "arcade" in slug or slug in {"mame", "fbneo", "finalburnneo"}:
        return "arcade"
    return "gamepad"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _platform_slug(db, platform: dict) -> str:
    """Map a RomM platform onto Shelf, creating an obscure platform if needed."""
    igdb_id = platform.get("igdb_id")
    if isinstance(igdb_id, int) and igdb_id in _IGDB_TO_SHELF:
        slug = _IGDB_TO_SHELF[igdb_id]
    else:
        raw = str(platform.get("slug") or platform.get("fs_slug") or "").strip().lower()
        slug = _PLATFORM_ALIASES.get(raw)
        if not slug:
            compact = _slugify(raw)
            compact_aliases = {
                "psx": "ps1",
                "playstation": "ps1",
                "playstation1": "ps1",
                "playstation2": "ps2",
                "playstation3": "ps3",
                "playstation4": "ps4",
                "playstation5": "ps5",
                "xbox360": "xbox360",
                "xboxone": "xboxone",
                "xboxseriesxs": "xboxsx",
                "megadrive": "genesis",
                "segagenesis": "genesis",
                "gameboy": "gameboy",
                "gameboyadvance": "gba",
                "nintendods": "nds",
                "nintendo3ds": "3ds",
            }
            slug = compact_aliases.get(compact, compact)

    if not slug:
        slug = f"romm{platform.get('id', 'platform')}"

    row = db.execute("SELECT slug FROM game_platforms WHERE slug = ?", (slug,)).fetchone()
    if not row:
        name = str(
            platform.get("display_name")
            or platform.get("custom_name")
            or platform.get("name")
            or platform.get("slug")
            or slug
        ).strip()
        db.execute(
            "INSERT INTO game_platforms (slug, name) VALUES (?, ?)",
            (slug, name or slug),
        )
    return slug


def _publish_year(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).year
    except (TypeError, ValueError, OSError, OverflowError):
        text = str(value).strip()
        if len(text) >= 4 and text[:4].isdigit():
            return int(text[:4])
        return None


def _publisher(metadata: dict) -> str | None:
    values = metadata.get("publishers") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None
    names = [str(value).strip() for value in values if str(value).strip()]
    return ", ".join(dict.fromkeys(names)) or None


def _normal_title(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"^(the|a|an)\s+", "", value)
    value = re.sub(r"\s*\((?:usa|europe|japan|world|rev[^)]*|disc[^)]*)\)\s*$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _load_adoption_candidates(shelf_platform: str) -> dict[str, deque]:
    """Index unclaimed Shelf digital games once for a RomM platform.

    The old sync re-selected and re-normalised every unclaimed game for
    every incoming ROM. On large platforms that became quadratic work.
    Keep the same first-match adoption semantics, but build the title
    index once and consume candidates as they are claimed.
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT id, title, media_type, platform, publisher,
                      publish_year, description, romm_id,
                      romm_platform_id, cover_path, source
               FROM items
               WHERE media_type = 'digital_game'
                 AND platform = ? AND romm_id IS NULL
               ORDER BY id""",
            (shelf_platform,),
        ).fetchall()

    indexed: dict[str, deque] = {}
    for row in rows:
        key = _normal_title(str(row["title"] or ""))
        if not key:
            continue
        indexed.setdefault(key, deque()).append(row)
    return indexed


def _lookup_existing_by_romm_ids(db, romm_ids: list[str]) -> dict[str, object]:
    """Fetch existing RomM-owned/adopted rows in batched SQLite lookups."""
    unique_ids = list(dict.fromkeys(romm_id for romm_id in romm_ids if romm_id))
    if not unique_ids:
        return {}

    existing: dict[str, object] = {}
    for start in range(0, len(unique_ids), DB_BATCH_SIZE):
        chunk = unique_ids[start:start + DB_BATCH_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"""SELECT id, title, media_type, platform, publisher,
                       publish_year, description, romm_id,
                       romm_platform_id, cover_path, source
                FROM items WHERE romm_id IN ({placeholders})""",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            existing[str(row["romm_id"])] = row
    return existing


def _import_rom_batch(
    db,
    roms: list[dict],
    shelf_platform: str,
    platform_id: str,
    adoption_candidates: dict[str, deque],
    stats: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, int]]]:
    """Import one small DB batch and return progress + cover work."""
    romm_ids = [str(rom.get("id") or "").strip() for rom in roms]
    existing_by_id = _lookup_existing_by_romm_ids(db, romm_ids)
    outcomes: list[tuple[str, str]] = []
    cover_jobs: list[tuple[str, int]] = []

    for rom in roms:
        romm_id = str(rom.get("id") or "").strip()
        title = str(
            rom.get("name")
            or rom.get("fs_name_no_tags")
            or rom.get("fs_name_no_ext")
            or ""
        ).strip()
        if not romm_id or not title:
            stats["skipped"] += 1
            outcomes.append((title or romm_id or "Unknown ROM", "skipped"))
            continue

        metadata = rom.get("metadatum") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        publisher = _publisher(metadata)
        publish_year = _publish_year(metadata.get("first_release_date"))
        description = str(rom.get("summary") or "").strip() or None
        cover_url = _cover_url("", rom)

        existing = existing_by_id.get(romm_id)
        if not existing:
            normal = _normal_title(title)
            candidates = adoption_candidates.get(normal)
            if candidates:
                existing = candidates.popleft()
                if not candidates:
                    adoption_candidates.pop(normal, None)

        desired = {
            "title": title,
            "media_type": "digital_game",
            "platform": shelf_platform,
            "publisher": publisher,
            "publish_year": publish_year,
            "description": description,
            "romm_id": romm_id,
            "romm_platform_id": platform_id,
        }

        if existing:
            changed = any(existing[key] != value for key, value in desired.items())
            if changed:
                db.execute(
                    """UPDATE items SET title=?, media_type='digital_game',
                       platform=?, publisher=?, publish_year=?,
                       description=?, romm_id=?, romm_platform_id=?,
                       updated_at=datetime('now') WHERE id=?""",
                    (
                        title,
                        shelf_platform,
                        publisher,
                        publish_year,
                        description,
                        romm_id,
                        platform_id,
                        existing["id"],
                    ),
                )
                stats["updated"] += 1
                status = "updated"
            else:
                stats["unchanged"] += 1
                status = "unchanged"
            item_id = existing["id"]
            fetch_cover = not existing["cover_path"] and bool(cover_url)
        else:
            item_id = insert_item(
                db,
                title=title,
                media_type="digital_game",
                platform=shelf_platform,
                publisher=publisher,
                publish_year=publish_year,
                description=description,
                romm_id=romm_id,
                romm_platform_id=platform_id,
                source="romm",
            )
            stats["added"] += 1
            status = "added"
            fetch_cover = bool(cover_url)

        if fetch_cover and cover_url:
            cover_jobs.append((cover_url, item_id))
        outcomes.append((title, status))

    return outcomes, cover_jobs


async def _fetch_platform_page(
    client: httpx.AsyncClient,
    romm_url: str,
    token: str,
    platform_id: str,
    offset: int,
    *,
    with_total: bool,
) -> tuple[list[dict] | None, int | None, bool]:
    """Fetch one RomM page with retries.

    RomM 5.x filters the list endpoint with ``platform_ids`` (plural),
    not ``platform_id``. It also allows callers that already know the
    first-page total to skip repeating the COUNT on later pages.
    """
    params = {
        "platform_ids": [platform_id],
        "limit": PAGE_SIZE,
        "offset": offset,
        "order_by": "id",
        "order_dir": "asc",
        "group_by_meta_id": "true",
        "with_char_index": "false",
        "with_filter_values": "false",
        "with_rom_id_index": "false",
        "with_total": "true" if with_total else "false",
    }
    response = None
    for attempt in range(PAGE_RETRIES + 1):
        try:
            response = await client.get(
                f"{romm_url}/api/roms", headers=_headers(token), params=params
            )
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt < PAGE_RETRIES:
                delay = PAGE_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "RomM platform %s offset %s timed out; retry %s/%s in %.1fs",
                    platform_id,
                    offset,
                    attempt + 1,
                    PAGE_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "RomM platform %s offset %s failed after %s retries",
                platform_id,
                offset,
                PAGE_RETRIES,
            )
            return None, None, False

        if response.status_code == 200:
            break
        if response.status_code in (408, 429) or response.status_code >= 500:
            if attempt < PAGE_RETRIES:
                delay = PAGE_RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "RomM platform %s offset %s returned HTTP %s; retry %s/%s in %.1fs",
                    platform_id,
                    offset,
                    response.status_code,
                    attempt + 1,
                    PAGE_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
        logger.warning(
            "RomM platform %s returned HTTP %s at offset %s",
            platform_id,
            response.status_code,
            offset,
        )
        return None, None, False

    if response is None or response.status_code != 200:
        return None, None, False
    try:
        data = response.json()
    except ValueError:
        logger.warning("RomM platform %s returned invalid JSON", platform_id)
        return None, None, False

    if isinstance(data, list):
        page = data
        total = None
    elif isinstance(data, dict):
        page = data.get("items") or []
        raw_total = data.get("total")
        total = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else None
    else:
        return None, None, False
    if not isinstance(page, list):
        return None, None, False
    return page, total, True


def _cover_url(romm_url: str, rom: dict) -> str | None:
    value = (
        rom.get("path_cover_large")
        or rom.get("path_cover_small")
        or rom.get("url_cover")
    )
    if not value:
        return None
    return urljoin(romm_url.rstrip("/") + "/", str(value))


def _same_host(a: str, b: str) -> bool:
    aa, bb = urlparse(a), urlparse(b)
    return (aa.scheme, aa.hostname, aa.port) == (bb.scheme, bb.hostname, bb.port)


async def _download_cover(
    client: httpx.AsyncClient,
    romm_url: str,
    token: str,
    cover_url: str,
    item_id: int,
) -> str:
    """Download one RomM cover without leaking its token to external hosts."""
    headers = {"Accept": "image/*,*/*"}
    if _same_host(romm_url, cover_url):
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(COVER_RETRIES + 1):
        try:
            response = await client.get(cover_url, headers=headers, timeout=COVER_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt < COVER_RETRIES:
                continue
            return "error"

        if response.status_code in (204, 404):
            return "missing"
        if response.status_code >= 500 and attempt < COVER_RETRIES:
            continue
        if response.status_code != 200:
            return "error"
        if len(response.content) <= 500:
            return "missing"

        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        ext = ".webp" if content_type == "image/webp" else ".png" if content_type == "image/png" else ".jpg"
        try:
            covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            dest = covers.COVERS_DIR / f"{item_id}{ext}"
            tmp = covers.COVERS_DIR / f".{item_id}{ext}.tmp"
            tmp.write_bytes(response.content)
            tmp.replace(dest)
            with get_db() as db:
                db.execute(
                    "UPDATE items SET cover_path = ? WHERE id = ?",
                    (f"covers/{item_id}{ext}", item_id),
                )
            return "downloaded"
        except OSError:
            logger.warning("Failed writing RomM cover for item %s", item_id, exc_info=True)
            return "error"
    return "error"


async def _download_cover_with_deadline(*args) -> str:
    try:
        async with asyncio.timeout(COVER_WALL_TIMEOUT):
            return await _download_cover(*args)
    except TimeoutError:
        logger.warning("RomM cover exceeded %.1fs deadline", COVER_WALL_TIMEOUT)
        return "error"


async def _drain_cover_batch(batch: list[tuple], stats: dict) -> None:
    if not batch:
        return
    results = await asyncio.gather(
        *(_download_cover_with_deadline(*job) for job in batch),
        return_exceptions=True,
    )
    batch.clear()
    for result in results:
        if isinstance(result, BaseException) or result == "error":
            stats["cover_errors"] += 1
        elif result == "downloaded":
            stats["covers"] += 1


async def sync(romm_url: str, token: str, on_progress=None) -> dict:
    """Stream selected RomM platforms into Shelf as Digital Games.

    ROM pages are imported as soon as they arrive, so peak memory is
    bounded by one API page plus small SQLite batches instead of the
    entire RomM library. Existing/adoptable rows are indexed in batches
    to avoid an O(n²) title scan on large platforms.
    """
    stats = {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "incomplete_platforms": 0,
        "covers": 0,
        "cover_errors": 0,
    }
    excluded = get_excluded_platforms()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{romm_url}/api/platforms", headers=_headers(token)
            )
        except httpx.TimeoutException:
            return {"error": "Timed out connecting to RomM"}
        except httpx.HTTPError:
            return {"error": "Failed to connect to RomM"}

        if response.status_code in (401, 403):
            return {"error": "RomM rejected the Client API Token"}
        if response.status_code != 200:
            return {"error": f"Failed to connect: HTTP {response.status_code}"}
        try:
            raw_platforms = response.json()
        except ValueError:
            return {"error": "RomM returned an invalid platform response"}
        if not isinstance(raw_platforms, list):
            return {"error": "RomM returned an invalid platform response"}

        selected_platforms = [
            platform
            for platform in raw_platforms
            if isinstance(platform, dict)
            and platform.get("id") is not None
            and str(platform["id"]) not in excluded
        ]

        def platform_estimate(platform: dict) -> int:
            try:
                return max(0, int(platform.get("rom_count") or 0))
            except (TypeError, ValueError):
                return 0

        total_estimate = sum(platform_estimate(platform) for platform in selected_platforms)
        current = 0
        cover_batch: list[tuple] = []
        platform_icon_slugs: dict[str, str] = {}
        platform_specs: list[tuple[dict, str, str]] = []

        for platform in selected_platforms:
            with get_db() as db:
                shelf_platform = _platform_slug(db, platform)
            icon_slug = _platform_icon_slug(platform)
            if icon_slug:
                platform_icon_slugs[shelf_platform] = icon_slug
            platform_name = str(
                platform.get("display_name")
                or platform.get("custom_name")
                or platform.get("name")
                or platform.get("slug")
                or platform["id"]
            ).strip()
            platform_specs.append((platform, shelf_platform, platform_name))

        with get_db() as db:
            icon_map_json = json.dumps(platform_icon_slugs, sort_keys=True)
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('romm_platform_icon_slugs', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (icon_map_json,),
            )

        if on_progress:
            await on_progress(
                0,
                total_estimate,
                "Syncing RomM library…",
                "discovering",
            )

        for platform, shelf_platform, platform_name in platform_specs:
            platform_id = str(platform["id"])
            estimate = platform_estimate(platform)
            platform_total: int | None = None
            offset = 0
            seen: set[str] = set()
            adoption_candidates = _load_adoption_candidates(shelf_platform)

            while True:
                if on_progress:
                    await on_progress(
                        current,
                        max(total_estimate, current),
                        f"Fetching {platform_name}",
                        "discovering",
                    )

                page, returned_total, ok = await _fetch_platform_page(
                    client,
                    romm_url,
                    token,
                    platform_id,
                    offset,
                    with_total=(offset == 0),
                )
                if not ok or page is None:
                    stats["errors"] += 1
                    stats["incomplete_platforms"] += 1
                    break

                if platform_total is None and returned_total is not None:
                    platform_total = returned_total
                    total_estimate = max(
                        current,
                        total_estimate - estimate + returned_total,
                    )

                raw_count = len(page)
                unseen: list[dict] = []
                for rom in page:
                    if not isinstance(rom, dict):
                        continue
                    romm_id = str(rom.get("id") or "").strip()
                    if romm_id:
                        if romm_id in seen:
                            continue
                        seen.add(romm_id)
                    unseen.append(rom)

                if page and not unseen:
                    logger.warning(
                        "RomM platform %s pagination made no progress at offset %s",
                        platform_id,
                        offset,
                    )
                    stats["errors"] += 1
                    stats["incomplete_platforms"] += 1
                    break

                for start in range(0, len(unseen), DB_BATCH_SIZE):
                    batch = unseen[start:start + DB_BATCH_SIZE]
                    with get_db() as db:
                        outcomes, cover_jobs = _import_rom_batch(
                            db,
                            batch,
                            shelf_platform,
                            platform_id,
                            adoption_candidates,
                            stats,
                        )

                    for title, status in outcomes:
                        current += 1
                        if on_progress:
                            await on_progress(
                                current,
                                max(total_estimate, current),
                                title,
                                status,
                            )

                    for cover_url, item_id in cover_jobs:
                        resolved_cover = urljoin(
                            romm_url.rstrip("/") + "/",
                            cover_url,
                        )
                        cover_batch.append(
                            (client, romm_url, token, resolved_cover, item_id)
                        )
                        if len(cover_batch) >= COVER_BATCH_SIZE:
                            await _drain_cover_batch(cover_batch, stats)

                if not page or raw_count < PAGE_SIZE:
                    break
                if platform_total is not None and offset + raw_count >= platform_total:
                    break
                offset += raw_count

        await _drain_cover_batch(cover_batch, stats)

    _auto_link_items()
    return stats


def _auto_link_items() -> None:
    """Group same-title game representations across platforms and formats."""
    from app.services import media_groups
    with get_db() as db:
        media_groups.auto_link_family(db, "game")
