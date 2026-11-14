"""Derived navigation and its isolated fragment, not main/base integration.

The small immutable fixtures are NOT registry coverage. Separate real-registry
tests below consume the exact parent dependency. Main/base and query-route
acceptance remain parent gates. Visibility always comes from real visible_tabs.
"""

import json
from copy import deepcopy
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import nav


ADMIN = {"id": 1, "username": "admin", "role": "admin"}
EDITOR = {"id": 2, "username": "editor", "role": "editor"}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer"}


@dataclass(frozen=True)
class FixtureFormat:
    media_type: str
    selector: str
    label: str


@dataclass(frozen=True)
class FixtureFamily:
    key: str
    selector: str
    label: str
    origin_tab: str
    formats: tuple[FixtureFormat, ...]


@dataclass(frozen=True)
class FixtureIntent:
    media_types: tuple[str, ...]


@pytest.fixture
def nodes():
    """Small explicit examples of the frozen interface, not registry coverage."""
    return (
        FixtureFamily("books", "family:books", "Books", "browse", (
            FixtureFormat("book", "format:book", "Book"),
            FixtureFormat("ebook", "format:ebook", "eBook"),
        )),
        FixtureFamily("comics", "family:comics", "Comics", "browse", (
            FixtureFormat("comic", "format:comic", "Comic / Graphic Novel"),
            FixtureFormat("manga", "format:manga", "Manga"),
        )),
        FixtureFamily("music", "family:music", "Music", "music", (
            FixtureFormat("vinyl", "format:vinyl", "Vinyl"),
            FixtureFormat("cd", "format:cd", "CD"),
        )),
        FixtureFamily("films", "family:films", "Films", "browse", (
            FixtureFormat("dvd", "format:dvd", "DVD / Blu-ray"),
            FixtureFormat("digital_video", "format:digital_video", "Digital Video"),
        )),
        FixtureFamily("tv", "family:tv", "TV", "browse", (
            FixtureFormat("tv_series", "format:tv_series", "TV Series"),
        )),
        FixtureFamily("games", "family:games", "Games", "browse", (
            FixtureFormat("video_game", "format:video_game", "Video Game"),
        )),
        FixtureFamily("periodicals", "family:periodicals", "Periodicals", "periodicals", (
            FixtureFormat("magazine", "format:magazine", "Magazine"),
        )),
    )


@pytest.fixture
def intent_for(nodes):
    """A strict test-only resolver; no fake app.media_presentation module."""
    expansions = {}
    for family in nodes:
        expansions[family.selector] = tuple(fmt.media_type for fmt in family.formats)
        for fmt in family.formats:
            expansions[fmt.selector] = (fmt.media_type,)

    def resolve(selectors):
        assert len(selectors) == 1
        return FixtureIntent(expansions[selectors[0]])

    return resolve


def _setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value),
    )
    db.commit()
    nav.invalidate_cache()


def _feature(db, key, enabled):
    from app.features import set_feature_enabled

    set_feature_enabled(db, key, enabled)
    db.commit()


def _model(user, nodes, intent_for):
    return nav.media_navigation(nav.visible_tabs(user), nodes, intent_for=intent_for)


def _family(model, key):
    return next(family for family in model["families"] if family["key"] == key)


def test_existing_mandatory_and_hideable_policy_is_a_passing_control(db):
    _setting(db, "nav_hidden_tabs", json.dumps(["browse", "settings", "library", "music"]))
    keys = {tab["key"] for tab in nav.visible_tabs(ADMIN)}
    assert {"browse", "settings"} <= keys
    assert not {"library", "music"} & keys
    assert set(nav.ALWAYS_VISIBLE) == {"browse", "settings"}
    assert {"library", "music", "periodicals"} <= nav.HIDEABLE_KEYS


