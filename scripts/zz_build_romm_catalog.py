from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"pattern not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# Keep the existing #100 router composition intact and add this read-only layer.
replace_once(
    "app/routers/__init__.py",
    "from app.routers import romm as _romm\n\n_pages.router.include_router(_romm.router)",
    "from app.routers import romm as _romm\nfrom app.routers import romm_catalog as _romm_catalog\n\n_pages.router.include_router(_romm.router)\n_pages.router.include_router(_romm_catalog.router)",
)

# Make the synced catalogue discoverable from the existing integration card.
replace_once(
    "app/templates/fragments/settings/romm.html",
    '''        <div class="mt-4 pt-4 border-t border-shelf-border">\n            <button id="romm-sync" type="button" class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">Sync Now</button>''',
    '''        <div class="mt-4 pt-4 border-t border-shelf-border">\n            <div class="flex flex-wrap items-center gap-2">\n                <button id="romm-sync" type="button" class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">Sync Now</button>\n                <a href="/romm/library" class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">Browse synced library</a>\n            </div>''',
)

Path("app/services/romm_catalog.py").write_text(r'''"""Read-only catalogue queries for RomM-backed Shelf items."""

from __future__ import annotations


def platform_summaries(db) -> list[dict]:
    rows = db.execute(
        "SELECT rr.platform_id, i.platform AS shelf_platform, "
        "COALESCE(gp.name, i.platform, rr.platform_id) AS platform_name, "
        "COUNT(*) AS game_count "
        "FROM romm_records rr JOIN items i ON i.id = rr.item_id "
        "LEFT JOIN game_platforms gp ON gp.slug = i.platform "
        "GROUP BY rr.platform_id, i.platform, gp.name "
        "ORDER BY platform_name COLLATE NOCASE, rr.platform_id"
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_page(
    db,
    *,
    platform_id: str = "",
    query: str = "",
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where = []
    params: list[object] = []
    clean_platform = str(platform_id or "").strip()
    clean_query = str(query or "").strip()[:200]
    if clean_platform:
        where.append("rr.platform_id = ?")
        params.append(clean_platform)
    if clean_query:
        where.append(
            "(i.title LIKE ? COLLATE NOCASE OR COALESCE(i.publisher, '') LIKE ? COLLATE NOCASE)"
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle])
    clause = " WHERE " + " AND ".join(where) if where else ""

    total = db.execute(
        "SELECT COUNT(*) AS c FROM romm_records rr JOIN items i ON i.id = rr.item_id"
        + clause,
        params,
    ).fetchone()["c"]
    rows = db.execute(
        "SELECT rr.romm_id, rr.platform_id, i.id AS item_id, i.title, i.publisher, "
        "i.publish_year, i.platform, i.cover_path, i.description, "
        "COALESCE(gp.name, i.platform, rr.platform_id) AS platform_name "
        "FROM romm_records rr JOIN items i ON i.id = rr.item_id "
        "LEFT JOIN game_platforms gp ON gp.slug = i.platform"
        + clause
        + " ORDER BY i.title COLLATE NOCASE, i.publish_year, rr.romm_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(row) for row in rows], total
''')

Path("app/routers/romm_catalog.py").write_text(r'''"""Viewer-facing catalogue for games synchronised from RomM."""

from math import ceil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import romm_catalog, romm_client, romm_sync

router = APIRouter()
PER_PAGE = 60


@router.get("/romm")
async def romm_index(_=Depends(require_role("viewer"))):
    return RedirectResponse(url="/romm/library", status_code=303)


@router.get("/romm/library")
async def romm_library(
    request: Request,
    q: str = Query("", max_length=200),
    platform: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    _=Depends(require_role("viewer")),
):
    offset = (page - 1) * PER_PAGE
    with get_db() as db:
        summaries = romm_catalog.platform_summaries(db)
        games, total = romm_catalog.fetch_page(
            db,
            platform_id=platform,
            query=q,
            limit=PER_PAGE,
            offset=offset,
        )

    config = romm_sync.configuration()
    server = str(config.get("url") or "").strip()
    public = str(config.get("public_url") or "").strip() or None
    for game in games:
        game["romm_url"] = None
        if server:
            try:
                game["romm_url"] = romm_client.browser_rom_url(
                    server, game["romm_id"], public_url=public
                )
            except romm_client.RomMError:
                pass

    page_count = max(1, ceil(total / PER_PAGE)) if total else 1
    if page > page_count and total:
        return RedirectResponse(
            url=f"/romm/library?page={page_count}", status_code=303
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "romm_library.html",
        {
            "games": games,
            "platforms": summaries,
            "total": total,
            "q": q,
            "selected_platform": platform,
            "page": page,
            "page_count": page_count,
        },
    )
''')

