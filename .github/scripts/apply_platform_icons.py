from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# RomM: remember the raw RomM platform slug so Shelf can use RomM's own
# platform artwork (including user-supplied custom icons) from the browser URL.
romm_path = "app/services/romm.py"
replace_once(
    romm_path,
    '''def get_browser_url(romm_url: str, romm_id: str) -> str:\n    """Construct the browser-facing RomM game-detail URL."""\n    with get_db() as db:\n        public_url = get_setting(db, "romm_public_url")\n    base = (public_url or romm_url).rstrip("/")\n    return f"{base}/rom/{romm_id}"\n\n\n''',
    '''def get_browser_url(romm_url: str, romm_id: str) -> str:\n    """Construct the browser-facing RomM game-detail URL."""\n    with get_db() as db:\n        public_url = get_setting(db, "romm_public_url")\n    base = (public_url or romm_url).rstrip("/")\n    return f"{base}/rom/{romm_id}"\n\n\ndef _platform_icon_slug(platform: dict) -> str | None:\n    """RomM's icon filename stem for a platform, if it is path-safe."""\n    raw = str(platform.get("slug") or platform.get("fs_slug") or "").strip().lower()\n    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw):\n        return raw\n    return None\n\n\ndef get_platform_icon_urls(romm_url: str) -> dict[str, str]:\n    """Browser-safe RomM platform icon URLs keyed by Shelf platform slug.\n\n    RomM serves built-in and custom platform icons from /assets/platforms.\n    Shelf only uses an HTTPS browser-facing root here: internal Docker/LAN HTTP\n    URLs would be blocked as mixed content by Shelf's CSP and by modern browsers.\n    """\n    with get_db() as db:\n        public_url = get_setting(db, "romm_public_url")\n        raw_map = get_setting(db, "romm_platform_icon_slugs")\n    base = (public_url or romm_url or "").rstrip("/")\n    if urlparse(base).scheme.lower() != "https":\n        return {}\n    try:\n        mapping = json.loads(raw_map or "{}")\n    except (json.JSONDecodeError, TypeError):\n        return {}\n    if not isinstance(mapping, dict):\n        return {}\n\n    urls: dict[str, str] = {}\n    for shelf_slug, icon_slug in mapping.items():\n        if not isinstance(shelf_slug, str) or not isinstance(icon_slug, str):\n            continue\n        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", icon_slug):\n            continue\n        urls[shelf_slug] = f"{base}/assets/platforms/{icon_slug}.ico"\n    return urls\n\n\n_HANDHELD_PLATFORMS = frozenset({\n    "gb", "gbc", "gameboy", "gba", "nds", "3ds", "switch", "psp", "vita",\n    "gamegear", "lynx", "ngp", "ngpc", "wonderswan", "wonderswancolor",\n})\n_COMPUTER_PLATFORMS = frozenset({\n    "pc", "windows", "linux", "mac", "macos", "dos", "amiga", "c64",\n    "c128", "msx", "spectrum", "acpc", "atarist", "x68000", "pc98",\n})\n\n\ndef get_platform_icon_kind(platform: str | None) -> str:\n    """Local fallback glyph when no RomM-specific icon can be displayed."""\n    slug = (platform or "").casefold().replace("-", "").replace("_", "")\n    if slug in {value.replace("-", "").replace("_", "") for value in _HANDHELD_PLATFORMS}:\n        return "handheld"\n    if slug in {value.replace("-", "").replace("_", "") for value in _COMPUTER_PLATFORMS}:\n        return "computer"\n    if "arcade" in slug or slug in {"mame", "fbneo", "finalburnneo"}:\n        return "arcade"\n    return "gamepad"\n\n\n''',
)