def test_primary_browse_is_mixed_reset_items_recent(db, nodes, intent_for):
    model = _model(ADMIN, nodes, intent_for)
    assert model["primary"]["key"] == "browse"
    assert model["primary"]["path"] == "/library?reset=1&view=items&sort=recent"
    assert model["all_media"]["key"] == "library"
    assert model["all_media"]["path"] == model["primary"]["path"]
    assert model["management"]["path"] == "/browse"


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
def test_disabled_library_uses_a_nonempty_core_browse_query(db, nodes, intent_for, user):
    _feature(db, "connected_library", False)
    model = _model(user, nodes, intent_for)
    assert model["primary"]["path"] == "/browse?sort=newest"
    assert model["all_media"] is None
    assert model["management"]["path"] == "/browse"
    for family in model["families"]:
        for fmt in family["formats"]:
            parts = urlsplit(fmt["path"])
            assert parts.path == "/browse"
            assert parse_qs(parts.query) == {"sort": ["newest"], "media_type_filter": [fmt["key"]]}


def test_hidden_library_does_not_hide_browse_or_leak_its_optional_route(db, nodes, intent_for):
    _setting(db, "nav_hidden_tabs", json.dumps(["library", "browse", "settings"]))
    model = _model(ADMIN, nodes, intent_for)
    assert model["primary"]["path"] == "/browse?sort=newest"
    assert model["all_media"] is None
    assert "settings" in {tab["key"] for tab in nav.visible_tabs(ADMIN)}


def test_selectors_expand_without_reusing_the_old_book_family(db, nodes, intent_for):
    model = _model(ADMIN, nodes, intent_for)
    assert [family["key"] for family in model["families"]] == [node.key for node in nodes]
    for family in model["families"]:
        for link in (family, *family["formats"]):
            parts = urlsplit(link["path"])
            assert parts.path == "/library"
            query = parse_qs(parts.query)
            assert query == {
                "reset": ["1"], "view": ["items"], "sort": ["recent"],
                "types": [",".join(link["media_types"])],
            }
            assert "family" not in query
    assert _family(model, "books")["media_types"] == ("book", "ebook")
    assert _family(model, "comics")["media_types"] == ("comic", "manga")


def test_core_fallback_never_silently_broadens_a_multiformat_family(db, nodes, intent_for):
    _feature(db, "connected_library", False)
    model = _model(ADMIN, nodes, intent_for)
    assert _family(model, "books")["path"] is None
    assert _family(model, "tv")["path"] == "/browse?sort=newest&media_type_filter=tv_series"
    assert _family(model, "music")["landing"]["path"] == "/music"
    assert _family(model, "periodicals")["landing"]["path"] == "/periodicals"


@pytest.mark.parametrize("key", ["music", "periodicals"])
@pytest.mark.parametrize("cause", ["feature", "manual"])
def test_optional_branch_visibility_is_inherited_from_its_origin_tab(db, nodes, intent_for, key, cause):
    if cause == "feature":
        _feature(db, key, False)
    else:
        _setting(db, "nav_hidden_tabs", json.dumps([key]))
    seen = []

    def resolve(selectors):
        seen.extend(selectors)
        return intent_for(selectors)

    model = _model(ADMIN, nodes, resolve)
    assert key not in {family["key"] for family in model["families"]}
    hidden = next(node for node in nodes if node.key == key)
    assert not {hidden.selector, *(fmt.selector for fmt in hidden.formats)} & set(seen)
    assert model["primary"]["path"] == "/library?reset=1&view=items&sort=recent"


def test_derivation_is_pure_does_not_mutate_or_read_policy(db, monkeypatch, nodes, intent_for):
    tabs = nav.visible_tabs(ADMIN)
    before = deepcopy(tabs)

    def forbidden(*args, **kwargs):
        raise AssertionError("derived navigation must not read settings, flags or the database")

    monkeypatch.setattr(nav, "_nav_settings", forbidden)
    monkeypatch.setattr(nav, "feature_enabled", forbidden)
    monkeypatch.setattr(nav, "visible_tabs", forbidden)
    import app.database as database
    monkeypatch.setattr(database, "get_db", forbidden)
    model = nav.media_navigation(tabs, nodes, intent_for=intent_for)
    assert tabs == before
    assert model["primary"]["path"] == "/library?reset=1&view=items&sort=recent"
    assert [group["key"] for group in model["groups"]] == ["add", "explore"]
    group_tabs = [tab for group in model["groups"] for tab in group["tabs"]]
    assert not {"browse", "library", "music", "periodicals"} & {tab["key"] for tab in group_tabs}
    assert "series" in {tab["key"] for tab in group_tabs}


