"""All page templates: desktop width use, mobile fit, and visual audit artifacts.

Unlike overflow-only tests, this catches a phone-sized page centered inside a
perfectly fitting laptop shell. All data is disposable; external I/O is refused.
Set SHELF_LAYOUT_ARTIFACTS to retain screenshots and measured geometry.
"""

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    ADMIN_DISPLAY,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    assert_page_clean,
    attach_page_guard,
)
from tests.e2e.test_responsive import _COLLAPSE, _MEASURE, _settle
from tests.e2e.test_source_delivery import seed_progress_layout
from tests.e2e.test_library_delivery import (
    _decision,
    _repair_preview,
    _replacement_preview,
    _seed_machine_records,
    _seed_reading_life,
)


pytestmark = pytest.mark.e2e
WIDTHS = (320, 375, 640, 768, 1024, 1280, 1366, 1920)

# Every routable template, plus alternate views and independently hidden panes.
# Auth and disabled states are visited explicitly by the test below.
PAGES = (
    ("home", "/", "home.html"),
    ("library", "/library", "library.html"),
    ("library-series", "/library?view=series", "library.html"),
    ("activity", "/activity", "activity.html"),
    ("reading-life", "/reading-life?year=2025", "reading_life.html"),
    ("reading-life-item", "/reading-life/items/{book}", "reading_life_item.html"),
    ("reading-life-session", "/reading-life/sessions/{reading_session}", "reading_life_session.html"),
    ("reading-life-journal", "/reading-life/journal/{reading_entry}", "reading_life_journal.html"),
    ("source-progress", "/account/source-progress?item_id={audio}", "source_progress.html"),
    ("source-progress-expanded", "/account/source-progress?item_id={audio}", "source_progress.html"),
    ("audiobook-metadata", "/api/audiobooks/{audio}/metadata", "audiobook_metadata.html"),
    ("browse", "/browse", "browse.html"),
    ("browse-list", "/browse", "browse.html"),
    ("series", "/series", "series.html"),
    ("music", "/music", "music.html"),
    ("music-item", "/music/item/{music}", "music_item.html"),
    ("periodicals", "/periodicals", "periodicals.html"),
    ("publication", "/periodicals/{publication}", "periodical_publication.html"),
    ("scan", "/scan", "scan.html"),
    ("scan-result", "/scan", "scan.html"),
    ("scan-manual", "/scan?add=manual", "scan.html"),
    ("shelf-fill", "/shelf-fill", "shelf_fill.html"),
    ("intake", "/intake", "intake.html"),
    ("intake-review", "/intake", "intake.html"),
    ("item-detail", "/item/{book}", "item_detail.html"),
    ("item-edit", "/item/{book}/edit", "item_edit.html"),
    ("sources", "/sources", "sources.html"),
    ("source-opds", "/sources/{opds}", "source_detail.html"),
    ("source-links", "/sources/{links}", "source_detail.html"),
    ("source-migration-workspace", "/source-migrations", "source_migration_inventory.html"),
    ("source-migration-compare", "/source-migrations?old_source_id={old_source}&new_source_id={new_source}", "source_migration_inventory.html"),
    ("source-migration-repair", "/source-migrations?repair_source_id={new_source}&repair_record_id={new_record}", "source_migration_inventory.html"),
    ("source-migration-repair-preview", "/source-migrations/{repair_plan}", "source_migration_review.html"),
    ("source-migration-preview", "/source-migrations/{migration_plan}", "source_migration_review.html"),
    ("source-migration-applied", "/source-migrations/{migration_plan}", "source_migration_review.html"),
    ("source-migration-retired", "/source-migrations/{migration_plan}", "source_migration_review.html"),
    ("source-migration-rolled-back", "/source-migrations/{migration_plan}", "source_migration_review.html"),
    ("source-migration-stale", "/source-migrations/{stale_plan}", "source_migration_review.html"),
    ("source-migration-error", "/source-migrations/layout-missing-review", "source_migration_error.html"),
    ("attention", "/needs-attention", "needs_attention.html"),
    ("covers", "/cover-review", "cover_review.html"),
    ("duplicates", "/duplicates", "duplicates.html"),
    ("trash", "/trash", "trash.html"),
    ("audits", "/audits", "audits.html"),
    ("audit", "/audits/{audit}", "audit.html"),
    ("labels", "/labels", "labels.html"),
    ("label-sheet", "/labels/sheet", "labels_sheet.html"),
    ("label-sheet-a4", "/labels/sheet?preset=averyl7160", "labels_sheet.html"),
    ("arrange", "/locations/{location}/arrange", "location_order.html"),
    ("upcoming", "/upcoming", "upcoming.html"),
    ("discover", "/discover", "discover.html"),
    ("stats", "/stats", "stats.html"),
    ("wrapup", "/stats/wrapup", "stats_wrapup.html"),
    ("valuation", "/api/valuation/report", "valuation_report.html"),
    ("logs", "/logs", "logs.html"),
    *((f"settings-{tab}", "/settings", "settings.html") for tab in
      ("library", "integrations", "data", "users", "features")),
    ("share", "/share/layout-audit-share", "share.html"),
    ("store", "/store", "store.html"),
    ("store-result", "/store", "store.html"),
)

