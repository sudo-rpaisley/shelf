"""RF2 component contracts over real authorized navigation/template sources.

The Node DOM double checks finite logic, not native browser/focus/layout behavior.
Shared base wiring and full authenticated HTTP acceptance belong to the parent.
"""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Element:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.text = ""
        self.parent = None

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def content(self):
        return self.text + "".join(child.content() for child in self.children)

    def serialize(self):
        return {"tag": self.tag, "attrs": self.attrs, "text": self.text,
                "children": [child.serialize() for child in self.children]}


class DOM(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = self.current = Element("document")
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        child = Element(tag, attrs)
        child.parent = self.current
        self.current.children.append(child)
        if tag not in {"input", "img", "br", "meta", "link", "hr"}:
            self.current = child

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.text += data


def _setting(db, key, value):
    from app.nav import invalidate_cache

    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()
    invalidate_cache()


def render_component(role="admin", *, empty=False):
    """Real app Jinja globals and real visibility/model; no copied tab payload."""
    from app.main import templates
    from app.media_presentation import family_nodes
    from app.nav import media_navigation, visible_tabs
    from starlette.requests import Request

    user = {"id": 1, "username": "component", "display_name": "Component", "role": role} if role else None
    tabs = [] if empty else visible_tabs(user)
    context = {"user": user, "nav_tabs": tabs,
               "media_navigation": media_navigation(tabs, family_nodes()),
               "request": Request({"type": "http", "path": "/", "headers": [], "query_string": b""})}
    return (templates.env.get_template("fragments/media_navigation.html").render(context)
            + '<main><h1>Component host</h1></main>'
            + templates.env.get_template("fragments/command_palette.html").render(context))


NODE_DOUBLE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
let document, window;
class Element {
    constructor(tag, attrs = {}, parent = null, text = '') {
        this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.parentElement = parent;
        this.children = []; this.listeners = {}; this.ownText = text; this.value = '';
    }
    matches(selector) {
        return selector.split(',').some(part => {
            if (/^#[\w-]+$/.test(part)) return this.getAttribute('id') === part.slice(1);
            const match = /^([a-z]+)?((?:\[[^\]]+\])*)$/.exec(part);
            assert.ok(match, 'Unsupported DOM-double selector: ' + part);
            if (match[1] && this.tagName !== match[1].toUpperCase()) return false;
            return [...match[2].matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)].every(attr =>
                Object.hasOwn(this.attrs, attr[1]) && (attr[2] === undefined || this.getAttribute(attr[1]) === attr[2]));
        });
    }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    contains(other) { return this === other || this.children.some(child => child.contains(other)); }
    hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
    getAttribute(name) { return this.hasAttribute(name) ? this.attrs[name] ?? '' : null; }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    removeAttribute(name) { delete this.attrs[name]; }
    get attributes() { return Object.keys(this.attrs).map(name => ({name, value: this.attrs[name]})); }
    get id() { return this.getAttribute('id') || ''; }
    set id(value) { this.setAttribute('id', value); }
    set className(value) { this.setAttribute('class', value); }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.replaceChildren(); this.ownText = String(value); }
    get tabIndex() {
        if (this.hasAttribute('tabindex')) return Number(this.getAttribute('tabindex'));
        return ['INPUT', 'BUTTON'].includes(this.tagName) || (this.tagName === 'A' && this.hasAttribute('href')) ? 0 : -1;
    }
    set tabIndex(value) { this.setAttribute('tabindex', value); }
    get hidden() { return this.hasAttribute('hidden'); }
    set hidden(value) { if (value) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
    get open() { return this.hasAttribute('open'); }
    get disabled() { return this.hasAttribute('disabled'); }
    get isConnected() { return document.contains(this); }
    get isContentEditable() {
        const owner = this.closest('[contenteditable]');
        return !!owner && owner.getAttribute('contenteditable') !== 'false';
    }
    append(child) { child.parentElement = this; this.children.push(child); }
    replaceChildren() { this.children.forEach(child => {child.parentElement = null;}); this.children = []; this.ownText = ''; }
    remove() { const parent = this.parentElement; parent.children = parent.children.filter(child => child !== this); this.parentElement = null; }
    focus() { document.activeElement = this; }
    scrollIntoView() { this.scrolled = (this.scrolled || 0) + 1; }
    getClientRects() {
        for (let node = this; node; node = node.parentElement) {
            if (node.hidden || node.hasAttribute('x-cloak') || node.getAttribute('x-show') === 'showAccount' ||
                (node.tagName === 'DIALOG' && !node.open)) return [];
        }
        return [{}];
    }
    addEventListener(type, callback, capture = false) { (this.listeners[type] ||= []).push({callback, capture: capture === true}); }
    showModal() { this.setAttribute('open', ''); }
    close() { this.removeAttribute('open'); emit(this, 'close', {bubbles: false}); }
}
function build(node, parent = null) {
    const element = new Element(node.tag, node.attrs, parent, node.text);
    element.children = node.children.map(child => build(child, element));
    return element;
}
document = build(data.tree);
document.readyState = 'complete'; document.activeElement = document.querySelector('main');
document.createElement = tag => new Element(tag);
window = new Element('window');
const navigations = [];
window.location = {origin: 'http://shelf.test', assign: href => navigations.push(href)};
window.getComputedStyle = () => ({visibility: 'visible'});
function emit(target, type, properties = {}) {
    const path = [];
    for (let node = target; node; node = node.parentElement) path.push(node);
    path.push(window);
    const event = {target, type, key: '', keyCode: 0, repeat: false, ctrlKey: false, metaKey: false,
        altKey: false, shiftKey: false, defaultPrevented: false, stopped: false, bubbles: true,
        composedPath: () => path, preventDefault() {this.defaultPrevented = true;},
        stopPropagation() {this.stopped = true;}, ...properties};
    const invoke = (node, capture) => (node.listeners[type] || []).filter(listener => listener.capture === capture).forEach(listener => listener.callback(event));
    for (const node of [...path].reverse()) { invoke(node, true); if (event.stopped) return event; }
    for (const node of path) { invoke(node, false); if (event.stopped || !event.bubbles) return event; }
    return event;
}
const source = fs.readFileSync('static/js/command-palette.js', 'utf8');
const sandbox = {document, window, URL};
vm.runInNewContext(source, sandbox);
const root = document.querySelector('[data-command-palette]');
const dialog = root.querySelector('[data-command-palette-dialog]');
const launch = root.querySelector('[data-command-palette-launcher]');
const input = root.querySelector('[data-command-palette-query]');
const results = root.querySelector('[data-command-palette-results]');
const nav = document.querySelector('[data-media-navigation]');
const options = () => results.querySelectorAll('[data-command-palette-result]');
const open = () => {emit(launch, 'click'); assert.ok(dialog.open);};
const query = value => {input.value = value; emit(input, 'input');};
const key = (key, props = {}, target = input) => emit(target, 'keydown', {key, ...props});
const addLink = (href, attrs = {}, label = 'Injected') => {
    const anchor = new Element('a', {href, 'data-nav-tab': 'fixture', ...attrs}, null, label);
    nav.append(anchor); return anchor;
};
"""


def _node(html, assertions, **data):
    completed = subprocess.run(
        ["node", "-e", NODE_DOUBLE + assertions + "\nconsole.log('DOM-double logic only; not browser evidence.');"],
        input=json.dumps({"tree": DOM(html).root.serialize(), **data}),
        text=True, capture_output=True, timeout=25, cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "not browser evidence" in completed.stdout


def test_fragment_is_named_labelled_native_and_contains_no_destination_payload():
    html = render_component()
    nodes = list(DOM(html).root.walk())
    palette = next(node for node in nodes if "data-command-palette" in node.attrs)
    descendants = list(palette.walk())
    dialog, = [node for node in descendants if node.tag == "dialog"]
    ids = {node.attrs["id"]: node for node in descendants if "id" in node.attrs}
    assert ids[dialog.attrs["aria-labelledby"]].content().strip() == "Commands"
    assert ids[dialog.attrs["aria-describedby"]].content().strip()
    query, = [node for node in descendants if "data-command-palette-query" in node.attrs]
    assert any(node.tag == "label" and node.attrs.get("for") == query.attrs["id"] for node in descendants)
    assert query.attrs["maxlength"] == "200"
    assert ids[query.attrs["aria-controls"]].attrs["role"] == "listbox"
    assert "open" not in dialog.attrs
    assert not any(node.tag in {"script", "form", "a"} for node in descendants)
    assert not any(attr.startswith(("on", "hx-", "x-")) for node in descendants for attr in node.attrs)
    assert any(node.tag == "summary" and node.content().strip() == "Commands" for node in descendants)
    launch, = [node for node in descendants if "data-command-palette-launcher" in node.attrs]
    assert launch.attrs["type"] == "button" and "hidden" in launch.attrs
    assert palette.parent.tag == "document", "Parent chooses placement; this fragment does not modify nav/main"


def test_fragment_forceescapes_translated_markup(monkeypatch):
    from app.main import templates
    from markupsafe import Markup

    hostile = '<palette-probe data-owned="no">translated</palette-probe> & "quoted"'
    monkeypatch.setitem(templates.env.globals, "_", lambda *args, **kwargs: Markup(hostile))
    html = templates.env.get_template("fragments/command_palette.html").render()
    nodes = list(DOM(html).root.walk())
    assert not any(node.tag == "palette-probe" for node in nodes)
    assert "&lt;palette-probe" in html
    dialog = next(node for node in nodes if node.tag == "dialog")
    assert dialog.attrs["data-search-label"] == hostile


@pytest.mark.parametrize("role", ["admin", "editor", "viewer", None])
@pytest.mark.parametrize("case", ["default", "configured", "manual", "feature"])
def test_real_authorized_render_is_the_only_inventory(db, role, case):
    from app.features import set_feature_enabled

    if case == "configured":
        _setting(db, "vision_provider", "ollama")
        _setting(db, "hardcover_token", "component-fixture-not-a-credential")
    elif case == "manual":
        _setting(db, "nav_hidden_tabs", json.dumps(["library", "music", "periodicals", "series", "stats", "reading-life"]))
    elif case == "feature":
        for feature in ("connected_library", "music", "periodicals", "series", "stats", "store"):
            set_feature_enabled(db, feature, False)
        db.commit()
    html = render_component(role)
    _node(html, r"""