def test_invalid_selection_is_not_swallowed_into_a_mixed_link(db, nodes):
    def invalid(selectors):
        raise ValueError("invalid selector")

    with pytest.raises(ValueError, match="invalid selector"):
        _model(ADMIN, nodes, invalid)


def test_empty_nonmixed_expansion_is_rejected(db, nodes):
    with pytest.raises(ValueError, match="empty"):
        _model(ADMIN, nodes, lambda selectors: FixtureIntent(()))


class Element:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        self.text = ""

    def find(self, tag=None, **attrs):
        return [child for child in self.walk()
                if (tag is None or child.tag == tag)
                and all(key in child.attrs and child.attrs[key] == value for key, value in attrs.items())]

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()


class FragmentDOM(HTMLParser):
    """Parse rendered HTML; never use source-string href fixtures as evidence."""
    def __init__(self, html):
        super().__init__()
        self.root = Element("document")
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        child = Element(tag, attrs, self.current)
        self.current.children.append(child)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.current = child

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, data):
        current = self.current
        while current:
            current.text += data
            current = current.parent


def _render(user, nodes, intent_for, *, model=None, **extra):
    # Use the server's real Jinja globals/autoescape (G92), without pretending
    # this fragment has already replaced the parent's base navigation.
    import app.main as main
    from starlette.requests import Request

    tabs = nav.visible_tabs(user)
    request = Request({"type": "http", "path": "/stats", "headers": [], "query_string": b""})
    html = main.templates.env.get_template("fragments/media_navigation.html").render({
        "request": request, "user": user, "nav_tabs": tabs,
        "media_navigation": model if model is not None else nav.media_navigation(tabs, nodes, intent_for=intent_for),
        **extra,
    })
    return html, FragmentDOM(html).root


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
def test_rendered_fragment_preserves_every_authorized_destination(db, user):
    from app import media_presentation as presentation

    _setting(db, "vision_provider", "ollama")
    _setting(db, "hardcover_token", "fixture-only")
    _, dom = _render(user, presentation.family_nodes(), presentation.filter_intent)
    anchors = dom.find("a")
    by_key = {anchor.attrs["data-nav-tab"]: anchor
              for anchor in anchors if "data-nav-tab" in anchor.attrs}
    visible = nav.visible_tabs(user)
    assert set(by_key) == {tab["key"] for tab in visible}
    for tab in visible:
        assert urlsplit(by_key[tab["key"]].attrs["href"]).path == (
            "/library" if tab["key"] == "browse" and user else tab["path"]
        )
    if user:
        assert {"reading-life", "source-progress", "sources"} <= set(by_key)
    assert ("source-migrations" in by_key) == (user == ADMIN)
    assert ("settings" in by_key) == (user == ADMIN)
    assert len(dom.find("a", **{"data-nav-primary": "browse"})) == 1
    for key in ("library", "music", "periodicals"):
        matching = dom.find("a", **{"data-nav-tab": key})
        assert len(matching) <= 1
        for anchor in matching:
            ancestor = anchor.parent
            assert any(parent.tag == "details" for parent in _ancestors(ancestor))


def _ancestors(element):
    while element:
        yield element
        element = element.parent


