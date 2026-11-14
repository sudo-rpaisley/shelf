"""Native-browser RF2 component/shell tests, NOT parent integrated acceptance.

Real base/nav/account templates, authorized visible_tabs/media_navigation, and
shipped scripts/styles are rendered into a local intercepted shell. The owned
fragment/assets are injected by this harness because only the parent may wire
shared files. No HTTP authentication/production/provider claim is made here.
"""

import json
import mimetypes
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://shelf.test"

HOST_JS = """
// Test-only host: execute the real Browse key handler without claiming that
// this standalone shell initializes Browse's HTTP filters/grid/controller.
window.rf2Host = {keys: [], views: [], clears: 0, browse: browsePage()};
rf2Host.browse.selectMode = true;
rf2Host.browse.selectedIds = [17];
rf2Host.browse.setView = function(value) { this.viewMode = value; rf2Host.views.push(value); };
rf2Host.browse.clearAllFilters = function() { rf2Host.clears++; };
document.addEventListener('keydown', function(event) {
    rf2Host.keys.push(event.key);
    rf2Host.browse.handleKey(event);
});
document.getElementById('host-confirm-form').addEventListener('submit', function(event) {
    if (!confirm('Confirm the standalone form?')) event.preventDefault();
});
document.getElementById('host-alert').addEventListener('click', function() { alert('Standalone native alert'); });
"""

HOST_TEMPLATE = """
{% extends "base.html" %}
{% block head %}
<link rel="stylesheet" href="/static/css/command-palette.source.css">
<script src="/static/js/browse.js"></script>
{% endblock %}
{% block content %}
<h1 id="host-heading">Standalone command palette shell</h1>
<label for="host-query">Page search</label><input id="host-query" name="q" type="text" class="w-full min-w-0">
<label for="host-textarea">Notes</label><textarea id="host-textarea" class="w-full min-w-0"></textarea>
<label for="host-select">Choice</label><select id="host-select"><option>One</option></select>
<div id="host-editable" contenteditable="true"><span id="host-editable-child">Editable descendant</span></div>
<div id="host-textbox" role="textbox" tabindex="0">Custom editable</div>
<dialog id="host-native"><button type="button">Other native dialog</button></dialog>
<div id="host-legacy" role="dialog" aria-label="Other legacy dialog" hidden><button type="button">Other legacy dialog</button></div>
<form id="host-confirm-form" method="POST" action="/__rf2/post"><button type="submit">Submit standalone form</button></form>
<button id="host-alert" type="button">Native alert</button>
<a href="/api/items/1/delete" data-nav-tab="outside">Outside metadata action</a>
{% endblock %}
"""


def _render(role, path, *, empty=False):
    from app.main import templates
    from app.media_presentation import family_nodes
    from app.nav import media_navigation, visible_tabs
    from starlette.requests import Request

    user = {"id": 1, "username": "component", "display_name": "Component", "role": role} if role else None
    tabs = [] if empty else visible_tabs(user)
    context = {"user": user, "nav_tabs": tabs,
               "media_navigation": media_navigation(tabs, family_nodes()),
               "request": Request({"type": "http", "path": path, "headers": [], "query_string": b""})}
    html = templates.env.from_string(HOST_TEMPLATE).render(context)
    fragment = templates.env.get_template("fragments/command_palette.html").render(context)
    assert html.count("    <!-- Content -->") == 1
    html = html.replace("    <!-- Content -->", fragment + "    <!-- Content -->")
    html = html.replace('<script src="/static/js/app.js"></script>',
                        '<script src="/static/js/app.js"></script>'
                        '<script src="/__rf2/host.js"></script>'
                        '<script src="/static/js/command-palette.js"></script>')
    # The parent owns this stable marker on the real existing help button.
    html = html.replace('title="Keyboard shortcuts (?)"', 'data-shortcut-help title="Keyboard shortcuts (?)"')
    return html


def _configure(db, case):
    from app.features import set_feature_enabled
    from app.nav import invalidate_cache

    settings = {}
    if case == "configured":
        settings = {"vision_provider": "ollama", "hardcover_token": "standalone-fixture-not-a-credential"}
    elif case == "manual":
        settings = {"nav_hidden_tabs": json.dumps(["library", "music", "periodicals", "series", "stats", "reading-life"])}
    elif case == "feature":
        for feature in ("connected_library", "music", "periodicals", "series", "stats", "store"):
            set_feature_enabled(db, feature, False)
    for key, value in settings.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()
    invalidate_cache()