open();
const marked = nav.querySelectorAll('a[data-nav-primary],a[data-nav-tab],a[data-nav-family-link],a[data-nav-format],a[data-nav-management]');
const expected = new Set(marked.map(a => {const u = new URL(a.getAttribute('href'), window.location.origin); u.searchParams.sort(); return u.pathname + u.search;}));
const actual = options().map(a => {const u = new URL(a.getAttribute('href'), window.location.origin); u.searchParams.sort(); return u.pathname + u.search;});
assert.deepEqual(new Set(actual), expected);
assert.equal(actual.length, expected.size, 'destinations must be deduplicated');
assert.equal(options()[0].textContent.trim(), 'Browse', 'primary wording must beat All media');
assert.ok(!options().some(a => a.textContent.includes('Sign out') || a.textContent.includes('API token')));
const primary = nav.querySelector('[data-nav-primary="browse"]').getAttribute('href');
assert.equal(options()[0].getAttribute('href'), primary);
assert.equal(primary.startsWith('/browse?'), data.core);
query('Unmatched catalogue text');
const search = options().at(-1);
const url = new URL(search.getAttribute('href'), window.location.origin);
assert.equal(url.pathname, new URL(primary, window.location.origin).pathname);
assert.equal(url.searchParams.get('q'), 'Unmatched catalogue text');
""", core=case in {"manual", "feature"} or role is None)


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "data:text/html,owned", "vbscript:msgbox(1)", "file:///browse", "mailto:test@example.test",
    "https://evil.example/browse", "//evil.example/browse", "//shelf.test/browse", "http://user:pass@shelf.test/browse",
    "/api/items/1", "/api/v1/tokens", "/logout", "/settings/tokens", "/discover/dismiss/1", "/sources/1/delete",
    "/browse?next=https%3A%2F%2Fevil.example", "/library?redirect=%2F%2Fevil.example", "/browse?return_to=/logout",
    "/browse?sort=newest&sort=recent", "/browse?reset=delete", "/browse?view=delete", "/browse?types=book;delete",
    "/browse?action=delete", "/settings?clear=1", "/browse#logout", "/%62rowse", "/library/../browse",
    "/browse\\evil", "/browse\n", " /browse", "browse", "/not-an-approved-page",
])
def test_link_boundary_rejects_schemes_redirects_mutations_and_unknown_paths(href):
    _node(render_component(), r"""
