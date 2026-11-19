"""Authored shared-sizing regressions; no executed/qualified result is implied.

The Node bridge extends the existing finite DOM double with a storage ledger.
Its trusted-event flag is model input, NOT proof of native browser interaction.
Real keyboard, navigation, layout and storage are covered separately in e2e.
"""

import json
from pathlib import Path
import subprocess
from urllib.parse import quote

import pytest

from app.services.media_cards import build_media_card
from tests.test_connected_layout import _Markup
from tests.test_cover_size_control import _DOCUMENT_DOUBLE
from tests.test_media_cards import _environment, _HTML


SCRIPT = Path(__file__).resolve().parents[1] / "static/js/cover-size.js"
PERCENTAGES = (50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200)


def _key(account="17", mount=""):
    encoded_mount = quote(mount, safe="~()*!.'-")
    return f"shelf.coverSize.v2:{encoded_mount}:u:{account}"


_STORAGE_BRIDGE = r"""
const options = OPTIONS;
const entries = new Map(Object.entries(options.entries));
const ledger = [];
const windowListeners = {};
const shellAttrs = {'data-cover-size-account': options.account, 'data-cover-size-mount': options.mount};
const browserStorage = {
    getItem(key) {
        ledger.push(['get', key]);
        if (options.readFailure) throw Error('synthetic storage read refusal');
        return entries.has(key) ? entries.get(key) : null;
    },
    setItem(key, value) {
        ledger.push(['set', key, value]);
        if (options.writeFailure) throw Error('synthetic quota refusal');
        entries.set(key, value);
    },
};
doc.defaultView = {addEventListener(name, callback) { (windowListeners[name] ||= []).push(callback); }};
Object.defineProperty(doc.defaultView, 'localStorage', {get() {
    if (options.getterFailure) throw Error('synthetic storage getter refusal');
    return browserStorage;
}});
doc.documentElement.getAttribute = name => shellAttrs[name] ?? null;
doc.documentElement.setAttribute = (name, value) => { shellAttrs[name] = value; };
sandbox.Event = class Event { constructor(type) { this.type = type; } };
doc.dispatchEvent = event => {
    for (const entry of listeners[event.type] || []) entry.callback(event);
    return true;
};
function prepare(root) {
    root.children = root.children.filter(child => child.tagName !== 'BUTTON');
    root.attrs['data-cover-size-saved'] = 'Saved for this account in this browser.';
    root.attrs['data-cover-size-unavailable'] = 'Storage unavailable; this page only.';
    root.attrs['data-cover-size-restored'] = 'Refresh before editing a restored page.';
    const message = new Element('p', {'data-cover-size-status': ''});
    message.parentElement = root; root.children.push(message);
    input(root).value = '5'; input(root).valueAsNumber = 5;
    return root;
}
doc.roots.forEach(prepare);
// Explicit model events. Native browser provenance is NOT simulated evidence.
emit = function (name, target, defaultPrevented = false, extra = {}) {
    const event = {target, defaultPrevented, isTrusted: true, ...extra,
        preventDefault() { this.defaultPrevented = true; }};
    for (const entry of [...(listeners[name] || [])]) {
        entry.callback(event);
        if (entry.once) listeners[name] = listeners[name].filter(candidate => candidate !== entry);
    }
    return event;
};
function nativeChange(value, type = 'input', extra = {}) {
    input(doc.roots[0]).valueAsNumber = value;
    emit(type, input(doc.roots[0]), false, extra);
}
function windowEvent(name, extra) {
    for (const callback of windowListeners[name] || []) callback(extra);
}
function storageEvent(key, value, area = browserStorage) {
    windowEvent('storage', {key, newValue: value, storageArea: area});
}
function messages() { return doc.roots.map(root => root.querySelector('[data-cover-size-status]')?.textContent || ''); }
"""


def _run_script(scenario, *, before="", account="17", mount="", stored=None, entries=None,
                getter_failure=False, read_failure=False, write_failure=False):
    options = {"account": account, "mount": mount, "entries": dict(entries or {}),
               "getterFailure": getter_failure, "readFailure": read_failure,
               "writeFailure": write_failure}
    if stored is not None:
        options["entries"][_key(account, mount)] = stored
    bridge = _STORAGE_BRIDGE.replace("OPTIONS", json.dumps(options), 1)
    program = (_DOCUMENT_DOUBLE + "\n" + bridge + "\nconst source = " + json.dumps(SCRIPT.read_text())
               + ";\n" + before + "\nconst run = () => vm.runInContext(source, context);\nrun();\n" + scenario)
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("percentage", PERCENTAGES)
def test_every_saved_percentage_restores_the_centered_scale_without_initial_writes(percentage):
    result = _run_script("console.log(JSON.stringify({scale: scale(), values: values(), ledger}));",
                         stored=str(percentage))
    assert float(result["scale"]) == percentage / 100
    assert result["values"] == [str(PERCENTAGES.index(percentage))] * 2
    assert result["ledger"] == [["get", _key()]]


