from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one replacement target, found {count}")
    p.write_text(text.replace(old, new, 1))


# 1. Hidden series Browse filter: opening a collapsed series drills into its
# individual issues and every existing filter/hx/querystring path learns about
# it from the central registry.
replace_once(
    "app/browse_filters.py",
    '''def _tag(value):
    return (
        "i.id IN (SELECT it.item_id FROM item_tags it "
        "JOIN tags t ON it.tag_id = t.id WHERE t.name = ?)",
        [value],
    )


@dataclass(frozen=True)
''',
    '''def _tag(value):
    return (
        "i.id IN (SELECT it.item_id FROM item_tags it "
        "JOIN tags t ON it.tag_id = t.id WHERE t.name = ?)",
        [value],
    )


def _series(value):
    return "i.series_name = ? COLLATE NOCASE", [value]


@dataclass(frozen=True)
''',
)
replace_once(
    "app/browse_filters.py",
    '''    BrowseFilter("tag", prefix="Tag", condition=_tag, quote_in_qs=True),
    BrowseFilter("language", prefix="Language", condition=_column("i.language")),
    # `view` is the odd one: client-owned state (localStorage) that is sent to
''',
    '''    BrowseFilter("tag", prefix="Tag", condition=_tag, quote_in_qs=True),
    BrowseFilter("language", prefix="Language", condition=_column("i.language")),
    BrowseFilter("series", prefix="Series", condition=_series, quote_in_qs=True),
    # `view` is the odd one: client-owned state (localStorage) that is sent to
''',
)

# 2. First Browse paint uses grouping before pagination while collection/filter
# counts remain raw item counts.
replace_once(
    "app/routers/pages.py",
    "from app.routers.series import find_gaps\n",
    "from app.routers.series import find_gaps\nfrom app.services import browse_grouping\n",
)
replace_once(
    "app/routers/pages.py",
    '''        from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days
        items = db.execute(
            f"SELECT i.*, l.name as location_name, "
            f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
            f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
            f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
            f"FROM items i "
            f"LEFT JOIN locations l ON i.location_id = l.id "
            f"{where} ORDER BY {order_clause} LIMIT ?",
            [get_overdue_days(db)] + params + [DEFAULT_PAGE_SIZE],
        ).fetchall()

        total_filtered = db.execute(
            f"SELECT COUNT(*) as c FROM items i {where}", params
        ).fetchone()["c"]
''',
    '''        items, total_filtered, display_total = browse_grouping.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            values=values,
        )
''',
)
replace_once(
    "app/routers/pages.py",
    "        has_more = len(items) < total_filtered\n",
    "        has_more = len(items) < display_total\n",
)

# 3. HTMX /api/search uses the same grouped page source. Replace only the body
# of the known search route's DB block, leaving response/count rendering intact.
p = Path("app/routers/items.py")
text = p.read_text()
if "from app.services import browse_grouping\n" not in text:
    anchor = "from app.services import authors as authors_svc\n"
    if anchor not in text:
        raise RuntimeError("items.py: service import anchor not found")
    text = text.replace(anchor, anchor + "from app.services import browse_grouping\n", 1)
route_pos = text.find("async def search_items(")
if route_pos < 0:
    raise RuntimeError("items.py: search_items route not found")
next_route = text.find("\n\n@router.", route_pos)
if next_route < 0:
    next_route = len(text)
route = text[route_pos:next_route]
block_start = route.find("    with get_db() as db:\n")
has_more_pos = route.find("\n    has_more = ", block_start)
if block_start < 0 or has_more_pos < 0:
    raise RuntimeError("items.py: search_items DB/pagination block not found")
old_db_block = route[block_start:has_more_pos]
if "items = db.execute(" not in old_db_block or "items_common.filter_counts" not in old_db_block:
    raise RuntimeError("items.py: unexpected search_items DB block")
new_db_block = '''    with get_db() as db:
        items, total, display_total = browse_grouping.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=per_page,
            offset=offset,
            values=values,
        )
        counts = items_common.filter_counts(db, values, total) if page <= 1 else None
'''
route = route[:block_start] + new_db_block + route[has_more_pos:]
route = route.replace(
    "    has_more = (offset + per_page) < total",
    "    has_more = (offset + per_page) < display_total",
    1,
)
text = text[:route_pos] + route + text[next_route:]
p.write_text(text)

