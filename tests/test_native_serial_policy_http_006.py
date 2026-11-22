"""Native 977 policy over real loopback HTTP, not mocked provider results.

These are TestClient/TCP integration tests, not browser E2E or live-provider
tests. The existing ISSN URL override is the sole allowed TCP destination.
Run only after the authoring hold ends and the complete candidate is frozen.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Lock, Thread
from unittest.mock import AsyncMock

import httpx
import pytest


EAN = "9772280147904"
ISSN = "2280-1472"
BARCODE = EAN + "60066"
TITLE = "Internazionale"
PUBLISHER = "Native HTTP Publisher"
RETRY_AFTER = "86400"
NO_MATCH_NOTICE = "No publication metadata was returned for this ISSN. Enter the publication title below."
RATE_LIMIT_NOTICE = (
    "The ISSN metadata source is rate-limiting requests. The barcode was decoded; "
    "confirm the publication below."
)

# Expectations are independent of the application classifier/resolver.
WIRE_CASES = [
    pytest.param("missing", "no_match", 404, None, id="404-is-a-portal-miss"),
    pytest.param("limited", "rate_limited", 429, None, id="429-is-not-a-miss"),
    pytest.param("exact", "found", 200, TITLE, id="exact-issn-jsonld-hit"),
    pytest.param("wrong", "no_match", 200, None, id="wrong-issn-is-not-evidence"),
    pytest.param("generic", "no_match", 200, None, id="generic-title-is-not-evidence"),
    pytest.param("malformed", "no_match", 200, None, id="numeric-title-is-not-evidence"),
]


def _wire_response(case):
    if case == "missing":
        return 404, {}, {"error": "No such serial"}
    if case == "limited":
        return 429, {"Retry-After": RETRY_AFTER}, {"error": "Quota exhausted"}
    if case == "exact":
        # Exercise the real linked-title parser, including an unrelated first
        # node. Provider issue-like data must never become an inferred issue.
        return 200, {}, {"@graph": [
            {"@id": "resource/ISSN/0953-6167", "identifier": "0953-6167", "mainTitle": "Wrong first serial"},
            {"@id": f"resource/ISSN/{ISSN}", "identifier": [{"@value": ISSN}],
             "http://id.loc.gov/ontologies/bibframe/title": [{"@id": "#native-key-title"}],
             "publisher": [{"name": PUBLISHER}], "issue_number": "60066", "issue_date": "2099-05-01"},
            {"@id": "#native-key-title", "http://www.w3.org/1999/02/22-rdf-syntax-ns#value": [{"@value": TITLE}]},
        ]}
    if case == "wrong":
        return 200, {}, {"@graph": [{
            "@id": "resource/ISSN/0953-6167", "identifier": "0953-6167", "mainTitle": "Unrelated serial",
        }]}
    if case == "generic":
        return 200, {}, {"@graph": [{"identifier": ISSN, "mainTitle": "  Magazines \t "}]}
    if case == "malformed":
        return 200, {}, {"@graph": [{"identifier": ISSN, "mainTitle": 123}]}
    raise AssertionError(f"Unknown local response case: {case}")


class _PortalServer(ThreadingHTTPServer):
    # server_close joins request threads too, including when an assertion fails.
    daemon_threads = False
    block_on_close = True


@dataclass
class _Wire:
    origin: str = ""
    tcp_requests: list = field(default_factory=list)
    transport_calls: list = field(default_factory=list)
    response_statuses: list = field(default_factory=list)
    unexpected: list = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def assert_one_portal_request(self, *, status):
        path = f"/resource/ISSN/{ISSN}?format=json"
        assert self.transport_calls == [("GET", self.origin + path)]
        assert self.response_statuses == [status]
        assert self.tcp_requests == [(
            "GET", path, "application/ld+json, application/json;q=0.9",
            "Shelf/1.0 (+https://github.com/sudo-rpaisley/shelf)",
        )]
        assert self.unexpected == []

    def assert_idle(self):
        assert self.transport_calls == []
        assert self.response_statuses == []
        assert self.tcp_requests == []
        assert self.unexpected == []


@pytest.fixture
def native_wire(monkeypatch):
    """Only negative boundaries are stubbed; permitted requests use real TCP.

    The conftest autouse fixture supplies a fresh outbound host registry. Do
    not replace fetch, classification, portal lookup/parser or either caller.
    Guard failures are checked OUTSIDE provider exception handlers as well.
    """
    from app.services import crossref_journals, googlebooks, outbound, periodical_google, upcitemdb

    forbidden = []
    for module, method in (
        (googlebooks, "lookup_magazine_by_issn"), (googlebooks, "lookup"),
        (crossref_journals, "lookup"), (upcitemdb, "lookup"),
        (periodical_google, "search_issues"), (periodical_google, "lookup_issue"),
    ):
        stub = AsyncMock(side_effect=AssertionError(f"Native serial must not call {module.__name__}.{method}"))
        monkeypatch.setattr(module, method, stub)
        forbidden.append(stub)
    sleeper = AsyncMock(side_effect=AssertionError("This single loopback request must not sleep/retry"))
    monkeypatch.setattr(outbound, "_sleep", sleeper)
    assert float(RETRY_AFTER) > outbound.RETRY_AFTER_MAX
    # Prevent developer/CI proxy settings from routing even loopback externally.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    real_transport = httpx.AsyncHTTPTransport.handle_async_request
    active = None
    all_unexpected = []

    async def guarded_transport(transport, request):
        call = (request.method, str(request.url))
        if active is None:
            all_unexpected.append(call)
            raise AssertionError(f"No loopback provider is active: {call}")
        active.transport_calls.append(call)
        if (request.url.scheme, request.url.host, request.url.port) != (
            "http", "127.0.0.1", httpx.URL(active.origin).port,
        ):
            active.unexpected.append(call)
            all_unexpected.append(call)
            raise AssertionError(f"External/unexpected provider request refused: {call}")
        response = await real_transport(transport, request)
        active.response_statuses.append(response.status_code)
        return response

    def refuse_sync_transport(transport, request):
        call = (request.method, str(request.url))
        all_unexpected.append(call)
        raise AssertionError(f"Unexpected synchronous provider request refused: {call}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_transport)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_sync_transport)

    @contextmanager
    def serve(case):
        nonlocal active
        assert active is None
        status, headers, document = _wire_response(case)
        body = json.dumps(document).encode("utf-8")
        wire = _Wire()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def do_GET(self):
                with wire.lock:
                    wire.tcp_requests.append((
                        self.command, self.path, self.headers.get("Accept"), self.headers.get("User-Agent"),
                    ))
                self.send_response(status)
                self.send_header("Content-Type", "application/ld+json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                # No HTTP access-log noise; the actual request ledger is above.
                pass

        server = _PortalServer(("127.0.0.1", 0), Handler)
        thread = None
        started = False
        try:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, name="native-issn-006")
            wire.origin = f"http://127.0.0.1:{server.server_port}"
            active = wire
            monkeypatch.setenv("SHELF_ISSN_PORTAL_URL", wire.origin + "/resource/ISSN/{issn}")
            thread.start()
            started = True
            yield wire
        finally:
            try:
                if started:
                    server.shutdown()
            finally:
                try:
                    server.server_close()
                finally:
                    try:
                        if started:
                            thread.join(timeout=5)
                            assert not thread.is_alive(), "Local ISSN serving thread leaked"
                    finally:
                        active = None

    try:
        yield serve
    finally:
        # A provider can catch AssertionError: merely raising inside its callback
        # is NOT an oracle. These counters still fail if that exception is swallowed.
        assert all_unexpected == []
        for stub in forbidden:
            stub.assert_not_called()
        sleeper.assert_not_called()


def _expected_payload(title=None, *, raw=BARCODE, supplement="60066"):
    return {
        "media_type": "magazine", "title": title, "publication_title": title,
        "publisher": PUBLISHER if title is not None else None, "language": None, "description": None,
        "publication_issn": ISSN if title is not None else None, "issn": ISSN,
        "barcode_ean": raw[:13], "barcode_supplement": supplement, "barcode_variant": "90",
        "issue_number": None, "issue_date": None, "requires_issue_confirmation": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case,outcome,status,title", WIRE_CASES)
async def test_native_resolver_preserves_real_portal_quality_and_carrier(native_wire, case, outcome, status, title):
    from app.services import periodical_scan, provider_result

    with native_wire(case) as wire:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            result = await periodical_scan.resolve_serial_barcode(BARCODE, client)
        assert isinstance(result, provider_result.ProviderResult)
        assert (result.outcome, result.provider, result.status) == (outcome, "issn_portal", status)
        assert result.found is (outcome == "found")
        assert result.payload == _expected_payload(title)
        wire.assert_one_portal_request(status=status)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,supplement", [(EAN, None), (EAN + "05", "05"), (BARCODE, "60066")])
async def test_native_resolver_preserves_each_real_carrier_shape(native_wire, raw, supplement):
    from app.services import periodical_scan

    with native_wire("exact") as wire:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            result = await periodical_scan.resolve_serial_barcode(raw, client)
        assert (result.outcome, result.provider, result.status) == ("found", "issn_portal", 200)
        assert result.payload == _expected_payload(TITLE, raw=raw, supplement=supplement)
        wire.assert_one_portal_request(status=200)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "", "not a barcode", "9772280147905", EAN + "123", "978228014790460066",
    "4006381333931", "400638133393105", "01234567890512",
])
async def test_native_resolver_invalid_and_non977_serials_spend_zero_providers(native_wire, monkeypatch, raw):
    from app.services import issn_portal, periodical_scan

    portal = AsyncMock(side_effect=AssertionError("Invalid/non-977 input must stop before even portal lookup"))
    monkeypatch.setattr(issn_portal, "lookup", portal)
    with native_wire("exact") as wire:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            result = await periodical_scan.resolve_serial_barcode(raw, client)
        assert (result.outcome, result.provider, result.status, result.payload) == (
            "no_match", "periodical_barcode", None, None,
        )
        portal.assert_not_called()
        wire.assert_idle()


def _snapshot(db):
    """All persistent tables, including FTS, identities, private history and logs.

    There is no blanket 'ignore logs' rule. Expected native scan/429 diagnostic
    appends are checked in full below, including their sqlite_sequence effects.
    """
    db.commit()  # Requests write through other real connections; release old snapshots.
    schema = [tuple(row) for row in db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    )]
    rows = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({quoted})")]
        order = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
        rows[name] = [dict(row) for row in db.execute(f"SELECT * FROM {quoted} ORDER BY {order}")]
    return {"schema": schema, "rows": rows}


def _expect_append(expected, actual, table, fields, *, timestamp):
    old_rows = expected["rows"][table]
    new_rows = actual["rows"][table]
    assert len(new_rows) == len(old_rows) + 1
    assert new_rows[:-1] == old_rows
    added = new_rows[-1]
    assert added["id"] == max((row["id"] for row in old_rows), default=0) + 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", added[timestamp])
    assert added == {"id": added["id"], timestamp: added[timestamp], **fields}
    expected["rows"][table] = new_rows
    sequences = expected["rows"]["sqlite_sequence"]
    sequences[:] = [row for row in sequences if row["name"] != table]
    sequences.append({"name": table, "seq": added["id"]})
    sequences.sort(key=lambda row: row["name"])


def _assert_scan_only(db, before, *, user_id, mode, limited=False):
    actual = _snapshot(db)
    expected = deepcopy(before)
    _expect_append(expected, actual, "scan_log", {
        "isbn": BARCODE, "media_type": "magazine", "result": "periodical_confirm", "item_id": None,
        "mode": mode, "security_library_id": None, "security_user_id": user_id,
    }, timestamp="created_at")
    if limited:
        # Existing fetch deliberately logs its above-ceiling refusal. Assert the
        # EXACT diagnostic; do not drop log_entries or waive unexpected errors.
        _expect_append(expected, actual, "log_entries", {
            "level": "WARNING", "module": "app.services.outbound",
            "message": "outbound: 127.0.0.1 returned HTTP 429 asking for a 86400s wait, "
                       "beyond the 30s ceiling — not retrying",
        }, timestamp="timestamp")
    assert actual == expected


@pytest.fixture(params=["default", "minimal"])
def feature_profile(request, db):
    from app import features

    if request.param == "minimal":
        features.apply_profile(db, "minimal")
        db.commit()
    assert features.current_profile() == ("everything" if request.param == "default" else "minimal")
    assert all(features.feature_enabled(key) is (request.param == "default") for key in features.FEATURES)
    return request.param


def _retained_controls(db, controls):
    """Stable pre-existing IDs and private rows, not a count-only preservation check."""
    item_ids = (controls["visible"], controls["hidden"])
    result = {}
    for table, column, values in (
        ("items", "id", item_ids), ("item_copies", "item_id", item_ids),
        ("library_items", "item_id", item_ids), ("user_item_state", "item_id", item_ids),
        ("periodical_issues", "item_id", item_ids),
        ("periodical_publications", "id", (controls["publication"],)),
    ):
        marks = ",".join("?" for _ in values)
        result[table] = [tuple(row) for row in db.execute(
            f"SELECT * FROM {table} WHERE {column} IN ({marks}) ORDER BY 1, 2", values,
        )]
    for table in ("users", "libraries", "library_memberships", "api_tokens", "user_reading_log", "reading_log"):
        result[table] = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
    result["other_actor_state"] = [tuple(row) for row in db.execute(
        "SELECT * FROM user_item_state WHERE user_id = ? ORDER BY item_id", (controls["other_user"],),
    )]
    return result


@pytest.fixture
def catalogue_controls(db, editor_user, viewer_user):
    from app.services import api_tokens, item_write, libraries, periodical_records, user_state
    from tests.conftest import _insert_node_location, _insert_personal_history, _legacy_location

    node = _insert_node_location(db, "Retained precise native shelf")
    flat = _legacy_location(db, node)
    hidden_library = libraries.create_library(db, "Other actor's private serials")
    libraries.set_membership(db, hidden_library["id"], viewer_user["id"], "viewer")
    visible = item_write.insert_item(db, title="Retained household title", isbn="9780590435062",
                                     media_type="book", authors="Retained Author", location_id=flat,
                                     library_id=1, owned=1, notes="Retained shared catalogue notes")
    hidden = item_write.insert_item(db, title="Retained hidden serial issue", media_type="magazine",
                                    location_id=flat, library_id=hidden_library["id"], owned=1)
    publication = periodical_records.upsert_publication(db, title="Retained hidden serial", issn="0953-6167")
    periodical_records.link_issue(db, item_id=hidden, publication_id=publication,
                                  issue_number="7", barcode_ean="9770953616115", barcode_supplement="01")
    for user_id, item_id, notes, rating in (
        (editor_user["id"], visible, "Retained editor private notes", 5),
        (viewer_user["id"], visible, "Retained other actor private notes", 2),
        (viewer_user["id"], hidden, "Retained hidden history notes", 3),
    ):
        user_state.save_state(db, user_id, item_id, reading_status="reading", date_started="2025-01-02",
                              rating=rating, wishlist=1, favourite=1, personal_notes=notes,
                              progress_value=7, progress_total=100, progress_unit="pages")
        _insert_personal_history(db, user_id, item_id, status="read", date_started="2024-01-01",
                                 date_finished="2024-01-07", notes=notes + " history")
    db.execute("INSERT INTO reading_log (item_id, status, date_started, date_finished) "
               "VALUES (?, 'read', '2023-01-01', '2023-01-07')", (visible,))
    db.execute("INSERT INTO scan_log (isbn, media_type, result, item_id, mode, security_library_id, security_user_id) "
               "VALUES ('retained-other-actor-scan', 'book', 'duplicate', ?, 'add', 1, ?)",
               (visible, viewer_user["id"]))
    db.execute("INSERT INTO log_entries (timestamp, level, module, message) "
               "VALUES (datetime('now'), 'INFO', 'native_serial_http_006.fixture', 'Retained diagnostic')")
    token = api_tokens.create(db, editor_user["id"], "Retained read-only capability")
    assert token is not None
    db.commit()
    capability = api_tokens.resolve(token["token"])
    assert (capability["id"], capability["api_token_id"], capability["role"], capability["account_role"]) == (
        editor_user["id"], token["id"], "viewer", "editor",
    )
    controls = {"visible": visible, "hidden": hidden, "publication": publication, "node": node, "flat": flat,
                "hidden_library": hidden_library["id"], "other_user": viewer_user["id"],
                "capability": capability, "token": token["token"]}
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM user_reading_log").fetchone()[0] == 3
    controls["retained"] = _retained_controls(db, controls)
    return controls


class _ConfirmationForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.forms = 0
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("action") == "/api/periodicals/confirm" and attrs.get("method") == "post"
            self.forms += int(self.active)
        elif self.active and tag == "input" and attrs.get("name"):
            assert attrs["name"] not in self.inputs, "Duplicate native confirmation input"
            self.inputs[attrs["name"]] = attrs

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


def _scan(client, controls, *, mode="wishlist", hint="dvd"):
    return client.post("/api/scan", data={
        "isbn": BARCODE, "media_type": hint, "mode": mode, "location_id": str(controls["flat"]),
        "library_id": "1", "tags": "Native scan tag", "reading_status": "reading",
    })


def _assert_card(response, client, controls, *, title, mode, outcome):
    assert response.status_code == 200
    assert 'data-scan-status="periodical_confirm"' in response.text
    assert "data-scan-error" not in response.text
    assert response.context["candidate"] == _expected_payload(title)
    assert f"ISSN {ISSN}" in response.text
    assert f"{EAN} + 60066" in response.text
    form = _ConfirmationForm()
    form.feed(response.text)
    assert form.forms == 1
    fields = {name: attrs.get("value", "") for name, attrs in form.inputs.items()}
    assert fields == {
        "_csrf": client.cookies.get("csrf_token"), "raw_barcode": BARCODE,
        "publisher": PUBLISHER if title is not None else "", "language": "", "cover_url": "",
        "location_id": str(controls["flat"]), "mode": mode, "shelf_fill_origin": "", "library_id": "1",
        "tags": "Native scan tag", "reading_status": "reading", "publication_title": title or "",
        # This EDITABLE value is the barcode hint, not a claim of provider confirmation.
        "publication_issn": ISSN, "volume": "", "issue_number": "", "issue_date": "", "cover_date_label": "",
    }
    for name in ("raw_barcode", "location_id", "library_id", "mode", "tags", "reading_status"):
        assert form.inputs[name]["type"] == "hidden"
    assert "required" in form.inputs["publication_title"]
    if outcome == "rate_limited":
        assert response.context["notice"] == RATE_LIMIT_NOTICE
        assert RATE_LIMIT_NOTICE in response.text
        assert NO_MATCH_NOTICE not in response.text
    elif outcome == "no_match":
        assert response.context["notice"] == NO_MATCH_NOTICE
        assert NO_MATCH_NOTICE in response.text
        assert RATE_LIMIT_NOTICE not in response.text
    else:
        assert outcome == "found"
        assert response.context["notice"] is None
        assert NO_MATCH_NOTICE not in response.text
        assert RATE_LIMIT_NOTICE not in response.text
    return fields


@pytest.mark.parametrize("case,outcome,status,title", WIRE_CASES)
def test_native_scan_real_http_outcomes_do_not_write_catalogue_or_private_history(
    editor_client, editor_user, db, native_wire, feature_profile, catalogue_controls, case, outcome, status, title,
):
    controls = catalogue_controls
    before = _snapshot(db)
    with native_wire(case) as wire:
        response = _scan(editor_client, controls)
        _assert_card(response, editor_client, controls, title=title, mode="wishlist", outcome=outcome)
        wire.assert_one_portal_request(status=status)
        _assert_scan_only(db, before, user_id=editor_user["id"], mode="wishlist", limited=(case == "limited"))


@pytest.mark.parametrize("mode", ["add", "wishlist"])
@pytest.mark.parametrize("hint", ["auto", "book", "dvd", "magazine"])
def test_native_977_miss_uses_only_portal_regardless_of_mode_hint_or_feature_profile(
    editor_client, editor_user, db, native_wire, feature_profile, catalogue_controls, mode, hint,
):
    before = _snapshot(db)
    with native_wire("missing") as wire:
        response = _scan(editor_client, catalogue_controls, mode=mode, hint=hint)
        _assert_card(response, editor_client, catalogue_controls, title=None, mode=mode, outcome="no_match")
        wire.assert_one_portal_request(status=404)
        _assert_scan_only(db, before, user_id=editor_user["id"], mode=mode)


@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_real_native_confirmation_all_features_off_saves_a_usable_core_item_and_separate_private_wish(
    editor_client, editor_user, viewer_user, db, native_wire, catalogue_controls, mode,
):
    from app import features
    from app.services import api_tokens, libraries, lists, user_state

    controls = catalogue_controls
    features.apply_profile(db, "minimal")
    db.commit()
    assert all(not features.feature_enabled(key) for key in features.FEATURES)
    before = _snapshot(db)
    with native_wire("exact") as wire:
        scan = _scan(editor_client, controls, mode=mode, hint="book")
        fields = _assert_card(scan, editor_client, controls, title=TITLE, mode=mode, outcome="found")
        _assert_scan_only(db, before, user_id=editor_user["id"], mode=mode)
        fields["issue_number"] = "42"  # User confirmation, NEVER supplement 60066 or variant 90.
        saved = editor_client.post("/api/periodicals/confirm", data=fields, follow_redirects=False)
        assert saved.status_code == 303
        db.commit()
        issue = db.execute("SELECT * FROM periodical_issues WHERE barcode_ean = ? AND barcode_supplement = ?",
                           (EAN, "60066")).fetchall()
        assert len(issue) == 1
        issue = issue[0]
        item_id = issue["item_id"]
        assert item_id not in (controls["visible"], controls["hidden"])
        assert (issue["issue_number"], issue["issue_date"], issue["volume"]) == ("42", None, None)
        publication = db.execute("SELECT * FROM periodical_publications WHERE id = ?", (issue["publication_id"],)).fetchone()
        assert (publication["title"], publication["issn"], publication["publisher"]) == (TITLE, ISSN, PUBLISHER)
        assert saved.headers["location"] == f"/item/{item_id}"
        page = editor_client.get(saved.headers["location"], follow_redirects=False)
        assert page.status_code == 200
        assert page.history == []
        assert page.url.path == f"/item/{item_id}"
        assert 'data-testid="item-hero"' in page.text
        assert 'data-testid="feature-disabled"' not in page.text
        assert f"{TITLE} — No. 42" in unescape(page.text)
        assert ("data-testid=\"wishlist-badge\"" in page.text) is (mode == "wishlist")
        assert "Retained other actor private notes" not in page.text
        item = db.execute("SELECT title, media_type, source, owned, location_id, reading_status FROM items WHERE id = ?",
                          (item_id,)).fetchone()
        assert tuple(item) == (f"{TITLE} — No. 42", "magazine", "issn", int(mode == "add"), controls["flat"], None)
        assert libraries.item_library_id(db, item_id) == 1
        copies = db.execute("SELECT is_primary, location_id FROM item_copies WHERE item_id = ?", (item_id,)).fetchall()
        assert [tuple(row) for row in copies] == ([(1, controls["node"])] if mode == "add" else [])
        assert lists.is_member(db, lists.WISHLIST, item_id) is (mode == "wishlist")
        state = user_state.get_state(db, editor_user["id"], item_id)
        # Native confirm's existing mode is shared acquisition intent. An actor's
        # private wish is independent, not inferred from owned=0/list membership.
        assert (state["persisted"], state["reading_status"], state["wishlist"]) == (True, "reading", 0)
        assert user_state.get_state(db, viewer_user["id"], item_id)["persisted"] is False
        private = editor_client.post(f"/api/items/{item_id}/personal-state", data={
            "wishlist": "1", "rating": "4", "personal_notes": "My confirmed serial note",
        })
        assert private.status_code == 200
        assert "On my wishlist" in private.text
        state = user_state.get_state(db, editor_user["id"], item_id)
        assert (state["wishlist"], state["rating"], state["personal_notes"]) == (1, 4, "My confirmed serial note")
        assert user_state.get_state(db, viewer_user["id"], item_id)["persisted"] is False
        assert lists.is_member(db, lists.WISHLIST, item_id) is (mode == "wishlist")
        assert _retained_controls(db, controls) == controls["retained"]
        assert api_tokens.resolve(controls["token"]) == controls["capability"]
        wire.assert_one_portal_request(status=200)  # Neither save, item GET nor personal save re-looks up metadata.


def _deny_portal(monkeypatch):
    from app.services import issn_portal

    portal = AsyncMock(side_effect=AssertionError("Authorization must precede portal expenditure"))
    monkeypatch.setattr(issn_portal, "lookup", portal)
    return portal


def test_genuine_viewer_cannot_scan_or_confirm_before_any_provider_call(
    viewer_client, db, native_wire, monkeypatch, feature_profile, catalogue_controls,
):
    portal = _deny_portal(monkeypatch)
    before = _snapshot(db)
    with native_wire("exact") as wire:
        response = _scan(viewer_client, catalogue_controls)
        assert response.status_code == 403
        confirmed = viewer_client.post("/api/periodicals/confirm", data={
            "raw_barcode": BARCODE, "publication_title": TITLE, "issue_number": "42", "library_id": "1",
        }, follow_redirects=False)
        assert confirmed.status_code == 403
        portal.assert_not_called()
        wire.assert_idle()
        assert _snapshot(db) == before


def test_global_editor_with_revoked_library_cannot_scan_before_any_provider_call(
    editor_client, editor_user, db, native_wire, monkeypatch, feature_profile, catalogue_controls,
):
    from app.services import libraries

    libraries.remove_membership(db, 1, editor_user["id"])
    db.commit()
    assert db.execute("SELECT role FROM users WHERE id = ?", (editor_user["id"],)).fetchone()[0] == "editor"
    assert libraries.accessible_library_ids(db, editor_user) == []
    portal = _deny_portal(monkeypatch)
    before = _snapshot(db)
    with native_wire("exact") as wire:
        response = _scan(editor_client, catalogue_controls)
        assert response.status_code == 403
        portal.assert_not_called()
        wire.assert_idle()
        assert _snapshot(db) == before


@pytest.mark.parametrize("target", ["inaccessible", "missing"])
def test_native_confirmation_refuses_an_unavailable_target_library_without_provider_or_writes(
    editor_client, db, native_wire, monkeypatch, feature_profile, catalogue_controls, target,
):
    library_id = catalogue_controls["hidden_library"] if target == "inaccessible" else 2**63 - 1
    portal = _deny_portal(monkeypatch)
    before = _snapshot(db)
    with native_wire("exact") as wire:
        response = editor_client.post("/api/periodicals/confirm", data={
            "raw_barcode": BARCODE, "publication_title": TITLE, "issue_number": "42", "library_id": str(library_id),
        }, follow_redirects=False)
        assert response.status_code == 404
        assert response.json() == {"detail": "Library not found"}
        portal.assert_not_called()
        wire.assert_idle()
        assert _snapshot(db) == before
