"""Execute the actual app.js shortcut registration against real rendered pages.

Reuses the navigation tests' Node DOM/event double, not a browser. Captured b
destinations are then followed through authenticated, registered FastAPI routes.
Browser key dispatch, CSP and page/HTMX lifecycle acceptance remain separate.
"""

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import config
from tests.conftest import _insert_item
from tests.test_features_gate import off
from tests.test_media_navigation import FragmentDOM, NODE_DOM_DOUBLE
from tests.test_nav import _set


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static/js/app.js"
NODE = "/usr/bin/node"


KEYBOARD = NODE_DOM_DOUBLE + r"""
document.getElementById = id => document.querySelector('[id="' + id + '"]');
const modal = document.getElementById('shortcut-modal');
assert.ok(modal, 'real base template supplies the shortcut modal');
const classes = new Set(modal.getAttribute('class').split(/\s+/));
modal.classList = {
    contains(name) { return classes.has(name); },
    add(name) { classes.add(name); },
    toggle(name) { if (classes.has(name)) classes.delete(name); else classes.add(name); }
};
const navigations = [];
window.location = {set href(value) { navigations.push(value); }};
function forbidden() { throw new Error('Shortcut must not access storage, fetch, history or status'); }
const storage = {getItem: forbidden, setItem: forbidden, removeItem: forbidden};
const history = {pushState: forbidden, replaceState: forbidden};
Object.assign(window, {localStorage: storage, sessionStorage: storage, fetch: forbidden, history});
Object.defineProperty(window, 'status', {set: forbidden});

// Execute the exact production registration, including Escape and input guards.
// Only unrelated app.js sections are excluded; no handler logic is rewritten.
const source = fs.readFileSync(process.argv[1], 'utf8');
const begin = source.indexOf('// --- Keyboard shortcuts ---\n');
const end = source.indexOf('\n// The visible shortcut-help controls', begin);
assert.ok(begin >= 0 && end > begin, 'locate the complete production keyboard registration');
vm.runInNewContext(source.slice(begin, end), {
    document, window, localStorage: storage, sessionStorage: storage, history, fetch: forbidden
}, {filename: process.argv[1]});
const registered = (document.listeners.keydown || []).length;
assert.equal(registered, 1, 'the actual source must register exactly one keydown handler');
const results = data.events.map(action => {
    const target = document.querySelector(action.focus || 'body');
    assert.ok(target, 'focus control must exist in the real rendered page: ' + action.focus);
    target.focus();
    const before = navigations.length;
    const event = emit(target, action.type || 'keydown', {key: action.key});
    return {
        key: event.key, type: event.type, prevented: event.defaultPrevented,
        activeTag: document.activeElement.tagName,
        hidden: modal.classList.contains('hidden'), navigations: navigations.slice(before)
    };
});
process.stdout.write(JSON.stringify({
    registered, results, proof: 'Actual app.js registration in a DOM/event double; not browser evidence.'
}));
"""