# Refusal pages are measured, not exempted from the walk or the geometry rules.
PAGE_STATUSES = {"source-migration-error": 404}

_GEOMETRY = """(() => {
    const main = document.querySelector('main');
    if (!main) return null;
    const cs = getComputedStyle(main), r = main.getBoundingClientRect();
    const first = [...main.children].find(el => {
        const b = el.getBoundingClientRect();
        return !['SCRIPT', 'STYLE', 'TEMPLATE'].includes(el.tagName) && b.width > 0 && b.height > 0
            && getComputedStyle(el).position !== 'fixed';
    });
    const b = first ? first.getBoundingClientRect() : r;
    const header = document.querySelector('body > nav > div, body > header > div');
    const hr = header.getBoundingClientRect(), hs = getComputedStyle(header);
    return {shell: r.width, content: r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
            page: b.width, pageLeft: b.left, contentLeft: r.left + parseFloat(cs.paddingLeft),
            gutter: parseFloat(cs.paddingLeft), rightGutter: parseFloat(cs.paddingRight), header: hr.width,
            headerContentLeft: hr.left + parseFloat(hs.paddingLeft)};
})()"""

_COLUMNS = """(() => [...document.querySelectorAll('.workspace-layout, .edit-layout, [data-testid="labels-workspace"], [data-testid="source-workspace"]')].map(el => {
    const children = [...el.children].map(child => child.getBoundingClientRect()).filter(r => r.width && r.height);
    return {label: el.getAttribute('data-testid') || 'item-edit', width: el.getBoundingClientRect().width,
            first: children[0].toJSON(), second: children[1].toJSON()};
}))()"""


