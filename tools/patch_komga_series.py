from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one exact match, found {text.count(old)}")
    write(path, text.replace(old, new, 1))


SERVICE = '''"""Browse and focused-detail helpers for Komga-backed series.

The Komga integration stores provider identity in ``komga_records`` rather
than on the catalogue item itself. Browse therefore groups only items with one
unambiguous Komga ``series_id``. If an item is linked to more than one distinct
Komga series, it remains an ordinary Shelf item instead of guessing which
source series owns it.

Grouping happens before LIMIT/OFFSET so a long manga/comic series consumes one
Browse slot and cannot spill repeated representatives onto later pages. An
explicit Shelf series-name filter disables grouping so users can still drill
through the ordinary item list when they intentionally choose that filter.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import quote

from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days

_ITEM_SERIES_CTE = """
WITH komga_item_series AS (
    SELECT item_id,
           CASE WHEN COUNT(DISTINCT TRIM(series_id)) = 1
                THEN MIN(TRIM(series_id))
                ELSE NULL END AS series_id
      FROM komga_records
     WHERE series_id IS NOT NULL AND TRIM(series_id) != ''
     GROUP BY item_id
)
"""

_GROUP_KEY = (
    "CASE WHEN kis.series_id IS NOT NULL "
    "THEN 'komga:' || kis.series_id "
    "ELSE 'item:' || CAST(i.id AS TEXT) END"
)


def _table_exists(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'komga_records'"
    ).fetchone() is not None


def _grouping_enabled(db, values: dict) -> bool:
    if values.get("series"):
        return False
    if not _table_exists(db):
        return False
    return db.execute(
        "SELECT 1 FROM komga_records "
        "WHERE series_id IS NOT NULL AND TRIM(series_id) != '' LIMIT 1"
    ).fetchone() is not None


def _decorate_plain(row) -> dict:
    item = dict(row)
    item.update(
        {
            "browse_series_group": False,
            "browse_series_id": None,
            "browse_series_name": None,
            "browse_series_kind": None,
            "browse_series_count": 1,
            "browse_series_url": None,
        }
    )
    return item


def _plain_page(db, where: str, params: list, order_clause: str, *, limit: int, offset: int):
    raw_total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]
    rows = db.execute(
        f"SELECT i.*, l.name as location_name, "
        f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
        f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id "
        f" AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
        f"FROM items i LEFT JOIN locations l ON i.location_id = l.id "
        f"{where} ORDER BY {order_clause}, i.id ASC LIMIT ? OFFSET ?",
        [get_overdue_days(db)] + list(params) + [limit, offset],
    ).fetchall()
    return [_decorate_plain(row) for row in rows], raw_total, raw_total


def _item_details(db, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT i.*, l.name as location_name, "
        f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
        f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id "
        f" AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
        f"FROM items i LEFT JOIN locations l ON i.location_id = l.id "
        f"WHERE i.id IN ({placeholders})",
        [get_overdue_days(db)] + ids,
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def _series_members(db, series_ids: list[str]) -> dict[str, list[dict]]:
    if not series_ids:
        return {}
    placeholders = ",".join("?" for _ in series_ids)
    rows = db.execute(
        f"""{_ITEM_SERIES_CTE}
        SELECT kis.series_id,
               i.id, i.title, i.authors, i.cover_path, i.series_name,
               i.series_position, i.publish_year, i.owned, i.media_type,
               (SELECT kr.kind FROM komga_records kr
                 WHERE kr.item_id = i.id AND TRIM(kr.series_id) = kis.series_id
                 ORDER BY kr.komga_id LIMIT 1) AS kind
          FROM komga_item_series kis
          JOIN items i ON i.id = kis.item_id
         WHERE kis.series_id IN ({placeholders})
         ORDER BY kis.series_id,
                  (i.series_position IS NULL), i.series_position ASC,
                  (i.publish_year IS NULL), i.publish_year ASC,
                  i.title COLLATE NOCASE, i.id ASC""",
        series_ids,
    ).fetchall()
    result: dict[str, list[dict]] = {series_id: [] for series_id in series_ids}
    for row in rows:
        result.setdefault(row["series_id"], []).append(dict(row))
    return result


def _common(values: list[str]) -> str | None:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return None
    counts = Counter(cleaned)
    return min(counts, key=lambda value: (-counts[value], value.casefold(), value))


def _series_meta(rows: list[dict], fallback_name: str | None = None) -> dict:
    name = _common([str(row.get("series_name") or "") for row in rows])
    if not name:
        name = fallback_name or (rows[0]["title"] if rows else "Komga series")
    kind_values = {
        str(row.get("kind") or "").strip().casefold()
        for row in rows
        if str(row.get("kind") or "").strip()
    }
    kind = next(iter(kind_values)) if len(kind_values) == 1 else "mixed"
    return {
        "name": name,
        "kind": kind,
        "count": len(rows),
        "cover_path": rows[0].get("cover_path") if rows else None,
    }


def fetch_page(
    db,
    where: str,
    params: list,
    order_clause: str,
    *,
    limit: int,
    offset: int,
    values: dict,
):
    """Return ``(items, raw_total, display_total)`` for one Browse page."""
    if not _grouping_enabled(db, values):
        return _plain_page(
            db, where, params, order_clause, limit=limit, offset=offset
        )

    raw_total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]
    display_total = db.execute(
        f"""{_ITEM_SERIES_CTE}
        SELECT COUNT(DISTINCT {_GROUP_KEY}) AS c
          FROM items i
          LEFT JOIN komga_item_series kis ON kis.item_id = i.id
          {where}""",
        params,
    ).fetchone()["c"]

    ranked = db.execute(
        f"""{_ITEM_SERIES_CTE},
        ranked AS (
            SELECT i.id, kis.series_id, {_GROUP_KEY} AS browse_group_key,
                   ROW_NUMBER() OVER (
                       PARTITION BY {_GROUP_KEY}
                       ORDER BY {order_clause}, i.id ASC
                   ) AS browse_group_rank
              FROM items i
              LEFT JOIN komga_item_series kis ON kis.item_id = i.id
              {where}
        )
        SELECT r.id, r.series_id, r.browse_group_key
          FROM ranked r
          JOIN items i ON i.id = r.id
         WHERE r.browse_group_rank = 1
         ORDER BY {order_clause}, i.id ASC
         LIMIT ? OFFSET ?""",
        list(params) + [limit, offset],
    ).fetchall()

    ids = [row["id"] for row in ranked]
    details = _item_details(db, ids)
    series_ids = [row["series_id"] for row in ranked if row["series_id"]]
    members = _series_members(db, series_ids)

    items: list[dict] = []
    for ranked_row in ranked:
        item = details[ranked_row["id"]]
        series_id = ranked_row["series_id"]
        if not series_id:
            items.append(_decorate_plain(item))
            continue
        series_rows = members.get(series_id) or []
        if not series_rows:
            items.append(_decorate_plain(item))
            continue
        meta = _series_meta(series_rows, item.get("series_name"))
        item.update(
            {
                "browse_series_group": True,
                "browse_series_id": series_id,
                "browse_series_name": meta["name"],
                "browse_series_kind": meta["kind"],
                "browse_series_count": meta["count"],
                "browse_series_url": f"/series/komga/{quote(series_id, safe='')}",
                # The earliest position is the stable representative artwork.
                "cover_path": meta["cover_path"],
            }
        )
        items.append(item)

    return items, raw_total, display_total


def _find_gaps(positions) -> list[int]:
    whole: set[int] = set()
    for position in positions:
        if position is None:
            continue
        try:
            value = float(position)
        except (TypeError, ValueError):
            continue
        if value >= 1 and value.is_integer():
            whole.add(int(value))
    if not whole:
        return []
    return [number for number in range(1, max(whole) + 1) if number not in whole]


def series_detail(db, series_id: str) -> dict | None:
    """Return one stable-ID Komga series for the focused read-only view."""
    clean_id = str(series_id or "").strip()
    if not clean_id or not _table_exists(db):
        return None
    rows = _series_members(db, [clean_id]).get(clean_id) or []
    if not rows:
        return None
    meta = _series_meta(rows)
    owned_count = sum(1 for row in rows if row.get("owned"))
    return {
        "id": clean_id,
        "name": meta["name"],
        "kind": meta["kind"],
        "kind_label": {"comic": "Comic", "manga": "Manga"}.get(meta["kind"], "Komga"),
        "items": rows,
        "item_count": len(rows),
        "owned_count": owned_count,
        "wishlist_count": len(rows) - owned_count,
        "gaps": _find_gaps(row.get("series_position") for row in rows),
    }
'''