replace_once(
    romm_path,
    '''        platform_roms: list[tuple[dict, list[dict]]] = []\n        for platform in raw_platforms:\n            if not isinstance(platform, dict) or platform.get("id") is None:\n                continue\n            platform_id = str(platform["id"])\n            if platform_id in excluded:\n                continue\n            roms = await _fetch_platform_roms(client, romm_url, token, platform_id)\n            if roms is None:\n                stats["errors"] += 1\n                continue\n            platform_roms.append((platform, roms))\n\n        total = sum(len(roms) for _, roms in platform_roms)\n        current = 0\n        cover_batch: list[tuple] = []\n\n        for platform, roms in platform_roms:\n            platform_id = str(platform["id"])\n            with get_db() as db:\n                shelf_platform = _platform_slug(db, platform)\n\n''',
    '''        platform_roms: list[tuple[dict, list[dict], str]] = []\n        platform_icon_slugs: dict[str, str] = {}\n        for platform in raw_platforms:\n            if not isinstance(platform, dict) or platform.get("id") is None:\n                continue\n            platform_id = str(platform["id"])\n            if platform_id in excluded:\n                continue\n            with get_db() as db:\n                shelf_platform = _platform_slug(db, platform)\n            icon_slug = _platform_icon_slug(platform)\n            if icon_slug:\n                platform_icon_slugs[shelf_platform] = icon_slug\n            roms = await _fetch_platform_roms(client, romm_url, token, platform_id)\n            if roms is None:\n                stats["errors"] += 1\n                continue\n            platform_roms.append((platform, roms, shelf_platform))\n\n        with get_db() as db:\n            icon_map_json = json.dumps(platform_icon_slugs, sort_keys=True)\n            db.execute(\n                "INSERT INTO settings (key, value) VALUES ('romm_platform_icon_slugs', ?) "\n                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",\n                (icon_map_json,),\n            )\n\n        total = sum(len(roms) for _, roms, _ in platform_roms)\n        current = 0\n        cover_batch: list[tuple] = []\n\n        for platform, roms, shelf_platform in platform_roms:\n            platform_id = str(platform["id"])\n\n''',
)

# Detail page: enrich each version and the group platform summary with an icon.
pages_path = "app/routers/pages.py"
replace_once(
    pages_path,
    '''        game_platforms = get_game_platforms(db)\n\n        # Enrich related rows once for the unified Related media panel. Provider\n''',
    '''        game_platforms = get_game_platforms(db)\n        from app.services.romm import get_platform_icon_kind, get_platform_icon_urls\n        romm_platform_icon_urls = (\n            get_platform_icon_urls(romm_url_val) if romm_url_val else {}\n        )\n\n        # Enrich related rows once for the unified Related media panel. Provider\n''',
)
replace_once(
    pages_path,
    '''            data["platform_label"] = (\n                game_platforms.get(data["platform"], data["platform"])\n                if data.get("platform") else None\n            )\n            # A related item may legitimately be represented in more than one\n''',
    '''            data["platform_label"] = (\n                game_platforms.get(data["platform"], data["platform"])\n                if data.get("platform") else None\n            )\n            data["platform_icon_url"] = (\n                romm_platform_icon_urls.get(data["platform"])\n                if data.get("platform") else None\n            )\n            data["platform_icon_kind"] = (\n                get_platform_icon_kind(data["platform"])\n                if data.get("platform") else None\n            )\n            # A related item may legitimately be represented in more than one\n''',
)
replace_once(
    pages_path,
    '''        related_formats = []\n        related_game_platforms = []\n        for group_item in all_group_items:\n''',
    '''        related_formats = []\n        related_game_platforms = []\n        seen_game_platforms: set[str] = set()\n        for group_item in all_group_items:\n''',
)
replace_once(
    pages_path,
    '''                platform_label = game_platforms.get(\n                    group_item["platform"], group_item["platform"]\n                )\n                if platform_label not in related_game_platforms:\n                    related_game_platforms.append(platform_label)\n''',
    '''                platform_slug = group_item["platform"]\n                platform_label = game_platforms.get(platform_slug, platform_slug)\n                if platform_slug not in seen_game_platforms:\n                    seen_game_platforms.add(platform_slug)\n                    related_game_platforms.append({\n                        "slug": platform_slug,\n                        "label": platform_label,\n                        "icon_url": romm_platform_icon_urls.get(platform_slug),\n                        "icon_kind": get_platform_icon_kind(platform_slug),\n                    })\n''',
)

# Small reusable partial: use RomM artwork when available, otherwise a local
# semantic device glyph. No JS/CDN is required and the text label always remains.
Path("app/templates/fragments/platform_icon.html").write_text('''{% if platform_icon_url %}\n<img src="{{ platform_icon_url }}" alt="" aria-hidden="true"\n     data-platform-icon-source="romm"\n     class="h-4 w-4 shrink-0 object-contain">\n{% elif platform_icon_kind == 'handheld' %}\n<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"\n     data-platform-icon="handheld" class="h-4 w-4 shrink-0">\n    <rect x="6" y="2.5" width="12" height="19" rx="3"/><rect x="8.5" y="5" width="7" height="7" rx="1"/>\n    <path d="M9 16h3m-1.5-1.5v3"/><circle cx="15" cy="15.5" r=".8"/>\n</svg>\n{% elif platform_icon_kind == 'computer' %}\n<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"\n     data-platform-icon="computer" class="h-4 w-4 shrink-0">\n    <rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8m-4-4v4"/>\n</svg>\n{% elif platform_icon_kind == 'arcade' %}\n<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"\n     data-platform-icon="arcade" class="h-4 w-4 shrink-0">\n    <path d="M6 2h12l2 6-2 14H6L4 8l2-6Z"/><rect x="7" y="5" width="10" height="6" rx="1"/>\n    <path d="M8 16h4m-2-2v4"/><circle cx="16" cy="16" r="1"/>\n</svg>\n{% else %}\n<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"\n     data-platform-icon="gamepad" class="h-4 w-4 shrink-0">\n    <path d="M7.5 8h9a4 4 0 0 1 3.8 2.8l1 3.1a3 3 0 0 1-5.2 2.8L14.8 15H9.2l-1.3 1.7a3 3 0 0 1-5.2-2.8l1-3.1A4 4 0 0 1 7.5 8Z"/>\n    <path d="M7 11v4m-2-2h4"/><circle cx="16" cy="12" r=".7" fill="currentColor" stroke="none"/><circle cx="18" cy="14" r=".7" fill="currentColor" stroke="none"/>\n</svg>\n{% endif %}\n''')