@contextmanager
def _shell(browser, *, width=1280, height=900, role="admin", empty=False,
           javascript=True, native=True, touch=False):
    requests, external = [], []
    context = browser.new_context(
        viewport={"width": width, "height": height}, java_script_enabled=javascript,
        reduced_motion="reduce", has_touch=touch, service_workers="block",
    )
    context.add_cookies([{"name": "csrf_token", "value": "rf2-harness-csrf", "url": ORIGIN}])
    if not native:
        context.add_init_script("HTMLDialogElement.prototype.showModal = undefined;")

    def serve(route):
        request = route.request
        parts = urlsplit(request.url)
        requests.append({"method": request.method, "url": request.url, "path": parts.path, "data": request.post_data})
        if parts.netloc != "shelf.test":
            external.append(request.url)
            return route.abort()
        if parts.path == "/__rf2/host.js":
            return route.fulfill(status=200, content_type="text/javascript", body=HOST_JS)
        if parts.path.startswith("/static/"):
            path = (ROOT / parts.path.lstrip("/")).resolve()
            assert path.is_relative_to(ROOT / "static") and path.is_file(), parts.path
            return route.fulfill(status=200, content_type=mimetypes.guess_type(path)[0], body=path.read_bytes())
        return route.fulfill(status=200, content_type="text/html", body=_render(role, parts.path, empty=empty),
                             headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'"})

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(ORIGIN, wait_until="load")
        if javascript and native:
            expect(page.locator("[data-command-palette]")).to_have_attribute("data-command-palette-enhanced", "true")
        yield page, requests
        assert external == []
        assert_page_clean(page)
    finally:
        context.close()


def _open(page):
    page.locator("[data-command-palette-launcher]").click()
    expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()
    expect(page.locator("[data-command-palette-query]")).to_be_focused()


def _options(page):
    return page.locator("[data-command-palette-result]")


def _fits(page):
    metrics = page.evaluate("() => ({width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth})")
    if metrics["scroll"] > metrics["width"]:
        metrics["overflowing"] = page.evaluate("""() => [...document.querySelectorAll('body *')].map(e=>({
            tag:e.tagName,id:e.id,cls:e.className,left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right
        })).filter(e=>e.right>document.documentElement.clientWidth+1 || e.left<0)""")
    assert metrics["scroll"] <= metrics["width"], metrics


@pytest.mark.parametrize("role,case", [("admin", "configured"), ("editor", "default"), ("viewer", "default"),
                                      (None, "default"), ("admin", "manual"), ("admin", "feature")])
def test_inventory_is_exact_real_authorized_render_including_closed_menus(browser, db, role, case):
    _configure(db, case)
    with _shell(browser, role=role) as (page, requests):
        before = list(requests)
        # Neither collapsed native details nor the closed Alpine account panel
        # may erase authorized links from the palette.
        expect(page.locator('[data-nav-format="book"]')).to_be_hidden()
        _open(page)
        inventory = page.evaluate("""() => {
            const marked = document.querySelectorAll('[data-media-navigation] a[data-nav-primary],[data-media-navigation] a[data-nav-tab],[data-media-navigation] a[data-nav-family-link],[data-media-navigation] a[data-nav-format],[data-media-navigation] a[data-nav-management]');
            const canonical = href => {const u=new URL(href,location.origin);u.searchParams.sort();return u.pathname+u.search;};
            return {expected:[...new Set([...marked].map(a=>canonical(a.getAttribute('href'))))].sort(),
                actual:[...document.querySelectorAll('[data-command-palette-result]')].map(a=>canonical(a.getAttribute('href'))).sort()};
        }""")
        assert inventory["actual"] == inventory["expected"]
        expect(_options(page).first).to_have_text("Browse")
        labels = _options(page).all_text_contents()
        assert ("Settings" in labels) == (role == "admin")
        assert ("ScanAdd / Scan" in labels) == (role in {"admin", "editor"})
        assert ("DiscoverExplore" in labels) == (case == "configured")
        assert not any("Sign out" in label or "API token" in label for label in labels)
        primary = page.locator('[data-nav-primary="browse"]').get_attribute("href")
        assert primary.startswith("/browse?") == (case in {"manual", "feature"} or role is None)
        assert requests == before, "Opening/filtering the palette must not fetch a catalogue or provider"


@pytest.mark.parametrize("label,path", [
    ("Browse", "/library?reset=1&view=items&sort=recent"),
    ("Catalogue management", "/browse"),
    ("Book", "/library?reset=1&view=items&sort=recent&types=book"),
    ("Settings", "/settings"),
])
def test_native_result_anchors_actually_follow_the_rendered_destinations(browser, label, path):
    with _shell(browser) as (page, requests):
        _open(page)
        page.locator("[data-command-palette-query]").fill(label)
        # Anchor labels may have a secondary family/group context.
        result = page.locator("[data-command-palette-result]").filter(has=page.get_by_text(label, exact=True))
        expect(result).to_have_count(1)
        expect(result).to_have_attribute("href", path)
        before = len([request for request in requests if request["path"] == urlsplit(path).path])
        result.click()
        page.wait_for_url(ORIGIN + path)
        assert len([request for request in requests if request["path"] == urlsplit(path).path]) == before + 1


@pytest.mark.parametrize("case", ["default", "feature"])
def test_search_is_encoded_bounded_and_inert_with_no_typing_requests(browser, db, case):
    _configure(db, case)
    with _shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        query = '<img src=x onerror=alert(1)> & next=https://evil.example/?x=1#frag 🐈 ' + "z" * 240
        # fill() deliberately exceeds native maxlength: the runtime must bound
        # programmatic/paste-like values as well as trusted keyboard input.
        page.locator("[data-command-palette-query]").evaluate("(e,value) => {e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));}", query)
        option = _options(page).last
        expect(option).to_contain_text("<img")
        assert option.locator("img").count() == 0
        href = option.get_attribute("href")
        parts, expected_query = urlsplit(href), query.strip()[:200]
        values = parse_qs(parts.query)
        assert parts.path == ("/library" if case == "default" else "/browse")
        assert values == ({"reset": ["1"], "view": ["items"], "sort": ["recent"], "q": [expected_query]}
                          if case == "default" else {"sort": ["newest"], "q": [expected_query]})
        assert requests == before
        page.keyboard.press("Enter")
        page.wait_for_url(ORIGIN + href)
        navigations = [request for request in requests[len(before):] if not request["path"].startswith(("/static/", "/__rf2/"))]
        assert len(navigations) == 1 and navigations[0]["method"] == "GET"


def test_malicious_anchor_payloads_metadata_and_form_actions_are_never_commands(browser):
    with _shell(browser) as (page, requests):
        page.evaluate("""() => {
            const nav=document.querySelector('[data-media-navigation]');
            const urls=['javascript:alert(1)','data:text/html,owned','//evil.example/browse','https://evil.example/browse',
                '/logout','/api/token','/api/items/1/delete','/discover/dismiss/1','/settings/tokens',
                '/browse?next=https://evil.example','/library?redirect=//evil.example','/browse?action=delete',
                '/settings?clear=1','/browse?sort=newest&sort=recent','/%62rowse','/library/../browse'];
            urls.forEach((href,i)=>{const a=document.createElement('a');a.href=href;a.dataset.navTab='unapproved-'+i;a.textContent='Unapproved '+i;nav.append(a);});
            const hidden=document.createElement('a');hidden.href='/browse?sort=recent';hidden.dataset.navTab='hidden';hidden.hidden=true;hidden.textContent='Unapproved hidden';nav.append(hidden);
            const form=document.createElement('form');form.method='POST';form.action='/api/mutate';
            const action=document.createElement('a');action.href='/browse?sort=recent';action.dataset.navTab='form';action.textContent='Unapproved form';form.append(action);nav.append(form);
            const approved=document.createElement('a');approved.href=location.origin+'/browse?sort=recent';approved.dataset.navTab='approved';approved.textContent='Approved local';nav.append(approved);
        }""")
        _open(page)
        assert not any("Unapproved" in text or "metadata" in text for text in _options(page).all_text_contents())
        before = list(requests)
        page.locator("[data-command-palette-query]").fill("javascript:alert(1) & next=//evil.example")
        assert _options(page).count() == 1  # Literal, encoded catalogue search only.
        assert requests == before
        page.locator("[data-command-palette-query]").fill("Approved local")
        page.get_by_role("option", name="Approved local", exact=True).click()
        page.wait_for_url(ORIGIN + "/browse?sort=recent")


@pytest.mark.parametrize("context", ["empty", "missing", "unsafe", "redirect", "noncatalogue"])
def test_no_primary_or_empty_navigation_cannot_invent_search_or_activate_old_result(browser, context):
    with _shell(browser, empty=context == "empty") as (page, requests):
        if context == "missing":
            page.locator('[data-nav-primary="browse"]').evaluate("e => e.remove()")
        elif context != "empty":
            href = {"unsafe": "javascript:alert(1)", "redirect": "/browse?next=//evil.example", "noncatalogue": "/settings"}[context]
            page.locator('[data-nav-primary="browse"]').evaluate("(e,href) => e.setAttribute('href',href)", href)
        _open(page)
        before = list(requests)
        page.locator("[data-command-palette-query]").fill("No such destination anywhere")
        expect(_options(page)).to_have_count(0)
        expect(page.locator("[data-command-palette-status]")).to_have_text("No commands match.")
        expect(page.locator("[data-command-palette-query]")).not_to_have_attribute("aria-activedescendant")
        for key in ("ArrowDown", "ArrowUp", "Enter"):
            page.keyboard.press(key)
        assert requests == before
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()


def test_unicode_limit_native_text_editing_and_modified_enter_remain_functional(browser):
    with _shell(browser) as (page, requests):
        _open(page)
        input = page.locator("[data-command-palette-query]")
        before = list(requests)
        page.keyboard.type("abcdef")
        page.keyboard.press("Home")
        page.keyboard.press("Shift+End")
        assert input.evaluate("e => [e.selectionStart,e.selectionEnd]") == [0, 6]
        page.keyboard.type("replacement")
        expect(input).to_have_value("replacement")
        selected = input.get_attribute("aria-activedescendant")
        for key in ("Shift+ArrowDown", "Control+ArrowUp", "Shift+Enter", "Control+Enter"):
            page.keyboard.press(key)
            expect(input).to_have_attribute("aria-activedescendant", selected)
        input.evaluate("e => {const event=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});event.preventDefault();e.dispatchEvent(event);}")
        input.evaluate("e => {e.value='🐈'.repeat(210);e.dispatchEvent(new Event('input',{bubbles:true}));}")
        expect(_options(page)).to_have_count(1)
        href = _options(page).first.get_attribute("href")
        assert parse_qs(urlsplit(href).query)["q"] == ["🐈" * 200]
        assert requests == before


def test_removed_authorized_anchor_cannot_be_followed_from_stale_pointer_result(browser):
    with _shell(browser) as (page, requests):
        _open(page)
        page.locator("[data-command-palette-query]").fill("Settings")
        option = page.get_by_role("option", name="Settings", exact=True)
        expect(option).to_be_visible()
        page.locator('[data-media-navigation] [data-nav-tab="settings"]').evaluate("e => e.remove()")
        before = list(requests)
        option.click()
        expect(page.get_by_role("option", name="Settings", exact=True)).to_have_count(0)
        expect(_options(page)).to_have_count(1)  # Only the authorized primary's literal catalogue search remains.
        assert requests == before


def test_queued_native_close_does_not_reset_a_reopened_palette(browser):
    with _shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        page.evaluate("""() => {
            document.querySelector('[data-command-palette-dialog]').close();
            document.querySelector('[data-command-palette-launcher]').click();
        }""")
        # A native close event is queued rather than dispatched by close().
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()
        expect(page.locator("[data-command-palette-launcher]")).to_have_attribute("aria-expanded", "true")
        input = page.locator("[data-command-palette-query]")
        expect(input).to_have_attribute("aria-expanded", "true")
        page.keyboard.type("Book")
        expect(input).to_have_value("Book")
        assert _options(page).count() > 1
        assert requests == before


def test_owned_keys_are_stopped_before_real_document_app_and_browse_handlers(browser):
    with _shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        keys_before = page.evaluate("rf2Host.keys.slice()")
        input = page.locator("[data-command-palette-query]")
        page.keyboard.type("/b s ? gefx")
        expect(input).to_have_value("/b s ? gefx")
        expect(page.locator("#shortcut-modal")).to_be_hidden()
        page.keyboard.press("Tab")
        close = page.locator("[data-command-palette-close]")
        expect(close).to_be_focused()
        for key in ("/", "b", "s", "?", "g", "e", "f", "x"):
            page.keyboard.press(key)
        assert requests == before
        assert page.evaluate("rf2Host.keys") == keys_before
        assert page.evaluate("({select:rf2Host.browse.selectMode,ids:rf2Host.browse.selectedIds,views:rf2Host.views,clears:rf2Host.clears,filters:rf2Host.browse.filtersOpen})") == {
            "select": True, "ids": [17], "views": [], "clears": 0, "filters": False,
        }
        page.keyboard.press("Tab")
        expect(input).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(close).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_hidden()
        expect(page.locator("[data-command-palette-launcher]")).to_be_focused()
        assert page.evaluate("rf2Host.browse.selectMode") is True, "Palette Escape must not exit Browse selection"


def test_arrows_enter_repeat_composition_and_reinitialization_activate_exactly_once(browser):
    with _shell(browser) as (page, requests):
        page.add_script_tag(url=ORIGIN + "/static/js/command-palette.js")
        _open(page)
        input = page.locator("[data-command-palette-query]")
        expect(input).to_have_attribute("aria-activedescendant", "command-palette-result-0")
        page.keyboard.press("ArrowDown")
        expect(input).to_have_attribute("aria-activedescendant", "command-palette-result-1")
        page.keyboard.press("ArrowUp")
        expect(input).to_have_attribute("aria-activedescendant", "command-palette-result-0")
        page.keyboard.press("ArrowUp")
        expect(_options(page).last).to_have_attribute("aria-selected", "true")
        input.fill("Catalogue management")
        before = list(requests)
        keys_before = page.evaluate("rf2Host.keys.slice()")
        input.dispatch_event("keydown", {"key": "Enter", "repeat": True})
        input.dispatch_event("keydown", {"key": "ArrowDown", "repeat": True})
        input.dispatch_event("compositionstart")
        input.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
        input.dispatch_event("keydown", {"key": "Escape", "keyCode": 229})
        page.locator("[data-command-palette-dialog]").dispatch_event("cancel", {"cancelable": True})
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()
        input.dispatch_event("compositionend")
        assert requests == before
        assert page.evaluate("rf2Host.keys") == keys_before
        page.keyboard.press("Enter")
        page.wait_for_url(ORIGIN + "/browse")
        assert len([request for request in requests[len(before):] if request["path"] == "/browse"]) == 1


def test_escape_cancel_backdrop_and_removed_opener_restore_visible_focus(browser):
    with _shell(browser) as (page, _):
        _open(page)
        page.locator("[data-command-palette-dialog]").dispatch_event("cancel", {"cancelable": True})
        expect(page.locator("[data-command-palette-launcher]")).to_be_focused()
        _open(page)
        page.mouse.click(2, 2)
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_hidden()
        expect(page.locator("[data-command-palette-launcher]")).to_be_focused()
        page.locator("#host-heading").evaluate("e => {e.tabIndex=0;e.focus();}")
        page.keyboard.press("Control+k")
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#host-heading")).to_be_focused()
        _open(page)
        page.locator("[data-command-palette-launcher]").evaluate("e => e.remove()")
        page.keyboard.press("Escape")
        expect(page.locator('[data-nav-primary="browse"]')).to_be_focused()
        # No remaining visible launcher or primary: use the existing main,
        # without permanently changing the parent's tabindex contract.
        page.locator('[data-nav-primary="browse"]').evaluate("e => e.remove()")
        page.locator("#host-heading").evaluate("e => e.focus()")
        page.keyboard.press("Control+k")
        page.locator("#host-heading").evaluate("e => e.remove()")
        page.keyboard.press("Escape")
        expect(page.locator("main")).to_be_focused()
        expect(page.locator("main")).to_have_attribute("tabindex", "-1")
        page.locator("#host-query").focus()
        expect(page.locator("main")).not_to_have_attribute("tabindex")


def test_optional_shortcut_respects_editables_default_prevention_modifiers_repeat_and_ime(browser):
    with _shell(browser) as (page, requests):
        dialog = page.locator("[data-command-palette-dialog]")
        before = list(requests)
        for selector in ("#host-query", "#host-textarea", "#host-select", "#host-editable", "#host-textbox"):
            page.locator(selector).focus()
            page.keyboard.press("Control+k")
            expect(dialog).to_be_hidden()
        page.locator("#host-editable-child").dispatch_event("keydown", {"key": "k", "ctrlKey": True})
        expect(dialog).to_be_hidden()
        page.locator("[data-command-palette-launcher]").focus()
        for props in ({"repeat": True}, {"altKey": True}, {"shiftKey": True}, {"metaKey": True},
                      {"isComposing": True}, {"keyCode": 229}):
            page.locator("body").dispatch_event("keydown", {"key": "k", "ctrlKey": True, **props})
            expect(dialog).to_be_hidden()
        page.evaluate("document.addEventListener('keydown',e=>e.preventDefault(),{once:true})")
        page.locator("body").dispatch_event("keydown", {"key": "k", "ctrlKey": True})
        expect(dialog).to_be_hidden()
        page.locator("body").dispatch_event("compositionstart")
        page.keyboard.press("Control+k")
        expect(dialog).to_be_hidden()
        page.locator("body").dispatch_event("compositionend")
        page.keyboard.press("Meta+k")
        expect(dialog).to_be_visible()
        page.keyboard.press("Meta+k")
        expect(dialog).to_be_hidden()
        assert requests == before


def test_other_native_legacy_account_and_shortcut_surfaces_keep_ownership(browser):
    with _shell(browser) as (page, requests):
        palette = page.locator("[data-command-palette-dialog]")
        before = list(requests)
        page.locator("#host-native").evaluate("e => e.showModal()")
        page.get_by_role("button", name="Other native dialog", exact=True).focus()
        page.keyboard.press("Control+k")
        expect(palette).to_be_hidden()
        page.locator("#host-native").evaluate("e => e.close()")
        page.locator("#host-legacy").evaluate("e => {e.hidden=false;e.querySelector('button').focus();}")
        page.keyboard.press("Control+k")
        expect(palette).to_be_hidden()
        page.locator("#host-legacy").evaluate("e => e.hidden=true")
        page.locator("[data-command-palette-launcher]").focus()
        page.keyboard.press("?")
        expect(page.locator("#shortcut-modal")).to_be_visible()
        page.keyboard.press("Control+k")
        expect(palette).to_be_hidden()
        page.keyboard.press("Escape")
        page.get_by_test_id("account-menu-button").click()
        expect(page.get_by_test_id("account-menu-panel")).to_be_visible()
        page.keyboard.press("Control+k")
        expect(palette).to_be_hidden()
        page.get_by_test_id("account-menu-profile").click()
        close_account = page.get_by_role("button", name="Close account dialog", exact=True)
        expect(close_account).to_be_visible()
        close_account.focus()
        page.keyboard.press("Control+k")
        expect(palette).to_be_hidden()
        expect(close_account).to_be_visible()
        close_account.click()
        _open(page)
        assert requests == before


def test_global_and_browse_shortcuts_are_unchanged_outside_the_palette(browser):
    with _shell(browser) as (page, _):
        launch = page.locator("[data-command-palette-launcher]")
        launch.focus()
        page.keyboard.press("/")
        expect(page.locator("#host-query")).to_be_focused()
        page.keyboard.press("Escape")  # Real Browse input blur, outside palette.
        expect(page.locator("#host-query")).not_to_be_focused()
        launch.focus()
        page.keyboard.press("g")
        page.keyboard.press("e")
        page.keyboard.press("f")
        page.keyboard.press("x")
        assert page.evaluate("({views:rf2Host.views,select:rf2Host.browse.selectMode,filters:rf2Host.browse.filtersOpen,clears:rf2Host.clears})") == {
            "views": ["list"], "select": False, "filters": True, "clears": 1,
        }
        page.keyboard.press("?")
        expect(page.locator("#shortcut-modal")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#shortcut-modal")).to_be_hidden()
        page.keyboard.press("b")
        page.wait_for_url(ORIGIN + "/library?reset=1&view=items&sort=recent")
        page.locator("[data-command-palette-launcher]").focus()
        page.keyboard.press("s")
        page.wait_for_url(ORIGIN + "/scan")


def test_native_confirmation_alert_and_plain_post_csrf_are_not_bypassed(browser):
    with _shell(browser) as (page, requests):
        native = page.evaluate("({confirm:confirm.toString(),alert:alert.toString()})")
        _open(page)
        assert not any("Submit" in text or "Sign out" in text for text in _options(page).all_text_contents())
        page.keyboard.press("Escape")
        assert page.evaluate("({confirm:confirm.toString(),alert:alert.toString()})") == native
        messages = []

        def dismiss(dialog):
            messages.append((dialog.type, dialog.message))
            dialog.dismiss()

        def accept(dialog):
            messages.append((dialog.type, dialog.message))
            dialog.accept()

        page.once("dialog", dismiss)
        page.get_by_role("button", name="Submit standalone form", exact=True).click()
        assert messages == [("confirm", "Confirm the standalone form?")]
        assert not any(request["method"] == "POST" for request in requests)
        page.once("dialog", accept)
        page.get_by_role("button", name="Submit standalone form", exact=True).click()
        page.wait_for_url(ORIGIN + "/__rf2/post")
        posts = [request for request in requests if request["method"] == "POST"]
        assert len(posts) == 1 and posts[0]["path"] == "/__rf2/post"
        assert parse_qs(posts[0]["data"])["_csrf"] == ["rf2-harness-csrf"]
        page.once("dialog", accept)
        page.get_by_role("button", name="Native alert", exact=True).click()
        assert messages == [("confirm", "Confirm the standalone form?"), ("confirm", "Confirm the standalone form?"),
                            ("alert", "Standalone native alert")]
        page.get_by_test_id("account-menu-button").click()
        logout = page.locator('form[action="/logout"]')
        expect(logout).to_have_attribute("method", "POST")
        expect(logout.locator('input[name="_csrf"]')).to_have_value("rf2-harness-csrf")


@pytest.mark.parametrize("javascript,native", [(False, True), (True, False)])
def test_native_fallback_keeps_navigation_usable_without_js_or_dialog_support(browser, javascript, native):
    with _shell(browser, width=375, javascript=javascript, native=native) as (page, requests):
        expect(page.locator("[data-command-palette-launcher]")).to_be_hidden()
        fallback = page.locator("[data-command-palette-fallback]")
        expect(fallback).to_be_visible()
        fallback.locator("summary").click()
        expect(fallback).to_contain_text("Use the primary navigation above")
        expect(page.locator("[data-command-palette-dialog]")).to_be_hidden()
        page.locator('[data-nav-primary="browse"]').click()
        page.wait_for_url(ORIGIN + "/library?reset=1&view=items&sort=recent")
        assert any(request["path"] == "/library" for request in requests)
        _fits(page)


@pytest.mark.parametrize("width", [320, 375, 640, 768, 1024, 1280, 1366, 1920])
def test_eight_widths_keep_inflow_launcher_and_native_dialog_reachable(browser, width):
    with _shell(browser, width=width, height=700, touch=width <= 375) as (page, _):
        launcher = page.locator("[data-command-palette-launcher]")
        assert launcher.evaluate("e => !e.closest('nav,main')")
        launch_box = launcher.bounding_box()
        help_box = page.locator("[data-shortcut-help]").bounding_box()
        assert launch_box["width"] >= 80 and launch_box["height"] >= 44
        assert launch_box["y"] + launch_box["height"] <= help_box["y"] or launch_box["x"] >= help_box["x"] + help_box["width"]
        _fits(page)
        _open(page)
        dialog = page.locator("[data-command-palette-dialog]").bounding_box()
        assert dialog["x"] >= 0 and dialog["x"] + dialog["width"] <= width + 1
        assert dialog["y"] >= 0 and dialog["y"] + dialog["height"] <= 701
        for selector in ("[data-command-palette-query]", "[data-command-palette-close]"):
            box = page.locator(selector).bounding_box()
            assert box["width"] >= 80 and box["height"] >= 44, (width, selector, box)
        _options(page).last.scroll_into_view_if_needed()
        expect(_options(page).last).to_be_visible()
        page.keyboard.press("Escape")
        expect(launcher).to_be_focused()
        _fits(page)


def test_320px_large_text_reflow_preserves_text_control_floor_and_scroll_access(browser):
    with _shell(browser, width=320, height=568, touch=True) as (page, _):
        page.evaluate("document.documentElement.style.fontSize='200%'")
        _fits(page)
        _open(page)
        dialog = page.locator("[data-command-palette-dialog]")
        assert dialog.bounding_box()["width"] <= 320
        input = page.locator("[data-command-palette-query]")
        input.scroll_into_view_if_needed()
        assert input.bounding_box()["width"] >= 80
        page.keyboard.type("Book")
        expect(input).to_have_value("Book")
        page.keyboard.press("Tab")
        close = page.locator("[data-command-palette-close]")
        expect(close).to_be_focused()
        close.scroll_into_view_if_needed()
        assert close.bounding_box()["width"] >= 80
        page.keyboard.press("Escape")
        _fits(page)