Path("app/templates/romm_library.html").write_text(r'''{% extends "base.html" %}
{% block title %}RomM Library — Shelf{% endblock %}
{% block content %}
<div class="max-w-7xl mx-auto">
    <div class="flex flex-wrap items-end justify-between gap-4 mb-6">
        <div>
            <h1 class="text-2xl font-bold">RomM Library</h1>
            <p class="text-sm text-shelf-muted mt-1">{{ total }} synced digital game{{ '' if total == 1 else 's' }}. RomM remains the source of the game files.</p>
        </div>
        <a href="/settings" class="text-sm text-shelf-accent2 hover:underline">RomM settings</a>
    </div>

    {% if platforms %}
    <div class="flex gap-2 overflow-x-auto pb-2 mb-5">
        <a href="/romm/library{% if q %}?q={{ q|urlencode }}{% endif %}"
           class="shrink-0 px-3 py-2 rounded-lg text-sm {% if not selected_platform %}bg-shelf-accent text-white{% else %}bg-shelf-card border border-shelf-border{% endif %}">All · {{ platforms|sum(attribute='game_count') }}</a>
        {% for platform in platforms %}
        <a href="/romm/library?platform={{ platform.platform_id|urlencode }}{% if q %}&q={{ q|urlencode }}{% endif %}"
           class="shrink-0 px-3 py-2 rounded-lg text-sm {% if selected_platform == platform.platform_id %}bg-shelf-accent text-white{% else %}bg-shelf-card border border-shelf-border{% endif %}">{{ platform.platform_name }} · {{ platform.game_count }}</a>
        {% endfor %}
    </div>
    {% endif %}

    <form method="get" action="/romm/library" class="flex flex-wrap gap-2 mb-6">
        {% if selected_platform %}<input type="hidden" name="platform" value="{{ selected_platform }}">{% endif %}
        <input type="search" name="q" value="{{ q }}" placeholder="Search synced games or publisher…"
               class="flex-1 min-w-64 bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
        <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg hover:bg-shelf-accent2">Search</button>
        {% if q %}<a href="/romm/library{% if selected_platform %}?platform={{ selected_platform|urlencode }}{% endif %}" class="px-4 py-2 bg-shelf-card border border-shelf-border rounded-lg">Clear</a>{% endif %}
    </form>

    {% if games %}
    <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
        {% for game in games %}
        <div class="bg-shelf-card rounded-xl border border-shelf-border overflow-hidden min-w-0">
            <a href="/item/{{ game.item_id }}" class="block aspect-[3/4] bg-shelf-bg">
                {% if game.cover_path %}<img src="/covers/{{ game.cover_path }}" alt="" class="w-full h-full object-cover">{% endif %}
            </a>
            <div class="p-3">
                <a href="/item/{{ game.item_id }}" class="font-medium text-sm line-clamp-2 hover:text-shelf-accent2">{{ game.title }}</a>
                <p class="text-xs text-shelf-muted mt-1 truncate">{{ game.platform_name }}{% if game.publish_year %} · {{ game.publish_year }}{% endif %}</p>
                {% if game.romm_url %}<a href="{{ game.romm_url }}" target="_blank" rel="noopener" class="inline-block mt-2 text-xs text-shelf-accent2 hover:underline">Open in RomM</a>{% endif %}
            </div>
        </div>
        {% endfor %}
    </div>

    {% if page_count > 1 %}
    <div class="flex items-center justify-center gap-3 mt-8 text-sm">
        {% if page > 1 %}<a class="px-3 py-2 bg-shelf-card border border-shelf-border rounded-lg" href="/romm/library?page={{ page - 1 }}{% if selected_platform %}&platform={{ selected_platform|urlencode }}{% endif %}{% if q %}&q={{ q|urlencode }}{% endif %}">Previous</a>{% endif %}
        <span class="text-shelf-muted">Page {{ page }} of {{ page_count }}</span>
        {% if page < page_count %}<a class="px-3 py-2 bg-shelf-card border border-shelf-border rounded-lg" href="/romm/library?page={{ page + 1 }}{% if selected_platform %}&platform={{ selected_platform|urlencode }}{% endif %}{% if q %}&q={{ q|urlencode }}{% endif %}">Next</a>{% endif %}
    </div>
    {% endif %}
    {% else %}
    <div class="bg-shelf-card rounded-xl border border-shelf-border p-10 text-center text-shelf-muted">
        {% if q or selected_platform %}No synced RomM games match these filters.{% else %}No RomM games have been synchronised yet.{% endif %}
    </div>
    {% endif %}
</div>
{% endblock %}
''')