addLink(data.href);
open();
assert.ok(!options().some(a => a.textContent.includes('Injected')), data.href);
""", href=href)


@pytest.mark.parametrize("attrs", [{"hidden": ""}, {"aria-hidden": "true"}, {"inert": ""},
                                   {"hx-post": "/api/mutate"}, {"onclick": "alert(1)"}, {"download": "x"}])
def test_non_navigation_action_or_explicit_hidden_anchor_is_not_a_command(attrs):
    _node(render_component(), r"""
addLink('/browse?sort=recent', data.attrs);
open();
assert.ok(!options().some(a => a.textContent.includes('Injected')));
""", attrs=attrs)


def test_closed_disclosures_and_account_panel_are_included_but_unmarked_and_outside_links_are_not():
    _node(render_component(), r"""
nav.querySelectorAll('details').forEach(d => d.removeAttribute('open'));
const outside = new Element('a', {href:'/browse?sort=recent', 'data-nav-tab':'outside'}, null, 'Outside');
document.append(outside);
nav.append(new Element('a', {href:'/browse?sort=recent'}, null, 'Unmarked'));
const form = new Element('form'); form.append(new Element('a', {href:'/browse?sort=recent','data-nav-tab':'form'}, null, 'Form action')); nav.append(form);
open();
assert.ok(options().some(a => a.textContent.includes('Book')));
assert.ok(options().some(a => a.textContent.includes('Settings')));
assert.ok(!options().some(a => /Outside|Unmarked|Form action/.test(a.textContent)));
""")


def test_absolute_same_origin_link_and_canonical_dedup_preserve_primary_browse_wording():
    _node(render_component(), r"""
