"""Real compiled-locale journeys; no translated response/asset interception.

Each case owns a fresh server and browser context. The expected translations are
the canonical Italian/Simplified Chinese copy proposed for the parent-owned
catalogs, not gettext-derived expectations that could accept English fallback.
"""

from contextlib import closing, contextmanager
import sqlite3
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)
from tests.e2e.test_connected_library import _add_user
from tests.e2e.test_item_copies import _copy_row, _insert_location


pytestmark = pytest.mark.e2e

LOCALES = {
    "it": {
        "browser": "it-IT",
        "title": "Scopri — Shelf",
        "heading": "Scopri libri",
        "connect": "Collega il tuo account Hardcover per cercare libri e aggiungerli alla tua lista dei desideri.",
        "settings": "Vai alle impostazioni",
        "not_connected": "Hardcover non è collegato. Chiedi a un amministratore di configurare l'integrazione.",
        "confirm": "Spostare la copia n. %(n)s nel Cestino? Per recuperarla, ripristinala dal Cestino.",
    },
    "zh_Hans": {
        "browser": "zh-CN",
        "title": "发现 — Shelf",
        "heading": "发现图书",
        "connect": "关联你的 Hardcover 账号，即可搜索图书并将其加入心愿单。",
        "settings": "前往设置",
        "not_connected": "尚未关联 Hardcover。请管理员配置此集成。",
        "confirm": "将副本 #%(n)s 移至回收站？可从回收站还原该副本。",
    },
}
USER_VALUE = 'Copia 用户 <copy-probe> & "quoted" 100%d'