def _keyboard(dom, events):
    def serialize(element):
        return {"tag": element.tag, "attrs": element.attrs,
                "children": [serialize(child) for child in element.children]}

    payload = {**serialize(dom), "events": events}
    result = subprocess.run(
        [NODE, "-e", KEYBOARD, str(APP_JS)], cwd=ROOT, input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state["registered"] == 1
    assert "not browser evidence" in state["proof"]
    print("KEYBOARD_DOM_DOUBLE=" + json.dumps(state, sort_keys=True))
    return state["results"]


def _page(client, path="/browse"):
    response = client.get(path)
    assert response.status_code == 200
    dom = FragmentDOM(response.text).root
    script, = dom.find("script", src="/static/js/app.js")
    served = client.get(script.attrs["src"])
    assert served.status_code == 200 and served.text == APP_JS.read_text()
    return response, dom


def _mixed(db):
    # Raw fixture seeding deliberately includes an unknown *legacy* format.
    live = [
        _insert_item(db, title=f"Shortcut {media_type}", isbn=None, media_type=media_type,
                     created_at=f"2026-01-01 00:{index:02d}:00")
        for index, media_type in enumerate((*config.MEDIA_TYPES, "legacy_shortcut_format"))
    ]
    trashed = _insert_item(db, title="Shortcut trashed", isbn=None,
                           deleted_at="2026-01-02 00:00:00")
    db.commit()
    return live, trashed


@pytest.mark.parametrize("client_name", ["admin_client", "editor_client", "viewer_client"])
@pytest.mark.parametrize("library", ["visible", "hidden", "disabled"])
def test_b_uses_rendered_primary_and_reaches_the_live_mixed_catalogue(db, request, client_name, library):
    live, trashed = _mixed(db)
    client = request.getfixturevalue(client_name)
    if library == "hidden":
        _set(db, "nav_hidden_tabs", json.dumps(["library"]))
    elif library == "disabled":
        off("connected_library")
    page, dom = _page(client)
    assert page.context["user"]["role"] == client_name.removesuffix("_client")
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    href = primary.attrs["href"]
    expected_href = ("/library?reset=1&view=items&sort=recent" if library == "visible"
                     else "/browse?sort=newest")
    assert href == expected_href
    query = parse_qs(urlsplit(href).query)
    assert query == ({"reset": ["1"], "view": ["items"], "sort": ["recent"]}
                     if library == "visible" else {"sort": ["newest"]})
    manager, = dom.find("a", **{"data-nav-management": "catalogue"})
    assert manager.attrs["href"] == "/browse" and manager.attrs["href"] != href

    event, = _keyboard(dom, [{"key": "b"}])
    assert event["navigations"] == [href], "b must follow primary Browse, not the management shortcut"
    assert event["type"] == "keydown" and event["prevented"] is False
    destination = client.get(event["navigations"][0])
    assert destination.status_code == 200
    rows = FragmentDOM(destination.text).root
    attribute = "data-library-member" if library == "visible" else "data-item-id"
    ids = list(dict.fromkeys(int(node.attrs[attribute]) for node in rows.walk() if attribute in node.attrs))
    assert ids == list(reversed(live))
    assert trashed not in ids
    print(f"FOLLOWED_SHORTCUT role={page.context['user']['role']} library={library} "
          f"href={href} status={destination.status_code} live={len(ids)} trash_excluded={trashed not in ids}")


@pytest.mark.parametrize("control", [
    'input[id="personal-progress"]', 'textarea[id="personal-notes"]', 'select[id="personal-status"]',
])
def test_typing_controls_suppress_shortcuts_with_a_positive_body_control(viewer_client, db, control):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    _, dom = _page(viewer_client, f"/item/{item_id}")
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    results = _keyboard(dom, [{"key": key, "focus": control} for key in ("b", "s", "/", "?")]
                        + [{"key": "b"}])
    for event in results[:-1]:
        assert event["navigations"] == [] and event["prevented"] is False
        assert event["activeTag"] == control.split("[", 1)[0].upper()
        assert event["hidden"] is True
    assert results[-1]["navigations"] == [primary.attrs["href"]]


def test_other_keys_and_escape_before_the_input_guard_keep_their_behavior(viewer_client):
    _, dom = _page(viewer_client)
    results = _keyboard(dom, [
        {"key": "z"}, {"key": "B"}, {"key": "b", "type": "keyup"},
        {"key": "/"}, {"key": "s"}, {"key": "?"},
        {"key": "Escape", "focus": 'input[name="q"]'}, {"key": "Escape"},
    ])
    for event in results[:3]:
        assert event["navigations"] == [] and event["prevented"] is False and event["hidden"] is True
    search, scan, show, close, already_closed = results[3:]
    assert search["prevented"] is True and search["activeTag"] == "INPUT" and search["navigations"] == []
    assert scan["navigations"] == ["/scan"] and scan["prevented"] is False
    assert show["hidden"] is False and show["navigations"] == [] and show["prevented"] is False
    assert close["activeTag"] == "INPUT" and close["hidden"] is True
    assert close["navigations"] == [] and close["prevented"] is False
    assert already_closed["hidden"] is True and already_closed["navigations"] == []
    assert already_closed["prevented"] is False


def test_explicit_empty_navigation_retains_the_legacy_core_fallback(viewer_client):
    from app.main import templates

    page, _ = _page(viewer_client)
    # Real base + wrapper, with the intentionally empty authorized nav context.
    rendered = templates.TemplateResponse(page.context["request"], "base.html", {"nav_tabs": []})
    assert rendered.context["nav_tabs"] == [] and rendered.context["media_navigation"]["primary"] is None
    dom = FragmentDOM(rendered.body.decode()).root
    assert not dom.find("a", **{"data-nav-primary": "browse"})
    event, = _keyboard(dom, [{"key": "b"}])
    assert event["navigations"] == ["/browse"] and event["prevented"] is False