SERIES_CARD = '''{# One stable-ID Komga series in Browse grid view. #}
<a href="{{ item.browse_series_url }}" class="group relative"
   data-komga-series-id="{{ item.browse_series_id }}">
    <div class="cover-card bg-shelf-card rounded-lg overflow-hidden border border-shelf-border group-hover:border-shelf-accent/50 transition-colors shadow-lg">
        <span class="absolute top-2 right-2 z-10 px-2 py-1 rounded-md text-[10px] font-semibold bg-shelf-card/95 text-shelf-text border border-shelf-border shadow">
            {{ item.browse_series_count }} item{% if item.browse_series_count != 1 %}s{% endif %}
        </span>
        {% if item.cover_path %}
        <img src="/{{ item.cover_path }}" alt="{{ item.browse_series_name }}" loading="lazy">
        {% else %}
        <div class="w-full h-full flex items-center justify-center p-4 text-center bg-shelf-hover">
            <div>
                <svg class="w-10 h-10 mx-auto mb-2 text-shelf-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 19.5A2.5 2.5 0 016.5 17H20M4 19.5A2.5 2.5 0 006.5 22H20V5a2 2 0 00-2-2H6.5A2.5 2.5 0 004 5.5v14z"/></svg>
                <p class="text-xs text-shelf-muted line-clamp-3">{{ item.browse_series_name }}</p>
            </div>
        </div>
        {% endif %}
    </div>
    <div class="absolute bottom-0 left-0 right-0 p-3 pt-10 bg-gradient-to-t from-black/90 via-black/55 to-transparent rounded-b-lg opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
        <p class="text-white text-sm font-semibold leading-tight line-clamp-2">{{ item.browse_series_name }}</p>
        <p class="text-white/70 text-xs mt-0.5">{{ item.browse_series_kind|capitalize }} · Open series</p>
    </div>
</a>
'''

