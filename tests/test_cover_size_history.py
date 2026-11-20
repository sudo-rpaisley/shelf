"""History-sizing regression source, unexecuted at the authoring checkpoint.

The finite Node double models asynchronous ownership/cancellation, not native
event trust, BFCache, cookie delivery or browser hit testing. No network in it.
"""

import asyncio
import json
from pathlib import Path
import subprocess

import pytest
from starlette.requests import Request

from tests.test_cover_size_control import _DOCUMENT_DOUBLE, _render
from tests.test_shared_cover_size_setting import _STORAGE_BRIDGE, _key


SCRIPT = Path(__file__).resolve().parents[1] / "static/js/cover-size.js"
ENDPOINT = "/api/ui/cover-size-owner"

_HISTORY_BRIDGE = r"""
let marker = 'synthetic-session-a', markerFailure = false;
const requests = [], timers = new Map();
let timerId = 0;
sandbox.URL = URL;
doc.defaultView.location = {href: 'https://synthetic.invalid/original?ignored=1#ignored'};
doc.defaultView.csrfToken = () => {
    if (markerFailure) throw Error('synthetic cookie refusal');
    return marker;
};
doc.defaultView.AbortController = AbortController;
doc.defaultView.setTimeout = (callback, delay) => {
    if (delay !== 5000) throw Error('Unexpected confirmation deadline');
    timers.set(++timerId, callback); return timerId;
};
doc.defaultView.clearTimeout = id => timers.delete(id);
doc.defaultView.fetch = (url, init) => new Promise((resolve, reject) => requests.push({url, init, resolve, reject}));
function response(data = {account: options.account, mount: options.mount}, extra = {}) {
    return {ok: true, redirected: false, headers: {get: () => 'application/json; charset=utf-8'},
        json: () => Promise.resolve(data), ...extra};
}
const prepareStored = prepare;
function addHistoryControl(root) {
    for (const state of ['checking', 'unverified', 'changed', 'unsaved']) root.attrs['data-cover-size-' + state] = state;
    const retry = new Element('button', {type: 'button', 'data-cover-size-retry': '', disabled: ''});
    retry.hidden = true; retry.parentElement = root; root.children.push(retry);
    return root;
}
prepare = root => addHistoryControl(prepareStored(root));
doc.roots.forEach(addHistoryControl);
const retry = () => doc.roots[0].querySelector('[data-cover-size-retry]');
const persistence = () => doc.roots.map(root => root.getAttribute('data-cover-size-persistence'));
const sets = () => ledger.filter(row => row[0] === 'set');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function restore() { windowEvent('pagehide', {}); windowEvent('pageshow', {persisted: true}); }
"""


def _run(scenario, *, before="", account="17", mount="", stored="100", **failures):
    options = {"account": account, "mount": mount, "entries": {_key(account, mount): stored},
               "getterFailure": False, "readFailure": False, "writeFailure": False, **failures}
    bridge = _STORAGE_BRIDGE.replace("OPTIONS", json.dumps(options), 1)
    program = (_DOCUMENT_DOUBLE + bridge + _HISTORY_BRIDGE + "\nconst source = " + json.dumps(SCRIPT.read_text())
               + ";\nconst run = () => vm.runInContext(source, context);\n" + before
               + "\nrun();\n(async () => {\n" + scenario
               + "\n})().catch(error => { console.error(error); process.exitCode = 1; });")
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_initial_and_native_change_have_no_owner_http_or_duplicate_writes():
    result = _run("""
        input(doc.roots[0]).value = '0'; windowEvent('pageshow', {persisted: false});
        if (input(doc.roots[0]).value !== '5') throw Error('Native history form state was not reconciled');
        nativeChange(10); nativeChange(10, 'change'); run(); run();
        console.log(JSON.stringify({count: requests.length, sets: sets(), values: values(), persistence: persistence(),
            listeners: Object.fromEntries(Object.entries(listeners).map(([k,v]) => [k,v.length])),
            windows: Object.fromEntries(Object.entries(windowListeners).map(([k,v]) => [k,v.length]))}));
    """)
    assert result == {"count": 0, "sets": [["set", _key(), "200"]], "values": ["10", "10"],
                      "persistence": ["ready", "ready"],
                      "listeners": {"input": 1, "change": 1, "click": 1, "htmx:afterSwap": 1, "htmx:oobAfterSwap": 1},
                      "windows": {"storage": 1, "pageshow": 1, "pagehide": 1}}