@pytest.mark.parametrize("cause", ["manual", "feature"])
def test_rendered_tree_contains_no_hidden_optional_destination_or_format(db, nodes, intent_for, cause):
    keys = ["library", "music", "periodicals", "series", "stats", "store", "reading-life", "source-progress", "source-migrations"]
    if cause == "manual":
        _setting(db, "nav_hidden_tabs", json.dumps(keys))
    else:
        for key in ("connected_library", "music", "periodicals", "series", "stats", "store"):
            _feature(db, key, False)
    _, dom = _render(ADMIN, nodes, intent_for)
    hrefs = [anchor.attrs["href"] for anchor in dom.find("a")]
    forbidden = {tab["path"] for tab in nav.NAV_TABS if tab["key"] in keys}
    assert not forbidden & {urlsplit(href).path for href in hrefs}
    assert not dom.find(**{"data-nav-family": "music"})
    assert not dom.find(**{"data-nav-family": "periodicals"})
    assert not any("data-nav-format" in anchor.attrs and anchor.attrs["data-nav-format"] in {"vinyl", "cd", "magazine"} for anchor in dom.find("a"))
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/browse?sort=newest"
    manager, = dom.find("a", **{"data-nav-management": "catalogue"})
    assert manager.attrs["href"] == "/browse"
    assert dom.find("a", **{"data-nav-tab": "settings"})


def test_rendered_primary_disclosure_and_exact_filter_hrefs(db, nodes, intent_for):
    _, dom = _render(ADMIN, nodes, intent_for)
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/library?reset=1&view=items&sort=recent"
    assert not any(attr.startswith(("@", "on", "x-", "hx-")) for attr in primary.attrs)
    assert primary.parent.children.index(primary) + 1 == next(
        index for index, element in enumerate(primary.parent.children)
        if element.tag == "details" and "data-nav-shell" in element.attrs
    )
    for node in nodes:
        branch, = dom.find(**{"data-nav-family": node.key})
        family_anchor, = branch.find("a", **{"data-nav-family-link": node.key})
        assert parse_qs(urlsplit(family_anchor.attrs["href"]).query)["types"] == [",".join(fmt.media_type for fmt in node.formats)]
        for fmt in node.formats:
            anchor, = branch.find("a", **{"data-nav-format": fmt.media_type})
            assert parse_qs(urlsplit(anchor.attrs["href"]).query) == {
                "reset": ["1"], "view": ["items"], "sort": ["recent"], "types": [fmt.media_type],
            }
    series, = dom.find("a", **{"data-nav-tab": "series"})
    assert series.attrs["href"] == "/series"
    assert any(parent.attrs.get("data-nav-group") == "explore" for parent in _ancestors(series))
    manager, = dom.find("a", **{"data-nav-management": "catalogue"})
    assert manager.attrs["href"] == "/browse"
    assert any(parent.attrs.get("data-nav-group") == "manage" for parent in _ancestors(manager))


def test_missing_integration_configuration_is_not_reintroduced_in_the_fragment(db, nodes, intent_for):
    _, dom = _render(ADMIN, nodes, intent_for)
    paths = {urlsplit(anchor.attrs["href"]).path for anchor in dom.find("a")}
    assert not {"/intake", "/discover"} & paths
    assert {"/scan", "/shelf-fill", "/store", "/series"} <= paths


def test_native_fallback_has_summaries_links_and_no_new_alpine_component(db, nodes, intent_for):
    _, dom = _render(None, nodes, intent_for)
    root, = dom.find("nav", **{"data-media-navigation": None})
    shell, = dom.find("details", **{"data-nav-shell": None})
    assert "open" in shell.attrs  # no-JS content is never permanently hidden
    for details in root.find("details"):
        assert "data-nav-disclosure" in details.attrs
        assert details.children[0].tag == "summary"
        assert details.children[0].text.strip()
    assert not root.find("script")
    assert not any("x-data" in element.attrs or "x-cloak" in element.attrs for element in root.walk())
    assert not any(name.startswith("on") for element in root.walk() for name in element.attrs)
    assert not any("data-nav-count" in element.attrs for element in root.walk())


def test_server_render_escapes_ui_labels_and_never_translates_catalogue_text(db, nodes, intent_for, monkeypatch):
    import app.i18n as i18n
    from dataclasses import replace

    hostile = '<img src=x onerror="alert(1)"> & "title"'
    seen = []
    original = i18n._catalog

    def catalogue(domain, lang):
        translator = original(domain, lang)

        class Recording:
            def gettext(self, text):
                seen.append(text)
                return translator.gettext(text)

        return Recording()

    monkeypatch.setattr(i18n, "_catalog", catalogue)
    changed = (replace(nodes[0], label=hostile), *nodes[1:])
    user = {**ADMIN, "display_name": hostile}
    html, dom = _render(user, changed, intent_for, items=[{"title": "private catalogue title"}])
    assert not dom.find("img")
    assert hostile in dom.text
    assert "&lt;img" in html
    assert "private catalogue title" not in html
    assert "private catalogue title" not in seen
    # The one label is UI text; the identical display name is escaped by the
    # original account fragment, not sent to gettext a second time.
    assert seen.count(hostile) == 1