addLink('http://shelf.test/library?sort=recent&view=items&reset=1', {}, 'Duplicate primary');
addLink('http://shelf.test/browse?sort=newest', {}, 'Approved local');
open();
assert.equal(options().filter(a => a.getAttribute('href').startsWith('/library?') && !a.getAttribute('href').includes('types=')).length, 1);
assert.equal(options()[0].textContent, 'Browse');
query('Approved local');
key('Enter');
assert.deepEqual(navigations, ['/browse?sort=newest']);
""")


def test_search_text_is_bounded_encoded_inert_and_keeps_only_rendered_primary_parameters():
    _node(render_component(), r"""
open();
query('  <img src=x onerror=alert(1)> & q=//evil.example ?# 🐈 ' + 'x'.repeat(240));
const search = options().at(-1), url = new URL(search.getAttribute('href'), window.location.origin);
assert.equal(Array.from(url.searchParams.get('q')).length, 200);
assert.equal(url.origin, window.location.origin);
assert.equal(url.pathname, '/library');
assert.deepEqual([...url.searchParams.keys()].sort(), ['q','reset','sort','view']);
assert.equal(url.searchParams.get('reset'), '1');
assert.equal(url.searchParams.get('view'), 'items');
assert.equal(url.searchParams.get('sort'), 'recent');
assert.equal(search.querySelector('img'), null);
assert.ok(search.textContent.includes('<img'));
assert.deepEqual(navigations, [], 'input only filters locally');
key('Enter');
assert.deepEqual(navigations, [search.getAttribute('href')]);
""")


def test_full_200_unicode_character_query_survives_url_encoding():
    _node(render_component(), r"""