# Related-media template: platform summary becomes icon chips, and each game row
# gets the same icon next to its platform label.
template_path = "app/templates/fragments/related_media.html"
replace_once(
    template_path,
    '''            {% if related_game_platforms|length > 1 %}\n            <p class="text-xs text-shelf-muted mt-1" data-testid="related-platforms">\n                Platforms: {{ related_game_platforms | join(' · ') }}\n            </p>\n            {% endif %}\n''',
    '''            {% if related_game_platforms|length > 1 %}\n            <div class="flex flex-wrap items-center gap-1.5 text-xs text-shelf-muted mt-1" data-testid="related-platforms">\n                <span>Platforms:</span>\n                {% for platform in related_game_platforms %}\n                <span class="inline-flex items-center gap-1 rounded bg-shelf-hover px-1.5 py-0.5">\n                    {% with platform_icon_url=platform.icon_url, platform_icon_kind=platform.icon_kind %}\n                    {% include "fragments/platform_icon.html" %}\n                    {% endwith %}\n                    {{ platform.label }}\n                </span>\n                {% endfor %}\n            </div>\n            {% endif %}\n''',
)
replace_once(
    template_path,
    '''                    {% if related.platform_label %}\n                    <span class="text-[10px] px-1.5 py-0.5 rounded bg-shelf-accent/15 text-shelf-accent2">{{ related.platform_label }}</span>\n                    {% endif %}\n''',
    '''                    {% if related.platform_label %}\n                    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-shelf-accent/15 text-shelf-accent2">\n                        {% with platform_icon_url=related.platform_icon_url, platform_icon_kind=related.platform_icon_kind %}\n                        {% include "fragments/platform_icon.html" %}\n                        {% endwith %}\n                        {{ related.platform_label }}\n                    </span>\n                    {% endif %}\n''',
)

# Regressions: real RomM icon URLs for the three platform versions, plus a local
# computer fallback for a manually related game when no public RomM icon exists.
ui_test = "tests/test_related_media_ui.py"
replace_once(
    ui_test,
    '''    db.execute(\n        "INSERT INTO settings (key, value) VALUES ('romm_url', 'http://romm:8080')"\n    )\n''',
    '''    db.executemany(\n        "INSERT INTO settings (key, value) VALUES (?, ?)",\n        [\n            ("romm_url", "http://romm:8080"),\n            ("romm_public_url", "https://romm.example"),\n            ("romm_platform_icon_slugs", '{"snes":"snes","gba":"gba","pc":"windows"}'),\n        ],\n    )\n''',
)
replace_once(
    ui_test,
    '''    assert response.text.count("Also in RomM (Digital Game)") == 2\n''',
    '''    assert response.text.count("Also in RomM (Digital Game)") == 2\n    assert "https://romm.example/assets/platforms/snes.ico" in response.text\n    assert "https://romm.example/assets/platforms/gba.ico" in response.text\n    assert "https://romm.example/assets/platforms/windows.ico" in response.text\n    assert response.text.count('data-platform-icon-source="romm"') >= 3\n''',
)
replace_once(
    ui_test,
    '''    assert "Video Game" in response.text\n    assert "Stephen Fry" in response.text\n''',
    '''    assert "Video Game" in response.text\n    assert 'data-platform-icon="computer"' in response.text\n    assert "Stephen Fry" in response.text\n''',
)

sync_test = "tests/test_romm_sync.py"
replace_once(
    sync_test,
    '''    assert cover.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"\n''',
    '''    assert cover.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"\n    icon_map = json.loads(\n        db.execute(\n            "SELECT value FROM settings WHERE key='romm_platform_icon_slugs'"\n        ).fetchone()["value"]\n    )\n    assert icon_map == {"snes": "snes"}\n''',
)
