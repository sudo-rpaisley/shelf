"""Store/account review regressions; SOURCE ONLY until parent freezes candidate.

Fresh server_factory database per case; no production state or live providers.
Only catalogue GET/auth and feature/account routes reach the server. Queue
writer replies are explicitly intercepted, so no metadata provider is called.
The real browser/IndexedDB tests complement, not replace, the standalone Node
protocol regressions in tests/js/store_account_review_20261121.test.cjs.
"""

import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)

pytestmark = pytest.mark.e2e
OWNED = "9780306406157"
UNKNOWN = "9780441172710"  # intentionally invalid: never manufacture a false pass


@pytest.fixture
def store_account_review_session(server_factory, browser):
    server = server_factory()
    credentials = _run_setup_wizard(browser, server["url"], profile="everything")
    # These tests exercise requests held/released by Playwright, not SW asset
    # caching. Existing PWA regressions retain coverage of real offline reloads.
    context = browser.new_context(service_workers="block")
    page = attach_page_guard(context.new_page())
    page.goto(f"{server['url']}/login")
    page.locator('input[name="username"]').fill(credentials["username"])
    page.locator('input[name="password"]').fill(credentials["password"])
    page.locator('button[type="submit"]').click()
    page.wait_for_url(f"{server['url']}/")
    insert_item(server["data_dir"], title="Review owned control", isbn=OWNED)
    try:
        yield {**server, "context": context, "page": page}
    finally:
        context.close()


def _open_store(session, page=None):
    page = page or session["page"]
    page.goto(f"{session['url']}/store")
    expect(page.get_by_test_id("status-line")).to_contain_text("titles cached")
    return page


def _queue_raw(page):
    return page.evaluate("() => localStorage.getItem('shelf-store-queue')")


def _submit(page, code):
    page.get_by_test_id("isbn-input").fill(code)
    page.get_by_test_id("check-button").click()


def _revoke_server_login(session, page):
    owner = page.locator("body").get_attribute("data-store-user-id")
    # This is only the fresh server_factory DB owned by this test. Do not
    # rotate/delete the CSRF cookie: the review fault specifically kept it.
    with sqlite3.connect(session["data_dir"] / "shelf.db") as connection:
        connection.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (int(owner),))


def _refresh_visible_store(page):
    page.bring_to_front()
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")


def test_revoked_server_login_same_csrf_blocks_cached_verdicts_without_queue_loss(store_account_review_session):
    session = store_account_review_session
    page = _open_store(session)
    page.route("**/api/store/queue", lambda route: route.abort())
    _submit(page, UNKNOWN)
    expect(page.get_by_test_id("queue-count")).to_have_text("1")
    _submit(page, OWNED)
    expect(page.get_by_test_id("verdict")).to_contain_text("OWNED")
    before = _queue_raw(page)
    csrf = page.evaluate("() => document.cookie")
    owner = page.locator("body").get_attribute("data-store-user-id")
    _revoke_server_login(session, page)
    with page.expect_response("**/api/store/data"):
        _refresh_visible_store(page)
    expect(page.get_by_test_id("check-button")).to_be_disabled()
    expect(page.get_by_test_id("verdict")).to_be_hidden()
    expect(page.locator("#store-session-warning")).to_contain_text("kept")
    assert page.evaluate("() => document.cookie") == csrf
    assert _queue_raw(page) == before
    assert page.evaluate("owner => localStorage.getItem('shelf-store-data-owner:' + owner)", owner) is None
    assert_page_clean(page)


def test_followed_login_html_is_not_an_offline_cache_fallback(store_account_review_session):
    session = store_account_review_session
    page = _open_store(session)
    page.route("**/api/store/queue", lambda route: route.abort())
    _submit(page, UNKNOWN)
    expect(page.get_by_test_id("queue-count")).to_have_text("1")
    before = _queue_raw(page)
    csrf = page.evaluate("() => document.cookie")
    # Independent redirect control even if the integrated auth writer now
    # returns 401 directly for /api/* instead of the historical 303 + HTML.
    page.route("**/login", lambda route: route.fulfill(content_type="text/html", body="<html><body>Sign in</body></html>"))
    page.route("**/api/store/data", lambda route: route.fulfill(status=303, headers={"Location": "/login"}))
    with page.expect_response("**/login"):
        _refresh_visible_store(page)
    expect(page.get_by_test_id("check-button")).to_be_disabled()
    expect(page.get_by_test_id("verdict")).to_be_hidden()
    assert _queue_raw(page) == before
    assert page.evaluate("() => document.cookie") == csrf
    assert_page_clean(page)