def test_same_owner_history_rereads_missed_storage_then_resumes_saving_without_refresh():
    result = _run("""
        windowEvent('pagehide', {}); entries.set('shelf.coverSize.v2::u:17', '60');
        windowEvent('pageshow', {persisted: true});
        const waiting = {messages: messages(), enabled: doc.roots.every(root => !input(root).disabled)};
        const request = {url: requests[0].url, ...requests[0].init}; delete request.signal;
        requests[0].resolve(response()); await settle(); const restored = scale();
        nativeChange(10); nativeChange(10, 'change');
        console.log(JSON.stringify({waiting, request, restored, scale: scale(), ledger, timers: timers.size}));
    """)
    assert result["waiting"] == {"messages": ["checking"] * 2, "enabled": True}
    assert result["request"] == {"url": "https://synthetic.invalid" + ENDPOINT, "method": "GET", "mode": "same-origin",
                                 "credentials": "same-origin", "cache": "no-store", "redirect": "error",
                                 "headers": {"Accept": "application/json"}}
    assert result["restored"] == "0.6" and result["scale"] == "2"
    assert result["ledger"] == [["get", _key()], ["get", _key()], ["set", _key(), "200"]]
    assert result["timers"] == 0


def test_history_form_values_restored_after_pageshow_are_reconciled_at_next_paint_without_io():
    result = _run("""
        const frames = []; doc.defaultView.requestAnimationFrame = callback => frames.push(callback);
        windowEvent('pageshow', {persisted: false}); input(doc.roots[0]).value = '0';
        frames[0]();
        console.log(JSON.stringify({values: values(), scale: scale(), count: requests.length, sets: sets()}));
    """, stored="140")
    assert result == {"values": ["7"] * 2, "scale": "1.4", "count": 0, "sets": []}


def test_edits_during_confirmation_survive_reply_storage_and_oob_without_becoming_queued_saves():
    result = _run("""
        restore(); nativeChange(7); storageEvent('shelf.coverSize.v2::u:17', '50');
        const old = doc.roots[0]; doc.roots[0] = prepare(control()); emit('htmx:afterSwap', old); run();
        requests[0].resolve(response()); await settle();
        nativeChange(7, 'change'); storageEvent('shelf.coverSize.v2::u:17', '200');
        const after = {scale: scale(), sets: sets(), messages: messages(), values: values()};
        input(old).valueAsNumber = 10; emit('input', input(old));
        nativeChange(10, 'input', {isTrusted: false}); nativeChange(8); nativeChange(8, 'change');
        console.log(JSON.stringify({after, scale: scale(), sets: sets(), values: values()}));
    """)
    assert result["after"] == {"scale": "1.4", "sets": [], "messages": ["unsaved"] * 2, "values": ["7"] * 2}
    assert result["scale"] == "1.6" and result["values"] == ["8"] * 2
    assert result["sets"] == [["set", _key(), "160"]]


@pytest.mark.parametrize("reply", [{"account": "18", "mount": ""}, {"account": "17", "mount": "/other"},
                                    {"account": 17, "mount": ""}, {}, None])
