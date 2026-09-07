from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"pattern not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


# Append a real migration without editing any existing migration.
db_path = Path("app/database.py")
db = db_path.read_text()
old_tail = '''    (30, "Index hierarchical location children",
     "CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent_id, sort_order)"),
)'''
new_tail = '''    (30, "Index hierarchical location children",
     "CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent_id, sort_order)"),
    (31, "Add physical copy shelf position",
     "ALTER TABLE item_copies ADD COLUMN position_order INTEGER DEFAULT NULL"),
)'''
if old_tail not in db:
    raise SystemExit("migration tail not found")
db = db.replace(old_tail, new_tail, 1)

# Fresh databases need the same column baked into MIGRATION_TABLES. Do not
# rewrite migration 25: the append-only historical migration remains intact.
marker = 'MIGRATION_TABLES = """'
head, tail = db.split(marker, 1)
old_copy = '''    is_primary         INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),'''
new_copy = '''    is_primary         INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    position_order     INTEGER DEFAULT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),'''
if old_copy not in tail:
    raise SystemExit("fresh item_copies definition not found")
tail = tail.replace(old_copy, new_copy, 1)
db_path.write_text(head + marker + tail)

# Register the focused ordering router explicitly.
replace_once(
    "app/main.py",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
)
replace_once(
    "app/main.py",
    "app.include_router(locations.router)\napp.include_router(platforms.router)",
    "app.include_router(locations.router)\napp.include_router(location_order.router)\napp.include_router(platforms.router)",
)

# Location administration gets a direct path to the physical arrangement page.
replace_once(
    "app/templates/fragments/settings/library.html",
    '''                    <span>{{ loc.name }}</span>\n                    <form action="/api/locations/{{ loc.id }}/delete" method="post" class="inline"''',
    '''                    <span>{{ loc.name }}</span>\n                    <a href="/locations/{{ loc.id }}/arrange" class="text-shelf-accent2 hover:underline text-sm">Arrange</a>\n                    <form action="/api/locations/{{ loc.id }}/delete" method="post" class="inline"''',
)

Path("app/services/location_order.py").write_text(r'''"""Physical copy ordering within Shelf's native hierarchical locations."""

from __future__ import annotations

import re


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _optional_metadata(db, rows: list[dict]) -> None:
    """Enrich sort hints when optional media contributions are installed.

    The 0.36 ordering foundation must not depend on Periodicals or Music. If
    those tables exist later, however, issue/release ordering becomes richer
    automatically without changing the physical-copy schema.
    """
    if not rows:
        return
    ids = [row["item_id"] for row in rows]
    marks = ",".join("?" for _ in ids)

    if _table_exists(db, "periodical_issues"):
        issue_rows = db.execute(
            f"SELECT item_id, issue_date, issue_number FROM periodical_issues "
            f"WHERE item_id IN ({marks})",
            ids,
        ).fetchall()
        by_item = {row["item_id"]: row for row in issue_rows}
        for row in rows:
            extra = by_item.get(row["item_id"])
            if extra:
                row["issue_date"] = extra["issue_date"]
                row["issue_number"] = extra["issue_number"]

    if _table_exists(db, "music_releases"):
        release_rows = db.execute(
            f"SELECT item_id, release_date FROM music_releases WHERE item_id IN ({marks})",
            ids,
        ).fetchall()
        by_item = {row["item_id"]: row for row in release_rows}
        for row in rows:
            extra = by_item.get(row["item_id"])
            if extra:
                row["release_date"] = extra["release_date"]


def direct_copies(db, location_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT c.id AS copy_id, c.copy_number, c.position_order, c.condition, "
        "c.copy_barcode, c.is_primary, i.id AS item_id, i.title, i.authors, "
        "i.media_type, i.cover_path, i.series_name, i.series_position, "
        "i.publish_year FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.location_id = ? "
        "ORDER BY CASE WHEN c.position_order IS NULL THEN 1 ELSE 0 END, "
        "c.position_order, i.title COLLATE NOCASE, c.copy_number, c.id",
        (location_id,),
    ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["issue_date"] = None
        row["issue_number"] = None
        row["release_date"] = None
    _optional_metadata(db, result)
    return result


def apply_copy_order(db, location_id: int, copy_ids: list[int]) -> None:
    """Persist an exact complete ordering for one physical location."""
    existing = {
        row["id"]
        for row in db.execute(
            "SELECT id FROM item_copies WHERE location_id = ?", (location_id,)
        ).fetchall()
    }
    supplied = set(copy_ids)
    if len(copy_ids) != len(supplied) or supplied != existing:
        raise ValueError("Copy order must contain every copy in this location exactly once")
    for position, copy_id in enumerate(copy_ids, start=1):
        db.execute(
            "UPDATE item_copies SET position_order = ?, updated_at = datetime('now') "
            "WHERE id = ? AND location_id = ?",
            (position, copy_id, location_id),
        )


def _issue_number(value) -> tuple:
    text = str(value or "").strip()
    if not text:
        return (1, 0, "")
    match = re.match(r"^(\d+(?:\.\d+)?)(.*)$", text)
    if match:
        return (0, float(match.group(1)), match.group(2).casefold())
    return (0, float("inf"), text.casefold())


def _year_key(row: dict) -> str:
    year = row.get("publish_year")
    return f"{int(year):04d}" if isinstance(year, int) else "9999"


_SORT_KEYS = {
    "title": lambda row: ((row.get("title") or "").casefold(), row["copy_id"]),
    "author": lambda row: (
        (row.get("authors") or "").casefold(),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "series": lambda row: (
        (row.get("series_name") or row.get("title") or "").casefold(),
        row.get("series_position")
        if row.get("series_position") is not None
        else float("inf"),
        _year_key(row),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "release": lambda row: (
        row.get("release_date") or _year_key(row),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "issue": lambda row: (
        row.get("issue_date") or _year_key(row),
        _issue_number(row.get("issue_number")),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
}


def auto_order_copies(db, location_id: int, sort_key: str) -> list[int]:
    """Order a shelf by a deterministic catalogue/media key."""
    if sort_key not in _SORT_KEYS:
        raise ValueError("Unknown sort order")
    rows = direct_copies(db, location_id)
    ids = [row["copy_id"] for row in sorted(rows, key=_SORT_KEYS[sort_key])]
    apply_copy_order(db, location_id, ids)
    return ids
''')

