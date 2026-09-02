from pathlib import Path


romm_path = Path("app/services/romm.py")
text = romm_path.read_text()

text = text.replace(
    "PAGE_SIZE = 200\nCOVER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)\n",
    "PAGE_SIZE = 200\nPAGE_RETRIES = 3\nPAGE_RETRY_BACKOFF = 1.0\nCOVER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)\n",
    1,
)

start = text.index("async def _fetch_platform_roms(")
end = text.index("\ndef _cover_url", start)
new_fetch = '''async def _fetch_platform_roms(
    client: httpx.AsyncClient,
    romm_url: str,
    token: str,
    platform_id: str,
    on_page=None,
) -> tuple[list[dict] | None, bool]:
    """Fetch one platform with retries and preserve already-fetched pages.

    Large RomM libraries can require dozens of 200-row requests.  A single
    transient timeout late in that walk must not discard every page already
    collected.  Retry transient failures with bounded exponential backoff; if
    retries are exhausted, return the partial platform marked incomplete so
    Shelf can import the durable work and tell the user to re-run the sync.
    """
    offset = 0
    roms: list[dict] = []
    seen: set[str] = set()

    while True:
        params = {
            "platform_id": platform_id,
            "limit": PAGE_SIZE,
            "offset": offset,
            "group_by_meta_id": "true",
            "with_char_index": "false",
            "with_filter_values": "false",
            "with_rom_id_index": "false",
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
                return (roms or None), False

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
            return (roms or None), False

        if response is None or response.status_code != 200:
            return (roms or None), False
        try:
            data = response.json()
        except ValueError:
            logger.warning("RomM platform %s returned invalid JSON", platform_id)
            return (roms or None), False

        if isinstance(data, list):
            page = data
            total = None
        elif isinstance(data, dict):
            page = data.get("items") or []
            total = data.get("total")
        else:
            return (roms or None), False
        if not isinstance(page, list):
            return (roms or None), False

        added = 0
        for rom in page:
            if not isinstance(rom, dict):
                continue
            rom_id = str(rom.get("id") or "")
            if not rom_id or rom_id in seen:
                continue
            seen.add(rom_id)
            roms.append(rom)
            added += 1

        if on_page:
            await on_page(len(roms), total if isinstance(total, int) else None)

        if not page or len(page) < PAGE_SIZE:
            break
        if isinstance(total, int) and offset + len(page) >= total:
            break
        if added == 0:
            logger.warning(
                "RomM platform %s pagination made no progress at offset %s",
                platform_id,
                offset,
            )
            break
        offset += len(page)

    return roms, True
'''
text = text[:start] + new_fetch + text[end:]

text = text.replace(
    '        "errors": 0,\n        "covers": 0,\n',
    '        "errors": 0,\n        "incomplete_platforms": 0,\n        "covers": 0,\n',
    1,
)

old_loop = '''        platform_roms: list[tuple[dict, list[dict], str]] = []
        platform_icon_slugs: dict[str, str] = {}
        for platform in raw_platforms:
            if not isinstance(platform, dict) or platform.get("id") is None:
                continue
            platform_id = str(platform["id"])
            if platform_id in excluded:
                continue
            with get_db() as db:
                shelf_platform = _platform_slug(db, platform)
            icon_slug = _platform_icon_slug(platform)
            if icon_slug:
                platform_icon_slugs[shelf_platform] = icon_slug
            roms = await _fetch_platform_roms(client, romm_url, token, platform_id)
            if roms is None:
                stats["errors"] += 1
                continue
            platform_roms.append((platform, roms, shelf_platform))
'''
new_loop = '''        platform_roms: list[tuple[dict, list[dict], str]] = []
        platform_icon_slugs: dict[str, str] = {}
        selected_platforms = [
            platform
            for platform in raw_platforms
            if isinstance(platform, dict)
            and platform.get("id") is not None
            and str(platform["id"]) not in excluded
        ]
        discovery_estimate = sum(
            max(0, int(platform.get("rom_count") or 0))
            for platform in selected_platforms
            if str(platform.get("rom_count") or "0").isdigit()
        )
        discovered = 0
        if on_progress and discovery_estimate:
            await on_progress(0, discovery_estimate, "Discovering RomM library…", "discovering")

        for platform in selected_platforms:
            platform_id = str(platform["id"])
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
                or platform_id
            ).strip()
            discovery_base = discovered

            async def report_page(platform_current, platform_total, *, base=discovery_base, name=platform_name):
                if not on_progress:
                    return
                global_current = base + platform_current
                global_total = max(
                    discovery_estimate,
                    global_current,
                    base + (platform_total or platform_current),
                )
                await on_progress(
                    global_current,
                    global_total,
                    f"Discovering {name}",
                    "discovering",
                )

            roms, complete = await _fetch_platform_roms(
                client, romm_url, token, platform_id, report_page
            )
            if roms is None:
                stats["errors"] += 1
                stats["incomplete_platforms"] += 1
                continue
            discovered += len(roms)
            if not complete:
                stats["errors"] += 1
                stats["incomplete_platforms"] += 1
            platform_roms.append((platform, roms, shelf_platform))
'''
if old_loop not in text:
    raise SystemExit("RomM platform loop anchor not found")