def test_mismatched_or_malformed_owner_never_rebinds_reads_or_writes_other_accounts(reply):
    result = _run("""
        entries.set('shelf.coverSize.v2::u:18', '50'); restore();
        requests[0].resolve(response(REPLY)); await settle(); nativeChange(7);
        storageEvent('shelf.coverSize.v2::u:17', '200');
        console.log(JSON.stringify({scale: scale(), ledger, messages: messages(), persistence: persistence(),
            other: entries.get('shelf.coverSize.v2::u:18'), retry: !retry().hidden && !retry().disabled}));
    """.replace("REPLY", json.dumps(reply)))
    assert result == {"scale": "1.4", "ledger": [["get", _key()]], "messages": ["changed"] * 2,
                      "persistence": ["page-only"] * 2, "other": "50", "retry": True}


def test_adjacent_large_account_ids_are_not_rounded_into_an_owner_match():
    result = _run("""
        restore(); requests[0].resolve(response({account:'9007199254740992', mount:''})); await settle(); nativeChange(8);
        console.log(JSON.stringify({sets: sets(), messages: messages()}));
    """, account="9007199254740993")
    assert result == {"sets": [], "messages": ["changed"] * 2}


@pytest.mark.parametrize("when", ["before_reply", "after_reply"])
def test_signin_marker_changes_fence_delayed_confirmation_and_subsequent_trusted_writes(when):
    before_reply = "marker = 'synthetic-session-b';" if when == "before_reply" else ""
    result = _run("""
        restore(); BEFORE
        requests[0].resolve(response()); await settle(); marker = 'synthetic-session-b';
        nativeChange(7); storageEvent('shelf.coverSize.v2::u:17', '200');
        console.log(JSON.stringify({sets: sets(), scale: scale(), messages: messages(), persistence: persistence()}));
    """.replace("BEFORE", before_reply))
    assert result == {"sets": [], "scale": "1.4", "messages": ["changed"] * 2, "persistence": ["page-only"] * 2}


@pytest.mark.parametrize("phase", ["response", "json"])
def test_new_restoration_and_pagehide_invalidate_late_response_or_json_even_when_abort_is_ignored(phase):
    first = ("let finishJSON; requests[0].resolve(response(undefined, {json: () => new Promise(resolve => finishJSON = resolve)})); await settle();"
             if phase == "json" else "")
    finish = ("finishJSON({account:'18', mount:''});" if phase == "json"
              else "requests[0].resolve(response({account:'18', mount:''}));")
    result = _run("""
        restore(); FIRST restore();
        requests[1].resolve(response()); await settle(); nativeChange(8);
        FINISH await settle(); const afterOld = {scale: scale(), messages: messages(), sets: sets()};
        restore(); windowEvent('pagehide', {}); requests[2].resolve(response()); await settle();
        console.log(JSON.stringify({afterOld, persistence: persistence(), aborted: requests.map(request => request.init.signal.aborted),
            scale: scale(), sets: sets(), timers: timers.size}));
    """.replace("FIRST", first).replace("FINISH", finish))
    assert result["afterOld"] == {"scale": "1.6", "messages": ["Saved for this account in this browser."] * 2,
                                  "sets": [["set", _key(), "160"]]}
    assert result["aborted"] == [True, False, True] and result["timers"] == 0
    assert result["persistence"] == ["checking"] * 2
    assert result["scale"] == "1.6" and result["sets"] == [["set", _key(), "160"]]
    # Pagehide does not mutate the hidden DOM; a later pageshow owns its sync.


def test_timeout_leaves_local_sizing_and_explicit_retry_never_retroactively_saves():
    result = _run("""
        restore(); const expire = [...timers.values()][0]; expire(); nativeChange(0);
        const failed = {scale: scale(), count: requests.length, messages: messages(), retry: !retry().hidden};
        emit('click', retry(), false, {isTrusted: false});
        emit('click', retry()); requests[1].resolve(response()); await settle();
        requests[0].resolve(response()); await settle();
        const confirmed = {scale: scale(), sets: sets(), messages: messages()};
        nativeChange(0, 'change'); nativeChange(1);
        console.log(JSON.stringify({failed, confirmed, count: requests.length, sets: sets(), timers: timers.size}));
    """)
    assert result["failed"] == {"scale": "0.5", "count": 1, "messages": ["unverified"] * 2, "retry": True}
    assert result["confirmed"] == {"scale": "0.5", "sets": [], "messages": ["unsaved"] * 2}
    assert result["count"] == 2 and result["sets"] == [["set", _key(), "60"]] and result["timers"] == 0


