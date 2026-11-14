"""Real disposable-app HTTP/auth fallback plus explicitly asset-wired browsers.

Parent owns shared asset registration. `_wire_owned_assets` adds only the two
owned asset tags to real document responses for enhancement tests; those tests
are NOT evidence that base.html already loads them. The native fallback test
uses the unmodified app response with JavaScript disabled.
"""

import sqlite3
import uuid

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard

pytestmark = pytest.mark.e2e


@pytest.fixture
def locator_seed(live_server, setup_admin):
    suffix = uuid.uuid4().hex[:10]
    with sqlite3.connect(live_server["data_dir"] / "shelf.db") as conn:
        root = conn.execute(
            "INSERT INTO locations (name, label) VALUES (?, ?)",
            (f"RF11 Room / literal {suffix}", f"RF11 Room / literal {suffix}"),
        ).lastrowid
        shelf = conn.execute(
            "INSERT INTO locations (name, label, parent_id) VALUES (?, ?, ?)",
            (f"RF11 Room / literal {suffix} / Shelf", "Shelf", root),
        ).lastrowid
        primary_shelf = conn.execute(
            "INSERT INTO locations (name, label) VALUES (?, 'Primary shelf')",
            (f"RF11 Primary {suffix}",),
        ).lastrowid
        item = conn.execute(
            "INSERT INTO items (title, media_type, location_id) VALUES (?, 'book', ?)",
            (f"RF11 selected & <literal> {suffix} " + "long-unbroken-title-" * 8, primary_shelf),
        ).lastrowid
        primary = conn.execute(
            "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) VALUES (?, 1, ?, 1)",
            (item, primary_shelf),
        ).lastrowid
        ordered = []
        for n in range(13):
            neighbour_item = item if n == 9 else conn.execute(
                "INSERT INTO items (title, media_type) VALUES (?, 'book')",
                (f"RF11 neighbour {suffix} {n}",),
            ).lastrowid
            copy = conn.execute(
                "INSERT INTO item_copies (item_id, copy_number, location_id, position_order, copy_barcode, missing_since) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (neighbour_item, 9 if n == 9 else 1, shelf, n * 10,
                 f"EXACT-NINE-{suffix}-" + "long-barcode-" * 12 if n == 9 else None,
                 "2026-01-02" if n == 9 else None),
            ).lastrowid
            ordered.append(copy)
    return {"item": item, "copy": ordered[9], "primary": primary, "shelf": shelf,
            "primary_shelf": primary_shelf, "root": root, "ordered": ordered}


def _url(base, seed):
    return f'{base}/item/{seed["item"]}/copies/{seed["copy"]}/locate?at={seed["shelf"]}#copy-{seed["copy"]}'


def _wire_owned_assets(page):
    """Explicit test-only document wiring, after the unchanged shared app.js."""
    def wire(route):
        if route.request.resource_type != "document":
            route.continue_()
            return
        response = route.fetch()
        html = response.text()
        if "/static/css/copy-locator.source.css" not in html:
            html = html.replace('</head>', '<link rel="stylesheet" href="/static/css/copy-locator.source.css"></head>')
        if "/static/js/copy-locator.js" not in html:
            html = html.replace('</body>', '<script src="/static/js/copy-locator.js"></script></body>')
        route.fulfill(response=response, body=html)
    page.route("**/item/**", wire)


def _window_ids(page):
    return page.locator("[data-locator-copy-id]").evaluate_all(
        "nodes => nodes.map(node => Number(node.dataset.locatorCopyId))"
    )


def _assert_target_clear_and_reflowed(page, seed):
    expect(page.locator("[data-copy-locator-target]")).to_be_in_viewport()
    metrics = page.evaluate("""() => {
        const target = document.querySelector('[data-copy-locator-target]');
        const nav = document.querySelector('[data-media-navigation]');
        return {top: target.getBoundingClientRect().top,
                bottom: nav.getBoundingClientRect().bottom,
                width: innerWidth, height: innerHeight,
                scrollWidth: document.documentElement.scrollWidth,
                focus: document.activeElement.id,
                margin: parseFloat(getComputedStyle(target).scrollMarginBlockStart)};
    }""")
    assert metrics["focus"] == f'copy-{seed["copy"]}'
    assert metrics["top"] >= metrics["bottom"] + 12, metrics
    assert metrics["top"] < metrics["height"] - 40, metrics
    assert metrics["scrollWidth"] <= metrics["width"] + 1, metrics
    assert metrics["margin"] >= metrics["bottom"], metrics


