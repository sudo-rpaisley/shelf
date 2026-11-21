"""Real HTMX ownership: a held local search must not overwrite newer Browse HTML."""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error, expect

from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)

pytestmark = pytest.mark.e2e

OLD_QUERY = "OrderAlpha004"
NEW_QUERY = "OrderBeta004"
OLD_TITLE = f"{OLD_QUERY} Local Book"
NEW_BOOK = f"{NEW_QUERY} Local Book"
NEW_DISC = f"{NEW_QUERY} Local Disc"

_CSP_PROBE = """
window.__browseOrderCsp004 = [];
document.addEventListener('securitypolicyviolation', event => {
    window.__browseOrderCsp004.push(event.violatedDirective + ' <- ' + event.blockedURI);
});
"""


@pytest.fixture
def browse_order_session(server_factory, browser):
    # The same real setup flow as setup_admin, against THIS factory's disposable
    # install rather than the unrelated module-scoped live_server database.
    server = server_factory()
    credentials = _run_setup_wizard(browser, server["url"])
    for title, media_type in ((OLD_TITLE, "book"), (NEW_BOOK, "book"), (NEW_DISC, "dvd")):
        insert_item(server["data_dir"], title=title, media_type=media_type)
    context = browser.new_context()
    try:
        page = attach_page_guard(context.new_page())
        page.add_init_script(_CSP_PROBE)
        page.goto(f"{server['url']}/login")
        expect(page.locator("input[name=username]")).to_be_visible()
        assert page.evaluate("window.__browseOrderCsp004") == []
        page.locator("input[name=username]").fill(credentials["username"])
        page.locator("input[name=password]").fill(credentials["password"])
        page.locator("button[type=submit]").click()
        page.wait_for_url(f"{server['url']}/", timeout=10_000)
        assert page.evaluate("window.__browseOrderCsp004") == []
        assert_page_clean(page)
        yield server, page
        assert page.evaluate("window.__browseOrderCsp004") == []
        assert_page_clean(page)
    finally:
        context.close()