@pytest.mark.parametrize("failure", ["network", "401", "500", "redirect", "html", "json"])
def test_failed_confirmation_stays_page_only_with_no_automatic_retry(failure):
    completion = {
        "network": "requests[0].reject(Error('synthetic offline'));",
        "401": "requests[0].resolve(response(undefined, {ok:false, status:401}));",
        "500": "requests[0].resolve(response(undefined, {ok:false, status:500}));",
        "redirect": "requests[0].resolve(response(undefined, {redirected:true}));",
        "html": "requests[0].resolve(response(undefined, {headers:{get:()=>'text/html'}}));",
        "json": "requests[0].resolve(response(undefined, {json:()=>Promise.reject(Error('synthetic malformed JSON'))}));",
    }[failure]
    result = _run("""
        restore(); COMPLETE await settle(); nativeChange(8); nativeChange(8, 'change');
        console.log(JSON.stringify({count: requests.length, scale: scale(), sets: sets(), messages: messages(), timers: timers.size}));
    """.replace("COMPLETE", completion))
    assert result == {"count": 1, "scale": "1.6", "sets": [], "messages": ["unverified"] * 2, "timers": 0}


@pytest.mark.parametrize("failure", ["getterFailure", "readFailure", "writeFailure"])
def test_storage_refusal_after_history_still_allows_sizing_without_false_save(failure):
    result = _run("""
        restore(); requests[0].resolve(response()); await settle(); nativeChange(8); nativeChange(8, 'change');
        console.log(JSON.stringify({scale: scale(), messages: messages(), enabled: !input(doc.roots[0]).disabled,
            writes: sets().length}));
    """, **{failure: True, "writeFailure": True})
    assert result == {"scale": "1.6", "messages": ["Storage unavailable; this page only."] * 2,
                      "enabled": True, "writes": 0 if failure == "getterFailure" else 1}


@pytest.mark.parametrize("before", ["marker = '';", "markerFailure = true;"])
def test_unreadable_or_missing_marker_fails_closed_but_never_disables_local_sizing(before):
    result = _run("""
        restore(); nativeChange(8);
        console.log(JSON.stringify({count: requests.length, sets: sets(), scale: scale(), messages: messages()}));
    """, before=before)
    assert result == {"count": 0, "sets": [], "scale": "1.6", "messages": ["unverified"] * 2}


@pytest.mark.parametrize("raw", ["050", "100 ", "1e2", "101", "", None])
def test_malformed_saved_values_found_on_restore_default_without_repair_writes(raw):
    result = _run("""
        restore(); entries.set('shelf.coverSize.v2::u:17', RAW);
        requests[0].resolve(response()); await settle();
        const restored = {scale: scale(), sets: sets()};
        storageEvent('shelf.coverSize.v2::u:17', '180');
        console.log(JSON.stringify({restored, scale: scale(), sets: sets()}));
    """.replace("RAW", json.dumps(raw)), stored="200")
    assert result == {"restored": {"scale": "1", "sets": []}, "scale": "1.8", "sets": []}


@pytest.mark.parametrize("mount", ["/shelf", "/shelf-é", "//not-a-host.invalid"])
def test_request_stays_on_original_origin_mount_and_captured_owner_despite_shell_or_url_edits(mount):
    result = _run("""
        doc.documentElement.setAttribute('data-cover-size-account', '18');
        doc.documentElement.setAttribute('data-cover-size-mount', '/other');
        doc.defaultView.location.href = 'https://synthetic.invalid/changed?account=18#x';
        restore(); requests[0].resolve(response()); await settle(); nativeChange(7);
        console.log(JSON.stringify({url: requests[0].url, sets: sets()}));
    """, mount=mount)
    from urllib.parse import quote, urlsplit

    assert urlsplit(result["url"]).netloc == "synthetic.invalid"
    assert urlsplit(result["url"]).path == quote(mount, safe="/") + ENDPOINT
    assert result["sets"] == [["set", _key(mount=mount), "140"]]