@pytest.mark.parametrize("raw", (None, "", "050", "100 ", "1e2", "101", "null", "9" * 4096))
def test_missing_malformed_or_unsupported_storage_defaults_without_repair_writes(raw):
    result = _run_script("console.log(JSON.stringify({scale: scale(), values: values(), ledger}));", stored=raw)
    assert result == {"scale": "1", "values": ["5", "5"], "ledger": [["get", _key()]]}


def test_one_native_change_updates_all_controls_and_writes_only_one_own_key():
    result = _run_script("""
        nativeChange(10); nativeChange(10, 'change'); run(); run();
        console.log(JSON.stringify({scale: scale(), values: values(), ledger, messages: messages(),
            listeners: Object.fromEntries(Object.entries(listeners).map(([k,v]) => [k,v.length])),
            windowListeners: Object.fromEntries(Object.entries(windowListeners).map(([k,v]) => [k,v.length]))}));
    """)
    assert result["scale"] == "2" and result["values"] == ["10", "10"]
    assert result["ledger"] == [["get", _key()], ["set", _key(), "200"]]
    assert result["messages"] == ["Saved for this account in this browser."] * 2
    assert result["listeners"] == {"input": 1, "change": 1, "htmx:afterSwap": 1, "htmx:oobAfterSwap": 1}
    assert result["windowListeners"] == {"storage": 1, "pageshow": 1}


def test_account_and_mount_partitions_are_nonvacuous_and_large_ids_never_round():
    account = "9007199254740993"
    other = "9007199254740992"
    a = _run_script("nativeChange(10); console.log(JSON.stringify({entries: Object.fromEntries(entries)}));",
                    account=account, mount="/shelf-a")
    b = _run_script("console.log(JSON.stringify({scale: scale(), ledger}));",
                    account=other, mount="/shelf-a", entries=a["entries"])
    mounted = _run_script("console.log(JSON.stringify({scale: scale(), ledger}));",
                          account=account, mount="/shelf-b", entries=a["entries"])
    again = _run_script("console.log(JSON.stringify({scale: scale(), ledger}));",
                        account=account, mount="/shelf-a", entries=a["entries"])
    assert a["entries"] == {_key(account, "/shelf-a"): "200"}
    assert b == {"scale": "1", "ledger": [["get", _key(other, "/shelf-a")]]}
    assert mounted == {"scale": "1", "ledger": [["get", _key(account, "/shelf-b")]]}
    assert again == {"scale": "2", "ledger": [["get", _key(account, "/shelf-a")]]}


def test_fragment_and_shell_edits_cannot_recapture_another_owner_or_adopt_old_scale():
    result = _run_script("""
        doc.documentElement.setAttribute('data-cover-size-account', '18');
        doc.documentElement.setAttribute('data-cover-size-mount', '/other');
        const old = doc.roots[0]; doc.roots[0] = prepare(control());
        emit('htmx:afterSwap', old); emit('htmx:oobAfterSwap', old);
        storageEvent('shelf.coverSize.v2:%2Fother:u:18', '200');
        nativeChange(7);
        input(old).valueAsNumber = 10; emit('input', input(old));
        console.log(JSON.stringify({scale: scale(), values: values(), ledger}));
    """, entries={"shelf.coverSize.v1::u:17": "200", _key("18", "/other"): "200"})
    assert result == {"scale": "1.4", "values": ["7", "7"],
                      "ledger": [["get", _key()], ["set", _key(), "140"]]}


def test_storage_events_sync_only_own_key_or_clear_without_echo_writes():
    result = _run_script("""
        storageEvent('other', '200'); storageEvent('shelf.coverSize.v2::u:17', '200', {});
        const ignored = scale();
        storageEvent('shelf.coverSize.v2::u:17', '160'); const changed = scale();
        storageEvent('shelf.coverSize.v2::u:17', 'invalid'); const invalid = scale();
        storageEvent('shelf.coverSize.v2::u:17', '200'); storageEvent(null, null);
        console.log(JSON.stringify({ignored, changed, invalid, cleared: scale(), ledger}));
    """)
    assert result == {"ignored": "1", "changed": "1.6", "invalid": "1", "cleared": "1",
                      "ledger": [["get", _key()]]}


@pytest.mark.parametrize("failure", ("getter_failure", "read_failure", "write_failure"))
def test_storage_refusals_leave_current_page_usable_without_a_fake_save_or_duplicate_retry(failure):
    failures = {failure: True, "write_failure": True}
    result = _run_script("""
        nativeChange(8); nativeChange(8, 'change');
        console.log(JSON.stringify({scale: scale(), enabled: !input(doc.roots[0]).disabled,
            ledger, messages: messages()}));
    """, **failures)
    assert result["scale"] == "1.6" and result["enabled"]
    assert result["messages"] == ["Storage unavailable; this page only."] * 2
    assert sum(row[0] == "set" for row in result["ledger"]) == (0 if failure == "getter_failure" else 1)