SERIES_ROW = '''{# One stable-ID Komga series in Browse list view. #}
{% set col_class = {
    'author': 'text-shelf-muted text-sm truncate max-w-[200px]',
    'status': '',
    'value': 'text-shelf-success text-sm',
    'series': 'text-shelf-muted text-xs truncate max-w-[200px]',
    'publisher': 'text-shelf-muted text-xs truncate max-w-[200px]',
    'identifier': 'text-shelf-muted text-xs font-mono',
} %}
<tr class="border-b border-shelf-border hover:bg-shelf-hover/50 transition-colors"
    data-komga-series-id="{{ item.browse_series_id }}">
    {% for col in browse_columns %}
    {%- if col.name == 'select' %}
    <td x-show="selectMode" data-col="select" class="px-3 py-2 w-8"></td>
    {%- elif col.name == 'cover' %}
    <td data-col="cover" class="px-3 py-2 w-10">
        <a href="{{ item.browse_series_url }}" aria-label="Open {{ item.browse_series_name }} series">
            {% if item.cover_path %}
            <img src="/{{ item.cover_path }}" alt="" class="w-8 h-12 object-cover rounded" loading="lazy">
            {% else %}
            <div class="w-8 h-12 bg-shelf-hover rounded flex items-center justify-center">
                <svg class="w-4 h-4 text-shelf-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 19.5A2.5 2.5 0 016.5 17H20M4 19.5A2.5 2.5 0 006.5 22H20V5a2 2 0 00-2-2H6.5A2.5 2.5 0 004 5.5v14z"/></svg>
            </div>
            {% endif %}
        </a>
    </td>
    {%- elif col.name == 'title' %}
    <td data-col="title" class="px-3 py-2">
        <div class="flex items-center gap-2">
            <a href="{{ item.browse_series_url }}"
               class="text-shelf-text text-sm font-medium truncate max-w-xs hover:underline">{{ item.browse_series_name }}</a>
            <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-shelf-hover text-shelf-muted shrink-0">
                {{ item.browse_series_count }} item{% if item.browse_series_count != 1 %}s{% endif %}
            </span>
        </div>
    </td>
    {%- else %}
    <td x-show="visibleCols.{{ col.name }}" data-col="{{ col.name }}"
        class="px-3 py-2 {{ col_class.get(col.name, 'text-shelf-muted text-xs') }}">
        {%- if col.name == 'media_type' %}{{ media_types.get(item.browse_series_kind, item.browse_series_kind|capitalize) }}
        {%- elif col.name == 'series' %}{{ item.browse_series_name }}
        {%- else %}—
        {%- endif %}
    </td>
    {%- endif %}
    {% endfor %}
</tr>
'''

