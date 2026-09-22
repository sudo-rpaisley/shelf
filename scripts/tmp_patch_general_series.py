from pathlib import Path
import re


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"pattern not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Browse first paint.
replace_once(
    "app/routers/pages.py",
    "from app.services import item_copies, item_template, item_write\n",
    "from app.services import item_copies, item_template, item_write, series_browse\n",
)
p = Path("app/routers/pages.py")
text = p.read_text()
route = text.index('@router.get("/browse")')
start = text.index("        from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days\n", route)
end = text.index("        series_names = [\n", start)
replacement = '''        total_filtered = db.execute(
            f"SELECT COUNT(*) as c FROM items_live i {where}", params
        ).fetchone()["c"]

        units, display_total = series_browse.fetch_units(
            db, where, params, order_clause,
            limit=DEFAULT_PAGE_SIZE, offset=0,
        )

        from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days
        unit_ids = [unit["id"] for unit in units]
        if unit_ids:
            placeholders = ",".join("?" for _ in unit_ids)
            detail_rows = db.execute(
                f"SELECT i.*, l.name as location_name, "
                f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
                f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
                f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue, "
                f"{lists.WISHLISTED_SQL} AS wishlisted "
                f"FROM items_live i LEFT JOIN locations l ON i.location_id = l.id "
                f"WHERE i.id IN ({placeholders})",
                [get_overdue_days(db)] + unit_ids,
            ).fetchall()
            items = series_browse.merge_item_details(units, detail_rows)
        else:
            items = []

'''
text = text[:start] + replacement + text[end:]
text = text.replace("        has_more = len(items) < total_filtered\n", "        has_more = len(items) < display_total\n", 1)
text = text.replace('        "load_more_url": load_more_url,\n', '        "load_more_url": load_more_url,\n        "display_total": display_total,\n', 1)
p.write_text(text)


# HTMX Browse pagination.
replace_once(
    "app/routers/items.py",
    "from app.services import item_merge\n",
    "from app.services import item_merge, series_browse\n",
)
p = Path("app/routers/items.py")
text = p.read_text()
route = text.index("async def search_items(")
start = text.index("        total = db.execute(\n", route)
end = text.index("        # Cross-filter counts for dropdowns (page 1 only).", start)
replacement = '''        total = db.execute(
            f"SELECT COUNT(*) as c FROM items_live i {where}", params
        ).fetchone()["c"]

        units, display_total = series_browse.fetch_units(
            db, where, params, order_clause,
            limit=per_page, offset=offset,
        )

        from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days
        unit_ids = [unit["id"] for unit in units]
        if unit_ids:
            placeholders = ",".join("?" for _ in unit_ids)
            detail_rows = db.execute(
                f"SELECT i.*, l.name as location_name, "
                f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
                f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
                f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue, "
                f"{lists.WISHLISTED_SQL} AS wishlisted "
                f"FROM items_live i LEFT JOIN locations l ON i.location_id = l.id "
                f"WHERE i.id IN ({placeholders})",
                [get_overdue_days(db)] + unit_ids,
            ).fetchall()
            items = series_browse.merge_item_details(units, detail_rows)
        else:
            items = []

'''
text = text[:start] + replacement + text[end:]
text = text.replace("    has_more = (offset + per_page) < total\n", "    has_more = (offset + per_page) < display_total\n", 1)
text = text.replace('        "total": total,\n', '        "total": display_total,\n        "display_total": display_total,\n', 1)
p.write_text(text)


# Collection count reflects Browse units, while filter-option counts remain item counts.
replace_once(
    "app/templates/browse.html",
    'id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span>',
    'id="collection-count" class="text-shelf-muted text-lg font-normal">({{ display_total if display_total is defined else filtered_total }})</span>',
)
replace_once(
    "app/templates/fragments/filter_counts_oob.html",
    '<span id="collection-count" hx-swap-oob="true" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span>',
    '<span id="collection-count" hx-swap-oob="true" class="text-shelf-muted text-lg font-normal">({{ display_total if display_total is defined else filtered_total }})</span>',
)


# Grid/list render grouped units through dedicated templates.
card_cond = '''{% if item.browse_series_group %}
    {% include "fragments/series_card.html" %}
    {% else %}
    {% include "fragments/item_card.html" %}
    {% endif %}'''