Path("app/routers/location_order.py").write_text(r'''"""Browse and arrange physical copies stored at one location."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import location_order as order_svc

router = APIRouter()


@router.get("/locations/{location_id}/arrange")
async def arrange_location(
    request: Request,
    location_id: int,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        location = db.execute(
            "SELECT id, name, label, parent_id FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        if not location:
            return RedirectResponse(url="/settings?location_error=missing", status_code=303)
        children = db.execute(
            "SELECT id, name, label FROM locations WHERE parent_id = ? "
            "ORDER BY sort_order, lower(COALESCE(label, name)), id",
            (location_id,),
        ).fetchall()
        copies = order_svc.direct_copies(db, location_id)

    user = getattr(request.state, "user", None) or {}
    return request.app.state.templates.TemplateResponse(
        request,
        "location_order.html",
        {
            "location": dict(location),
            "children": [dict(row) for row in children],
            "copies": copies,
            "can_edit": user.get("role") in {"admin", "editor"},
        },
    )


def _integer_ids(value) -> list[int] | None:
    if not isinstance(value, list):
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    return value


@router.post("/api/locations/{location_id}/order")
async def save_location_order(
    request: Request,
    location_id: int,
    _=Depends(require_role("editor")),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    copy_ids = _integer_ids(body.get("copy_ids") if isinstance(body, dict) else None)
    if copy_ids is None:
        return JSONResponse({"ok": False, "message": "Invalid copy order"}, status_code=400)
    with get_db() as db:
        if not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            order_svc.apply_copy_order(db, location_id, copy_ids)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return {"ok": True, "copy_ids": copy_ids}


@router.post("/api/locations/{location_id}/auto-order")
async def auto_location_order(
    request: Request,
    location_id: int,
    _=Depends(require_role("editor")),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sort_key = body.get("sort_key") if isinstance(body, dict) else None
    if not isinstance(sort_key, str):
        return JSONResponse({"ok": False, "message": "Invalid sort order"}, status_code=400)
    with get_db() as db:
        if not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            ids = order_svc.auto_order_copies(db, location_id, sort_key)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "copy_ids": ids}
''')