def _seed(server):
    root = server["data_dir"]
    db = sqlite3.connect(root / "shelf.db")
    try:
        location = db.execute("INSERT INTO locations (name, label) VALUES ('Office reading shelves', 'Office reading shelves')").lastrowid
        ids = {}
        for index, (key, media, title) in enumerate((
            ("book", "book", "The Long Way Around: An Illustrated Field Guide"),
            ("ebook", "ebook", "The Long Way Around — digital edition"),
            ("music", "vinyl", "Evening Signals: Collected Recordings"),
            ("issue", "magazine", "The Observatory — Autumn Edition"),
            ("game", "video_game", "Horizons Beyond the Archive"),
            ("audio", "audiobook", "The Long Way Around — a narrated archive edition"),
            ("duplicate-a", "book", "A Layout Audit Duplicate"),
            ("duplicate-b", "book", "A Layout Audit Duplicate"),
        )):
            cover = f"layout-{key}.svg"
            (root / "covers" / cover).write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="240">'
                f'<rect width="160" height="240" fill="{("#434879", "#275a5f", "#6a414b")[index % 3]}"/>'
                '<circle cx="80" cy="100" r="42" fill="#ffffff" opacity=".15"/>'
                '<path d="M25 180h110M25 190h80" stroke="#ffffff" opacity=".5"/>'
                '</svg>'
            )
            ids[key] = db.execute(
                "INSERT INTO items (title, media_type, authors, publisher, description, cover_path, series_name, series_position, manual_value) "
                "VALUES (?, ?, 'Rowan Avery, Morgan Lee', 'The Independent Press', ?, ?, 'The Archive Collection', ?, 12.50)",
                (title, media, "A substantial description with readable words, editions and long descriptive metadata. " * 4,
                 f"covers/{cover}", index + 1),
            ).lastrowid
        for copy_number in (1, 2):
            db.execute("INSERT INTO item_copies (item_id, copy_number, location_id, is_primary, copy_barcode) VALUES (?, ?, ?, ?, ?)",
                       (ids["book"], copy_number, location, int(copy_number == 1), f"LAYOUT-{copy_number:06}"))
        db.execute("UPDATE items SET location_id = ? WHERE id = ?", (location, ids["book"]))
        db.execute("UPDATE items SET isbn = '9780140328721' WHERE id = ?", (ids["book"],))
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (ids["book"], ids["ebook"]))
        user = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()[0]
        db.execute("INSERT INTO user_item_state (user_id, item_id, status, progress, on_list, favourite, notes) VALUES (?, ?, 'in_progress', 35, 1, 1, 'Resume after the field notes.')", (user, ids["book"]))
        db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'updated', ?)",
                   (user, ids["book"], json.dumps({"status": "in_progress", "progress": 35, "notes": "Resume after the field notes."})))
        ids["publication"] = db.execute("INSERT INTO periodical_publications (title, publisher) VALUES ('The Observatory Journal of Science and Exploration', 'The Independent Press')").lastrowid
        db.execute("INSERT INTO periodical_issues (item_id, publication_id, issue_number, issue_date) VALUES (?, ?, 'Autumn 42', '2026-10-01')", (ids["issue"], ids["publication"]))
        db.execute("INSERT INTO music_releases (item_id, artist_credit, release_type, label, catalog_number) VALUES (?, 'Rowan Avery Ensemble', 'album', 'Independent Recording Company', 'ARC-2026-001')", (ids["music"],))
        medium = db.execute("INSERT INTO music_media (item_id, position, format, title) VALUES (?, 1, 'Vinyl', 'Evening side')", (ids["music"],)).lastrowid
        db.execute("INSERT INTO music_tracks (medium_id, position, title, duration_ms) VALUES (?, 1, 'A Deliberately Long Track Name for Reading at Laptop Widths', 245000)", (medium,))
        ids["audit"] = db.execute("INSERT INTO audit_sessions (scope, location_name, notes) VALUES ('library', 'Whole library', 'Check all shelves and preserve annotations.')").lastrowid
        db.execute("INSERT INTO items (title, deleted_at) VALUES ('A discarded catalogue record', datetime('now'))")
        ids["links"] = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('links', 'Reading room external links', 'http://127.0.0.1:1')").lastrowid
        ids["opds"] = db.execute("INSERT INTO source_instances (provider, name, base_url, last_status, last_error) VALUES ('opds', 'Reading room — digital books and special editions', 'http://127.0.0.1:1/opds', 'error', 'connection_failed')").lastrowid
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type, open_url) VALUES (?, ?, 'layout:ebook', 'Reading room digital edition', 'ebook', 'http://127.0.0.1:1/read')", (ids["links"], ids["ebook"]))
        db.execute("INSERT INTO share_links (token, scope, label) VALUES ('layout-audit-share', 'collection', 'Shared reading room library')")
        watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('The Archive Collection')").lastrowid
        db.execute("INSERT INTO upcoming_releases (watch_id, hc_book_id, title, authors, release_date) VALUES (?, 12345, 'An Upcoming Archive Collection Volume', 'Rowan Avery', '2099-01-01')", (watch,))
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('vision_provider', 'ollama')")
        ids.update(_seed_reading_life(db, user, ids["book"]))
        ids.update(_seed_machine_records(db, keep=ids["book"], previous=ids["ebook"]))
        db.execute("UPDATE items SET asin = 'B012345678' WHERE id = ?", (ids["audio"],))
        seed_progress_layout(db, user, ids["audio"])
        db.commit()
        return {**ids, "location": location}
    finally:
        db.close()