def test_two_tabs_wait_for_real_transaction_then_preserve_both_enqueues(store_account_review_session):
    session = store_account_review_session
    first = _open_store(session)
    second = _open_store(session, attach_page_guard(session["context"].new_page()))
    session["context"].set_offline(True)
    # Hold a REAL IDB readwrite transaction. Both production enqueue writers
    # must wait for it, rather than read/modify/write localStorage unlocked.
    first.evaluate("""() => new Promise((resolve, reject) => {
        const request = indexedDB.open('shelf-store-coordination', 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('queue-lock', 'readwrite');
            const store = tx.objectStore('queue-lock');
            window.__reviewReleaseQueue = false;
            tx.oncomplete = () => db.close();
            function hold() {
                store.get('held-by-regression').onsuccess = () => {
                    resolve(true);
                    if (!window.__reviewReleaseQueue) hold();
                };
            }
            hold();
        };
    })""")
    _submit(first, UNKNOWN)
    _submit(second, "9780140328721")
    # Awaiting durable admission must not claim saved or clear either input.
    expect(first.get_by_test_id("isbn-input")).to_have_value(UNKNOWN)
    expect(second.get_by_test_id("isbn-input")).to_have_value("9780140328721")
    assert _queue_raw(first) == "[]"
    first.evaluate("() => { window.__reviewReleaseQueue = true; }")
    for page in (first, second):
        expect(page.get_by_test_id("queue-count")).to_have_text("2")
        expect(page.get_by_test_id("isbn-input")).to_have_value("")
        assert_page_clean(page)
    entries = first.evaluate("() => JSON.parse(localStorage.getItem('shelf-store-queue'))")
    assert {entry["isbn"] for entry in entries} == {UNKNOWN, "9780140328721"}
    assert len({entry["id"] for entry in entries}) == 2


def test_cross_tab_revocation_fences_a_held_successful_flush(store_account_review_session):
    session = store_account_review_session
    first = _open_store(session)
    second = _open_store(session, attach_page_guard(session["context"].new_page()))
    held = []
    def hold_queue(route):
        held.append(route)
        first.evaluate("() => { document.body.dataset.reviewHeldQueue = '1'; }")

    first.route("**/api/store/queue", hold_queue)
    _submit(first, UNKNOWN)
    expect(first.get_by_test_id("queue-count")).to_have_text("1")
    # Auto-sync starts only in the enqueuing tab; the other tab observes storage.
    expect(first.get_by_test_id("sync-now")).to_have_attribute("aria-busy", "true")
    expect(first.locator("body")).to_have_attribute("data-review-held-queue", "1")
    assert len(held) == 1
    before = _queue_raw(first)
    csrf = second.evaluate("() => document.cookie")
    _revoke_server_login(session, second)
    with second.expect_response("**/api/store/data"):
        _refresh_visible_store(second)
    expect(second.get_by_test_id("check-button")).to_be_disabled()
    # Wait for the shared fence, not just the disabled state set before the
    # asynchronous transaction publishes that fence to sibling tabs.
    expect(first.get_by_test_id("check-button")).to_be_disabled()
    held[0].fulfill(json={"results": [{"code": UNKNOWN, "isbn": UNKNOWN, "status": "added_bare", "owned": False}]})
    expect(first.get_by_test_id("sync-now")).not_to_have_attribute("aria-busy", "true")
    assert _queue_raw(first) == before
    assert second.evaluate("() => document.cookie") == csrf
    expect(first.get_by_test_id("verdict")).to_be_hidden()
    for page in (first, second):
        assert_page_clean(page)