text = text.replace(old_loop, new_loop, 1)

old_total = '''        total = sum(len(roms) for _, roms, _ in platform_roms)
        current = 0
        cover_batch: list[tuple] = []

        for platform, roms, shelf_platform in platform_roms:
'''
new_total = '''        total = sum(len(roms) for _, roms, _ in platform_roms)
        current = 0
        cover_batch: list[tuple] = []
        if on_progress and total:
            await on_progress(0, total, "Importing RomM library…", "importing")

        for platform, roms, shelf_platform in platform_roms:
'''
if old_total not in text:
    raise SystemExit("RomM total anchor not found")
text = text.replace(old_total, new_total, 1)
romm_path.write_text(text)


test_path = Path("tests/test_romm_sync.py")
tests = test_path.read_text()
tests = tests.replace(
    "from app.services.romm import PAGE_SIZE, get_excluded_platforms, sync\n",
    "from app.services import romm as romm_service\nfrom app.services.romm import PAGE_SIZE, get_excluded_platforms, sync\n",
    1,
)
append = r'''

@respx.mock
def test_transient_page_timeout_is_retried_without_dropping_platform(db, monkeypatch):
    monkeypatch.setattr(romm_service, "PAGE_RETRY_BACKOFF", 0)
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    last = _rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)
    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 1, offset=0),
        httpx.ReadTimeout("temporary RomM timeout"),
        _page(last, total=PAGE_SIZE + 1, offset=PAGE_SIZE),
    ])

    stats = asyncio.run(sync(ROMM, TOKEN))

    assert stats["added"] == PAGE_SIZE + 1
    assert stats["incomplete_platforms"] == 0
    assert route.call_count == 3


@respx.mock
def test_exhausted_page_retries_preserve_partial_platform(db, monkeypatch):
    monkeypatch.setattr(romm_service, "PAGE_RETRY_BACKOFF", 0)
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 50)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 50, offset=0),
        *[httpx.ReadTimeout("RomM stayed unavailable") for _ in range(romm_service.PAGE_RETRIES + 1)],
    ])

    stats = asyncio.run(sync(ROMM, TOKEN))

    assert stats["added"] == PAGE_SIZE
    assert stats["errors"] == 1
    assert stats["incomplete_platforms"] == 1
    assert route.call_count == romm_service.PAGE_RETRIES + 2


@respx.mock
def test_discovery_reports_page_progress_before_import(db):
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    last = _rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)
    respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 1, offset=0),
        _page(last, total=PAGE_SIZE + 1, offset=PAGE_SIZE),
    ])
    progress = []

    async def on_progress(current, total, title, status):
        progress.append((current, total, title, status))

    stats = asyncio.run(sync(ROMM, TOKEN, on_progress=on_progress))

    assert stats["added"] == PAGE_SIZE + 1
    assert any(
        current == PAGE_SIZE and total >= PAGE_SIZE + 1
        and title.startswith("Discovering ") and status == "discovering"
        for current, total, title, status in progress
    )
    importing = next(entry for entry in progress if entry[3] == "importing")
    assert importing[:2] == (0, PAGE_SIZE + 1)
'''
if "test_transient_page_timeout_is_retried_without_dropping_platform" in tests:
    raise SystemExit("retry tests already present")
test_path.write_text(tests + append)


template_path = Path("app/templates/fragments/settings/romm.html")
template = template_path.read_text()
template = template.replace(
    '<p class="text-shelf-success">Added: <span x-text="result.added"></span>, Updated: <span x-text="result.updated"></span>, Unchanged: <span x-text="result.unchanged"></span>, Skipped: <span x-text="result.skipped"></span></p>\n                    <p class="text-xs text-shelf-muted mt-1">Covers imported: <span x-text="result.covers"></span> · Cover errors: <span x-text="result.cover_errors"></span></p>',
    '<p class="text-shelf-success">Added: <span x-text="result.added"></span>, Updated: <span x-text="result.updated"></span>, Unchanged: <span x-text="result.unchanged"></span>, Skipped: <span x-text="result.skipped"></span>, Errors: <span x-text="result.errors"></span></p>\n                    <p class="text-xs text-shelf-muted mt-1">Covers imported: <span x-text="result.covers"></span> · Cover errors: <span x-text="result.cover_errors"></span></p>\n                    <p x-show="result.incomplete_platforms" class="text-xs text-shelf-warning mt-1">Incomplete RomM platforms: <span x-text="result.incomplete_platforms"></span>. Re-run sync to retry the missing pages.</p>',
    1,
)
template_path.write_text(template)

progress_test = Path("tests/test_romm_progress_ui.py")
pt = progress_test.read_text()
pt += '''\n\ndef test_romm_result_surfaces_incomplete_platforms():\n    template = Path("app/templates/fragments/settings/romm.html").read_text()\n    assert "Incomplete RomM platforms:" in template\n    assert 'x-text="result.incomplete_platforms"' in template\n    assert 'x-text="result.errors"' in template\n'''
progress_test.write_text(pt)