def _assert_document(response):
    assert response is not None and response.status == 200
    directives = {
        tokens[0]: tokens[1:]
        for part in response.headers["content-security-policy"].split(";")
        if (tokens := part.split())
    }
    assert directives["script-src"] == ["'self'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["form-action"] == ["'self'"]


@contextmanager
def _page(browser, server, **options):
    context = browser.new_context(service_workers="block", **options)
    blocked, console_errors = [], []
    origin = urlsplit(server["url"]).netloc

    def local_only(route):
        if urlsplit(route.request.url).netloc == origin:
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    # Keep every console error, including CSP failures; no text-based exemptions.
    page.on("console", lambda message: console_errors.append(message.text)
            if message.type == "error" else None)
    try:
        yield page
        assert_page_clean(page)
        assert console_errors == [], console_errors
        assert blocked == [], blocked
    finally:
        # Retain diagnostics even when a preceding locale assertion fails. Do not
        # mask that failure with a guard assertion in finally (G44).
        if console_errors:
            print("Unfiltered browser console errors:", console_errors)
        if blocked:
            print("Blocked non-local browser requests:", blocked)
        context.close()


def _login(page, server, credentials):
    _assert_document(page.goto(f"{server['url']}/login", wait_until="load"))
    page.locator('input[name="username"]').fill(credentials["username"])
    page.locator('input[name="password"]').fill(credentials["password"])
    with page.expect_navigation(wait_until="load") as navigation:
        page.locator('button[type="submit"]').click()
    _assert_document(navigation.value)
    expect(page).to_have_url(f"{server['url']}/")


def _pick_language(page, server, locale):
    """The actual Account-picker/reload path used by test_language.py."""
    _assert_document(page.goto(f"{server['url']}/browse", wait_until="load"))
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-profile").click()
    picker = page.get_by_test_id("account-language")
    expect(picker).to_be_visible()
    with page.expect_navigation(wait_until="load") as navigation:
        picker.select_option(locale)
    _assert_document(navigation.value)
    expect(page.locator("html")).to_have_attribute("lang", locale.replace("_", "-"))


def _physical_snapshot(server):
    """Physical rows, including Trash: a live-view-only check could miss a delete."""
    with closing(sqlite3.connect(server["data_dir"] / "shelf.db")) as db:
        return {
            table: tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("items", "item_copies", "locations", "list_items", "checkouts")
        }


def _seed_copies(server):
    from app.services.item_copies import insert_copy

    data_dir = server["data_dir"]
    location = _insert_location(data_dir, "Locale test shelf 用户")
    decoy = insert_item(data_dir, title="Locale decoy", location_id=location)
    item = insert_item(data_dir, title=USER_VALUE, location_id=location)
    with closing(sqlite3.connect(data_dir / "shelf.db")) as db:
        insert_copy(db, {"item_id": decoy, "copy_number": 1, "is_primary": 1,
                         "location_id": location})
        insert_copy(db, {"item_id": item, "copy_number": 1, "is_primary": 1,
                         "location_id": location})
        copy = insert_copy(db, {"item_id": item, "copy_number": 27, "is_primary": 0,
                               "location_id": location, "condition": USER_VALUE})
        db.commit()
    row = _copy_row(data_dir, copy)
    assert row["copy_number"] == 27 and row["copy_barcode"] is None
    assert 27 not in {item, copy}, "Distinguish copy number from item/copy identity"
    return {"item": item, "copy": copy, "number": row["copy_number"]}


def _open_panel(page, server, seed, locale, *, collector):
    _assert_document(page.goto(f"{server['url']}/item/{seed['item']}", wait_until="load"))
    expect(page.locator("html")).to_have_attribute("lang", locale.replace("_", "-"))
    expect(page.locator("h1")).to_have_text(USER_VALUE)
    expect(page.locator("copy-probe")).to_have_count(0)
    expect(page.get_by_test_id("add-copy")).to_be_visible()
    expect(page.get_by_test_id("print-copy-labels")).to_have_count(1 if collector else 0)
    path = f"/api/items/{seed['item']}/copies/{seed['copy']}/edit"
    with page.expect_response(
        lambda response: urlsplit(response.url).path == path and response.request.method == "GET"
    ) as fragment:
        page.get_by_test_id(f"edit-copy-{seed['copy']}").click()
    _assert_document(fragment.value)
    assert fragment.value.request.headers["hx-request"] == "true"
    expect(page.locator(f"#copy-condition-{seed['copy']}")).to_have_value(USER_VALUE)
    expect(page.get_by_test_id("assign-copy-code")).to_have_count(1 if collector else 0)
    remove = page.get_by_test_id("remove-copy")
    expect(remove).to_be_visible()
    expect(remove).to_have_attribute("type", "button")
    expect(remove).to_have_attribute("hx-delete", f"/api/items/{seed['item']}/copies/{seed['copy']}")
    expected = LOCALES[locale]["confirm"] % {"n": seed["number"]}
    confirmation = remove.get_attribute("hx-confirm")
    assert confirmation and "%(n)s" not in confirmation
    return remove, expected


def _dismiss_without_writes(page, server, seed, remove, expected):
    before = _physical_snapshot(server)
    before_copy = dict(_copy_row(server["data_dir"], seed["copy"]))
    assert before_copy["deleted_at"] is None
    confirmation = remove.get_attribute("hx-confirm")
    messages, mutations = [], []

    def dismiss(dialog):
        messages.append((dialog.type, dialog.message))
        dialog.dismiss()

    def record_mutation(request):
        if request.method not in {"GET", "HEAD"}:
            mutations.append((request.method, request.url))

    page.on("request", record_mutation)
    page.once("dialog", dismiss)
    with page.expect_event("dialog"):
        remove.click()
    assert len(messages) == 1 and messages[0][0] == "confirm"
    assert "%(n)s" not in messages[0][1]
    expect(remove).to_be_visible()
    assert dict(_copy_row(server["data_dir"], seed["copy"])) == before_copy
    assert _physical_snapshot(server) == before
    _assert_document(page.reload(wait_until="load"))
    expect(page.get_by_test_id(f"edit-copy-{seed['copy']}")).to_be_visible()
    assert _physical_snapshot(server) == before
    assert mutations == [], mutations
    print("Native confirm dismissed with physical rows unchanged:", messages)
    # Assert both exact strings after exercising dismissal, so a missing catalog
    # still gives attributable evidence for the real native/HTMX/data contract.
    assert confirmation == expected
    assert messages == [("confirm", expected)]


@pytest.mark.parametrize("locale", ["it", "zh_Hans"])
def test_real_locale_discover_configuration_link_is_native_without_javascript(
        browser, server_factory, locale):
    server = server_factory({"SHELF_I18N": "on"})
    credentials = _run_setup_wizard(browser, server["url"])
    words = LOCALES[locale]
    with _page(browser, server, locale=words["browser"], java_script_enabled=False) as page:
        _login(page, server, credentials)
        before = _physical_snapshot(server)
        _assert_document(page.goto(f"{server['url']}/discover", wait_until="load"))
        expect(page.locator("html")).to_have_attribute("lang", locale.replace("_", "-"))
        expect(page).to_have_title(words["title"])
        main = page.locator("main")
        expect(main.locator('input[name="q"]')).to_have_count(0)
        link = main.locator('a[href="/settings"]')
        expect(link).to_be_visible()
        rendered = {"heading": main.get_by_role("heading", level=1).inner_text(),
                    "connect": main.locator("p").inner_text(), "settings": link.inner_text()}
        expect(link).to_have_attribute("href", "/settings")
        assert link.get_attribute("hx-get") is None
        assert link.get_attribute("onclick") is None
        with page.expect_navigation(wait_until="load") as navigation:
            link.press("Enter")
        _assert_document(navigation.value)
        expect(page).to_have_url(f"{server['url']}/settings")
        assert _physical_snapshot(server) == before
        print("Native no-JS Settings navigation preserved physical rows:", rendered)
        assert rendered == {key: words[key] for key in ("heading", "connect", "settings")}


@pytest.mark.parametrize("locale", ["it", "zh_Hans"])
def test_real_locale_current_copy_confirm_dismiss_keeps_all_physical_rows(
        browser, server_factory, locale):
    server = server_factory({"SHELF_I18N": "on"})
    credentials = _run_setup_wizard(browser, server["url"])
    seed = _seed_copies(server)
    with _page(browser, server, locale="en-US") as page:
        _login(page, server, credentials)
        _pick_language(page, server, locale)
        remove, expected = _open_panel(page, server, seed, locale, collector=True)
        _dismiss_without_writes(page, server, seed, remove, expected)


@pytest.mark.parametrize("locale", ["it", "zh_Hans"])
@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_real_locale_discover_and_copy_controls_keep_role_boundaries(
        browser, server_factory, locale, role):
    server = server_factory({"SHELF_I18N": "on"})
    admin = _run_setup_wizard(browser, server["url"])
    credentials = {"username": f"locale_{role}", "password": "locale-test-password1"}
    with _page(browser, server, locale="en-US") as admin_page:
        _login(admin_page, server, admin)
        _add_user(admin_page, server, credentials["username"], credentials["password"], role)
    seed = _seed_copies(server)
    with _page(browser, server, locale=LOCALES[locale]["browser"]) as page:
        _login(page, server, credentials)
        before = _physical_snapshot(server)
        _assert_document(page.goto(f"{server['url']}/discover", wait_until="load"))
        expect(page.locator("html")).to_have_attribute("lang", locale.replace("_", "-"))
        main = page.locator("main")
        expect(main.get_by_role("heading", level=1)).to_have_text(LOCALES[locale]["heading"])
        expect(main.locator("p")).to_have_text(LOCALES[locale]["not_connected"])
        expect(main.locator("a, input, form")).to_have_count(0)
        if role == "editor":
            remove, expected = _open_panel(page, server, seed, locale, collector=True)
            _dismiss_without_writes(page, server, seed, remove, expected)
        else:
            _assert_document(page.goto(f"{server['url']}/item/{seed['item']}", wait_until="load"))
            copies = page.locator("#item-copies")
            expect(copies).to_contain_text(USER_VALUE)
            expect(copies.locator("button, form")).to_have_count(0)
            expect(page.get_by_test_id("print-copy-labels")).to_have_count(0)
            response = page.request.get(
                f"{server['url']}/api/items/{seed['item']}/copies/{seed['copy']}/edit"
            )
            assert response.status == 403
        assert _physical_snapshot(server) == before


@pytest.mark.parametrize("locale", ["it", "zh_Hans"])
def test_real_locale_feature_off_keeps_discover_hidden_and_core_copy_edit_available(
        browser, server_factory, locale):
    server = server_factory({"SHELF_I18N": "on"})
    credentials = _run_setup_wizard(browser, server["url"], profile="standard")
    seed = _seed_copies(server)
    with _page(browser, server, locale=LOCALES[locale]["browser"]) as page:
        _login(page, server, credentials)
        before = _physical_snapshot(server)
        _assert_document(page.goto(f"{server['url']}/discover", wait_until="load"))
        expect(page.get_by_test_id("feature-disabled")).to_be_visible()
        expect(page.locator('main input[name="q"], #hc-results, [data-nav-tab="discover"]')).to_have_count(0)
        expect(page.locator('main a[href="/settings"]')).to_have_count(0)
        remove, expected = _open_panel(page, server, seed, locale, collector=False)
        _dismiss_without_writes(page, server, seed, remove, expected)
        assert _physical_snapshot(server) == before