def test_reverse_order_catalogue_requests_keep_newest_data_and_clear_busy(store_account_review_session):
    session = store_account_review_session
    page = _open_store(session)
    held = []
    # Mark the old body's parse in the following task, after its downstream
    # promise callbacks have run. Then an IDB barrier below waits for any
    # wrongly admitted old write as well, so a too-early negative assertion
    # cannot accidentally pass before the stale response has been processed.
    page.evaluate("""() => {
        const realFetch = window.fetch;
        let sequence = 0;
        window.fetch = function (...args) {
            const old = args[0] === '/api/store/data' && ++sequence === 1;
            return realFetch.apply(this, args).then(response => {
                if (old) {
                    const parse = response.json.bind(response);
                    response.json = async function () {
                        const body = await parse();
                        const channel = new MessageChannel();
                        channel.port1.onmessage = () => {
                            document.body.dataset.reviewOldDataParsed = '1';
                            channel.port1.close(); channel.port2.close();
                        };
                        channel.port2.postMessage('parsed');
                        return body;
                    };
                }
                return response;
            });
        };
    }""")
    def hold_data(route):
        held.append(route)
        page.evaluate("count => { document.body.dataset.reviewHeldData = String(count); }", len(held))

    page.route("**/api/store/data", hold_data)
    _refresh_visible_store(page)
    expect(page.get_by_test_id("status-line")).to_have_attribute("aria-busy", "true")
    expect(page.locator("body")).to_have_attribute("data-review-held-data", "1")
    _refresh_visible_store(page)
    # A synchronous protocol checkpoint, not a sleep: the second request must
    # be issued and held before the test chooses reverse completion order.
    expect(page.locator("body")).to_have_attribute("data-review-held-data", "2")
    assert len(held) == 2

    def body(title):
        return {"generated_at": "2026-11-21T00:00:00Z", "count": 1,
                "items": [{"title": title, "authors": None, "owned": True, "codes": [OWNED]}]}

    held[1].fulfill(json=body("Newest catalogue generation"))
    expect(page.get_by_test_id("status-line")).not_to_have_attribute("aria-busy", "true")
    held[0].fulfill(json=body("Stale catalogue generation"))
    expect(page.locator("body")).to_have_attribute("data-review-old-data-parsed", "1")
    page.evaluate("""() => new Promise((resolve, reject) => {
        const request = indexedDB.open('shelf-store-coordination', 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('queue-lock', 'readwrite');
            tx.objectStore('queue-lock').get('after-old-response');
            tx.oncomplete = () => { db.close(); resolve(true); };
            tx.onabort = () => { db.close(); reject(tx.error); };
        };
    })""")
    _submit(page, OWNED)
    expect(page.get_by_test_id("verdict")).to_contain_text("Newest catalogue generation")
    expect(page.get_by_test_id("verdict")).not_to_contain_text("Stale catalogue generation")
    assert_page_clean(page)


def _features(page, url):
    page.goto(f"{url}/settings")
    page.get_by_test_id("tab-features").click()
    expect(page.get_by_test_id("feature-row-api")).to_be_visible()