def test_unmodified_http_native_exact_link_no_js_and_back(live_server, browser, authed_page, locator_seed):
    """No owned asset injection: verifies the actual registered page fallback."""
    seed = locator_seed
    base = live_server["url"]
    ctx = browser.new_context(java_script_enabled=False, storage_state=authed_page.context.storage_state(),
                              viewport={"width": 375, "height": 900})
    page = attach_page_guard(ctx.new_page())
    mutations = []
    page.on("request", lambda req: mutations.append(req.url) if req.method != "GET" else None)
    page.goto(f'{base}/item/{seed["item"]}?from=series')
    opener = page.locator(f'#copy-locator-open-{seed["copy"]}')
    expect(opener).to_have_text("Find this copy")
    assert "position" not in opener.evaluate("node => node.outerHTML")
    opener.focus()
    opener.press("Enter")
    page.wait_for_url(f'**/copies/{seed["copy"]}/locate?at={seed["shelf"]}&from=series#copy-{seed["copy"]}')
    selected = page.locator("[data-copy-locator-target]")
    expect(selected).to_contain_text("Selected copy")
    expect(selected).to_contain_text("Copy #9")
    expect(selected).to_contain_text("Marked missing since 2026-01-02")
    expect(selected).to_be_visible()
    assert _window_ids(page) == seed["ordered"][6:13]
    assert seed["primary"] not in _window_ids(page)
    assert page.locator('[data-copy-locator] form, [data-copy-locator] button').count() == 0
    page.get_by_role("link", name="Back to item", exact=True).click()
    page.wait_for_url(f'**/item/{seed["item"]}?from=series#copy-locator-open-{seed["copy"]}')
    expect(page.locator(f'#copy-locator-open-{seed["copy"]}')).to_be_visible()
    page.go_back()
    expect(page.locator("[data-copy-locator-target]")).to_contain_text("Selected copy")
    assert mutations == []
    assert_page_clean(page)
    ctx.close()


@pytest.mark.parametrize("width", [320, 375, 640, 768, 1024, 1280, 1366, 1920])
def test_asset_wired_exact_target_focus_clearance_and_reflow(live_server, authed_page, locator_seed, width):
    """Owned-asset geometry, not WCAG conformance/shared wiring acceptance."""
    seed = locator_seed
    page = authed_page
    _wire_owned_assets(page)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(_url(live_server["url"], seed))
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert _window_ids(page) == seed["ordered"][6:13]
    expect(page.get_by_role("link", name="Open full location in Arrange")).to_have_attribute(
        "href", f'/locations/{seed["shelf"]}/arrange')
    # A same-hash native jump is still keyboard operable and refocuses once.
    jump = page.get_by_role("link", name="Jump to selected copy")
    jump.focus()
    jump.press("Enter")
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("[data-copy-locator-target] a")).to_be_focused()
    page.get_by_role("link", name="Back to item", exact=True).click()
    page.wait_for_url(f'**/item/{seed["item"]}#copy-locator-open-{seed["copy"]}')
    expect(page.locator(f'#copy-locator-open-{seed["copy"]}')).to_be_focused()
    page.go_back()
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert_page_clean(page)


def test_asset_wired_touch_reduced_motion_zoom_and_idempotence(live_server, browser, authed_page, locator_seed):
    seed = locator_seed
    ctx = browser.new_context(has_touch=True, reduced_motion="reduce", viewport={"width": 640, "height": 900},
                              storage_state=authed_page.context.storage_state())
    page = attach_page_guard(ctx.new_page())
    _wire_owned_assets(page)
    page.goto(_url(live_server["url"], seed))
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    # CSS zoom is a rendered reflow probe; it is not a claim about browser UI zoom.
    page.evaluate("document.documentElement.style.zoom = '2'")
    page.set_viewport_size({"width": 640, "height": 1200})
    jump = page.get_by_role("link", name="Jump to selected copy")
    jump.tap()
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert page.locator("[data-copy-locator-target]").evaluate(
        "node => getComputedStyle(node).scrollBehavior") == "auto"
    assert page.locator("[data-copy-locator-target]").evaluate(
        "node => getComputedStyle(node).transitionDuration") == "0s"
    for _ in range(3):
        page.add_script_tag(url=f'{live_server["url"]}/static/js/copy-locator.js')
    page.evaluate("""() => {
        window.rf11FocusCalls = 0;
        const target = document.querySelector('[data-copy-locator-target]');
        const original = target.focus.bind(target);
        target.focus = options => { window.rf11FocusCalls++; original(options); };
    }""")
    jump.tap()
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    assert page.evaluate("window.rf11FocusCalls") == 1
    assert_page_clean(page)
    ctx.close()