def test_a_real_user_change_can_save_after_an_initial_read_refusal():
    result = _run_script("""
        const initial = messages(); nativeChange(7);
        console.log(JSON.stringify({initial, messages: messages(), ledger, scale: scale()}));
    """, read_failure=True)
    assert result == {"initial": ["Storage unavailable; this page only."] * 2,
                      "messages": ["Saved for this account in this browser."] * 2,
                      "ledger": [["get", _key()], ["set", _key(), "140"]], "scale": "1.4"}


@pytest.mark.parametrize("account,mount", ((None, ""), ("0", ""), ("01", ""),
                                           ("9223372036854775808", ""), ("17", "//bad?key")))
def test_invalid_or_missing_shell_identity_never_touches_storage(account, mount):
    result = _run_script("nativeChange(0); console.log(JSON.stringify({scale: scale(), ledger}));",
                         account=account, mount=mount)
    assert result == {"scale": "0.5", "ledger": []}


def test_untrusted_events_and_bfcache_restoration_cannot_write_saved_state():
    result = _run_script("""
        nativeChange(10, 'input', {isTrusted: false}); const synthetic = scale();
        windowEvent('pageshow', {persisted: true});
        doc.roots[0] = prepare(control()); emit('htmx:afterSwap', doc.roots[0]); run();
        windowEvent('pageshow', {persisted: false}); nativeChange(10);
        storageEvent('shelf.coverSize.v2::u:17', '200');
        console.log(JSON.stringify({synthetic, scale: scale(), ledger, messages: messages(),
            disabled: doc.roots.map(root => input(root).disabled)}));
    """)
    assert result == {"synthetic": "1", "scale": "1", "ledger": [["get", _key()]],
                      "messages": ["Refresh before editing a restored page."] * 2, "disabled": [True, True]}


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
@pytest.mark.parametrize("path", ("/", "/browse", "/library", "/library?view=items",
                                  "/library?view=series", "/music", "/activity", "/series"))
def test_browsing_pages_have_one_top_right_shell_control_not_page_local_copies(fixture, path, request):
    response = request.getfixturevalue(fixture).get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    toolbar = markup.one(**{"data-media-display-tools": None, "class": "media-display-tools app-shell no-print"})
    (control,) = [node for node in markup.elements if "data-cover-size-control" in node.attrs]
    assert toolbar in control.ancestors
    assert not any(node.tag in ("main", "nav", "form") for node in control.ancestors)
    assert not markup.find("button", within=control)
    slider = markup.one("input", within=control)
    assert (slider.attrs["min"], slider.attrs["max"], slider.attrs["value"]) == ("0", "10", "5")
    assert "disabled" in slider.attrs and slider.attrs["aria-valuetext"] == "100%"
    assert markup.one("p", id="global-cover-size-description")
    assert len([node for node in markup.find(within=toolbar) if "data-command-palette" in node.attrs]) == 1
    shell = markup.one("html")
    assert shell.attrs["data-cover-size-account"].isdecimal() and shell.attrs["data-cover-size-mount"] == ""


def test_anonymous_login_does_not_offer_or_identify_a_saved_cover_size(client):
    response = client.get("/login")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not [node for node in markup.elements if "data-cover-size-control" in node.attrs]
    assert "data-cover-size-account" not in markup.one("html").attrs


def test_coverless_settings_keeps_commands_without_an_inapplicable_size_control(admin_client):
    response = admin_client.get("/settings")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert [node for node in markup.elements if "data-command-palette" in node.attrs]
    assert not [node for node in markup.elements if "data-cover-size-control" in node.attrs]
    assert "data-cover-size-account" not in markup.one("html").attrs


@pytest.mark.parametrize("media_type", ("book", "cd", "cassette", "video_game", "future-format"))
@pytest.mark.parametrize("covered", (False, True))
def test_shared_frame_is_explicit_and_preserves_native_shapes_and_complete_metadata(media_type, covered):
    env, calls = _environment()
    card = build_media_card({"id": 23, "title": "<b>Full title & metadata</b>", "authors": "Full creator",
                             "media_type": media_type, "cover_path": "covers/shared.png" if covered else None})
    root = _HTML(env.get_template("components/media_card.html").render(
        card=card, media_card_size_mode="shared-v2",
    )).root
    (article,) = root.find("article")
    assert article.attrs["data-cover-size-card"] == "catalogue-v2"
    assert article.find("div", cls="media-card__art-stage")
    assert article.find("h3")[0].text == card.title
    assert not article.find("b") and card.title not in calls
    artwork = article.find("span", cls="media-artwork")[0]
    assert artwork.attrs["data-cover-shape"] == card.artwork.shape
    assert len(artwork.find("img")) == int(covered)
    assert article.find("p", cls="media-card__creator")[0].text == card.creator_text
    assert not article.find("div", cls="media-card__caption")


def test_library_live_fragment_owns_the_same_layout_without_repeating_shell_controls(viewer_client):
    response = viewer_client.get("/api/library/search?view=series")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    assert markup.testid("library-results").attrs["data-item-size"] == "density-v1"
    assert not [node for node in markup.elements if "data-cover-size-control" in node.attrs]