DETAIL_TEMPLATE = '''{% extends "base.html" %}
{% block title %}{{ series.name }} — Shelf{% endblock %}
{% block content %}
<div class="mb-6">
    <a href="/browse" class="text-sm text-shelf-accent2 hover:text-shelf-text">&larr; Back to Browse</a>
    <div class="flex items-start justify-between gap-4 mt-3 flex-wrap">
        <div>
            <div class="flex items-center gap-2 flex-wrap">
                <h1 class="text-2xl font-bold" data-testid="komga-series-heading">{{ series.name }}</h1>
                <span class="px-2 py-0.5 rounded-full text-xs font-medium bg-shelf-accent/15 text-shelf-accent2 border border-shelf-border">Komga {{ series.kind_label }}</span>
            </div>
            <p class="text-sm text-shelf-muted mt-1">
                {{ series.item_count }} item{% if series.item_count != 1 %}s{% endif %}
                · {{ series.owned_count }} owned
                {% if series.wishlist_count %} · {{ series.wishlist_count }} wishlisted{% endif %}
                {% if series.gaps %}
                · <span class="text-shelf-warning">possibly missing
                    {%- for gap in series.gaps[:8] %} #{{ gap }}{% if not loop.last %},{% endif %}{% endfor %}
                    {%- if series.gaps|length > 8 %} +{{ series.gaps|length - 8 }} more{% endif %}</span>
                {% endif %}
            </p>
        </div>
    </div>
</div>

<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8 gap-4">
    {% for item in series['items'] %}
    <a href="/item/{{ item.id }}?from=series" class="group min-w-0"
       data-testid="komga-series-item"
       data-series-position="{{ item.series_position if item.series_position is not none else '' }}">
        <div class="relative aspect-[2/3] bg-shelf-card rounded-lg overflow-hidden border border-shelf-border group-hover:border-shelf-accent/50 transition-colors shadow-lg">
            {% if item.cover_path %}
            <img src="/{{ item.cover_path }}" alt="{{ item.title }}" class="w-full h-full object-cover" loading="lazy">
            {% else %}
            <div class="w-full h-full flex items-center justify-center p-3 text-center bg-shelf-hover">
                <span class="text-xs text-shelf-muted line-clamp-4">{{ item.title }}</span>
            </div>
            {% endif %}
            {% if item.series_position is not none %}
            <span class="absolute top-1 left-1 bg-black/75 text-white text-[10px] px-1.5 py-0.5 rounded">
                #{{ item.series_position|int if item.series_position == item.series_position|int else item.series_position }}
            </span>
            {% endif %}
            {% if not item.owned %}
            <span class="absolute bottom-1 left-1 bg-shelf-warning/90 text-black text-[9px] px-1 rounded font-medium">Wishlist</span>
            {% endif %}
        </div>
        <p class="text-sm text-shelf-text font-medium mt-2 leading-tight line-clamp-2">{{ item.title }}</p>
        {% if item.authors %}<p class="text-xs text-shelf-muted truncate mt-0.5">{{ item.authors }}</p>{% endif %}
    </a>
    {% endfor %}
</div>
{% endblock %}
'''