def test_gnu_catalogue_markup_is_escaped_not_trusted(db, nodes, intent_for, monkeypatch):
    import gettext
    from io import BytesIO
    import struct
    import app.i18n as i18n

    # A real serialized GNU catalogue exercises gettext, not a lambda that
    # silently returns Markup or bypasses server translation behavior.
    translations = {"": "Content-Type: text/plain; charset=UTF-8\n", "Browse": '<svg onload="alert(1)">GNU</svg>'}
    pairs = [(key.encode(), value.encode()) for key, value in sorted(translations.items())]
    count = len(pairs)
    offset = 28 + 16 * count
    originals = b"".join(key + b"\0" for key, _ in pairs)
    translated = b"".join(value + b"\0" for _, value in pairs)
    key_table = []
    value_table = []
    key_offset = offset
    value_offset = offset + len(originals)
    for key, value in pairs:
        key_table.extend((len(key), key_offset))
        value_table.extend((len(value), value_offset))
        key_offset += len(key) + 1
        value_offset += len(value) + 1
    data = struct.pack("<7I", 0x950412DE, 0, count, 28, 28 + 8 * count, 0, 0)
    data += struct.pack(f"<{4 * count}I", *key_table, *value_table) + originals + translated
    catalogue = gettext.GNUTranslations(BytesIO(data))
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: catalogue)
    html, dom = _render(None, nodes, intent_for)
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert primary.text.strip() == '<svg onload="alert(1)">GNU</svg>'
    assert not primary.find("svg")
    assert "&lt;svg" in html