@pytest.mark.parametrize("later_action", ["enter", "filter", "view"])
def test_held_typeahead_cannot_overwrite_newer_search(browse_order_session, later_action):
    server, page = browse_order_session
    page.goto(f"{server['url']}/browse?media_type_filter=book")
    search = page.get_by_test_id("search-input")
    expect(page.locator('input[name="q"]')).to_have_count(1)
    expect(search).to_be_visible()
    expect(page.locator("#item-grid")).to_contain_text(OLD_TITLE)
    expect(page.locator("#item-grid")).to_contain_text(NEW_BOOK)

    # Read-only lifecycle instrumentation, not an XHR/HTMX replacement. In
    # particular beforeSend retains the REAL native XHR so a never-issued A
    # cannot turn the final-state assertion into a vacuous pass.
    page.evaluate("""({oldQuery, newQuery}) => {
        const form = document.getElementById('browse-filters');
        const state = window.__browseOrder004 = {starts: [], xhrs: {}, implicit: []};
        const query = detail => new URL(detail.pathInfo.finalRequestPath, location.href).searchParams;
        document.body.addEventListener('htmx:beforeSend', event => {
            const detail = event.detail;
            const params = query(detail);
            const q = params.get('q');
            if (q !== oldQuery && q !== newQuery) return;
            state.xhrs[q] = detail.xhr;
            state.starts.push({q, sourceId: detail.elt.id, sourceName: detail.elt.name,
                realXHR: detail.xhr instanceof XMLHttpRequest,
                formOwned: form['htmx-internal-data']?.xhr === detail.xhr,
                params: Array.from(params), hiddenView: form.elements.view.value});
        });
        document.body.addEventListener('htmx:confirm', event => {
            if (event.detail.elt.name === 'q' && form.elements.q.value === newQuery) {
                state.implicit.push({cancelled: event.defaultPrevented});
                document.body.dataset.browseOrderImplicitSeen = '1';
            }
        });
        for (const [name, outcome] of [['htmx:afterOnLoad', 'loaded'], ['htmx:sendAbort', 'aborted']]) {
            document.body.addEventListener(name, event => {
                const q = query(event.detail).get('q');
                if (q === oldQuery) document.body.dataset.browseOrderOldDone = outcome;
                if (q === newQuery) document.body.dataset.browseOrderNewDone = outcome;
            });
        }
    }""", {"oldQuery": OLD_QUERY, "newQuery": NEW_QUERY})

    held = []
    entered = []
    failed = []
    pattern = f"{server['url']}/api/search?*"

    def hold_actual_old_html(route):
        request = route.request
        params = parse_qs(urlsplit(request.url).query)
        entered.append(params)
        assert request.method == "GET"
        assert request.resource_type == "xhr"
        assert request.headers.get("hx-request") == "true"
        if params.get("q") != [OLD_QUERY]:
            route.continue_()
            return
        response = route.fetch(max_redirects=0)
        entry = {"route": route, "response": response, "request": request, "released": False}
        held.append(entry)  # Even a failing assertion below must release it.
        assert response.status == 200
        assert response.url == request.url
        assert OLD_TITLE in response.text()
        assert NEW_BOOK not in response.text()
        assert NEW_DISC not in response.text()
        page.evaluate("count => { document.body.dataset.browseOrderHeld = String(count); }", len(held))

    def record_failure(request):
        if urlsplit(request.url).path == "/api/search":
            failed.append((request.url, request.failure))

    def release_old():
        for entry in held:
            if entry["released"]:
                continue
            entry["released"] = True
            try:
                # Unmodified HTML from the genuine local GET, including OOB
                # selects/counts. An aborted route may reject fulfillment;
                # accept that ONLY with its actual Chromium cancellation.
                entry["route"].fulfill(response=entry["response"])
            except Error:
                assert (entry["request"].url, "net::ERR_ABORTED") in failed
            finally:
                entry["response"].dispose()

    page.route(pattern, hold_actual_old_html)
    page.on("requestfailed", record_failure)
    try:
        # fill alone emits no keyup. A genuine final keystroke starts the
        # existing changed/300ms type-ahead trigger without a synthetic request.
        search.fill(OLD_QUERY[:-1])
        search.press(OLD_QUERY[-1])
        expect(page.locator("body")).to_have_attribute("data-browse-order-held", "1")
        assert len(held) == 1
        assert page.evaluate(
            "q => window.__browseOrder004.xhrs[q] instanceof XMLHttpRequest && "
            "window.__browseOrder004.xhrs[q].readyState === 1", OLD_QUERY
        ) is True

        expected_type = "dvd" if later_action == "filter" else "book"
        expected_view = "list" if later_action == "view" else "grid"
        expected_title = NEW_DISC if later_action == "filter" else NEW_BOOK
        search.fill(NEW_QUERY)  # Dirty B, with A's actual 200 HTML still held.
        if later_action == "filter":
            page.get_by_test_id("filters-toggle").click()
            expect(page.get_by_test_id("filters-panel")).to_be_visible()
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/search"
            and parse_qs(urlsplit(response.url).query).get("q") == [NEW_QUERY]
        ) as latest_response:
            if later_action == "enter":
                search.press("Enter")  # The genuine form/requestSubmit path.
            elif later_action == "filter":
                page.locator("#type-filter").select_option("dvd")
            else:
                page.get_by_test_id("view-list").click()
        assert latest_response.value.status == 200
        assert latest_response.value.request.method == "GET"
        assert latest_response.value.request.resource_type == "xhr"
        latest_response.value.finished()
        expect(page.locator("body")).to_have_attribute("data-browse-order-new-done", "loaded")
        expect(page.locator("#item-grid")).to_contain_text(expected_title)
        expect(page.locator("#item-grid")).not_to_contain_text(OLD_TITLE)
        if later_action == "enter":
            # Observe the REAL delayed keyup attempt as well, not a sleep or a
            # synthetic second request. It must not cancel the valid B XHR.
            expect(page.locator("body")).to_have_attribute("data-browse-order-implicit-seen", "1")

        if page.locator("body").get_attribute("data-browse-order-old-done") == "aborted":
            release_old()
        else:
            # Negative-control compatibility: an unfixed A must really finish
            # and run its late onload before checking final DOM, URL and pills.
            with page.expect_event("requestfinished", predicate=lambda request: request.url == held[0]["request"].url):
                release_old()
            expect(page.locator("body")).to_have_attribute("data-browse-order-old-done", "loaded")

        expect(page.locator("#item-grid")).to_contain_text(expected_title)
        expect(page.locator("#item-grid")).not_to_contain_text(OLD_TITLE)
        expect(page.locator("#item-grid")).not_to_contain_text(NEW_BOOK if later_action == "filter" else NEW_DISC)
        expect(page.locator("#type-filter")).to_have_value(expected_type)
        expect(page.locator('input[name="view"]')).to_have_value(expected_view)
        expect(page.locator("#item-grid table")).to_have_count(1 if expected_view == "list" else 0)
        type_label = "DVD / Blu-ray" if expected_type == "dvd" else "Book"
        expect(page.locator('[x-text="pill.label"]')).to_have_text([f"Search: {NEW_QUERY}", f"Type: {type_label}"])
        assert urlsplit(page.url).path == "/browse"
        assert parse_qs(urlsplit(page.url).query) == {"q": [NEW_QUERY], "media_type_filter": [expected_type]}
        starts = page.evaluate("window.__browseOrder004.starts")
        assert [start["q"] for start in starts] == [OLD_QUERY, NEW_QUERY]
        assert starts[0]["sourceName"] == "q"
        assert starts[1]["sourceId"] == ("browse-filters" if later_action == "enter" else "type-filter")
        for start in starts:
            assert start["realXHR"] is True
            assert start["formOwned"] is True
            params = dict(start["params"])
            assert len(params) == len(start["params"]), "each actual filter is serialized only once"
            assert params["view"] == start["hiddenView"]
        assert dict(starts[1]["params"])["media_type_filter"] == expected_type
        assert dict(starts[1]["params"])["view"] == expected_view
        assert len([params for params in entered if params.get("q") == [OLD_QUERY]]) == 1
        assert len([params for params in entered if params.get("q") == [NEW_QUERY]]) == 1
        assert failed == [(held[0]["request"].url, "net::ERR_ABORTED")]
        expect(page.locator("body")).to_have_attribute("data-browse-order-old-done", "aborted")
        if later_action == "enter":
            assert page.evaluate("window.__browseOrder004.implicit") == [{"cancelled": True}]
        assert page.evaluate("window.__browseOrderCsp004") == []
        assert_page_clean(page)
    finally:
        try:
            release_old()
        finally:
            page.unroute(pattern, hold_actual_old_html)
            page.remove_listener("requestfailed", record_failure)