UNIT_TESTS = '''from app.services import komga_records, komga_series_browse


def _candidate(komga_id: str, series_id: str, position: float, *, name="Shared Name", kind="manga"):
    return {
        "komga_id": komga_id,
        "komga_library_id": f"library-{kind}",
        "komga_series_id": series_id,
        "library_kind": kind,
        "title": f"{name} {position:g}",
        "authors": "Series Author",
        "isbn": None,
        "series_name": name,
        "series_position": position,
        "publish_year": 2020 + int(position),
        "description": None,
        "page_count": 180,
    }


def _add(db, komga_id: str, series_id: str, position: float, **kwargs):
    return komga_records.persist_candidate(
        db, _candidate(komga_id, series_id, position, **kwargs)
    )["item_id"]


def test_browse_groups_by_stable_komga_series_id_not_display_name(db):
    _add(db, "book-a1", "series-a", 1)
    _add(db, "book-a2", "series-a", 2)
    _add(db, "book-b1", "series-b", 1)

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=20, offset=0, values={},
    )

    assert raw_total == 3
    assert display_total == 2
    groups = {item["browse_series_id"]: item for item in items}
    assert set(groups) == {"series-a", "series-b"}
    assert groups["series-a"]["browse_series_count"] == 2
    assert groups["series-b"]["browse_series_count"] == 1
    assert groups["series-a"]["browse_series_name"] == "Shared Name"


def test_explicit_series_filter_keeps_individual_item_browse(db):
    _add(db, "book-a1", "series-a", 1)
    _add(db, "book-a2", "series-a", 2)

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db,
        "WHERE i.series_name = ? COLLATE NOCASE",
        ["Shared Name"],
        "i.series_position ASC",
        limit=20,
        offset=0,
        values={"series": "Shared Name"},
    )

    assert raw_total == display_total == 2
    assert len(items) == 2
    assert all(item["browse_series_group"] is False for item in items)


def test_conflicting_komga_series_links_are_not_guessed_into_a_group(db):
    first = komga_records.persist_candidate(
        db,
        {
            **_candidate("book-one", "series-one", 1),
            "isbn": "9781974700523",
        },
    )
    second = komga_records.persist_candidate(
        db,
        {
            **_candidate("book-two", "series-two", 1),
            "isbn": "9781974700523",
        },
    )
    assert first["item_id"] == second["item_id"]

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=20, offset=0, values={},
    )

    assert raw_total == display_total == 1
    assert len(items) == 1
    assert items[0]["browse_series_group"] is False
    assert komga_series_browse.series_detail(db, "series-one") is None


def test_focused_series_orders_positions_and_reports_local_gaps(db):
    _add(db, "book-4", "series-gap", 4, name="Gap Series", kind="comic")
    _add(db, "book-1", "series-gap", 1, name="Gap Series", kind="comic")
    _add(db, "book-2", "series-gap", 2, name="Gap Series", kind="comic")

    series = komga_series_browse.series_detail(db, "series-gap")

    assert series is not None
    assert series["name"] == "Gap Series"
    assert series["kind"] == "comic"
    assert series["item_count"] == 3
    assert series["gaps"] == [3]
    assert [row["series_position"] for row in series["items"]] == [1.0, 2.0, 4.0]
'''