def test_asset_wired_browser_move_and_rename_follow_current_exact_copy(live_server, authed_page, locator_seed):
    seed = locator_seed
    page = authed_page
    _wire_owned_assets(page)
    page.goto(_url(live_server["url"], seed))
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    with sqlite3.connect(live_server["data_dir"] / "shelf.db") as conn:
        new = conn.execute(
            "INSERT INTO locations (name, label) VALUES (?, 'Moved shelf')", (f'Moved {seed["copy"]}',),
        ).lastrowid
        conn.execute("UPDATE item_copies SET location_id = ?, position_order = NULL WHERE id = ?", (new, seed["copy"]))
    page.reload()
    expect(page.locator("[data-copy-location-changed]")).to_contain_text("Location changed")
    expect(page.locator("[data-copy-locator-target]")).to_contain_text("No position recorded")
    expect(page.locator("[data-copy-locator-target]")).to_contain_text("Copy #9")
    assert _window_ids(page) == [seed["copy"]]
    with sqlite3.connect(live_server["data_dir"] / "shelf.db") as conn:
        conn.execute("UPDATE locations SET name = ?, label = 'Renamed shelf' WHERE id = ?", (f'Renamed {seed["copy"]}', new))
    page.reload()
    expect(page.locator("[data-copy-current-location]")).to_have_text(f'Renamed {seed["copy"]}')
    expect(page.locator("[data-copy-locator-target]")).to_contain_text("Marked missing since 2026-01-02")
    assert_page_clean(page)


def test_asset_wired_no_js_native_anchor_has_sticky_clearance(live_server, browser, authed_page, locator_seed):
    """CSS-only fallback with test wiring, distinct from unmodified HTTP above."""
    seed = locator_seed
    ctx = browser.new_context(java_script_enabled=False, viewport={"width": 320, "height": 900},
                              storage_state=authed_page.context.storage_state())
    page = attach_page_guard(ctx.new_page())
    _wire_owned_assets(page)
    page.goto(_url(live_server["url"], seed))
    expect(page.locator("[data-copy-locator-target]")).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert page.evaluate("typeof window.__shelfCopyLocator") == "undefined"
    expect(page.locator("[data-copy-locator-target]")).to_contain_text("Selected copy")
    assert_page_clean(page)
    ctx.close()


def test_actual_viewer_login_can_locate_without_edit_or_found(live_server, browser, locator_seed):
    from app.auth import hash_password

    seed = locator_seed
    username = f'locator-viewer-{seed["copy"]}'
    password = "test-only-locator-viewer-password"
    with sqlite3.connect(live_server["data_dir"] / "shelf.db") as conn:
        conn.execute("INSERT INTO users (username, password, display_name, role) VALUES (?, ?, 'Locator viewer', 'viewer')",
                     (username, hash_password(password)))
    ctx = browser.new_context()
    page = attach_page_guard(ctx.new_page())
    base = live_server["url"]
    page.goto(f'{base}/login')
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(f'{base}/', timeout=10_000)
    mutations = []
    page.on("request", lambda req: mutations.append(req.url) if req.method != "GET" else None)
    page.goto(f'{base}/item/{seed["item"]}')
    expect(page.locator('#item-copies [data-testid^="copy-found-"]')).to_have_count(0)
    expect(page.locator('#item-copies [data-testid^="edit-copy-"]')).to_have_count(0)
    expect(page.locator('#item-copies [data-testid^="copy-missing-"]')).to_be_visible()
    page.locator(f'#copy-locator-open-{seed["copy"]}').press("Enter")
    page.wait_for_url(f'**/copies/{seed["copy"]}/locate?at={seed["shelf"]}#copy-{seed["copy"]}')
    expect(page.locator('[data-copy-locator-target]')).to_contain_text("Copy #9")
    expect(page.locator('[data-copy-locator] form, [data-copy-locator] button')).to_have_count(0)
    assert mutations == []
    assert_page_clean(page)
    ctx.close()