NODE_LOGIC = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
let desktop = true;
const pending = [];
class Element {
    constructor(tag, attrs, parent) {
        this.tagName = tag.toUpperCase(); this.attrs = attrs; this.parentElement = parent;
        this.children = []; this.listeners = {}; this._open = Object.hasOwn(attrs, 'open');
    }
    matches(selector) {
        const match = /^([a-z]+)?(?:\[([^=\]]+)(?:="([^"]*)")?\])?$/.exec(selector);
        assert.ok(match, 'DOM double must explicitly support selector: ' + selector);
        return (!match[1] || this.tagName === match[1].toUpperCase()) &&
            (!match[2] || (Object.hasOwn(this.attrs, match[2]) &&
             (match[3] === undefined || this.attrs[match[2]] === match[3])));
    }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) {
        if (selector === ':scope > summary') return this.children.find(child => child.tagName === 'SUMMARY') || null;
        return this.querySelectorAll(selector)[0] || null;
    }
    closest(selector) { return this.matches(selector) ? this : (this.parentElement?.closest(selector) || null); }
    contains(other) { return !!other && (other === this || this.children.some(child => child.contains(other))); }
    getAttribute(key) { return this.attrs[key] ?? null; }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    get open() { return this._open; }
    set open(value) {
        if (this._open !== !!value) {
            this._open = !!value;
            pending.push(() => emit(this, 'toggle', {bubbles: false}));
        }
    }
    focus() { document.activeElement = this; }
}
function build(node, parent) {
    const element = new Element(node.tag, node.attrs, parent);
    element.children = node.children.map(child => build(child, element));
    return element;
}
const document = build(data, null);
document.readyState = 'complete';
document.activeElement = null;
const queries = [];
const window = {matchMedia(query) {
    const media = {
        get matches() { return query.includes('hover') ? desktop : desktop; },
        addEventListener(type, listener) { queries.push(listener); }
    };
    return media;
}};
function emit(target, type, properties = {}) {
    const event = {target, type, bubbles: true, defaultPrevented: false, stopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.stopped = true; }, ...properties};
    for (let element = target; element; element = element.parentElement) {
        for (const listener of element.listeners[type] || []) listener(event);
        if (!event.bubbles || event.stopped) break;
    }
    return event;
}
function flush() { while (pending.length) pending.shift()(); }
function summary(details) { return details.querySelector(':scope > summary'); }
function activate(details, key) {
    const trigger = summary(details);
    trigger.focus();
    const event = emit(trigger, key ? 'keydown' : 'click', key ? {key} : {});
    assert.equal(event.defaultPrevented, false, 'native Enter/Space/click must not be hijacked');
    details.open = !details.open; // the native default, deliberately not browser proof
    flush();
}
vm.runInNewContext(fs.readFileSync('static/js/media-navigation.js', 'utf8'), {document, window});
flush();
const root = document.querySelector('[data-media-navigation]');
const shell = root.querySelector('details[data-nav-shell]');
const browse = root.querySelector('details[data-nav-browse]');
const family = root.querySelector('[data-nav-family="books"]');
const familyDetails = family.querySelector('details[data-nav-disclosure]');
const format = family.querySelector('[data-nav-format="book"]');
const primary = root.querySelector('[data-nav-primary="browse"]');
const add = root.querySelector('details[data-nav-group="add"]');
assert.ok(shell.open, 'desktop shell is persistent');
assert.equal(browse.open, false);
const href = primary.getAttribute('href');
assert.equal(emit(primary, 'click').defaultPrevented, false);
assert.equal(primary.getAttribute('href'), href, 'Browse remains a real unmodified anchor');
emit(browse.parentElement, 'pointerenter', {pointerType: 'touch', bubbles: false}); flush();
assert.equal(browse.open, false, 'touch must not synthesize hover');
emit(browse.parentElement, 'pointerenter', {pointerType: 'mouse', bubbles: false}); flush();
assert.ok(browse.open, 'desktop pointer hover opens Browse');
emit(family, 'pointerenter', {pointerType: 'mouse', bubbles: false}); flush();
assert.ok(familyDetails.open, 'desktop family hover opens nested formats');
format.focus();
emit(family, 'pointerleave', {pointerType: 'mouse', bubbles: false}); flush();
assert.ok(familyDetails.open, 'pointer leave cannot hide keyboard focus');
const escape = emit(format, 'keydown', {key: 'Escape'}); flush();
assert.ok(escape.defaultPrevented);
assert.equal(familyDetails.open, false);
assert.equal(document.activeElement, summary(familyDetails), 'Escape returns to that disclosure');
assert.ok(browse.open, 'Escape closes nearest level, not the whole tree');
emit(summary(familyDetails), 'keydown', {key: 'Escape'}); flush();
assert.equal(browse.open, false);
assert.equal(document.activeElement, summary(browse));
activate(browse, 'Enter'); assert.ok(browse.open);
activate(browse, ' '); assert.equal(browse.open, false);
activate(browse); assert.ok(browse.open);
const tab = emit(summary(browse), 'keydown', {key: 'Tab'});
assert.equal(tab.defaultPrevented, false, 'Tab is native; there is no focus trap');
emit(document, 'pointerdown'); flush();
assert.equal(browse.open, false, 'outside pointer closes');
assert.ok(shell.open, 'outside pointer cannot hide the desktop navigation');
activate(browse);
emit(root, 'focusout', {relatedTarget: document}); flush();
assert.equal(browse.open, false, 'Tab out closes without stealing destination focus');
assert.equal(document.activeElement, summary(browse));
desktop = false;
for (const change of queries) change(); flush();
assert.equal(shell.open, false, 'mobile starts collapsed after enhancement');
activate(shell, 'Enter'); assert.ok(shell.open); assert.ok(browse.open);
activate(familyDetails, ' '); assert.ok(familyDetails.open);
activate(add); assert.ok(add.open); assert.equal(browse.open, false, 'mobile accordion closes peer branches');
assert.equal(familyDetails.open, false, 'closed branches also close descendants');
emit(summary(add), 'keydown', {key: 'Escape'}); flush();
assert.equal(add.open, false); assert.equal(document.activeElement, summary(add));
emit(summary(add), 'keydown', {key: 'Escape'}); flush();
assert.equal(shell.open, false); assert.equal(document.activeElement, summary(shell));
console.log('Native navigation DOM-double logic passed; not browser/visual evidence.');
"""


def test_native_script_pointer_keyboard_and_focus_logic_in_node_dom_double(db):
    import subprocess
    from app import media_presentation as presentation

    _, dom = _render(ADMIN, presentation.family_nodes(), presentation.filter_intent)

    def serialize(element):
        return {"tag": element.tag, "attrs": element.attrs, "children": [serialize(child) for child in element.children]}

    result = subprocess.run(["node", "-e", NODE_LOGIC], input=json.dumps(serialize(dom)), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not browser/visual evidence" in result.stdout


def test_mobile_css_uses_inflow_tree_and_shared_shell_not_sideways_flyouts():
    css = Path("static/css/media-navigation.source.css").read_text()
    # This is a source contract, not a layout assertion or browser proof.
    mobile, desktop = css.split("@media (min-width: 64rem)", 1)
    assert "position: static" in mobile
    assert "position: absolute" not in mobile
    assert "position: absolute" in desktop
    assert "inset-inline-start: 100%" in desktop
    assert "--app-gutter:" not in css  # the unchanged shared shell still owns its 80px cap


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
def test_real_registry_nodes_and_runtime_resolver_produce_exact_rendered_hrefs(db, user):
    from app import media_presentation as presentation

    nodes = presentation.family_nodes()
    tabs = nav.visible_tabs(user)
    model = nav.media_navigation(tabs, nodes)  # exercise the real lazy import
    _, dom = _render(user, nodes, presentation.filter_intent, model=model)
    visible = {tab["key"] for tab in tabs}
    formats = dom.find("a")
    formats = [anchor for anchor in formats if "data-nav-format" in anchor.attrs]
    assert {anchor.attrs["data-nav-format"] for anchor in formats} == {
        fmt.media_type for family in nodes if not family.origin_tab or family.origin_tab in visible
        for fmt in family.formats
    }
    for family in nodes:
        if family.origin_tab and family.origin_tab not in visible:
            assert not dom.find(**{"data-nav-family": family.key})
            continue
        branch, = dom.find(**{"data-nav-family": family.key})
        expected = presentation.filter_intent([family.selector])
        assert _family(model, family.key)["media_types"] == expected.media_types
        family_links = branch.find("a", **{"data-nav-family-link": family.key})
        if "library" in visible:
            anchor, = family_links
            assert parse_qs(urlsplit(anchor.attrs["href"]).query) == {
                "reset": ["1"], "view": ["items"], "sort": ["recent"],
                "types": [",".join(expected.media_types)],
            }
        else:
            assert bool(family_links) == (len(expected.media_types) == 1)
        for fmt in family.formats:
            anchor, = branch.find("a", **{"data-nav-format": fmt.media_type})
            query = parse_qs(urlsplit(anchor.attrs["href"]).query)
            assert query == ({
                "reset": ["1"], "view": ["items"], "sort": ["recent"], "types": [fmt.media_type],
            } if "library" in visible else {"sort": ["newest"], "media_type_filter": [fmt.media_type]})
    # All mixed entry points remain unfiltered, rather than enumerating the
    # registry's fourteen types and accidentally excluding unknown legacy data.
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert "types" not in parse_qs(urlsplit(primary.attrs["href"]).query)


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
@pytest.mark.parametrize("cause", ["feature", "manual"])
def test_real_registry_optional_branches_and_descendants_never_bypass_visibility(db, user, cause):
    from app import media_presentation as presentation

    if cause == "feature":
        for key in ("music", "periodicals", "connected_library"):
            _feature(db, key, False)
    else:
        _setting(db, "nav_hidden_tabs", json.dumps(["music", "periodicals", "library"]))
    nodes = presentation.family_nodes()
    model = nav.media_navigation(nav.visible_tabs(user), nodes)
    _, dom = _render(user, nodes, presentation.filter_intent, model=model)
    paths = {urlsplit(anchor.attrs["href"]).path for anchor in dom.find("a")}
    assert not {"/music", "/periodicals", "/library"} & paths
    hidden_types = {fmt.media_type for node in nodes if node.origin_tab in {"music", "periodicals"} for fmt in node.formats}
    rendered_types = {anchor.attrs["data-nav-format"] for anchor in dom.find("a") if "data-nav-format" in anchor.attrs}
    assert not hidden_types & rendered_types
    assert {"book", "comic", "dvd", "tv_series", "video_game"} <= rendered_types
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/browse?sort=newest"


def test_real_registry_expansion_is_still_pure_and_preserves_caches(db, monkeypatch):
    from app import database, media_presentation as presentation

    tabs = nav.visible_tabs(ADMIN)
    nodes = presentation.family_nodes()
    cached_settings = nav._cached_settings

    def forbidden(*args, **kwargs):
        raise AssertionError("derived navigation must not query or redo authorization")

    monkeypatch.setattr(database, "get_db", forbidden)
    monkeypatch.setattr(nav, "_nav_settings", forbidden)
    monkeypatch.setattr(nav, "visible_tabs", forbidden)
    monkeypatch.setattr(nav, "feature_enabled", forbidden)
    model = nav.media_navigation(tabs, nodes)
    assert nav._cached_settings is cached_settings
    assert len(model["families"]) == len(nodes)


def test_real_registry_with_every_hideable_tab_hidden_keeps_only_mandatory_destinations(db):
    from app import media_presentation as presentation

    _setting(db, "nav_hidden_tabs", json.dumps([*nav.HIDEABLE_KEYS, *nav.ALWAYS_VISIBLE]))
    _, dom = _render(ADMIN, presentation.family_nodes(), presentation.filter_intent)
    keyed = [anchor for anchor in dom.find("a") if "data-nav-tab" in anchor.attrs]
    assert {anchor.attrs["data-nav-tab"] for anchor in keyed} == {"browse", "settings"}
    assert all(urlsplit(anchor.attrs["href"]).path not in {"/music", "/library", "/periodicals"} for anchor in dom.find("a"))


def test_real_registry_strict_errors_are_not_caught_as_a_mixed_destination(db):
    from app import media_presentation as presentation
    from dataclasses import replace

    nodes = presentation.family_nodes()
    invalid = (replace(nodes[0], selector="family:book"), *nodes[1:])
    with pytest.raises(ValueError, match="Unknown media selector"):
        nav.media_navigation(nav.visible_tabs(ADMIN), invalid)


def test_preexpanded_immutable_nodes_do_not_require_a_registry_import(db, nodes, intent_for, monkeypatch):
    import builtins

    @dataclass(frozen=True)
    class ExpandedFormat(FixtureFormat):
        intent: FixtureIntent

    @dataclass(frozen=True)
    class ExpandedFamily(FixtureFamily):
        intent: FixtureIntent

    expanded = tuple(ExpandedFamily(
        node.key, node.selector, node.label, node.origin_tab,
        tuple(ExpandedFormat(fmt.media_type, fmt.selector, fmt.label, intent_for((fmt.selector,))) for fmt in node.formats),
        intent_for((node.selector,)),
    ) for node in nodes)
    tabs = nav.visible_tabs(ADMIN)
    original = builtins.__import__

    def no_registry(name, *args, **kwargs):
        if name == "app.media_presentation":
            raise AssertionError("expanded input should not import the registry")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_registry)
    model = nav.media_navigation(tabs, expanded)
    assert _family(model, "books")["media_types"] == ("book", "ebook")


@pytest.mark.parametrize("expansion", [("ebook",), ("book", "ebook")])
def test_format_expansion_must_match_the_canonical_format_not_another_filter(db, nodes, intent_for, expansion):
    def mismatched(selectors):
        return FixtureIntent(expansion) if selectors == ("format:book",) else intent_for(selectors)

    with pytest.raises(ValueError, match="its one media type"):
        _model(ADMIN, nodes, mismatched)