def _capture(page, label, width, records, *, app_page=True):
    _settle(page)
    failures = []
    heading = page.get_by_role("heading", level=1).first
    if heading.count():
        expect(heading).to_be_visible()
    else:
        # Keep the heading requirement, but finish measuring all routes so one
        # incomplete page cannot hide unrelated geometry regressions.
        failures.append(f"{label}: missing visible level-one page heading")
    overflow = page.evaluate(_MEASURE)
    controls = page.evaluate(_COLLAPSE)
    geometry = page.evaluate(_GEOMETRY)
    columns = page.evaluate(_COLUMNS)
    record = {"page": label, "width": width, "geometry": geometry, "overflow": overflow, "controls": controls, "columns": columns}
    records.append(record)
    output = os.environ.get("SHELF_LAYOUT_ARTIFACTS")
    if output:
        folder = Path(output) / str(width)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=folder / f"{label}.png", full_page=True, animations="disabled")
        (folder / "geometry.json").write_text(json.dumps(records, indent=2))
    if overflow["scrollWidth"] > overflow["clientWidth"] + 1:
        failures.append(f"{label}: horizontal overflow {overflow}")
    if controls:
        failures.append(f"{label}: squeezed controls {controls}")
    if app_page:
        if geometry is None:
            failures.append(f"{label}: missing main landmark")
        else:
            if abs(geometry["shell"] - min(width, 1536)) > 1:
                failures.append(f"{label}: shell {geometry['shell']}px does not use the shared desktop width")
            gutter = min(80, max(48, width * 0.05)) if width >= 1024 else 24 if width >= 640 else 16
            if (abs(geometry["gutter"] - gutter) > 0.1 or abs(geometry["rightGutter"] - gutter) > 0.1
                    or abs(geometry["header"] - geometry["shell"]) > 1
                    or abs(geometry["headerContentLeft"] - geometry["contentLeft"]) > 1):
                failures.append(f"{label}: header/page gutters are inconsistent {geometry}")
            if abs(geometry["page"] - geometry["content"]) > 1 or abs(geometry["pageLeft"] - geometry["contentLeft"]) > 1:
                failures.append(f"{label}: inner page remains capped/centered {geometry}")
    else:
        card = page.locator("body > div").bounding_box()
        if abs(card["width"] - min(width - 32, 448 if label == "setup" else 384)) > 1:
            failures.append(f"{label}: authentication card lost its intentional width {card}")
    for column in columns:
        first, second = column["first"], column["second"]
        seam = 1024 if column["label"] == "labels-workspace" else 1280
        if width >= seam:
            if abs(first["top"] - second["top"]) > 1 or first["right"] >= second["left"]:
                failures.append(f"{label}: desktop workspace did not split into columns {column}")
        elif second["top"] < first["bottom"] or abs(first["width"] - column["width"]) > 1:
            failures.append(f"{label}: narrow workspace did not stack {column}")
    return failures


def _check_print(page, label, width):
    """The wider app shell must not resize physical labels or clip PDF tables."""
    page.emulate_media(media="print")
    try:
        if label.startswith("label-sheet"):
            expect(page.locator("[data-label]")).to_have_count(2)
            sheet = page.locator(".label-sheet").bounding_box()
            millimetres = (210, 297) if label.endswith("a4") else (215.9, 279.4)
            assert abs(sheet["width"] - millimetres[0] * 96 / 25.4) < 1
            assert abs(sheet["height"] - millimetres[1] * 96 / 25.4) < 1
            assert sheet["x"] == 0 and sheet["y"] == 0
            expect(page.locator("body > nav")).to_be_hidden()
        else:
            assert page.locator("tbody tr").count() == 8
            assert page.locator("table").first.evaluate("el => getComputedStyle(el.parentElement).overflowX") == "visible"
        output = os.environ.get("SHELF_LAYOUT_ARTIFACTS")
        if output and width == 1366:
            page.pdf(path=Path(output) / str(width) / f"{label}.pdf", prefer_css_page_size=True)
    finally:
        page.emulate_media(media="screen")