E2E_TEST = '''import sqlite3

import pytest
from playwright.sync_api import expect

from app.services import komga_records
from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)

pytestmark = pytest.mark.e2e


def test_komga_series_collapses_in_browse_and_opens_ordered_detail(server_factory, browser):
    server = server_factory()
    base = server["url"]
    credentials = _run_setup_wizard(browser, base)

    volume_two = insert_item(
        server["data_dir"],
        title="Series Volume Two",
        media_type="manga",
        source="komga",
        series_name="E2E Komga Manga",
        series_position=2.0,
        owned=1,
    )
    volume_one = insert_item(
        server["data_dir"],
        title="Series Volume One",
        media_type="manga",
        source="komga",
        series_name="E2E Komga Manga",
        series_position=1.0,
        owned=1,
    )

    conn = sqlite3.connect(str(server["data_dir"] / "shelf.db"))
    try:
        komga_records.ensure_schema(conn)
        conn.execute(
            "INSERT INTO komga_records (komga_id, item_id, library_id, series_id, kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e2e-book-2", volume_two, "e2e-library", "e2e-series", "manga"),
        )
        conn.execute(
            "INSERT INTO komga_records (komga_id, item_id, library_id, series_id, kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e2e-book-1", volume_one, "e2e-library", "e2e-series", "manga"),
        )
        conn.commit()
    finally:
        conn.close()

    ctx = browser.new_context()
    try:
        page = attach_page_guard(ctx.new_page())
        page.goto(f"{base}/login")
        page.fill("input[name=username]", credentials["username"])
        page.fill("input[name=password]", credentials["password"])
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/browse", timeout=10_000)
        page.wait_for_load_state("networkidle")

        series_card = page.locator('[data-komga-series-id="e2e-series"]')
        expect(series_card).to_have_count(1)
        expect(series_card).to_contain_text("2 items")
        expect(series_card).to_contain_text("E2E Komga Manga")

        series_card.click()
        page.wait_for_url(f"{base}/series/komga/e2e-series", timeout=10_000)
        expect(page.get_by_test_id("komga-series-heading")).to_have_text("E2E Komga Manga")
        members = page.get_by_test_id("komga-series-item")
        expect(members).to_have_count(2)
        expect(members.nth(0)).to_contain_text("Series Volume One")
        expect(members.nth(1)).to_contain_text("Series Volume Two")
        assert_page_clean(page)
    finally:
        ctx.close()
'''

write("app/services/komga_series_browse.py", SERVICE.lstrip())
write("app/templates/fragments/komga_series_card.html", SERIES_CARD.lstrip())
write("app/templates/fragments/komga_series_row.html", SERIES_ROW.lstrip())
write("app/templates/komga_series_detail.html", DETAIL_TEMPLATE.lstrip())
write("tests/test_komga_series_browse.py", UNIT_TESTS.lstrip())
write("tests/e2e/test_komga_series_browse.py", E2E_TEST.lstrip())

# pages.py: import helper, replace the first-paint query, and add the detail route.
replace_once(
    "app/routers/pages.py",
    "from app.routers.series import find_gaps\n",
    "from app.routers.series import find_gaps\nfrom app.services import komga_series_browse\n",
)
pages = read("app/routers/pages.py")
pattern = re.compile(
    r"        from app\.routers\.checkouts import OVERDUE_CONDITION, get_overdue_days\n"
    r"        items = db\.execute\(.*?"
    r"        total_filtered = db\.execute\(\n"
    r"            f\"SELECT COUNT\(\*\) as c FROM items i \{where\}\", params\n"
    r"        \)\.fetchone\(\)\[\"c\"\]\n",
    re.S,
)
replacement = '''        items, total_filtered, display_total = komga_series_browse.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            values=values,
        )
'''
pages, count = pattern.subn(replacement, pages, count=1)
if count != 1:
    raise RuntimeError(f"pages.py browse query replacement matched {count} times")