@pytest.mark.parametrize("fixture,user", [("admin_client", "admin_user"), ("editor_client", "editor_user"), ("viewer_client", "viewer_user")])
def test_owner_endpoint_uses_current_cookie_identity_and_is_read_only_cache_free(fixture, user, request, db, monkeypatch):
    import httpx

    client, actor = request.getfixturevalue(fixture), request.getfixturevalue(user)
    before = tuple(db.iterdump())
    def forbidden(*args, **kwargs):
        raise AssertionError("Owner confirmation cannot query a provider")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    response = client.get(ENDPOINT + "?account=999&mount=/other")
    assert response.status_code == 200 and response.json() == {"account": str(actor["id"]), "mount": ""}
    assert response.headers["cache-control"] == "no-store" and response.headers["vary"] == "Cookie"
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("account", [9007199254740993, 9223372036854775807])
def test_owner_reply_keeps_large_id_text_and_trusted_mount_without_usernames_or_secrets(account):
    from app.routers.pages import cover_size_owner

    scope = {"type": "http", "path": ENDPOINT, "headers": [], "root_path": "/shelf-é"}
    reply = asyncio.run(cover_size_owner(Request(scope), user={"id": account, "username": "not-returned", "role": "viewer"}))
    assert json.loads(reply.body) == {"account": str(account), "mount": "/shelf-é"}


def test_owner_endpoint_requires_session_and_does_not_accept_bearer_or_supplied_account(client, viewer_user):
    response = client.get(ENDPOINT + "?account=" + str(viewer_user["id"]),
                          headers={"Authorization": "Bearer synthetic-not-a-session"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "account" not in response.text


def test_mounted_owner_endpoint_uses_trusted_asgi_mount_and_the_resolved_account(viewer_client, viewer_user):
    from fastapi.testclient import TestClient

    mounted = TestClient(viewer_client.app, base_url="https://testserver", root_path="/shelf")
    mounted.cookies.update(viewer_client.cookies)
    try:
        response = mounted.get("/shelf" + ENDPOINT + "?mount=/other")
        assert response.status_code == 200 and response.json() == {"account": str(viewer_user["id"]), "mount": "/shelf"}
        assert response.headers["cache-control"] == "no-store"
    finally:
        mounted.close()


def test_owner_confirmation_rejects_a_revoked_session_instead_of_trusting_old_claims(viewer_client, viewer_user, db):
    db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (viewer_user["id"],))
    db.commit()
    response = viewer_client.get(ENDPOINT, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("newstyle", [False, True])
def test_history_statuses_and_retry_are_truthfully_owned_and_escape_translations(newstyle):
    states = {
        "checking": "Checking this sign-in; size changes apply to this page for now.",
        "unverified": "Sign-in could not be confirmed; size changes apply to this page only. Retry saving when connected.",
        "changed": "This sign-in has changed; size changes apply to this page only.",
        "unsaved": "Size changed on this page; move the slider again to save for this account.",
    }
    hostile = '<probe data-untrusted="yes"> & "caption"'
    messages = {value: hostile + value for value in [*states.values(), "Retry saving"]}
    markup, calls = _render(messages, newstyle=newstyle)
    (root,) = [node for node in markup.nodes if "data-cover-size-control" in node["attrs"]]
    for state, value in states.items():
        assert root["attrs"]["data-cover-size-" + state] == hostile + value and value in calls
    (button,) = markup.tagged("button")
    assert button["attrs"]["type"] == "button" and "hidden" in button["attrs"] and "disabled" in button["attrs"]
    assert not markup.tagged("probe") and "Refresh this page" not in markup.text
    assert "data-cover-size-restored" not in root["attrs"]