@pytest.mark.parametrize("width", WIDTHS)
def test_all_pages_use_available_width_without_mobile_regressions(server_factory, browser, width):
    server = server_factory()
    context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
    context.add_init_script("localStorage.setItem('shelf-welcome-dismissed', '1'); localStorage.setItem('shelf-shortcuts-seen', '1');")
    external = []

    def local_only(route):
        if urlsplit(route.request.url).hostname in ("127.0.0.1", "localhost"):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    failures, records = [], []
    try:
        page.goto(server["url"] + "/setup")
        failures.extend(_capture(page, "setup", width, records, app_page=False))
        page.locator('[name="username"]').fill(ADMIN_USERNAME)
        page.locator('[name="display_name"]').fill(ADMIN_DISPLAY)
        page.locator('[name="password"]').fill(ADMIN_PASSWORD)
        page.locator('[name="password_confirm"]').fill(ADMIN_PASSWORD)
        page.locator('[name="profile"][value="everything"]').check()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator('button[type="submit"]').click()
        expect(page).to_have_url(server["url"] + "/")
        ids = _seed(server)
        ids["repair_plan"] = _repair_preview(page, server, ids)
        ids["migration_plan"] = _replacement_preview(page, server, ids, manual=False)
        # A second real review becomes stale when the first review is applied.
        ids["stale_plan"] = _replacement_preview(page, server, ids, manual=False)
        for label, path, template in PAGES:
            if label.startswith("settings-"):
                page.evaluate("tab => localStorage.setItem('shelf_settings_tab', tab)", label.removeprefix("settings-"))
            if label == "browse-list":
                page.evaluate("localStorage.setItem('shelf-view', 'list')")
            elif label == "browse":
                page.evaluate("localStorage.setItem('shelf-view', 'grid')")
            response = page.goto(server["url"] + path.format(**ids))
            assert response.status == PAGE_STATUSES.get(label, 200), (label, response.status)
            if label == "source-progress-expanded":
                for details in page.get_by_test_id("source-progress-page").locator("details").all():
                    if details.get_attribute("open") is None:
                        details.locator("summary").click()
                    expect(details).to_have_attribute("open", "")
                expect(page.locator('input[name="remote_user_id"]')).to_be_visible()
                expect(page.locator('select[name="interval_minutes"]')).to_be_visible()
                expect(page.locator("[data-progress-review]")).to_be_visible()
                expect(page.locator("#source-progress-history")).to_contain_text("Accepted remote progress")
                assert "invalid-token-for-read-only-layout" not in page.content()
            if label == "source-migration-compare":
                for details in page.get_by_test_id("source-migration-workspace").locator("details").all():
                    details.locator("summary").click()
                    expect(details).to_have_attribute("open", "")
                expect(page.locator('textarea[name="mappings"]')).to_have_value(f"{ids['old_record']},{ids['new_record']}\n")
            if label in ("source-migration-applied", "source-migration-retired", "source-migration-rolled-back"):
                action = {"source-migration-applied": "apply", "source-migration-retired": "retire", "source-migration-rolled-back": "rollback"}[label]
                _decision(page, action)
            if label in ("source-migration-repair-preview", "source-migration-preview", "source-migration-applied", "source-migration-retired", "source-migration-rolled-back"):
                expected = ("Bindings rolled back" if label.endswith("rolled-back") else
                            "Reviewed bindings applied" if label.endswith(("applied", "retired")) else "Dry run only")
                expect(page.get_by_test_id("source-migration-review").get_by_role("status")).to_contain_text(expected)
            if label == "source-migration-repair-preview":
                expect(page.get_by_role("alert")).to_contain_text("Different source and local media types")
            if label == "source-migration-stale":
                expect(page.get_by_role("alert")).to_contain_text("This review is stale")
                expect(page.get_by_test_id("source-migration-review").locator('form[method="post"]')).to_have_count(0)
            if label == "scan-result":
                page.locator("#isbn-input").fill("LAYOUT-000001")
                page.locator("#isbn-input").press("Enter")
                expect(page.locator(".scan-result").first).to_be_visible()
            if label == "store-result":
                with page.expect_response("**/api/store/data") as data:
                    page.reload()
                assert data.value.ok
                page.get_by_test_id("isbn-input").fill("9780140328721")
                page.get_by_test_id("check-button").click()
                expect(page.locator("#verdict-label")).to_have_text("OWNED")
            if label == "source-opds":
                page.get_by_test_id("source-configuration").locator("summary").click()
                expect(page.locator('input[name="username"]')).to_be_visible()
            if label == "intake-review":
                page.route("**/api/intake/plan", lambda route: route.fulfill(json={
                    "ok": True, "factor": 1, "needs_choice": False, "low_res": False,
                    "preview": {"w": 640, "h": 480}, "tiles": [], "grid": {"rows": 1, "cols": 1},
                    "cost_as_is_usd": None, "cost_tiled_usd": None,
                }))
                page.route("**/api/intake/analyze", lambda route: route.fulfill(json={"ok": True, "books": [{
                    "title": "A Deliberately Long Recognized Book Title", "authors": "Rowan Avery",
                    "isbn": None, "media_type": "book", "source": "recognized",
                }]}))
                page.get_by_test_id("intake-choose-input").set_input_files(Path(__file__).parents[1] / "fixtures/intake/eleven_books.jpg")
                page.get_by_role("button", name="Read Photo", exact=True).click()
                expect(page.get_by_test_id("intake-row")).to_be_visible()
            failures.extend(_capture(page, label, width, records))
            if label == "settings-integrations":
                # These are panels within Integrations, not independent tab
                # names. Assert the real controls are painted before auditing.
                for panel in ("romm", "komga", "discogs"):
                    target = page.locator(f"#{panel}-panel")
                    expect(target.locator("input").first).to_be_visible()
                    output = os.environ.get("SHELF_LAYOUT_ARTIFACTS")
                    if output:
                        target.screenshot(path=Path(output) / str(width) / f"settings-{panel}.png")
            if label.startswith("label-sheet") or label == "valuation":
                _check_print(page, label, width)
        # A feature refusal is a page too, and must use the same outer shell.
        result = page.request.post(server["url"] + "/api/settings/features/connected_library", form={"enabled": "0"},
                                   headers={"X-CSRF-Token": next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "csrf_token")})
        assert result.ok
        response = page.goto(server["url"] + "/library")
        assert response.status == 200
        expect(page.get_by_test_id("feature-disabled")).to_be_visible()
        failures.extend(_capture(page, "feature-disabled", width, records))
        assert_page_clean(page)
        assert not external, external
    finally:
        context.close()
    login_context = browser.new_context(viewport={"width": width, "height": 900})
    login_page = attach_page_guard(login_context.new_page())
    try:
        login_page.goto(server["url"] + "/login")
        failures.extend(_capture(login_page, "login", width, records, app_page=False))
        assert_page_clean(login_page)
    finally:
        login_context.close()
    assert not failures, "\n".join(failures)


def test_every_top_level_template_is_in_the_layout_walk():
    templates = {path.name for path in (Path(__file__).parents[2] / "app/templates").glob("*.html")} - {"base.html"}
    walked = {template for label, path, template in PAGES} | {"login.html", "setup.html", "feature_disabled.html"}
    assert walked == templates, f"Missing page audit coverage: {sorted(templates - walked)}"
