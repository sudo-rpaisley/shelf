"""Execute real Browse startup in Node, then replay captured HTMX through FastAPI.

The DOM double is built from the registered page's rendered controls and inert
registry JSON. It is not a browser/Alpine/HTMX lifecycle or visual acceptance test.
"""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app import browse_filters
from tests.conftest import _insert_borrower, _insert_item, _insert_location


ROOT = Path(__file__).resolve().parents[1]
NODE = "/usr/bin/node"
BROWSE_JS = ROOT / "static/js/browse.js"


class _BrowseMarkup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.controls = []
        self.scripts = {}
        self.initial_query = None
        self.select = self.option = self.script = None
        self.feed(text)
        for control in self.controls:
            if control["tagName"] == "SELECT" and control["options"]:
                selected = [option for option in control["options"] if "selected" in option["attrs"]]
                control["value"] = (selected or control["options"])[-1 if selected else 0]["value"]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("x-data") == "browsePage":
            self.initial_query = attrs["data-initial-query"]
        if tag in {"input", "select"} and attrs.get("name"):
            control = {"tagName": tag.upper(), "attrs": attrs, "value": attrs.get("value", ""), "options": []}
            self.controls.append(control)
            if tag == "select":
                self.select = control
        elif tag == "option" and self.select is not None:
            self.option = {"attrs": attrs, "value": attrs.get("value", ""), "text": ""}
            self.select["options"].append(self.option)
        elif tag == "script" and attrs.get("id") in {"browse-filter-config", "browse-column-config"}:
            self.script = attrs["id"]
            self.scripts[self.script] = ""

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data
        if self.script is not None:
            self.scripts[self.script] += data

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = self.option = None
        elif tag == "option":
            self.option = None
        elif tag == "script":
            self.script = None