row_cond = '''{% if item.browse_series_group %}
            {% include "fragments/series_row.html" %}
            {% else %}
            {% include "fragments/item_row.html" %}
            {% endif %}'''
replace_once("app/templates/fragments/item_grid.html", '{% include "fragments/item_card.html" %}', card_cond)
replace_once("app/templates/fragments/item_grid.html", '{% include "fragments/item_row.html" %}', row_cond)
replace_once("app/templates/fragments/item_cards_page.html", '{% include "fragments/item_card.html" %}', card_cond)
replace_once("app/templates/fragments/item_rows_page.html", '{% include "fragments/item_row.html" %}', row_cond)


# Grouped cards remain first-class bulk-selectable Browse units.
p = Path("static/js/browse.js")
text = p.read_text()
marker = '''        toggleItem(id) {
'''
insert = '''        groupIds(raw) {
            return String(raw || '').split(',').map(function(value) {
                return Number.parseInt(value, 10);
            }).filter(function(value) { return Number.isInteger(value) && value > 0; });
        },

        groupSelected(raw) {
            var ids = this.groupIds(raw);
            return ids.length > 0 && ids.every((id) => this.selectedIds.includes(id));
        },

        toggleGroup(raw) {
            var ids = this.groupIds(raw);
            var allSelected = ids.length > 0 && ids.every((id) => this.selectedIds.includes(id));
            if (allSelected) {
                this.selectedIds = this.selectedIds.filter((id) => ids.indexOf(id) < 0);
            } else {
                ids.forEach((id) => {
                    if (this.selectedIds.indexOf(id) < 0) this.selectedIds.push(id);
                });
            }
        },

        openSeriesOrToggle(raw, url, event) {
            if (this.selectMode) { this.toggleGroup(raw); return; }
            if (event && (event.ctrlKey || event.metaKey)) window.open(url, '_blank');
            else window.location = url;
        },

        toggleItem(id) {
'''
if marker not in text:
    raise SystemExit("toggleItem marker not found")
text = text.replace(marker, insert, 1)
old = '''        selectAll() {
            var self = this;
            document.querySelectorAll('[data-item-id]').forEach(function(el) {
                var id = parseInt(el.dataset.itemId);
                if (self.selectedIds.indexOf(id) < 0) self.selectedIds.push(id);
            });
        },
'''
new = '''        selectAll() {
            var self = this;
            document.querySelectorAll('[data-item-id], [data-item-ids]').forEach(function(el) {
                if (el.dataset.itemIds) {
                    self.groupIds(el.dataset.itemIds).forEach(function(id) {
                        if (self.selectedIds.indexOf(id) < 0) self.selectedIds.push(id);
                    });
                    return;
                }
                var id = parseInt(el.dataset.itemId);
                if (self.selectedIds.indexOf(id) < 0) self.selectedIds.push(id);
            });
        },
'''
if old not in text:
    raise SystemExit("selectAll block not found")
text = text.replace(old, new, 1)
p.write_text(text)


# Reuse series gap logic and add one name-keyed detail page to the existing router.
replace_once(
    "app/routers/series.py",
    "from fastapi import APIRouter, Depends, Form, Request\n",
    "from fastapi import APIRouter, Depends, Form, Request\nfrom fastapi.responses import RedirectResponse\n",
)
replace_once(
    "app/routers/series.py",
    "from app.services import lists\n",
    "from app.services import lists, series_browse\n",
)
p = Path("app/routers/series.py")
text = p.read_text()
start = text.index("def find_gaps(positions: list) -> list[int]:\n")
end = text.index("\n\n@router.get(\"/series\")", start)
text = text[:start] + "find_gaps = series_browse.find_gaps" + text[end:]
marker = '@router.get("/api/series/check")\nasync def check_series(name: str = "", _=Depends(require_role("viewer"))):\n'
detail = '''@router.get("/series/{name:path}")
async def series_detail_page(
    request: Request,
    name: str,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        series = series_browse.series_detail(db, name)
    if series is None:
        return RedirectResponse(url="/series", status_code=302)
    return request.app.state.templates.TemplateResponse(
        request, "series_detail.html", {"series": series}
    )


'''
if marker not in text:
    raise SystemExit("series check marker not found")
text = text.replace(marker, detail + marker, 1)
p.write_text(text)