# 4. The series filter is a real hidden control so it survives HTMX requests and
# clear-filter/querystring handling derived from the registry.
replace_once(
    "app/templates/browse.html",
    '''<div x-data="browsePage" data-initial-query="{{ initial_query or '' }}" data-can-select="{{ '1' if can_select else '0' }}">
''',
    '''<div x-data="browsePage" data-initial-query="{{ initial_query or '' }}" data-can-select="{{ '1' if can_select else '0' }}">
    <input type="hidden" name="series" value="{{ (initial_filters or {}).get('series', '') }}">
''',
)

# 5. Group cards/rows are navigation objects, not catalogue items, so they do
# not enter selection/bulk mutation state.
replace_once(
    "app/templates/fragments/item_grid.html",
    '''    {% for item in items %}
    {% include "fragments/item_card.html" %}
    {% endfor %}
''',
    '''    {% for item in items %}
    {% if item.browse_series_group|default(false) %}
    {% include "fragments/series_card.html" %}
    {% else %}
    {% include "fragments/item_card.html" %}
    {% endif %}
    {% endfor %}
''',
)
replace_once(
    "app/templates/fragments/item_grid.html",
    '''            {% for item in items %}
            {% include "fragments/item_row.html" %}
            {% endfor %}
''',
    '''            {% for item in items %}
            {% if item.browse_series_group|default(false) %}
            {% include "fragments/series_row.html" %}
            {% else %}
            {% include "fragments/item_row.html" %}
            {% endif %}
            {% endfor %}
''',
)

# 6. Library preference. The marker means legacy/partial clients posting only a
# currency cannot accidentally change the new checkbox.
replace_once(
    "app/routers/settings.py",
    '''    search_lang = (form.get("metadata_search_lang") or "").strip()
    if search_lang in SEARCH_LANGS:
        with get_db() as db:
            _upsert_setting(db, "metadata_search_lang", search_lang)

    return RedirectResponse(url="/settings", status_code=303)
''',
    '''    search_lang = (form.get("metadata_search_lang") or "").strip()
    if search_lang in SEARCH_LANGS:
        with get_db() as db:
            _upsert_setting(db, "metadata_search_lang", search_lang)

    if form.get("browse_group_digital_comics_present") == "1":
        group_digital_comics = "1" if form.get("browse_group_digital_comics") == "on" else "0"
        with get_db() as db:
            _upsert_setting(db, "browse_group_digital_comics", group_digital_comics)

    return RedirectResponse(url="/settings", status_code=303)
''',
)
replace_once(
    "app/routers/settings.py",
    '    """Save the display currency and metadata search language.\n',
    '    """Save display currency, metadata language and Browse grouping.\n',
)
replace_once(
    "app/templates/fragments/settings/library.html",
    '''            <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">
                Save
            </button>
''',
    '''            <div class="pt-2 border-t border-shelf-border">
                <input type="hidden" name="browse_group_digital_comics_present" value="1">
                {% set group_digital_comics = settings.get("browse_group_digital_comics", "1") != "0" %}
                <label class="flex items-start gap-3 cursor-pointer">
                    <input type="checkbox" name="browse_group_digital_comics"
                           class="mt-1 rounded border-shelf-border"
                           {% if group_digital_comics %}checked{% endif %}>
                    <span>
                        <span class="block text-sm font-medium text-shelf-text">Group Digital Comics by series in Browse</span>
                        <span class="block text-xs text-shelf-muted mt-0.5">Show one card per series by default. Open a series to browse its individual issues.</span>
                    </span>
                </label>
            </div>
            <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">
                Save
            </button>
''',
)

# 7. The background-job migration intentionally replaced the old SSE start
# endpoint. Update both route intercepts, both request assertions and the test
# documentation so this guard follows the real background-job start request.
p = Path("tests/e2e/test_settings_abs_sync_guard.py")
text = p.read_text()
old_path = "/api/sync/audiobookshelf/stream"
count = text.count(old_path)
if count != 5:
    raise RuntimeError(f"ABS E2E endpoint: expected 5 stale stream references, found {count}")
text = text.replace(old_path, "/api/sync/audiobookshelf/job")
text = text.replace("issues the stream request", "issues the background-job request")
text = text.replace("with the stream\nroute aborted first", "with the job\nroute aborted first")
p.write_text(text)