def test_api_off_on_refreshes_whole_account_without_reload_or_duplicate_listeners(store_account_review_session):
    session = store_account_review_session
    page = session["page"]
    _features(page, session["url"])
    page.evaluate("() => { window.__reviewDocument = 'unchanged'; }")
    for expected in ("off", "on", "off", "on"):
        page.get_by_test_id("feature-toggle-api").click()
        expect(page.locator(f'[data-testid="feature-row-api"] [data-feature-state="{expected}"]')).to_be_visible()
        expect(page.locator("[data-account-root]")).to_have_count(1)
        expect(page.locator("[data-account-dialog]")).to_be_hidden()
        expect(page.get_by_test_id("account-tab-tokens")).to_have_count(1 if expected == "on" else 0)
        expect(page.get_by_test_id("api-tokens-pane")).to_have_count(1 if expected == "on" else 0)
        assert page.evaluate("() => window.__reviewDocument") == "unchanged"
        assert page.evaluate("() => document.activeElement.isConnected")
        page.get_by_test_id("account-menu-button").click()
        expect(page.get_by_test_id("account-menu-panel")).to_be_visible()
        page.get_by_test_id("account-menu-profile").click()
        expect(page.locator("[data-account-dialog]")).to_be_visible()
        expect(page.get_by_role("button", name="Profile", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Password", exact=True)).to_be_visible()
        if expected == "on":
            page.get_by_test_id("account-tab-tokens").click()
            expect(page.get_by_test_id("api-token-create")).to_be_visible()
        page.get_by_role("button", name="Close account dialog").click()
        assert_page_clean(page)
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-profile").click()
    page.get_by_test_id("account-tab-tokens").click()
    page.get_by_test_id("api-token-name-input").focus()
    # Submit while the incompatible modal/tab is open. No hidden focusable
    # token controls or detached activeElement may remain after replacement.
    page.evaluate("() => document.querySelector('[data-testid=feature-toggle-api]').form.requestSubmit()")
    expect(page.get_by_test_id("account-tab-tokens")).to_have_count(0)
    expect(page.get_by_test_id("api-token-name-input")).to_have_count(0)
    expect(page.locator("[data-account-dialog]")).to_be_hidden()
    expect(page.get_by_test_id("account-menu-button")).to_be_focused()
    assert_page_clean(page)


def test_queued_feature_responses_do_not_restore_old_modal_flags(store_account_review_session):
    session = store_account_review_session
    page = session["page"]
    _features(page, session["url"])
    held = []

    def hold_actual_html(route):
        # Hold the server's actual 303, before the browser follows it. Automatic
        # redirected fetch GETs are not separately interceptable by page.route.
        # Releasing the unchanged redirect preserves the genuine /settings URL.
        response = route.fetch(max_redirects=0)
        assert route.request.method == "POST"
        assert response.status == 303
        assert response.headers["location"] == "/settings"
        held.append((route, response))
        page.evaluate("count => { document.body.dataset.reviewHeldFeatures = String(count); }", len(held))

    page.route("**/api/settings/features/*", hold_actual_html)
    page.get_by_test_id("feature-toggle-series").click()  # older HTML still has API enabled
    expect(page.locator("body")).to_have_attribute("data-review-held-features", "1")
    page.evaluate("""() => {
        const form = document.querySelector('[data-testid=feature-toggle-series]').form;
        const observer = new MutationObserver(() => {
            if (!form.hasAttribute('aria-busy')) {
                document.body.dataset.reviewOldFeatureSettled = '1';
                observer.disconnect();
            }
        });
        observer.observe(form, {attributes: true, attributeFilter: ['aria-busy']});
    }""")
    page.get_by_test_id("feature-toggle-api").click()     # newer HTML has API disabled
    expect(page.locator('[data-testid="feature-toggle-api"] >> xpath=..')).to_have_attribute("aria-busy", "true")
    # A queued mutation cannot issue its POST/redirected GET while the older
    # mutation's response is held. Release the first turn, then the second.
    assert len(held) == 1
    held[0][0].fulfill(response=held[0][1])
    expect(page.locator("body")).to_have_attribute("data-review-old-feature-settled", "1")
    expect(page.locator("body")).to_have_attribute("data-review-held-features", "2")
    assert len(held) == 2
    held[1][0].fulfill(response=held[1][1])
    expect(page.get_by_test_id("account-tab-tokens")).to_have_count(0)
    expect(page.get_by_test_id("feature-toggle-api")).to_be_enabled()
    expect(page.locator("body")).to_have_attribute("data-review-old-feature-settled", "1")
    expect(page.get_by_test_id("account-tab-tokens")).to_have_count(0)
    expect(page.get_by_test_id("api-tokens-pane")).to_have_count(0)
    expect(page.locator('[data-testid="feature-row-api"] [data-feature-state="off"]')).to_be_visible()
    assert_page_clean(page)


def test_feature_post_held_before_server_execution_cannot_run_after_a_later_profile(store_account_review_session):
    session = store_account_review_session
    page = session["page"]
    _features(page, session["url"])
    # Start Custom, making the genuine Everything profile button actionable.
    page.get_by_test_id("feature-toggle-series").click()
    expect(page.locator('[data-testid="feature-row-series"] [data-feature-state="off"]')).to_be_visible()
    expect(page.get_by_test_id("profile-apply-everything")).to_be_enabled()
    page.evaluate("() => { window.__reviewDocument = 'unchanged'; }")
    held = []
    profiles = []

    def hold_before_execution(route):
        # Deliberately NO route.fetch(): this POST has NOT executed on server.
        held.append(route)
        page.evaluate("() => { document.body.dataset.reviewHeldPost = '1'; }")

    def observe_profile(route):
        profiles.append(route.request.post_data)
        route.continue_()

    page.route("**/api/settings/features/api", hold_before_execution)
    page.route("**/api/settings/profile", observe_profile)
    page.get_by_test_id("feature-toggle-api").click()
    expect(page.locator("body")).to_have_attribute("data-review-held-post", "1")
    assert len(held) == 1
    # Keep the live tokens modal/focus open while submitting the real profile
    # form. The final authoritative root must close/reset it without navigation.
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-profile").click()
    page.get_by_test_id("account-tab-tokens").click()
    page.get_by_test_id("api-token-name-input").focus()
    page.evaluate("() => document.querySelector('[data-testid=profile-form-everything]').requestSubmit()")
    expect(page.get_by_test_id("profile-form-everything")).to_have_attribute("aria-busy", "true")
    assert profiles == [], "later POST must still be queued BEFORE server execution, not merely have its HTML discarded"
    held[0].continue_()
    expect(page.get_by_test_id("profile-form-everything")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator('[data-testid="feature-row-api"] [data-feature-state="on"]')).to_be_visible()
    expect(page.locator('[data-testid="feature-row-series"] [data-feature-state="on"]')).to_be_visible()
    assert len(profiles) == 1
    assert parse_qs(profiles[0])["profile"] == ["everything"]
    expect(page.locator("[data-account-root]")).to_have_count(1)
    expect(page.locator("[data-account-root]")).to_have_attribute("data-account-api", "1")
    expect(page.get_by_test_id("account-tab-tokens")).to_have_count(1)
    expect(page.get_by_test_id("api-tokens-pane")).to_have_count(1)
    expect(page.locator("[data-account-dialog]")).to_be_hidden()
    expect(page.get_by_test_id("account-menu-button")).to_be_focused()
    # Independent fresh SERVER rendering, not the older held redirected HTML.
    html = page.request.get(f"{session['url']}/settings").text()
    assert 'data-account-api="1"' in html
    assert html.count('data-testid="account-tab-tokens"') == 1
    assert page.evaluate("() => window.__reviewDocument") == "unchanged"
    assert_page_clean(page)


@pytest.mark.parametrize("new_manual_entry", [False, True])
def test_failed_held_camera_admission_keeps_new_manual_input_and_exposes_failed_code(store_account_review_session, new_manual_entry):
    session = store_account_review_session
    page = _open_store(session)
    session["context"].set_offline(True)
    page.evaluate("""() => {
        // A deterministic camera decode, not a replacement for enqueue/storage.
        window.createBarcodeScanner = options => {
            window.__reviewDecode = options.onDecode;
            return {engine: 'zxing', start: async () => {}, stop: async () => {}};
        };
    }""")
    page.get_by_test_id("camera-toggle").click()
    page.evaluate("""() => new Promise((resolve, reject) => {
        const request = indexedDB.open('shelf-store-coordination', 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('queue-lock', 'readwrite');
            const store = tx.objectStore('queue-lock');
            window.__reviewReleaseCameraAdmission = false;
            tx.oncomplete = () => db.close();
            function hold() {
                store.get('held-camera-admission').onsuccess = () => {
                    resolve(true);
                    if (!window.__reviewReleaseCameraAdmission) hold();
                };
            }
            hold();
        };
    })""")
    page.evaluate("code => window.__reviewDecode(code)", UNKNOWN)
    expect(page.locator("#verdict-note")).to_contain_text("Saving scan")
    assert _queue_raw(page) == "[]"
    if new_manual_entry:
        page.get_by_test_id("isbn-input").fill("9780140328721")
    page.evaluate("""() => {
        const save = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
            if (key === 'shelf-store-queue') throw new DOMException('Simulated quota failure', 'QuotaExceededError');
            return save.call(this, key, value);
        };
        window.__reviewReleaseCameraAdmission = true;
    }""")
    expect(page.locator("#store-session-warning")).to_contain_text(UNKNOWN)
    expect(page.locator("#store-session-warning")).to_contain_text("not saved")
    expect(page.get_by_test_id("isbn-input")).to_have_value("9780140328721" if new_manual_entry else UNKNOWN)
    assert _queue_raw(page) == "[]", "failed admission must neither enqueue nor acknowledge a scan"
    expect(page.get_by_test_id("queue-count")).to_have_text("0")
    assert_page_clean(page)


@pytest.mark.parametrize("width", [390, 1280])
def test_one_real_global_query_search_and_rendered_native_logout_csrf(store_account_review_session, width):
    session = store_account_review_session
    page = session["page"]
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{session['url']}/browse")
    global_form = page.get_by_test_id("nav-search-form")
    expect(global_form).to_have_count(1)
    expect(global_form.locator('input[name="query"]')).to_have_count(1)
    expect(page.locator("#nav-search-desktop")).to_be_visible()
    expect(page.locator('input[name="q"]')).to_have_count(1)  # Browse remains its own ONE control
    global_form.locator('input[name="query"]').fill("Review owned control")
    global_form.locator('button[type="submit"]').click()
    page.wait_for_url(re.compile(r"/browse\?q="))
    assert parse_qs(urlsplit(page.url).query)["q"] == ["Review owned control"]
    expect(page.locator("main")).to_contain_text("Review owned control")

    # Check SERVER-RENDERED markup, before csrf.js could inject missing fields.
    html = page.request.get(f"{session['url']}/browse").text()
    logout = re.search(r'<form[^>]*action="/logout"[^>]*>(.*?)</form>', html, re.S)
    assert logout is not None
    csrf = page.evaluate("() => window.csrfToken()")
    assert f'name="_csrf" value="{csrf}"' in logout.group(1)
    assert logout.group(1).count('name="_csrf"') == 1
    assert_page_clean(page)