Path("app/templates/location_order.html").write_text(r'''{% extends "base.html" %}
{% block title %}Arrange {{ location.label or location.name }} — Shelf{% endblock %}
{% block content %}
<div class="max-w-4xl mx-auto" data-location-order data-location-id="{{ location.id }}" data-can-edit="{{ '1' if can_edit else '0' }}">
    <div class="flex flex-wrap items-start justify-between gap-3 mb-6">
        <div>
            <a href="/settings" class="text-sm text-shelf-muted hover:text-shelf-text">← Settings</a>
            <h1 class="text-2xl font-bold mt-2">{{ location.name }}</h1>
            <p class="text-sm text-shelf-muted mt-1">{{ copies|length }} physical cop{{ 'y' if copies|length == 1 else 'ies' }} stored directly here.</p>
        </div>
    </div>

    {% if children %}
    <div class="bg-shelf-card rounded-xl border border-shelf-border p-4 mb-5">
        <h2 class="font-semibold mb-2">Inside this location</h2>
        <div class="flex flex-wrap gap-2">
            {% for child in children %}
            <a href="/locations/{{ child.id }}/arrange" class="px-3 py-1.5 rounded-lg bg-shelf-bg text-sm hover:bg-shelf-hover">{{ child.label or child.name }}</a>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    {% if can_edit and copies %}
    <div class="bg-shelf-card rounded-xl border border-shelf-border p-4 mb-5">
        <div class="flex flex-wrap items-center gap-2">
            <span class="text-sm text-shelf-muted mr-1">Auto order:</span>
            {% for key, label in [('title', 'Title'), ('author', 'Creator'), ('series', 'Series'), ('release', 'Release'), ('issue', 'Issue')] %}
            <button type="button" data-auto-order="{{ key }}" class="px-3 py-1.5 rounded-lg bg-shelf-bg text-sm hover:bg-shelf-hover">{{ label }}</button>
            {% endfor %}
            <button type="button" data-save-order class="ml-auto px-4 py-1.5 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2">Save order</button>
        </div>
        <p class="text-xs text-shelf-muted mt-2">Drag copies into their physical left-to-right/top-to-bottom order, then save. Optional Periodicals and Music metadata is used automatically when available.</p>
        <p data-order-status class="text-xs text-shelf-muted mt-2" aria-live="polite"></p>
    </div>
    {% endif %}

    {% if copies %}
    <div data-copy-list class="space-y-2">
        {% for copy in copies %}
        <div data-copy-id="{{ copy.copy_id }}" draggable="{{ 'true' if can_edit else 'false' }}"
             class="bg-shelf-card rounded-xl border border-shelf-border p-3 flex items-center gap-3">
            {% if can_edit %}<span data-drag-handle class="text-shelf-muted cursor-move select-none" aria-hidden="true">⋮⋮</span>{% endif %}
            {% if copy.cover_path %}
            <img src="/covers/{{ copy.cover_path }}" alt="" class="w-10 h-14 object-cover rounded bg-shelf-bg shrink-0">
            {% else %}
            <div class="w-10 h-14 rounded bg-shelf-bg shrink-0"></div>
            {% endif %}
            <div class="min-w-0 flex-1">
                <a href="/item/{{ copy.item_id }}" class="font-medium hover:text-shelf-accent2">{{ copy.title }}</a>
                <div class="text-sm text-shelf-muted truncate">
                    {% if copy.authors %}{{ copy.authors }}{% endif %}
                    {% if copy.series_name %} · {{ copy.series_name }}{% if copy.series_position is not none %} #{{ copy.series_position|int if copy.series_position == copy.series_position|int else copy.series_position }}{% endif %}{% endif %}
                </div>
                <div class="text-xs text-shelf-muted mt-1">Copy {{ copy.copy_number }}{% if copy.copy_barcode %} · {{ copy.copy_barcode }}{% endif %}</div>
            </div>
            {% if copy.position_order is not none %}<span class="text-xs text-shelf-muted">#{{ copy.position_order }}</span>{% endif %}
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="bg-shelf-card rounded-xl border border-shelf-border p-8 text-center text-shelf-muted">No physical copies are stored directly at this location yet.</div>
    {% endif %}
</div>
<script src="/static/js/location-order.js" defer></script>
{% endblock %}
''')

Path("static/js/location-order.js").write_text(r'''(function () {
    function start() {
        const root = document.querySelector('[data-location-order]');
        if (!root || root.dataset.canEdit !== '1') return;
        const list = root.querySelector('[data-copy-list]');
        if (!list) return;
        const locationId = root.dataset.locationId;
        const status = root.querySelector('[data-order-status]');
        let dragging = null;

        function message(text, error) {
            if (!status) return;
            status.textContent = text || '';
            status.classList.toggle('text-shelf-error', !!error);
            status.classList.toggle('text-shelf-muted', !error);
        }

        function ids() {
            return Array.from(list.querySelectorAll('[data-copy-id]')).map(function (row) {
                return Number(row.dataset.copyId);
            });
        }

        list.querySelectorAll('[data-copy-id]').forEach(function (row) {
            row.addEventListener('dragstart', function () {
                dragging = row;
                row.setAttribute('aria-grabbed', 'true');
            });
            row.addEventListener('dragend', function () {
                row.removeAttribute('aria-grabbed');
                dragging = null;
                message('Order changed — save when ready.', false);
            });
        });

        list.addEventListener('dragover', function (event) {
            if (!dragging) return;
            event.preventDefault();
            const target = event.target.closest('[data-copy-id]');
            if (!target || target === dragging) return;
            const box = target.getBoundingClientRect();
            if (event.clientY < box.top + box.height / 2) {
                list.insertBefore(dragging, target);
            } else {
                list.insertBefore(dragging, target.nextSibling);
            }
        });

        async function post(path, body) {
            const response = await fetch(path, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': window.csrfToken()
                },
                body: JSON.stringify(body)
            });
            let data = {};
            try { data = await response.json(); } catch (_) {}
            if (!response.ok || !data.ok) throw new Error(data.message || 'Could not save shelf order');
            return data;
        }

        const save = root.querySelector('[data-save-order]');
        if (save) save.addEventListener('click', async function () {
            save.disabled = true;
            message('Saving…', false);
            try {
                await post('/api/locations/' + locationId + '/order', {copy_ids: ids()});
                message('Shelf order saved.', false);
            } catch (error) {
                message(error.message, true);
            } finally {
                save.disabled = false;
            }
        });

        root.querySelectorAll('[data-auto-order]').forEach(function (button) {
            button.addEventListener('click', async function () {
                button.disabled = true;
                message('Ordering…', false);
                try {
                    await post('/api/locations/' + locationId + '/auto-order', {sort_key: button.dataset.autoOrder});
                    window.location.reload();
                } catch (error) {
                    message(error.message, true);
                    button.disabled = false;
                }
            });
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
''')

