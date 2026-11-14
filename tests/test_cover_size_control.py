"""Additive component contracts; no shared-page wiring or account-state claim.

Jinja/GNU tests exercise the real partial. The small Node document double executes
the shipped script to pin validation and handler ownership; it is not a browser
layout, native input, or HTMX integration substitute (see the browser shell tests).
"""

from gettext import GNUTranslations
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess

from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARTIAL = "fragments/cover_size_control.html"
SCRIPT = ROOT / "static/js/cover-size.js"
CSS = ROOT / "static/css/cover-size.source.css"


class _Markup(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack, self.text = [], [], ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parents": tuple(self.stack)}
        self.nodes.append(node)
        if tag not in {"input", "br", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        assert self.stack[-1]["tag"] == tag
        self.stack.pop()

    def handle_data(self, text):
        self.text += text

    def tagged(self, tag):
        return [node for node in self.nodes if node["tag"] == tag]


def _render(messages=None, *, copies=1, newstyle=True):
    catalog = Catalog(locale="it")
    for source, translated in (messages or {}).items():
        catalog.add(source, translated)
    data = BytesIO()
    write_mo(data, catalog)
    data.seek(0)
    translations = GNUTranslations(data)
    calls = []

    def gettext(message):
        calls.append(message)
        return translations.gettext(message)

    env = Environment(
        loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True,
        undefined=StrictUndefined, extensions=["jinja2.ext.i18n"],
    )
    env.install_gettext_callables(gettext, translations.ngettext, newstyle=newstyle)
    html = env.from_string(
        '{% for unused in range(copies) %}{% include "' + PARTIAL + '" %}{% endfor %}'
    ).render(copies=copies)
    return _Markup(html), calls


def test_native_markup_is_labelled_bounded_and_not_a_form_preference():
    markup, _ = _render()
    control = [node for node in markup.nodes if "data-cover-size-control" in node["attrs"]]
    assert len(control) == 1
    (input_node,) = markup.tagged("input")
    assert input_node["attrs"] == {
        "type": "range", "min": "50", "max": "100", "step": "10", "value": "100",
        "disabled": None, "data-cover-size-input": None,
    }
    assert input_node["parents"][-1]["tag"] == "label", "Implicit native association before enhancement"
    assert "Cover size" in markup.text and "100%" in markup.text
    assert "This page only; not saved." in markup.text
    assert "Enable JavaScript to change cover size." in markup.text
    assert markup.tagged("noscript")
    assert [node["attrs"]["data-cover-size-action"] for node in markup.tagged("button")] == [
        "smaller", "larger", "reset",
    ]
    assert all(node["attrs"]["type"] == "button" and "disabled" in node["attrs"]
               for node in markup.tagged("button"))
    assert not any(markup.tagged(tag) for tag in ("form", "script", "style", "a"))
    for node in markup.nodes:
        assert not {"name", "form", "style"} & node["attrs"].keys()
        assert not any(name.startswith(("on", "x-", "hx-", "@", ":")) for name in node["attrs"])


def test_repeated_unenhanced_partials_have_independent_native_labels_without_duplicate_ids():
    markup, _ = _render(copies=3)
    assert len(markup.tagged("label")) == len(markup.tagged("input")) == 3
    assert len({id(node["parents"][-1]) for node in markup.tagged("input")}) == 3
    assert not any("id" in node["attrs"] for node in markup.nodes)


@pytest.mark.parametrize("newstyle", [False, True], ids=["raw-gnu", "newstyle-gnu"])
def test_all_owned_labels_escape_real_gnu_translations_once(newstyle):
    labels = ("Cover size", "Smaller", "Larger", "Reset", "This page only; not saved.",
              "Enable JavaScript to change cover size.")
    percent = "100%%" if newstyle else "100%d"
    messages = {label: f'<img src=x onerror="bad()"> {label} {percent}' for label in labels}
    markup, calls = _render(messages, newstyle=newstyle)
    assert calls == list(labels)
    assert not markup.tagged("img")
    for label in labels:
        assert f'<img src=x onerror="bad()"> {label} {"100%" if newstyle else "100%d"}' in markup.text


def test_css_only_scales_card_width_once_with_default_and_text_floor():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    card_rule = re.search(r"\.media-card\s*\{([^}]+)\}", css)[1]
    compact = re.sub(r"\s+", "", card_rule)
    assert compact == "width:max(80px,calc(100%*var(--media-cover-scale,1)));max-width:100%;"
    assert css.count("--media-cover-scale") == 1
    assert not re.search(r"transform|zoom\s*:|grid-template|column-count|aspect-ratio|object-fit|transition|animation", css)
    assert not re.search(r"\.media-artwork", css)
    shelf_rule = re.search(r"\.media-shelf__slot\s*\{([^}]+)\}", css)[1]
    assert re.sub(r"\s+", "", shelf_rule) == "position:relative;", "Contain sr-only text, never scale slots"
    assert css.count(".media-shelf") == 1
    assert "scroll-behavior" not in css
    assert "font-size" not in card_rule and "overflow" not in card_rule


def test_script_has_no_preference_io_or_custom_keyboard_interpreter():
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", SCRIPT.read_text(), flags=re.S)
    assert not re.search(
        r"localStorage|sessionStorage|cookie|\bfetch\s*\(|XMLHttpRequest|sendBeacon|indexedDB|"
        r"\blocation\b|\bhistory\b|URLSearchParams|WebSocket|\.key(?:Code)?\b|keydown|keyup", source,
    )
    assert "new WeakSet()" in source
    assert "Number.isFinite" in source
    assert "htmx:afterSwap" in source and "htmx:oobAfterSwap" in source
    assert "htmx:afterSettle" not in source


# Only the minimal DOM operations used by this component are modelled. Browser
# range sanitisation/keyboard/layout are intentionally tested in Playwright, not
# imitated here. Node's sandbox exposes no storage, request or filesystem APIs.
_DOCUMENT_DOUBLE = r"""
const vm = require('node:vm');
let doc;
class Element {
    constructor(tag, attrs = {}, children = []) {
        this.tagName = tag.toUpperCase(); this.attrs = {...attrs};
        this.children = children; this.parentElement = null;
        children.forEach(child => child.parentElement = this);
        this.disabled = 'disabled' in attrs; this.value = attrs.value || '';
        this.valueAsNumber = Number(this.value); this.htmlFor = '';
    }
    get id() { return this.attrs.id || ''; }
    set id(value) { this.attrs.id = value; }
    get ownerDocument() { return doc; }
    get isConnected() { return doc.roots.some(root => root.contains(this)); }
    getAttribute(name) { return this.attrs[name] ?? null; }
    setAttribute(name, value) { this.attrs[name] = value; }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    matches(selector) {
        if (selector.startsWith('.')) return (this.attrs.class || '').split(' ').includes(selector.slice(1));
        const tag = selector.match(/^[a-z]+/);
        if (tag && tag[0].toUpperCase() !== this.tagName) return false;
        return [...selector.matchAll(/\[([^=\]]+)(?:="([^"]+)")?\]/g)].every(
            ([, key, value]) => key in this.attrs && (value === undefined || this.attrs[key] === value));
    }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function control() {
    return new Element('div', {'data-cover-size-control': ''}, [
        new Element('label', {}, [new Element('input', {
            type: 'range', 'data-cover-size-input': '', disabled: '', value: '100',
        })]),
        new Element('output', {'data-cover-size-output': ''}),
        ...['smaller', 'larger', 'reset'].map(action => new Element('button', {
            type: 'button', 'data-cover-size-action': action, disabled: '',
        })),
        new Element('p', {class: 'cover-size-control__note'}),
    ]);
}
const listeners = {};
const properties = {};
let writes = 0;
doc = {
    roots: [control(), control()], readyState: 'complete',
    documentElement: {style: {setProperty(key, value) { properties[key] = value; writes++; }}},
    addEventListener(name, callback, options = {}) {
        (listeners[name] ||= []).push({callback, once: options.once});
    },
    querySelectorAll(selector) {
        return this.roots.flatMap(root => [...(root.matches(selector) ? [root] : []), ...root.querySelectorAll(selector)]);
    },
    getElementById(id) {
        return this.roots.flatMap(root => [root, ...root.querySelectorAll('[id]')]).find(node => node.id === id);
    },
};
function emit(name, target, defaultPrevented = false) {
    const event = {target, defaultPrevented, preventDefault() { this.defaultPrevented = true; }};
    for (const entry of [...(listeners[name] || [])]) {
        entry.callback(event);
        if (entry.once) listeners[name] = listeners[name].filter(candidate => candidate !== entry);
    }
    return event;
}
const input = root => root.querySelector('[data-cover-size-input]');
const action = (root, name) => root.querySelector('[data-cover-size-action="' + name + '"]');
const scale = () => properties['--media-cover-scale'];
const values = () => doc.roots.map(root => input(root)?.value);
function forbidden() { throw Error('Forbidden component side effect'); }
Object.defineProperty(doc, 'cookie', {get: forbidden, set: forbidden});
const sandbox = {document: doc, Element, fetch: forbidden, XMLHttpRequest: forbidden};
for (const name of ['localStorage', 'sessionStorage', 'indexedDB', 'location', 'history']) {
    Object.defineProperty(sandbox, name, {get: forbidden});
}
const context = vm.createContext(sandbox);
"""


def _run_script(scenario, *, before=""):
    program = (
        _DOCUMENT_DOUBLE + "\nconst source = " + json.dumps(SCRIPT.read_text()) + ";\n" + before
        + "\nconst run = () => vm.runInContext(source, context);\nrun();\n" + scenario
    )
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_script_initialises_unique_associations_and_resets_untrusted_initial_values():
    result = _run_script("""
        const ids = doc.roots.map(root => input(root).id);
        console.log(JSON.stringify({scale: scale(), values: values(), ids,
            associations: doc.roots.map(root => ({
                label: root.querySelector('label').htmlFor,
                output: root.querySelector('output').htmlFor,
                note: input(root).getAttribute('aria-describedby'),
                enabled: !input(root).disabled,
            }))}));
    """, before="properties['--media-cover-scale'] = 'Infinity'; input(doc.roots[0]).value = '60';")
    assert result["scale"] == "1" and result["values"] == ["100", "100"]
    assert len(set(result["ids"])) == 2
    for identity, association in zip(result["ids"], result["associations"], strict=True):
        assert association == {"label": identity, "output": identity, "note": identity + "-note", "enabled": True}


@pytest.mark.parametrize("value, expected", [
    ("NaN", 100), ("Infinity", 100), ("-Infinity", 100), ("'60'", 100),
    ("-1e200", 50), ("1e200", 100), ("49", 50), ("55", 60), ("63", 60), ("88", 90), ("100", 100),
])
def test_script_sanitises_every_value_before_css_write(value, expected):
    result = _run_script("""
        input(doc.roots[0]).valueAsNumber = VALUE;
        emit('input', input(doc.roots[0]));
        console.log(JSON.stringify({scale: scale(), values: values(),
            outputs: doc.roots.map(root => root.querySelector('output').value)}));
    """.replace("VALUE", value))
    assert result == {"scale": str(expected / 100).removesuffix(".0"), "values": [str(expected)] * 2,
                      "outputs": [f"{expected}%"] * 2}


def test_script_buttons_are_bounded_coherent_and_idempotent_when_reexecuted():
    result = _run_script("""
        run(); run();
        const first = doc.roots[0];
        const steps = [];
        for (let n = 0; n < 7; n++) { emit('click', action(first, 'smaller')); steps.push(scale()); }
        const minimumDisabled = action(first, 'smaller').disabled;
        emit('click', action(doc.roots[1], 'larger'));
        const larger = values();
        emit('click', action(first, 'reset'));
        console.log(JSON.stringify({steps, minimumDisabled, larger, reset: values(),
            counts: Object.fromEntries(Object.entries(listeners).map(([key, entries]) => [key, entries.length]))}));
    """)
    assert result["steps"] == ["0.9", "0.8", "0.7", "0.6", "0.5", "0.5", "0.5"]
    assert result["minimumDisabled"] and result["larger"] == ["60", "60"]
    assert result["reset"] == ["100", "100"]
    assert result["counts"] == {"input": 1, "change": 1, "click": 1, "htmx:afterSwap": 1, "htmx:oobAfterSwap": 1}


def test_script_replacements_inherit_state_and_detached_or_unowned_events_do_nothing():
    result = _run_script("""
        const old = doc.roots[0];
        for (let n = 0; n < 3; n++) emit('click', action(old, 'smaller'));
        doc.roots[0] = control();
        for (const name of ['htmx:afterSwap', 'htmx:oobAfterSwap', 'htmx:afterSwap']) emit(name, old);
        input(old).valueAsNumber = 50;
        emit('input', input(old)); emit('click', action(old, 'smaller'));
        const fresh = doc.roots[0];
        input(fresh).valueAsNumber = 50;
        emit('input', input(fresh), true); emit('click', action(fresh, 'smaller'), true);
        const outside = new Element('input', {type: 'range', 'data-cover-size-input': '', value: '50'});
        doc.roots.push(outside); emit('input', outside); doc.roots.pop();
        console.log(JSON.stringify({scale: scale(), values: values(),
            ids: doc.roots.map(root => input(root).id), oldId: input(old).id}));
    """)
    assert result["scale"] == "0.7" and result["values"] == ["70", "70"]
    assert len(set(result["ids"] + [result["oldId"]])) == 3


def test_script_waits_for_dom_content_loaded_only_once():
    result = _run_script("""
        run();
        const beforeLoad = writes;
        emit('DOMContentLoaded', null); emit('DOMContentLoaded', null);
        console.log(JSON.stringify({beforeLoad, writes, values: values(), listeners: listeners.DOMContentLoaded.length}));
    """, before="doc.readyState = 'loading';")
    assert result == {"beforeLoad": 0, "writes": 1, "values": ["100", "100"], "listeners": 0}