open(); query('🐈'.repeat(210));
assert.equal(options().length, 1, 'bounded Unicode text must still offer catalogue search');
const url = new URL(options()[0].getAttribute('href'), window.location.origin);
assert.equal(Array.from(url.searchParams.get('q')).length, 200);
assert.equal(url.searchParams.get('q'), '🐈'.repeat(200));
""")


def test_no_primary_means_no_invented_search_and_empty_results_cannot_activate_stale_selection():
    _node(render_component(), r"""
nav.querySelector('[data-nav-primary="browse"]').remove();
open();
assert.ok(options().length > 0);
query('no-such-navigation-command');
assert.equal(options().length, 0);
assert.equal(input.hasAttribute('aria-activedescendant'), false);
assert.equal(root.querySelector('[data-command-palette-status]').textContent, 'No commands match.');
key('ArrowDown'); key('ArrowUp'); key('Enter');
assert.deepEqual(navigations, []);
""")


def test_explicit_empty_authorized_context_has_no_destinations_or_actions():
    _node(render_component(empty=True), r"""
open();
assert.equal(options().length, 0);
query('search or delete'); key('Enter');
assert.equal(options().length, 0);
assert.deepEqual(navigations, []);
""")


def test_reinitialization_does_not_duplicate_keys_and_activation_revalidates_live_navigation():
    _node(render_component(), r"""
vm.runInNewContext(source, sandbox);
open();
const original = input.getAttribute('aria-activedescendant');
key('ArrowDown');
assert.equal(input.getAttribute('aria-activedescendant'), 'command-palette-result-1');
assert.notEqual(input.getAttribute('aria-activedescendant'), original);
const chosen = options()[1].getAttribute('href');
nav.querySelectorAll('a').filter(a => a.getAttribute('href') === chosen).forEach(a => a.remove());
key('Enter');
assert.deepEqual(navigations, [], 'removed navigation is not still authorized by a stale result');
query('Catalogue management'); key('Enter', {repeat:true});
assert.deepEqual(navigations, []);
key('Enter'); assert.deepEqual(navigations, ['/browse']);
""")


def test_modified_input_keys_and_default_prevented_enter_keep_native_editing_defaults():
    _node(render_component(), r"""
open(); query('Catalogue management');
const selected = input.getAttribute('aria-activedescendant');
for (const keyName of ['ArrowDown', 'ArrowUp', 'Enter']) {
    for (const modifier of ['shiftKey', 'ctrlKey', 'metaKey', 'altKey']) {
        const event = key(keyName, {[modifier]:true});
        assert.equal(event.defaultPrevented, false, keyName + ':' + modifier);
        assert.equal(input.getAttribute('aria-activedescendant'), selected);
    }
}
key('Enter', {defaultPrevented:true});
assert.deepEqual(navigations, []);
""")


def test_component_source_does_not_override_native_dialogs_or_gain_an_action_interpreter():
    js = (ROOT / "static/js/command-palette.js").read_text()
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "sessionStorage", "document.cookie",
                      "eval(", "new Function", ".submit(", ".requestSubmit(", ".click(", "innerHTML", "window.confirm =", "window.alert ="):
        assert forbidden not in js
    assert "url.searchParams.set('q', query)" in js
    css = (ROOT / "static/css/command-palette.source.css").read_text()
    assert "position: fixed" not in css
    assert "min-inline-size: 5rem" in css
    assert "--app-gutter:" not in css