pages = pages.replace("        has_more = len(items) < total_filtered\n", "        has_more = len(items) < display_total\n", 1)
route_marker = '\n\n@router.get("/discover")\n'
if pages.count(route_marker) != 1:
    raise RuntimeError("pages.py discover marker changed")
route = '''

@router.get("/series/komga/{series_id}")
async def komga_series_detail(
    request: Request,
    series_id: str,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        series = komga_series_browse.series_detail(db, series_id)
    if series is None:
        return RedirectResponse(url="/browse")
    return request.app.state.templates.TemplateResponse(
        request,
        "komga_series_detail.html",
        {"series": series},
    )
'''
pages = pages.replace(route_marker, route + route_marker, 1)
write("app/routers/pages.py", pages)

# items.py: use the same grouping/pagination helper for HTMX Browse updates.
replace_once(
    "app/routers/items.py",
    "from app.services import synopsis as synopsis_svc\n",
    "from app.services import synopsis as synopsis_svc\nfrom app.services import komga_series_browse\n",
)
items_text = read("app/routers/items.py")
start = items_text.index('@router.get("/search")')
end = items_text.index('@router.post("/items/bulk-update")', start)
search_block = items_text[start:end]
query_pattern = re.compile(
    r"    with get_db\(\) as db:\n"
    r"        total = db\.execute\(.*?"
    r"        # Cross-filter counts for dropdowns \(page 1 only\)\.",
    re.S,
)
query_replacement = '''    with get_db() as db:
        items, total, display_total = komga_series_browse.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=per_page,
            offset=offset,
            values=values,
        )

        # Cross-filter counts for dropdowns (page 1 only).'''
search_block, count = query_pattern.subn(query_replacement, search_block, count=1)
if count != 1:
    raise RuntimeError(f"items.py search query replacement matched {count} times")
if "has_more = (offset + per_page) < total" not in search_block:
    raise RuntimeError("items.py has_more marker changed")
search_block = search_block.replace(
    "has_more = (offset + per_page) < total",
    "has_more = (offset + per_page) < display_total",
    1,
)
write("app/routers/items.py", items_text[:start] + search_block + items_text[end:])

# Browse fragment switches. Series representatives are navigation targets, not selectable items.
for path, ordinary, grouped in (
    ("app/templates/fragments/item_grid.html", '{% include "fragments/item_card.html" %}', '''{% if item.browse_series_group %}\n    {% include "fragments/komga_series_card.html" %}\n    {% else %}\n    {% include "fragments/item_card.html" %}\n    {% endif %}'''),
    ("app/templates/fragments/item_grid.html", '{% include "fragments/item_row.html" %}', '''{% if item.browse_series_group %}\n            {% include "fragments/komga_series_row.html" %}\n            {% else %}\n            {% include "fragments/item_row.html" %}\n            {% endif %}'''),
    ("app/templates/fragments/item_cards_page.html", '{% include "fragments/item_card.html" %}', '''{% if item.browse_series_group %}\n{% include "fragments/komga_series_card.html" %}\n{% else %}\n{% include "fragments/item_card.html" %}\n{% endif %}'''),
    ("app/templates/fragments/item_rows_page.html", '{% include "fragments/item_row.html" %}', '''{% if item.browse_series_group %}\n{% include "fragments/komga_series_row.html" %}\n{% else %}\n{% include "fragments/item_row.html" %}\n{% endif %}'''),
):
    replace_once(path, ordinary, grouped)

# Four unit tests and one E2E test are added by this slice.
replace_once(
    "README.md",
    "unit%20tests-2677%20passing",
    "unit%20tests-2681%20passing",
)
replace_once(
    "README.md",
    "e2e%20tests-224%20passing",
    "e2e%20tests-225%20passing",
)

print("Komga series Browse patch applied")