Path("tests/test_location_order.py").write_text(r'''import pytest

from app.services import location_order


def _location(db, name="Shelf 1"):
    return db.execute(
        "INSERT INTO locations (name, label) VALUES (?, ?)", (name, name)
    ).lastrowid


def _copy(db, location_id, title, *, author=None, series=None, position=None, year=None):
    item_id = db.execute(
        "INSERT INTO items (title, authors, media_type, series_name, series_position, publish_year) "
        "VALUES (?, ?, 'book', ?, ?, ?)",
        (title, author, series, position, year),
    ).lastrowid
    return db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 1, ?, 1)",
        (item_id, location_id),
    ).lastrowid


def test_exact_drag_order_is_persisted(db):
    location_id = _location(db)
    a = _copy(db, location_id, "A")
    b = _copy(db, location_id, "B")
    c = _copy(db, location_id, "C")

    location_order.apply_copy_order(db, location_id, [c, a, b])

    rows = location_order.direct_copies(db, location_id)
    assert [row["copy_id"] for row in rows] == [c, a, b]
    assert [row["position_order"] for row in rows] == [1, 2, 3]


def test_order_must_include_every_direct_copy_once(db):
    location_id = _location(db)
    a = _copy(db, location_id, "A")
    b = _copy(db, location_id, "B")

    with pytest.raises(ValueError):
        location_order.apply_copy_order(db, location_id, [a])
    with pytest.raises(ValueError):
        location_order.apply_copy_order(db, location_id, [a, a])
    assert {a, b} == {row["copy_id"] for row in location_order.direct_copies(db, location_id)}


def test_auto_order_series_uses_position_then_title(db):
    location_id = _location(db)
    three = _copy(db, location_id, "Volume Three", series="Series", position=3)
    one = _copy(db, location_id, "Volume One", series="Series", position=1)
    two = _copy(db, location_id, "Volume Two", series="Series", position=2)

    assert location_order.auto_order_copies(db, location_id, "series") == [one, two, three]


def test_issue_order_uses_periodical_metadata_when_available(db):
    location_id = _location(db)
    later = _copy(db, location_id, "Magazine later", year=2026)
    earlier = _copy(db, location_id, "Magazine earlier", year=2026)
    db.execute(
        "CREATE TABLE periodical_issues (item_id INTEGER PRIMARY KEY, issue_date TEXT, issue_number TEXT)"
    )
    later_item = db.execute("SELECT item_id FROM item_copies WHERE id = ?", (later,)).fetchone()["item_id"]
    earlier_item = db.execute("SELECT item_id FROM item_copies WHERE id = ?", (earlier,)).fetchone()["item_id"]
    db.execute("INSERT INTO periodical_issues VALUES (?, '2026-06-01', '6')", (later_item,))
    db.execute("INSERT INTO periodical_issues VALUES (?, '2026-05-01', '5')", (earlier_item,))

    assert location_order.auto_order_copies(db, location_id, "issue") == [earlier, later]
''')

# Keep user documentation close to the existing location guide.
doc = Path("docs/user-guide/locations.md")
text = doc.read_text()
section = r'''

## Arrange a physical shelf

Each location can have an explicit order for the physical copies stored directly
there. In **Settings → Library → Locations**, choose **Arrange** beside a room,
bookcase or shelf. Editors can drag copies into their real left-to-right (or
top-to-bottom) order and save it.

Shelf can also auto-order that location by title, creator, series position,
release date/year or periodical issue. When optional Periodicals or Music
metadata is installed, the richer issue/release dates are used automatically;
otherwise the core catalogue fields provide the fallback order.

Ordering is copy-specific, so two copies of the same title can sit next to each
other and retain distinct positions.
'''
if "## Arrange a physical shelf" not in text:
    doc.write_text(text.rstrip() + section + "\n")