# No replacement implementations of init/restoreFilters/restoreSort/runSearch or
# parameter collection: all are executed from the current production browse.js.
STARTUP = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const requests = [], redirects = [], writes = [], reads = [], listeners = {};
const registered = {};
const controls = input.controls.map(raw => {
    const element = {
        tagName: raw.tagName, name: raw.attrs.name, type: raw.attrs.type || '',
        value: raw.value, options: raw.options, dataset: {},
        addEventListener() {},
        querySelector(selector) {
            const match = /^option\[value="([^"]*)"\]$/.exec(selector);
            if (!match) throw new Error('Unsupported DOM selector: ' + selector);
            return this.options.find(option => option.value === match[1]) || null;
        }
    };
    Object.entries(raw.attrs).forEach(([key, value]) => {
        if (key.startsWith('data-')) {
            element.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
        }
    });
    Object.defineProperty(element, 'selectedIndex', {
        get() { return this.options.findIndex(option => option.value === this.value); }
    });
    return element;
});
function named(selector) {
    const match = /^\[name="([^"]+)"\]$/.exec(selector);
    if (!match) throw new Error('Unsupported DOM selector: ' + selector);
    return controls.filter(control => control.name === match[1]);
}
function storage(kind, initial) {
    const values = {...initial};
    return {
        getItem(key) {
            reads.push([kind, key]);
            if (kind === 'local' && key === input.deniedKey) {
                const error = new Error('Storage access denied');
                error.name = 'SecurityError';
                throw error;
            }
            return Object.hasOwn(values, key) ? values[key] : null;
        },
        setItem(key, value) { writes.push([kind, 'set', key, value]); values[key] = String(value); },
        removeItem(key) { writes.push([kind, 'remove', key]); delete values[key]; }
    };
}
const document = {
    body: {addEventListener(name) { listeners[name] = true; }},
    addEventListener(name, handler) { listeners[name] = handler; },
    removeEventListener() {},
    querySelector(selector) { return named(selector)[0] || null; },
    querySelectorAll: named,
    getElementById(id) {
        if (Object.hasOwn(input.scripts, id)) return {textContent: input.scripts[id]};
        if (id === 'item-grid') return {};
        for (const raw of input.controls) {
            const option = raw.options.find(option => option.attrs.id === id);
            if (option) return option;
        }
        return null;
    }
};
const context = {
    document, URLSearchParams,
    window: {location: {pathname: '/browse', search: input.search, replace(url) { redirects.push(url); }}},
    history: {replaceState() { throw new Error('Unexpected startup history mutation'); }},
    localStorage: storage('local', input.local), sessionStorage: storage('session', input.session),
    htmx: {
        ajax(method, url, options) { requests.push({method, url, options}); },
        trigger() { throw new Error('Startup must use htmx.ajax, not a racy trigger'); }
    },
    Alpine: {data(name, factory) { registered[name] = factory; }},
    t(value) { return value; }, showToast() {},
    setTimeout() { throw new Error('Unrelated shortcut timer was not disabled'); }
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context, {filename: process.argv[1]});
listeners['alpine:init']();
let page, error = null;
try {
    page = registered.browsePage();
    page.$el = {dataset: {initialQuery: input.initialQuery}};
    page.init();
} catch (failure) {
    error = {name: failure.name, message: failure.message};
}
process.stdout.write(JSON.stringify({
    requests, redirects, error, reads, writes,
    sort: controls.find(control => control.name === 'sort').value,
    controls: controls.map(control => ({name: control.name, value: control.value})),
    pills: page && page.filterPills, columns: page && page.visibleCols,
    listeners: Object.keys(listeners)
}));
"""


def _startup(response, url, view, *, saved_sort="title_asc", session=None, denied_key=None):
    markup = _BrowseMarkup(response.text)
    assert markup.initial_query is not None
    assert json.loads(markup.scripts["browse-filter-config"]) == browse_filters.client_config()
    local = {"shelf-view": view, "shelf-shortcuts-seen": "1"}
    if saved_sort is not None:
        local["shelf-sort"] = saved_sort
    result = subprocess.run(
        [NODE, "-e", STARTUP, str(BROWSE_JS)], cwd=ROOT, input=json.dumps({
            "controls": markup.controls, "scripts": markup.scripts,
            "initialQuery": markup.initial_query,
            "search": "?" + urlsplit(url).query if urlsplit(url).query else "",
            "local": local, "session": session or {}, "deniedKey": denied_key,
        }), capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout), markup


def _ids(response):
    return list(dict.fromkeys(int(value) for value in re.findall(r'data-item-id="(\d+)"', response.text)))


def _initial_request(state):
    assert state["error"] is None, state
    assert len(state["requests"]) == 1 and state["redirects"] == [], state
    request = state["requests"][0]
    assert request["method"] == "GET"
    assert request["options"] == {"target": "#item-grid", "swap": "innerHTML"}
    assert "htmx:afterSwap" in state["listeners"] and "keydown" in state["listeners"]
    assert state["writes"] == []  # Startup did not create another store/key.
    print("INITIAL_HTMX_REQUEST=" + json.dumps(request, sort_keys=True))
    return request["url"]


@pytest.mark.parametrize("view", ["grid", "list"])
@pytest.mark.parametrize("types,valid", [("book,ebook", True), ("book,unknown", False)])
def test_initializer_sort_restore_preserves_format_predicate(viewer_client, db, view, types, valid):
    paper = _insert_item(db, title="Beta paper", isbn=None, media_type="book")
    ebook = _insert_item(db, title="Alpha ebook", isbn=None, media_type="ebook")
    dvd = _insert_item(db, title="Excluded DVD", isbn=None, media_type="dvd")
    db.commit()
    url = "/browse?" + urlencode({"types": types})
    page = viewer_client.get(url)
    expected = {paper, ebook} if valid else set()
    assert page.status_code == 200 and set(_ids(page)) == expected
    state, _ = _startup(page, url, view)
    request_url = _initial_request(state)
    fragment = viewer_client.get(request_url)
    assert fragment.status_code == 200
    assert set(_ids(fragment)) == expected, state
    assert dvd not in _ids(fragment)
    query = parse_qs(urlsplit(request_url).query)
    assert query["view"] == [view] and query["sort"] == ["title_asc"]
    assert query["types"] == [next(control["value"] for control in state["controls"] if control["name"] == "types")]
    assert any(pill["name"] == "types" for pill in state["pills"])


@pytest.mark.parametrize("view", ["grid", "list"])
def test_initializer_restored_sort_preserves_all_rendered_registry_controls_and_deduplicates(viewer_client, db, view):
    location = _insert_location(db, "Selected shelf")
    wanted = _insert_item(db, title="Wanted & chosen", isbn=None, owned=1, source="manual",
                          language="eng", location_id=location, reading_status="read")
    _insert_item(db, title="Wanted & chosen film", isbn=None, media_type="dvd", source="manual")
    _insert_item(db, title="Other paper", isbn=None, source="manual")
    tag = db.execute("INSERT INTO tags (name) VALUES ('Signed & selected')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (wanted, tag))
    borrower = _insert_borrower(db)
    db.execute("INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)", (wanted, borrower))
    db.commit()
    url = "/browse?" + urlencode([
        ("q", "ignored first"), ("q", "Wanted & chosen"), ("types", "ebook,book"),
        ("media_type_filter", "book"), ("owned", "1"), ("source_filter", "manual"),
        ("location_filter", str(location)), ("reading_status", "read"), ("language", "eng"),
        ("tag", "Signed & selected"), ("lent_out", "1"), ("view", "grid"), ("view", "list"),
        ("unknown_url_field", "must-not-propagate"),
    ])
    page = viewer_client.get(url)
    assert page.status_code == 200 and _ids(page) == [wanted]
    state, markup = _startup(page, url, view)
    request_url = _initial_request(state)
    fragment = viewer_client.get(request_url)
    assert fragment.status_code == 200 and _ids(fragment) == [wanted], state
    query = parse_qs(urlsplit(request_url).query, keep_blank_values=True)
    expected = {control["attrs"]["name"]: control["value"] for control in markup.controls
                if control["attrs"]["name"] in browse_filters.FILTER_NAMES}
    expected.update(sort="title_asc", view=view)
    assert query == {name: [value] for name, value in expected.items()}
    assert query["q"] == ["Wanted & chosen"] and query["types"] == ["book,ebook"]
    assert query["view"] == [view] and "unknown_url_field" not in query
    assert {control["value"] for control in state["controls"] if control["name"] == "q"} == {"Wanted & chosen"}


@pytest.mark.parametrize("view", ["grid", "list"])
def test_existing_bare_page_sort_restore_changes_real_rows_without_losing_view(viewer_client, db, view):
    beta = _insert_item(db, title="Beta", isbn=None, created_at="2026-01-02")
    alpha = _insert_item(db, title="Alpha", isbn=None, created_at="2026-01-01")
    db.commit()
    page = viewer_client.get("/browse")
    assert _ids(page) == [beta, alpha]
    state, _ = _startup(page, "/browse", view)
    url = _initial_request(state)
    assert parse_qs(urlsplit(url).query)["view"] == [view]
    assert _ids(viewer_client.get(url)) == [alpha, beta]


@pytest.mark.parametrize("sort", ["newest", "title_desc"])
def test_explicit_url_sort_wins_over_saved_sort_without_a_startup_request(viewer_client, sort):
    url = "/browse?" + urlencode({"types": "book,ebook", "sort": sort})
    state, _ = _startup(viewer_client.get(url), url, "list")
    assert state["error"] is None and state["requests"] == [] and state["sort"] == sort


@pytest.mark.parametrize("saved_sort", [None, "", "newest", "obsolete_sort"])
def test_missing_default_and_obsolete_saved_sorts_remain_noops(viewer_client, saved_sort):
    url = "/browse?types=book,ebook"
    state, _ = _startup(viewer_client.get(url), url, "grid", saved_sort=saved_sort)
    assert state["error"] is None and state["requests"] == [] and state["sort"] == "newest"


def test_denied_sort_storage_keeps_the_existing_error_without_issuing_a_request(viewer_client):
    url = "/browse?types=book,ebook"
    state, _ = _startup(viewer_client.get(url), url, "grid", denied_key="shelf-sort")
    assert state["error"] == {"name": "SecurityError", "message": "Storage access denied"}
    assert state["requests"] == [] and state["writes"] == []


@pytest.mark.parametrize("view", ["grid", "list"])
def test_existing_session_filter_restore_precedes_sort_fallback(viewer_client, db, view):
    paper = _insert_item(db, title="Saved paper", isbn=None)
    _insert_item(db, title="Other film", isbn=None, media_type="dvd")
    db.commit()
    page = viewer_client.get("/browse")
    state, _ = _startup(page, "/browse", view, session={
        "shelf-browse-qs": "types=book,ebook&q=Saved&sort=title_desc&unknown_url_field=ignored",
    })
    url = _initial_request(state)
    assert parse_qs(urlsplit(url).query) == {
        "types": ["book,ebook"], "q": ["Saved"], "sort": ["title_desc"], "view": [view],
    }
    assert _ids(viewer_client.get(url)) == [paper]
