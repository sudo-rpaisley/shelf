"""Future fresh-synthetic history/menu journeys; authored, not executed here.

Real Back/Forward tests cover user-visible behavior whether the browser reloads
or uses BFCache. Explicit PageTransitionEvent tests exercise the BFCache branch
with native input and real cookies, but are NOT evidence of an actually frozen
document. Native BFCache eligibility/lifecycle still needs separate evidence.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.e2e.test_connected_library import _add_user, _login, connected_library  # noqa: F401
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_shared_cover_size_setting import _choose, _key, _logout, _seed_active


pytestmark = pytest.mark.e2e
ENDPOINT = "/api/ui/cover-size-owner"


def _restore_branch(page):
    # Deliberately injected lifecycle signal, never claimed as a native restore.
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")


def _ready(page):
    expect(page.locator("[data-cover-size-control]")).to_have_attribute("data-cover-size-persistence", "ready")
    expect(page.locator("[data-cover-size-input]")).to_be_enabled()


@pytest.mark.parametrize("width", [320, 1366])
def test_real_back_forward_resumes_latest_size_and_saves_without_refresh(connected_library, width):
    server, page = connected_library, connected_library["page"]
    _seed_active(server, f"history-{width}")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(server["url"] + "/")
    domain = _domain_snapshot(server)
    _choose(page, 200)
    key = _key(page)
    page.goto(server["url"] + "/library?view=items")
    _choose(page, 140)
    page.go_back()
    expect(page).to_have_url(server["url"] + "/")
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("140%")
    _choose(page, 60)
    assert page.evaluate("key => localStorage.getItem(key)", key) == "60"
    page.go_forward()
    expect(page).to_have_url(server["url"] + "/library?view=items")
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("60%")
    _choose(page, 180)
    page.go_back()
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("180%")
    _choose(page, 100)
    assert page.evaluate("key => localStorage.getItem(key)", key) == "100"
    assert _domain_snapshot(server) == domain
    assert_page_clean(page)


def test_modeled_restore_held_reply_keeps_native_edits_local_until_a_later_real_change(connected_library):
    server, page = connected_library, connected_library["page"]
    page.goto(server["url"] + "/")
    _choose(page, 200)
    key, domain, held = _key(page), _domain_snapshot(server), []
    pattern = server["url"] + ENDPOINT
    page.route(pattern, lambda route: held.append(route))
    try:
        with page.expect_request(pattern):
            _restore_branch(page)
        expect(page.locator("[data-cover-size-control]")).to_have_attribute("data-cover-size-persistence", "checking")
        _choose(page, 140)  # Real native range input, not a synthetic input event.
        assert page.evaluate("key => localStorage.getItem(key)", key) == "200"
        assert len(held) == 1
        with page.expect_response(pattern):
            held[0].fulfill(response=held[0].fetch())
        _ready(page)
        expect(page.locator("[data-cover-size-output]")).to_have_text("140%")
        expect(page.locator("[data-cover-size-status]")).to_contain_text("move the slider again")
        assert page.evaluate("key => localStorage.getItem(key)", key) == "200"
        page.locator("[data-cover-size-input]").press("ArrowRight")
        expect(page.locator("[data-cover-size-output]")).to_have_text("160%")
        assert page.evaluate("key => localStorage.getItem(key)", key) == "160"
        assert _domain_snapshot(server) == domain
        assert_page_clean(page)
    finally:
        page.unroute(pattern)


def test_failed_confirmation_can_retry_without_refresh_or_saving_pending_choice(connected_library):
    server, page = connected_library, connected_library["page"]
    page.goto(server["url"] + "/")
    _choose(page, 200)
    key, domain = _key(page), _domain_snapshot(server)
    pattern = server["url"] + ENDPOINT
    page.route(pattern, lambda route: route.fulfill(status=503, content_type="application/json", body='{"ok":false}'))
    with page.expect_response(pattern):
        _restore_branch(page)
    expect(page.locator("[data-cover-size-retry]")).to_be_visible()
    _choose(page, 50)
    assert page.evaluate("key => localStorage.getItem(key)", key) == "200"
    page.unroute(pattern)
    with page.expect_response(pattern):
        page.get_by_role("button", name="Retry saving", exact=True).click()
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("50%")
    assert page.evaluate("key => localStorage.getItem(key)", key) == "200"
    page.locator("[data-cover-size-input]").press("ArrowRight")
    assert page.evaluate("key => localStorage.getItem(key)", key) == "60"
    assert _domain_snapshot(server) == domain
    assert_page_clean(page)


def test_changed_login_cannot_rebind_a_restored_shell_or_write_either_accounts_size(connected_library):
    server, original = connected_library, connected_library["page"]
    username, password = "synthetic-history-viewer", "synthetic-history-password"
    _add_user(original, server, username, password)
    original.goto(server["url"] + "/")
    _choose(original, 200)
    original_key = _key(original)
    other = attach_page_guard(original.context.new_page())
    try:
        other.goto(server["url"] + "/")
        _logout(other, server)
        _login(other, server, username, password)
        _choose(other, 60)
        other_key, domain = _key(other), _domain_snapshot(server)
        assert other_key != original_key
        with original.expect_response(server["url"] + ENDPOINT):
            _restore_branch(original)
        expect(original.locator("[data-cover-size-control]")).to_have_attribute("data-cover-size-persistence", "page-only")
        expect(original.locator("[data-cover-size-status]")).to_contain_text("sign-in has changed")
        _choose(original, 140)
        assert original.evaluate("keys => keys.map(key => localStorage.getItem(key))", [original_key, other_key]) == ["200", "60"]
        assert original.locator("html").get_attribute("data-cover-size-account") != other.locator("html").get_attribute("data-cover-size-account")
        assert _domain_snapshot(server) == domain
        assert_page_clean(original)
        assert_page_clean(other)
    finally:
        other.close()


@pytest.mark.parametrize("width", [320, 1366])
def test_visible_library_menu_and_accessible_name_keep_existing_destination(connected_library, width):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(server["url"] + "/")
    if width == 320:
        page.get_by_test_id("nav-menu-button").click()
    summary = page.locator("[data-nav-browse] > summary")
    expect(summary).to_have_text("Library")
    expect(summary).to_have_attribute("aria-label", "Browse library")
    summary.click()
    expect(page.locator('[data-nav-primary="browse"]')).to_have_attribute("href", "/library?reset=1&view=items&sort=recent")
    assert_page_clean(page)