Path("tests/test_romm_catalog.py").write_text(r'''from app.services import romm_catalog, romm_records


def _candidate(romm_id, platform_id, platform, title, *, publisher=None, year=None):
    return {
        "romm_id": romm_id,
        "romm_platform_id": platform_id,
        "title": title,
        "platform": platform,
        "platform_name": platform.upper(),
        "publisher": publisher,
        "publish_year": year,
        "description": None,
    }


def test_platform_summaries_keep_provider_platforms_separate(db):
    romm_records.persist_candidate(db, _candidate("rom-1", "p-snes", "snes", "Same Game"))
    romm_records.persist_candidate(db, _candidate("rom-2", "p-gba", "gba", "Same Game"))

    rows = romm_catalog.platform_summaries(db)

    assert {row["platform_id"] for row in rows} == {"p-snes", "p-gba"}
    assert sum(row["game_count"] for row in rows) == 2


def test_catalog_filter_and_search_do_not_merge_same_titles(db):
    romm_records.persist_candidate(db, _candidate("rom-1", "p-snes", "snes", "Shared Title", publisher="Nintendo"))
    romm_records.persist_candidate(db, _candidate("rom-2", "p-gba", "gba", "Shared Title", publisher="Nintendo"))
    romm_records.persist_candidate(db, _candidate("rom-3", "p-snes", "snes", "Other Game", publisher="Sega"))

    rows, total = romm_catalog.fetch_page(db, platform_id="p-snes", query="Nintendo")

    assert total == 1
    assert [row["romm_id"] for row in rows] == ["rom-1"]


def test_catalog_paginates_stably_by_title(db):
    for number, title in enumerate(["Gamma", "Alpha", "Beta"], start=1):
        romm_records.persist_candidate(db, _candidate(f"rom-{number}", "p-one", "snes", title))

    first, total = romm_catalog.fetch_page(db, limit=2, offset=0)
    second, _ = romm_catalog.fetch_page(db, limit=2, offset=2)

    assert total == 3
    assert [row["title"] for row in first] == ["Alpha", "Beta"]
    assert [row["title"] for row in second] == ["Gamma"]
''')

# Extend existing RomM docs without changing #100's provider/persistence contract.
doc = Path("docs/romm.md")
text = doc.read_text()
section = r'''

## Browse the synced RomM catalogue

After a sync, **Settings → Integrations → RomM → Browse synced library** opens a
read-only catalogue view of the records RomM has contributed to Shelf. It shows
counts per RomM platform, supports platform filtering and title/publisher
search, and paginates large libraries rather than loading every ROM at once.

Games with the same title on different platforms remain separate records. This
view does not infer that they are the same release and does not automatically
join a RomM record to a physical cartridge or disc. Use Shelf's explicit
Related Media relationships for those decisions.
'''
if "## Browse the synced RomM catalogue" not in text:
    doc.write_text(text.rstrip() + section + "\n")
