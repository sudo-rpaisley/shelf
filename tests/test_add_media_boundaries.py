"""Add-owned transport, signed authority and real SQLite lock boundaries.

All requests enter app.main's actual ASGI middleware. Raw streams deliberately
avoid HTTP-client Content-Length correction; they do not bypass auth or CSRF.
"""

import asyncio
import copy
import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
import pytest

from app import auth, config, database, features
from app.database import get_db, set_setting
from app.services import discogs, igdb, music_artwork, music_review, musicbrainz, openlibrary, provider_result
from tests.conftest import _create_user
from tests.test_add_media_search import (
    BOOK, ISBN, MBID, EARLY_ERROR_TEXT, ROUTER_ERROR_TEXT, Forms, catalogue, clock, credentials, error, form, io,
    minimal_error_paragraph, no_cookie, page, pairs, post, preview, release, replace, search, snapshot, value,
)
from tests.test_add_media_save import prepared, receipt, stored, submit


ENDPOINTS = (("/add/search", 8192), ("/add/preview", 8192), ("/add/save", 130000), ("/add/manual", 32768))


async def raw_request(client, method, path, *, body=b"", chunks=None, headers=(), query=b"", root_path="",
                      raw_path=None, header_csrf=True, on_read=None):
    """Call the real app with exact ASGI scope/header/receive bytes and count reads."""
    # httpx builds only the cookie/header envelope. Its URL validator must
    # not consume malformed decoded-path probes before the real ASGI app.
    wire_path = raw_path.decode("ascii") if raw_path is not None else quote(path, safe="/%")
    request = client.build_request(method, "https://testserver" + wire_path, content=b"")
    base = [(name.lower(), val) for name, val in request.headers.raw
            if name.lower() not in {b"content-length", b"content-type"}
            and (header_csrf or name.lower() != b"x-csrf-token")]
    if not any(name.lower() == b"content-type" for name, _ in headers):
        base.append((b"content-type", b"application/x-www-form-urlencoded"))
    base.extend(headers)
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": method, "scheme": "https", "server": ("testserver", 443), "client": ("127.0.0.1", 12345),
        "path": path, "raw_path": raw_path if raw_path is not None else path.encode("utf-8"),
        "root_path": root_path, "query_string": query, "headers": base,
    }
    pieces, reads, sent, finished = list(chunks) if chunks is not None else [body], [], [], asyncio.Event()

    async def receive():
        if len(reads) < len(pieces):
            index = len(reads)
            if on_read is not None:
                answer = on_read(index, scope)
                if asyncio.iscoroutine(answer):
                    await answer
            reads.append(pieces[index])
            return {"type": "http.request", "body": pieces[index], "more_body": len(reads) < len(pieces)}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            finished.set()

    await asyncio.wait_for(client.app(scope, receive, send), 8)
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    response = httpx.Response(starts[0]["status"], headers=starts[0]["headers"],
                              content=b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body"),
                              request=httpx.Request(method, "https://testserver" + wire_path))
    return response, reads


@pytest.fixture
def no_refresh(monkeypatch):
    attempts = []

    def unexpected(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Add-owned response consulted optional auth refresh")

    monkeypatch.setattr(auth, "should_refresh_token", unexpected)
    yield attempts
    assert attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/add/", "/add/unknown", "/add/search/", "/add/preview/", "/add/save/", "/add/manual/",
                                  "/add//search", "/add/search/extra", "/add/../add/unknown"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_unknown_and_slashed_add_namespace_refuses_without_reading_body_or_redirect(
    editor_client, db, io, no_refresh, path, method,
):
    before = snapshot(db)
    response, reads = await raw_request(editor_client, method, path, chunks=[b"unread-body", b"unread-tail"])
    assert response.status_code == 404 and "location" not in response.headers
    no_cookie(response)
    assert reads == [] and io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method,allow", [("/add", "POST", {"GET"}), ("/add", "DELETE", {"GET"}),
    ("/add/search", "GET", {"POST"}), ("/add/preview", "GET", {"POST"}),
    ("/add/save", "PUT", {"POST"}), ("/add/manual", "PATCH", {"POST"})])
async def test_known_wrong_method_has_allow_and_no_body_work(editor_client, db, io, no_refresh, path, method, allow):
    before = snapshot(db)
    response, reads = await raw_request(editor_client, method, path, body=b"must-not-be-read")
    assert response.status_code == 405
    assert {part.strip() for part in response.headers["allow"].split(",")} == allow
    assert "location" not in response.headers and reads == [] and io.calls == []
    no_cookie(response)
    assert snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/shelf/add", "/add"])
async def test_framework_root_path_both_supported_representations_render_same_owned_host(editor_client, io, no_refresh, path):
    response, reads = await raw_request(editor_client, "GET", path, root_path="/shelf")
    assert response.status_code == 200 and 'data-testid="add-media"' in response.text
    selected = form(response, "add-media-search")
    assert urlsplit(selected["attrs"]["action"]).path == "/shelf/add/search"
    no_cookie(response)
    assert reads == [] and io.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,root", [("/adder", ""), ("/ADD", ""), ("/add/browse", "/add"),
                                       ("/shelf/adder", "/shelf"), ("/shelf/ADD", "/shelf")])
async def test_similar_and_external_mounted_paths_are_not_claimed_by_add(editor_client, io, monkeypatch, path, root):
    refresh = []
    monkeypatch.setattr(auth, "should_refresh_token", lambda *args: refresh.append(True))
    response, reads = await raw_request(editor_client, "GET", path, root_path=root)
    assert response.status_code == (200 if path == "/add/browse" else 404)
    assert 'data-testid="add-media"' not in response.text
    assert refresh == [True], "Ordinary unowned responses must keep the existing auth-refresh policy"
    assert reads == [] and io.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decoded,raw,status", [("/add/search", b"/%61dd/search", 405),
    ("/add/search", b"/add%2Fsearch", 405), ("/add//search", b"/add/%2Fsearch", 404),
    ("/add/%invalid", b"/add/%invalid", 404), ("/add/\x00", b"/add/%00", 404)])
async def test_encoded_paths_follow_framework_effective_namespace_without_slash_redirect(
    editor_client, io, no_refresh, decoded, raw, status,
):
    response, reads = await raw_request(editor_client, "GET", decoded, raw_path=raw, body=b"unread")
    assert response.status_code == status and "location" not in response.headers
    no_cookie(response)
    assert reads == [] and io.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,cap", ENDPOINTS)
@pytest.mark.parametrize("header_csrf", [False, True])
@pytest.mark.parametrize("length", [None, b"0", b"1"])
async def test_all_post_stream_caps_precede_csrf_and_stop_before_unread_tail(
    editor_client, db, io, no_refresh, path, cap, header_csrf, length,
):
    prefix = b"_csrf=" + editor_client.cookies.get("csrf_token").encode() + b"&q="
    first = prefix + b"x" * (cap - len(prefix))
    headers = [] if length is None else [(b"content-length", length)]
    before = snapshot(db)
    response, reads = await raw_request(editor_client, "POST", path, chunks=[first, b"x", b"MUST-NOT-BE-READ"],
                                        headers=headers, header_csrf=header_csrf)
    error(response, 413, "body_too_large")
    assert reads == [first, b"x"] and io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path,cap", ENDPOINTS)
async def test_exact_stream_cap_is_not_misreported_as_oversize(editor_client, db, catalogue, no_refresh, path, cap):
    # The payload is invalid *input* (or a wrong key for this endpoint), but
    # has valid wire syntax and is exactly the transport cap, not cap+1.
    selected, values = valid_operation(editor_client, path)
    name = {"/add/search": "q", "/add/preview": "selection", "/add/save": "ticket", "/add/manual": "title"}[path]
    base = urlencode(replace(values, **{name: ""})).encode()
    raw = urlencode(replace(values, **{name: "x" * (cap - len(base))})).encode()
    assert len(raw) == cap
    catalogue.clear()
    response, reads = await raw_request(editor_client, "POST", path, body=raw)
    assert response.status_code == (422 if path in {"/add/search", "/add/manual"} else 400)
    assert "body_too_large" not in response.text and reads == [raw] and catalogue.calls == []
    no_cookie(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("header_csrf", [False, True])
async def test_oversize_preflight_runs_even_without_csrf_cookie(editor_client, db, io, header_csrf):
    page(editor_client)
    editor_client.cookies.delete("csrf_token")
    response, reads = await raw_request(editor_client, "POST", "/add/search", body=b"q=" + b"x" * 8192,
                                        header_csrf=header_csrf)
    error(response, 413, "body_too_large")
    assert len(reads) == 1 and io.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,status,code", [
    ([(b"content-type", b"application/json")], 415, "unsupported_media_type"),
    ([(b"content-type", b"multipart/form-data; boundary=x")], 415, "unsupported_media_type"),
    ([(b"content-type", b"application/x-www-form-urlencoded; charset=latin-1")], 415, "unsupported_media_type"),
    ([(b"content-encoding", b"gzip")], 415, "unsupported_media_type"),
    ([(b"content-length", b"-1")], 400, "invalid_form"),
    ([(b"content-length", b"+1")], 400, "invalid_form"),
    ([(b"content-length", b"1, 1")], 400, "invalid_form"),
    ([(b"content-length", b"banana")], 400, "invalid_form"),
    ([(b"content-length", b"9223372036854775808")], 400, "invalid_form"),
    ([(b"content-length", b"1"), (b"content-length", b"1")], 400, "invalid_form"),
    ([(b"content-type", b"application/x-www-form-urlencoded"), (b"content-type", b"application/x-www-form-urlencoded")], 400, "invalid_form"),
    ([(b"content-encoding", b"identity"), (b"content-encoding", b"identity")], 400, "invalid_form"),
    ([(b"x-csrf-token", b"a"), (b"x-csrf-token", b"b")], 400, "invalid_form"),
])
async def test_critical_header_and_content_type_grammar_rejects_before_body(
    editor_client, db, io, no_refresh, headers, status, code,
):
    before = snapshot(db)
    response, reads = await raw_request(editor_client, "POST", "/add/search", body=b"unread", headers=headers,
                                        header_csrf=not any(key == b"x-csrf-token" for key, _ in headers))
    error(response, status, code)
    # headers8 is valid decimal grammar, not a SQLite-ID range check. Its
    # actual short body is bounded/read once before strict form rejection.
    expected_reads = [b"unread"] if headers == [(b"content-length", b"9223372036854775808")] else []
    assert reads == expected_reads and io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("suffix", [b"&q=second", b"&%71=second", b"&_csrf=second", b"&owned=0", b"&image=-1",
    b"&item_id=0", b"&url=https%3A%2F%2Fevil.test", b"&provider_facts=forged", b"&unknown=1",
    b"&bad=%", b"&bad=%0", b"&bad=%GG", b"&q=%FF", b"&q=%C0%AF", b"&q=%ED%A0%80",
    b"&q=%00", b"&q=%0D%0A", b"&q=%7F", b"&q=%09", b"&q=raw\xc3\xa8", b"&=value", b"&empty",
])
@pytest.mark.parametrize("header_csrf", [False, True])
def test_strict_wire_parser_rejects_duplicates_unknowns_utf8_controls_and_raw_nonascii(
    editor_client, db, io, no_refresh, suffix, header_csrf,
):
    selected = form(page(editor_client), "add-media-search")
    values = replace(pairs(selected), mode="add", media_type="book", method="title", provider="auto", q="Dune")
    # Keep each malformed-value probe decisive: a duplicate/unknown key must
    # not hide a decoder that silently replaces bad UTF-8 or percent escapes.
    if suffix.startswith(b"&bad=") or (suffix.startswith(b"&q=") and suffix != b"&q=second"):
        values = replace(values, q=None)
        suffix = suffix.replace(b"&bad=", b"&q=", 1)
    body = urlencode(values).encode() + suffix
    if not header_csrf:
        editor_client.headers.pop("X-CSRF-Token")
    before = snapshot(db)
    response = editor_client.post("/add/search", content=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    error(response, 400, "invalid_form")
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("header_csrf", [False, True])
async def test_native_percent_utf8_form_replays_exact_accepted_bytes_through_real_csrf(
    editor_client, db, io, no_refresh, header_csrf,
):
    selected = form(page(editor_client), "add-media-search")
    query = "Caffè 日本 + % & ="
    body = urlencode(replace(pairs(selected), mode="add", media_type="book", method="title", provider="auto", q=query)).encode("ascii")
    io.install(openlibrary, "search_books", provider_result.found("openlibrary", [{"title": "A real result", "isbn": ISBN}]))
    before = snapshot(db)
    chunks = [body[:17], body[17:77], body[77:]]
    response, reads = await raw_request(editor_client, "POST", "/add/search", chunks=chunks, header_csrf=header_csrf,
                                        headers=[(b"content-type", b"application/x-www-form-urlencoded; charset=UTF-8")])
    assert response.status_code == 200 and Forms(response.text).named("add-media-preview")
    assert reads == chunks and len(io.calls) == 1 and io.seen(openlibrary, "search_books")[0][0][0] == query
    assert snapshot(db) == before
    no_cookie(response)


@pytest.mark.parametrize("header,field,success", [(None, "correct", True), ("correct", "wrong", True),
    ("correct", "correct", True), ("wrong", "correct", False), (None, None, False)])
def test_old_csrf_header_precedence_and_native_field_semantics_are_preserved(editor_client, io, no_refresh, header, field, success):
    selected = form(page(editor_client), "add-media-search")
    token = editor_client.cookies.get("csrf_token")
    body = replace(pairs(selected), media_type="book", method="title", provider="auto", q="Dune",
                   _csrf=token if field == "correct" else field)
    editor_client.headers.pop("X-CSRF-Token")
    if header:
        editor_client.headers["X-CSRF-Token"] = token if header == "correct" else "wrong"
    if success:
        io.install(openlibrary, "search_books", provider_result.found("openlibrary", [{"title": "Dune", "isbn": ISBN}]))
    response = post(editor_client, "/add/search", body)
    assert response.status_code == (200 if success else 403)
    no_cookie(response)
    assert len(io.calls) == int(success)


@pytest.mark.parametrize("query,status,code", [
    ("q=Dune", 400, "invalid_form"), ("mode=add&mode=wishlist", 400, "invalid_form"),
    ("media_type=kids_book", 422, "invalid_input"), ("mode=invalid", 422, "invalid_input"),
    ("mode=add&media_type=book&receipt=x", 400, "invalid_form"), ("bad=%FF", 400, "invalid_form"),
    ("x=" + "x" * 4095, 414, "query_too_large"),
])
def test_get_query_is_bounded_closed_and_never_a_lookup(editor_client, db, io, no_refresh, query, status, code):
    before = snapshot(db)
    response = editor_client.get("/add?" + query, follow_redirects=False)
    error(response, status, code)
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("path,_cap", ENDPOINTS)
def test_post_query_is_always_empty(editor_client, db, catalogue, no_refresh, path, _cap):
    selected, values = valid_operation(editor_client, path)
    before = snapshot(db)
    catalogue.clear()
    response = post(editor_client, path + "?mode=add", values)
    error(response, 400, "invalid_form")
    assert catalogue.calls == [] and snapshot(db) == before


def valid_operation(client, path):
    """A successful-control set so negative tests cannot pass on missing context."""
    if path == "/add/search":
        selected = form(page(client), "add-media-search")
        values = replace(pairs(selected), mode="add", media_type="book", method="title", provider="auto", q="Dune")
    elif path == "/add/preview":
        selected = form(search(client), "add-media-preview")
        values = pairs(selected)
    elif path == "/add/save":
        selected = prepared(client)
        values = pairs(selected)
    else:
        selected = prepared(client, "manual")
        values = replace(pairs(selected), title="A valid manual item", identifier_kind="none", identifier="")
    return selected, values


@pytest.mark.parametrize("path,allowed", [
    ("/add/search", {"_csrf", "context", "mode", "media_type", "method", "provider", "q", "artist", "title_hint", "platform", "country"}),
    ("/add/preview", {"_csrf", "context", "selection"}),
    ("/add/save", {"_csrf", "context", "ticket", "location_id", "tags", "reading_status", "fields"}),
    ("/add/manual", {"_csrf", "context", "title", "authors", "publisher", "publish_year", "platform", "series_name", "language",
                      "identifier_kind", "identifier", "location_id", "tags", "reading_status"}),
])
@pytest.mark.parametrize("extra", ["owned", "image", "item_id", "media_type", "mode", "url", "upload", "shop_url", "candidate"])
def test_endpoint_exact_key_allowlists_not_legacy_owned_image_or_unsigned_metadata(
    editor_client, db, catalogue, no_refresh, path, allowed, extra,
):
    # Search legitimately owns mode/media_type; replace with a truly unknown
    # control there instead of accidentally asserting an allowed key is banned.
    if extra in allowed:
        extra = "unsigned_" + extra
    selected, values = valid_operation(editor_client, path)
    before = snapshot(db)
    catalogue.clear()
    response = post(editor_client, path, values + [(extra, "1")])
    error(response, 400, "invalid_form")
    assert catalogue.calls == [] and snapshot(db) == before


def context_free(response, *, phase):
    import re

    assert phase in {"router", "capture"}
    error(response, 401, "actor_changed")
    assert getattr(response, "context", None) is None, "Context-free refusals are not full Add templates"
    for header in ("location", "refresh", "hx-redirect", "hx-trigger"):
        assert header not in response.headers
    if phase == "capture":
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"code": "actor_changed", "message": EARLY_ERROR_TEXT["actor_changed"]}
    else:
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        message = minimal_error_paragraph(response, "actor_changed")
        for private_fact in ("editor", "admin", "viewer", "meat loaf", "dune", "musicbrainz", "discogs",
                             "selected movie", "selected game", "provider track fact", "canary", "access_token", "csrf_token",
                             "ticket=", "selection=", "receipt="):
            assert private_fact not in message.casefold(), private_fact
        assert not re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", message)
    assert "<nav" not in response.text and "<form" not in response.text


def change_actor(user_id, change, clock):
    if change == "expire":
        clock[0] += 61
        return
    with get_db() as db:
        if change == "demote":
            db.execute("UPDATE users SET role='viewer' WHERE id=?", (user_id,))
        elif change == "revoke":
            db.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (user_id,))
        elif change == "delete":
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
        elif change == "rename":
            db.execute("UPDATE users SET username='replacement-identity' WHERE id=?", (user_id,))
        else:
            raise AssertionError(change)


def short_session(client, user, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(auth, "JWT_EXPIRY_SECONDS", 60)
        token = auth.create_token(user["id"], user["username"], user["role"], user["display_name"])
    client.cookies.set("access_token", token)
    return token


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["demote", "revoke", "delete", "rename", "expire"])
@pytest.mark.parametrize("header_csrf", [False, True])
async def test_entry_actor_is_captured_before_stream_and_never_replaced_after_body_await(
    editor_client, editor_user, db, io, clock, monkeypatch, no_refresh, change, header_csrf,
):
    token = short_session(editor_client, editor_user, monkeypatch)
    selected = form(page(editor_client), "add-media-search")
    body = urlencode(replace(pairs(selected), media_type="book", method="title", provider="auto", q="Dune")).encode()
    context_module = importlib.import_module("app.add_media_context")
    original, captured = context_module.capture_actor, []

    def capture(request, user):
        actor = original(request, user)
        captured.append((request, actor))
        return actor

    monkeypatch.setattr(context_module, "capture_actor", capture)
    before = snapshot(db)

    def during_read(index, scope):
        assert len(captured) == 1, "Actor must be frozen before the FIRST body receive"
        request, actor = captured[0]
        assert asdict(actor) == {
            "id": editor_user["id"], "username": editor_user["username"], "role": "editor",
            "token_version": 1, "expires_at": jwt.decode(token, options={"verify_signature": False})["exp"],
        }
        if index == 1:
            change_actor(editor_user["id"], change, clock)
            # A fresh request-state dict is not a replacement capability.
            request.state.user = {**request.state.user, "role": "admin", "token_version": 2}

    response, reads = await raw_request(editor_client, "POST", "/add/search", chunks=[body[:31], body[31:]],
                                        header_csrf=header_csrf, on_read=during_read)
    context_free(response, phase="router")
    assert len(captured) == 1 and len(reads) == 2 and io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["search", "preview"])
@pytest.mark.parametrize("change", ["demote", "revoke", "delete", "rename", "expire"])
async def test_original_actor_revalidated_after_actual_provider_await_before_disclosure_or_signing(
    editor_client, editor_user, db, catalogue, clock, monkeypatch, no_refresh, stage, change,
):
    short_session(editor_client, editor_user, monkeypatch)
    if stage == "search":
        selected = form(page(editor_client), "add-media-search")
        body = replace(pairs(selected), media_type="book", method="title", provider="auto", q="Dune")
        module, name = openlibrary, "search_books"
        answer = provider_result.found("openlibrary", [{"title": "Must not disclose provider title", "isbn": ISBN}])
    else:
        selected = form(search(editor_client), "add-media-preview")
        body = pairs(selected)
        module, name = openlibrary, "lookup"
        answer = provider_result.found("openlibrary", {**BOOK, "title": "Must not disclose provider title"})
    entered, resume, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def lookup(*args, **kwargs):
        entered.set()
        try:
            await resume.wait()
            return answer
        finally:
            exited.set()

    catalogue.install(module, name, handler=lookup)
    catalogue.clear()
    before = snapshot(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_client.app), base_url="https://testserver",
                                cookies=editor_client.cookies, headers=dict(editor_client.headers)) as client:
        task = asyncio.create_task(client.post(selected["attrs"]["action"], content=urlencode(body).encode(),
                                              headers={"Content-Type": "application/x-www-form-urlencoded"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            change_actor(editor_user["id"], change, clock)
            resume.set()
            response = await asyncio.wait_for(task, 5)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    context_free(response, phase="router")
    assert exited.is_set() and "Must not disclose provider title" not in response.text
    assert len(catalogue.calls) == 1 and snapshot(db) == before


@pytest.mark.parametrize("stage", ["search", "preview", "save", "manual"])
def test_actor_a_form_dispatched_with_b_cookie_is_context_free_without_io_or_write(
    editor_client, editor_user, db, catalogue, no_refresh, stage,
):
    if stage == "search":
        selected = form(page(editor_client), "add-media-search")
        body = replace(pairs(selected), media_type="book", method="title", provider="auto", q="Dune")
    elif stage == "preview":
        selected = form(search(editor_client), "add-media-preview")
        body = pairs(selected)
    else:
        selected = prepared(editor_client, "manual" if stage == "manual" else "book")
        body = replace(pairs(selected), title="Manual title", identifier_kind="none", identifier="") if stage == "manual" else pairs(selected)
    other = _create_user("other-editor", "password123", "Other Editor", "editor")
    editor_client.cookies.set("access_token", auth.create_token(other["id"], other["username"], other["role"], other["display_name"]))
    before = snapshot(db)
    catalogue.clear()
    response = post(editor_client, selected["attrs"]["action"], body)
    context_free(response, phase="router")
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("family", ["book", "dvd", "video_game", "manual", "cassette"])
@pytest.mark.parametrize("change", ["demote", "revoke", "delete", "expire"])
def test_actor_changed_while_waiting_for_real_begin_immediate_cannot_write(
    editor_client, editor_user, db, catalogue, clock, monkeypatch, no_refresh, family, change,
):
    short_session(editor_client, editor_user, monkeypatch)
    selected = prepared(editor_client, family)
    changes = {"fields": ["title"]} if family == "cassette" else {}
    if family == "manual":
        changes.update(title="Must not be saved", identifier_kind="none", identifier="")
    before = snapshot(db)
    catalogue.clear()
    reached, acquired = threading.Event(), threading.Event()
    original = database._Connection.execute

    def execute(conn, sql, *args, **kwargs):
        is_begin = sql.strip().upper() == "BEGIN IMMEDIATE"
        if is_begin:
            reached.set()
        result = original(conn, sql, *args, **kwargs)
        if is_begin:
            acquired.set()
        return result

    monkeypatch.setattr(database._Connection, "execute", execute)
    rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
    rival.execute("PRAGMA foreign_keys=ON")
    rival.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(submit, editor_client, selected, **changes)
            try:
                assert reached.wait(5), "Native save never attempted its actual writer lock"
                assert not acquired.is_set(), "Writer lock was not really held by the rival"
                if change == "expire":
                    clock[0] += 61
                elif change == "demote":
                    rival.execute("UPDATE users SET role='viewer' WHERE id=?", (editor_user["id"],))
                elif change == "revoke":
                    rival.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (editor_user["id"],))
                else:
                    rival.execute("DELETE FROM users WHERE id=?", (editor_user["id"],))
            finally:
                rival.commit()
            response = pending.result(timeout=8)
    finally:
        rival.close()
    context_free(response, phase="router")
    assert acquired.is_set() and snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("family", ["book", "dvd", "video_game", "manual", "cassette"])
def test_native_identity_guard_and_actor_recheck_are_under_real_writer_lock_before_first_dml(
    editor_client, editor_user, db, catalogue, monkeypatch, family,
):
    selected = prepared(editor_client, family)
    changes = {"fields": ["title"]} if family == "cassette" else {}
    if family == "manual":
        changes.update(title="Locked manual", identifier_kind="isbn", identifier=ISBN)
    original = database._Connection.execute
    guards, actor_checks, writes, written = [], [], [], set()

    def probe():
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            try:
                rival.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).casefold()
                return True
            rival.rollback()
            return False
        finally:
            rival.close()

    def execute(conn, sql, *args, **kwargs):
        normalized = " ".join(sql.upper().split())
        prewrite = conn not in written
        if prewrite and normalized.startswith("SELECT"):
            identity = (("ITEMS_LIVE" in normalized and ("ISBN =" in normalized or "TITLE =" in normalized))
                        or "FROM MUSIC_RELEASES" in normalized or "FROM MUSIC_IDENTIFIERS" in normalized)
            if identity:
                guards.append(bool(conn.in_transaction and probe()))
            if "FROM USERS" in normalized and conn.in_transaction:
                actor_checks.append(probe())
        if normalized.startswith(("INSERT INTO ITEMS ", "UPDATE ITEMS SET")):
            writes.append(bool(conn.in_transaction and probe()))
            written.add(conn)
        return original(conn, sql, *args, **kwargs)

    monkeypatch.setattr(database._Connection, "execute", execute)
    response = submit(editor_client, selected, **changes)
    assert response.status_code == 303, (response.status_code, response.text[:500])
    assert guards and guards[-1] is True, "No decisive locked pre-DML identity guard was observed"
    assert actor_checks and all(actor_checks), "Original actor must be checked after acquiring the writer lock"
    assert writes and all(writes), "Initial ownership and data must ride the locked native write"


@pytest.mark.parametrize("stage", ["preview", "save"])
@pytest.mark.parametrize("changed", ["mode", "media_type", "method", "provider", "q", "artist", "title_hint", "country"])
def test_real_selected_and_review_tickets_reject_different_signed_intent_context(
    editor_client, db, catalogue, no_refresh, stage, changed,
):
    initial = {"mode": "add", "media_type": "cassette", "method": "catalog_number", "provider": "discogs",
               "q": "4026156", "artist": "Meat Loaf", "title_hint": "Hits Out of Hell", "country": "GB"}
    first = search(editor_client, **initial)
    selected = form(first, "add-media-preview") if stage == "preview" else form(preview(editor_client, first), "add-media-save")
    altered = {"mode": "wishlist", "media_type": "vinyl", "method": "title", "provider": "auto", "q": "Other catalogue",
               "artist": "Another artist", "title_hint": "Another title", "country": "US"}
    next_intent = {**initial, changed: altered[changed]}
    if changed == "method":
        next_intent["title_hint"] = ""
    if changed == "media_type":
        catalogue.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", "vinyl")]))
    second = search(editor_client, **next_intent)
    other_context = value(form(second, "add-media-preview"), "context")
    before = snapshot(db)
    catalogue.clear()
    response = submit(editor_client, selected, context=other_context, **({"fields": ["title"]} if stage == "save" else {}))
    error(response, 409, "intent_changed")
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("stage", ["selection", "review"])
@pytest.mark.parametrize("mutation", ["tamper", "wrong_purpose", "expired"])
def test_actual_ticket_producers_are_tamper_purpose_and_expiry_bound(
    editor_client, db, catalogue, clock, no_refresh, stage, mutation,
):
    searched = search(editor_client)
    selected = form(searched, "add-media-preview") if stage == "selection" else form(preview(editor_client, searched), "add-media-save")
    name = "selection" if stage == "selection" else "ticket"
    token = value(selected, name)
    if mutation == "tamper":
        token = token[:-8] + ("A" if token[-8] != "A" else "B") + token[-7:]
    elif mutation == "wrong_purpose":
        token = value(form(page(editor_client), "add-media-search"), "context")
    else:
        clock[0] += 901
        # A fresh, real marker for the SAME intent isolates ticket expiry
        # from marker expiry. The long-lived original cookie remains live.
        fresh = form(search(editor_client), "add-media-preview")
        selected = {**selected, "controls": [dict(control) for control in selected["controls"]]}
        for control in selected["controls"]:
            if control.get("name") == "context":
                control["value"] = value(fresh, "context")
    before = snapshot(db)
    catalogue.clear()
    response = submit(editor_client, selected, **{name: token})
    error(response, 409, "stale_review")
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("mutation", ["candidate_extra", "intent_extra", "intent_wrong_type"])
def test_public_producer_signed_but_nonclosed_music_review_schema_is_refused(
    editor_client, editor_user, db, catalogue, no_refresh, mutation,
):
    selected = prepared(editor_client, "cassette", provider="discogs")
    payload = music_review.verify(value(selected, "ticket"), user_id=editor_user["id"], purpose="shelf-add-music-review-v1")
    candidate = copy.deepcopy(payload["candidate"])
    if mutation == "candidate_extra":
        candidate["unsigned_owned_override"] = 0
    elif mutation == "intent_extra":
        candidate["add_media_intent"]["owned"] = 0
    else:
        candidate["add_media_intent"]["mode"] = ["add"]
    malformed = music_review.sign(candidate, user_id=editor_user["id"], item_id=0, fingerprint="new", purpose="shelf-add-music-review-v1")
    before = snapshot(db)
    catalogue.clear()
    error(submit(editor_client, selected, ticket=malformed, fields=["title"]), 409, "stale_review")
    assert snapshot(db) == before and catalogue.calls == []


def test_new_and_legacy_music_review_purposes_reject_each_other_using_real_producers(editor_client, db, catalogue):
    selected = prepared(editor_client, "cassette", provider="musicbrainz")
    new_ticket = value(selected, "ticket")
    catalogue.install(music_artwork, "release_candidates", provider_result.no_match("coverartarchive"))
    legacy = editor_client.post("/api/music/preview", data={"provider": "musicbrainz", "release_id": MBID, "format": "cassette"})
    assert legacy.status_code == 200
    tickets = [control["value"] for native in Forms(legacy.text).forms for control in native["controls"] if control.get("name") == "ticket"]
    assert len(tickets) == 1
    before = snapshot(db)
    catalogue.clear()
    error(submit(editor_client, selected, ticket=tickets[0], fields=["title"]), 409, "stale_review")
    # Legacy retains its exact fragment/error contract, not new-host status codes.
    old_response = editor_client.post("/api/music/apply-match", data={"ticket": new_ticket, "fields": "title", "media_type": "cassette"},
                                      follow_redirects=False)
    assert old_response.status_code == 200 and 'role="alert"' in old_response.text
    assert "location" not in old_response.headers and snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("field,invalid", [
    ("title", ""), ("title", "x" * 201), ("authors", "x" * 201), ("publisher", "x" * 201),
    ("series_name", "x" * 201), ("language", "x" * 36), ("publish_year", "0000"),
    ("publish_year", "10000"), ("publish_year", "+2024"), ("publish_year", "２０２４"), ("publish_year", "24"),
    ("identifier_kind", "catalog_number"), ("identifier", "x" * 33),
    ("tags", "x" * 257), ("reading_status", "finished"), ("location_id", "0"),
    ("location_id", "-1"), ("location_id", "9223372036854775808"), ("platform", "not-configured"),
])
def test_manual_literal_value_grammar_never_reaches_catalogue_write(editor_client, db, io, no_refresh, field, invalid):
    selected = prepared(editor_client, "manual")
    changes = {"title": "Valid title", "identifier_kind": "none", "identifier": "", field: invalid}
    before = snapshot(db)
    error(submit(editor_client, selected, **changes), 422, "invalid_input")
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("identifier_kind,identifier", [("none", "4026156"), ("isbn", "0441172710"),
    ("isbn", "ISBN9780441172719"), ("barcode", ISBN), ("barcode", "4026156"), ("barcode", "078073003500")])
def test_manual_identifier_kind_is_explicit_and_uses_strict_original_identifier_validation(
    editor_client, db, io, identifier_kind, identifier,
):
    selected = prepared(editor_client, "manual")
    before = snapshot(db)
    error(submit(editor_client, selected, title="Manual identifier refusal", identifier_kind=identifier_kind, identifier=identifier), 422, "invalid_input")
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("fields", [["title", "title"], ["title"] * 7])
def test_music_choice_multiplicity_and_six_field_limit_are_structural_refusals(editor_client, db, catalogue, fields):
    selected = prepared(editor_client, "cassette")
    before = snapshot(db)
    catalogue.clear()
    error(submit(editor_client, selected, fields=fields), 400, "invalid_form")
    assert snapshot(db) == before and catalogue.calls == []


def test_music_choices_must_be_candidate_present_and_status_inapplicable(editor_client, db, catalogue):
    selected = prepared(editor_client, "cassette")
    before = snapshot(db)
    catalogue.clear()
    error(submit(editor_client, selected, fields=["title", "description"]), 422, "invalid_input")
    error(submit(editor_client, selected, fields=["title"], reading_status="reading"), 422, "invalid_input")
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("stage,name,limit", [("search", "context", 4096), ("preview", "selection", 6000), ("save", "ticket", 120000)])
def test_opaque_ascii_token_literal_bounds_do_not_expand_native_limits(editor_client, db, catalogue, stage, name, limit):
    if stage == "search":
        selected = form(page(editor_client), "add-media-search")
    elif stage == "preview":
        selected = form(search(editor_client), "add-media-preview")
    else:
        selected = prepared(editor_client)
    before = snapshot(db)
    catalogue.clear()
    response = submit(editor_client, selected, **{name: "x" * (limit + 1)})
    error(response, 400, "invalid_form")
    assert snapshot(db) == before and catalogue.calls == []


def test_form_pair_budget_and_decoded_key_duplicates_are_not_parser_last_wins(editor_client, db, io):
    selected = form(page(editor_client), "add-media-search")
    body = pairs(selected) + [("q", str(index)) for index in range(33)]
    before = snapshot(db)
    error(post(editor_client, selected["attrs"]["action"], body), 400, "invalid_form")
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("stage", ["search", "preview", "save"])
def test_music_disabled_before_operation_has_no_provider_or_write(editor_client, db, catalogue, stage):
    if stage == "search":
        selected = form(page(editor_client), "add-media-search")
        body = replace(pairs(selected), media_type="cassette", method="title", provider="discogs", q="Hits Out of Hell")
    elif stage == "preview":
        selected = form(search(editor_client, media_type="cassette", provider="discogs"), "add-media-preview")
        body = pairs(selected)
    else:
        selected = prepared(editor_client, "cassette", provider="discogs")
        body = replace(pairs(selected), fields=["title"])
    with get_db() as writer:
        features.set_feature_enabled(writer, "music", False)
    features.invalidate_cache()
    before = snapshot(db)
    catalogue.clear()
    response = post(editor_client, selected["attrs"]["action"], body)
    assert response.status_code == 403
    no_cookie(response)
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["search", "preview"])
async def test_music_disabled_while_provider_is_awaited_discards_details_and_cannot_write(editor_client, db, catalogue, stage):
    if stage == "search":
        selected = form(page(editor_client), "add-media-search")
        body = replace(pairs(selected), media_type="cassette", method="title", provider="discogs", q="Hits Out of Hell")
        name, answer = "search_public_releases", provider_result.found("discogs", [release("discogs")])
    else:
        selected = form(search(editor_client, media_type="cassette", provider="discogs"), "add-media-preview")
        body = pairs(selected)
        name, answer = "lookup_public_release", provider_result.found("discogs", release("discogs"))
    entered, resume, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        try:
            await resume.wait()
            return answer
        finally:
            exited.set()

    catalogue.install(discogs, name, handler=pending)
    catalogue.clear()
    before = snapshot(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_client.app), base_url="https://testserver",
                                cookies=editor_client.cookies, headers=dict(editor_client.headers)) as client:
        task = asyncio.create_task(client.post(selected["attrs"]["action"], content=urlencode(body).encode(),
                                              headers={"Content-Type": "application/x-www-form-urlencoded"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with get_db() as writer:
                features.set_feature_enabled(writer, "music", False)
            features.invalidate_cache()
            resume.set()
            response = await asyncio.wait_for(task, 5)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert response.status_code == 403 and exited.is_set()
    assert "Provider track fact" not in response.text
    no_cookie(response)
    assert snapshot(db) == before and len(catalogue.calls) == 1


def test_marker_expiry_is_not_renewed_by_a_new_login_cookie(editor_client, editor_user, db, io, clock, no_refresh):
    selected = form(page(editor_client), "add-media-search")
    clock[0] += 901
    editor_client.cookies.set("access_token", auth.create_token(editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"]))
    before = snapshot(db)
    response = submit(editor_client, selected, media_type="book", method="title", provider="auto", q="Dune")
    error(response, 409, "stale_review")
    assert snapshot(db) == before and io.calls == []


def test_casefold_username_auth_compatibility_uses_the_actual_row_identity(editor_client, editor_user, io, no_refresh):
    editor_client.cookies.set("access_token", auth.create_token(editor_user["id"], editor_user["username"].upper(), editor_user["role"], editor_user["display_name"]))
    response = page(editor_client)
    assert value(form(response, "add-media-search"), "context") and io.calls == []
    no_cookie(response)


@pytest.mark.parametrize("path,method", [("/add", "get"), ("/add/search", "post"), ("/add/preview", "post"),
                                          ("/add/save", "post"), ("/add/manual", "post")])
def test_viewer_native_redirect_preserves_auth_policy_without_provider_or_write(viewer_client, db, io, no_refresh, path, method):
    before = snapshot(db)
    if method == "post":
        response = post(viewer_client, path, [("_csrf", viewer_client.cookies.get("csrf_token"))])
    else:
        response = viewer_client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/browse"
    no_cookie(response)
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("scenario,target,status", [("setup", "/setup", 303), ("login", "/login", 303), ("bounce", None, 200)])
def test_anonymous_setup_login_and_cross_site_bounce_are_owned_no_store_without_cookie_refresh(
    client, db, io, no_refresh, scenario, target, status,
):
    if scenario != "setup":
        _create_user("existing-editor", "password123", "Editor", "editor")
    headers = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site"} if scenario == "bounce" else {}
    response = client.get("/add", headers=headers, follow_redirects=False)
    assert response.status_code == status
    if target:
        assert response.headers["location"] == target
    else:
        assert response.headers["refresh"] == "0;url=/add"
    no_cookie(response)
    assert io.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,status,allow", [("/add", 405, "GET"), ("/add/search", 405, "POST"),
                                               ("/add/unknown", 404, None), ("/add/", 404, None)])
async def test_owned_head_refusals_send_no_response_body_or_read_request_body(editor_client, io, no_refresh, path, status, allow):
    response, reads = await raw_request(editor_client, "HEAD", path, body=b"must-not-read")
    assert response.status_code == status and response.content == b""
    if allow:
        assert response.headers["allow"] == allow
    no_cookie(response)
    assert reads == [] and io.calls == []


def test_handled_add_exception_is_fixed_no_store_and_never_reflects_or_logs_exception_canary(
    editor_client, db, io, monkeypatch, no_refresh, caplog,
):
    selected = form(page(editor_client), "add-media-search")
    canary = "ADD_PROVIDER_EXCEPTION_MUST_NOT_ESCAPE_81f79"

    async def fail(*args, **kwargs):
        raise RuntimeError(canary)

    io.install(openlibrary, "search_books", handler=fail)
    before = snapshot(db)
    response = submit(editor_client, selected, media_type="book", method="title", provider="auto", q="Dune")
    error(response, 500, "outcome_unknown")
    assert canary not in response.text and canary not in caplog.text
    assert snapshot(db) == before and len(io.calls) == 1


def test_add_uses_existing_ip_bucket_narrowly_and_retains_disable_switch(editor_client, io, monkeypatch):
    # Build the actual middleware stack while the fixture's disable switch is on.
    page(editor_client)
    from app.main import RateLimitMiddleware
    current = editor_client.app.middleware_stack
    while not isinstance(current, RateLimitMiddleware):
        current = current.app
    monkeypatch.setattr(current, "_hits", type(current._hits)())
    monkeypatch.delenv("SHELF_DISABLE_RATE_LIMIT")
    for _ in range(59):
        response = editor_client.get("/api/title-search", params={"q": ""})
        assert response.status_code == 200
    response = editor_client.get("/add", follow_redirects=False)
    assert response.status_code == 200
    no_cookie(response)
    refused = editor_client.get("/add", follow_redirects=False)
    assert refused.status_code == 429
    no_cookie(refused)
    assert editor_client.get("/api/title-search", params={"q": ""}).status_code == 429
    # /adder is unowned and must not draw on this full API/Add bucket.
    assert editor_client.get("/adder", follow_redirects=False).status_code == 404
    monkeypatch.setenv("SHELF_DISABLE_RATE_LIMIT", "1")
    assert editor_client.get("/add", follow_redirects=False).status_code == 200
    assert io.calls == []


def issued_session_with_claim_changes(user, monkeypatch, *, changes=None, omitted=(), token_version=1):
    """Instrument only create_token's encode call; forward its key opaquely.

    This is the authorised malformed/legacy JWT producer seam. No key is
    inspected, copied into evidence, retained, logged, or read from storage.
    The real public auth producer and real JWT encoder still issue the cookie.
    """
    real_encode, issued = auth.jwt.encode, []

    def encode(payload, *opaque_args, **opaque_kwargs):
        amended = dict(payload)
        for name in omitted:
            amended.pop(name, None)
        amended.update(changes or {})
        issued.append(True)
        return real_encode(amended, *opaque_args, **opaque_kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(auth.jwt, "encode", encode)
        token = auth.create_token(user["id"], user["username"], user["role"], user["display_name"], token_version)
    assert issued == [True]
    return token


@pytest.mark.parametrize("case,expected", [("native_tv1", 200), ("missing_tv_row1", 200), ("missing_tv_row2", 303)])
def test_actual_jwt_producer_native_auth_compatibility_control_without_add_host(
    editor_client, editor_user, db, io, monkeypatch, clock, case, expected,
):
    version = 2 if case == "missing_tv_row2" else 1
    if version == 2:
        db.execute("UPDATE users SET token_version=2 WHERE id=?", (editor_user["id"],))
        db.commit()
    omitted = ("tv",) if case.startswith("missing_tv") else ()
    token = issued_session_with_claim_changes(editor_user, monkeypatch, omitted=omitted, token_version=version)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert ("tv" not in claims) == bool(omitted)
    editor_client.cookies.set("access_token", token)
    before = snapshot(db)
    # This is a labelled producer/native-auth control, NOT new-host evidence.
    response = editor_client.get("/browse", follow_redirects=False)
    assert response.status_code == expected
    assert response.headers.get_list("set-cookie") == []
    if expected == 200:
        actual_user = response.context["user"]
        assert actual_user["id"] == editor_user["id"] and actual_user["token_version"] == 1
        assert actual_user["username"] == editor_user["username"] and actual_user["role"] == "editor"
    else:
        assert response.headers["location"] == "/login" and response.content == b""
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("claim_mode", ["native_tv1", "missing_tv1"])
def test_missing_tv1_and_native_session_controls_complete_real_add_wishlist_journey(
    editor_client, editor_user, db, catalogue, monkeypatch, clock, no_refresh, claim_mode,
):
    omitted = ("tv",) if claim_mode == "missing_tv1" else ()
    token = issued_session_with_claim_changes(editor_user, monkeypatch, omitted=omitted)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims.get("tv", 1) == 1 and ("tv" not in claims) == bool(omitted)
    editor_client.cookies.set("access_token", token)
    selected = prepared(editor_client, "book", "wishlist")
    catalogue.clear()
    _, actual = receipt(editor_client, submit(editor_client, selected), editor_user["id"])
    row = stored(db, actual.item_id)
    from app.services import lists
    assert actual.status == "wishlisted" and actual.actual_mode == "wishlist"
    assert row["owned"] == 0 and lists.is_member(db, lists.WISHLIST, actual.item_id)
    assert row["isbn"] == ISBN and row["source"] == "openlibrary" and row["cover_path"] is None
    assert editor_client.cookies.get("access_token") == token and catalogue.calls == []


@pytest.mark.parametrize("case,expected", [
    ("future_int", 200), ("future_float", 200), ("missing_exp", 401), ("numeric_string", 401),
    ("malformed_string", 303), ("null", 303), ("nan", 303), ("positive_infinity", 303),
    ("negative_infinity", 303), ("bool_true", 303), ("bool_false", 303), ("list", 303),
    ("object", 303), ("expired", 303), ("zero", 303),
])
def test_original_session_expiry_claim_is_required_finite_typed_and_checked_without_echo_or_cookies(
    editor_client, editor_user, db, catalogue, monkeypatch, clock, no_refresh, caplog, case, expected,
):
    canary = "ADD_SESSION_EXPIRY_PRIVATE_CANARY_49b2"
    expires = {
        "future_int": int(clock[0]) + 600, "future_float": int(clock[0]) + 600.125,
        "numeric_string": str(int(clock[0]) + 600), "malformed_string": canary, "null": None,
        "nan": float("nan"), "positive_infinity": float("inf"), "negative_infinity": float("-inf"),
        "bool_true": True, "bool_false": False, "list": [], "object": {}, "expired": -1, "zero": 0,
    }
    token = issued_session_with_claim_changes(editor_user, monkeypatch,
                                             changes={} if case == "missing_exp" else {"exp": expires[case]},
                                             omitted=("exp",) if case == "missing_exp" else ())
    editor_client.cookies.set("access_token", token)
    before = snapshot(db)
    catalogue.clear()
    response = editor_client.get("/add", follow_redirects=False)
    assert response.status_code == expected
    no_cookie(response)
    assert canary not in response.text and canary not in caplog.text and token not in response.text and token not in caplog.text
    assert snapshot(db) == before and catalogue.calls == []
    if expected == 303:
        assert response.headers["location"] == "/login" and response.content == b""
    elif expected == 401:
        context_free(response, phase="capture")
    else:
        assert 'data-testid="add-media"' in response.text
        assert value(form(response, "add-media-search"), "context")
        # Positive int/float controls must work through actual signed native
        # search/review/save, not merely survive a token decode in isolation.
        selected = prepared(editor_client, "book")
        catalogue.clear()
        _, actual = receipt(editor_client, submit(editor_client, selected), editor_user["id"])
        assert actual.status == "added" and stored(db, actual.item_id)["owned"] == 1
        assert editor_client.cookies.get("access_token") == token and catalogue.calls == []


@pytest.mark.parametrize("scenario", ["captured_a_live", "captured_a_expired"])
def test_delayed_native_save_keeps_dispatched_actor_and_never_rolls_browser_cookie_back_from_b(
    editor_client, editor_user, db, catalogue, monkeypatch, clock, no_refresh, scenario,
):
    from urllib.parse import parse_qs
    from fastapi.testclient import TestClient
    from app.services import lists

    # The form/review's original cookie and 900-second marker remain live in
    # BOTH cases. A later same-actor, short entry cookie isolates the entry
    # session's own expiry instead of letting an expired form hide that check.
    form_cookie = editor_client.cookies.get("access_token")
    form_claims = jwt.decode(form_cookie, options={"verify_signature": False})
    assert form_claims["exp"] > clock[0] + 901
    selected = prepared(editor_client, "book", "wishlist")
    context_marker = value(selected, "context")
    a_csrf = editor_client.cookies.get("csrf_token")
    entry_cookie = short_session(editor_client, editor_user, monkeypatch)
    entry_expiry = jwt.decode(entry_cookie, options={"verify_signature": False})["exp"]
    b_user = _create_user("later-browser-editor", "password123", "Later Browser Editor", "editor")
    b_cookie = auth.create_token(b_user["id"], b_user["username"], b_user["role"], b_user["display_name"])
    b_csrf = "later-browser-csrf-contract-marker"
    context_module = importlib.import_module("app.add_media_context")
    original_capture, captured = context_module.capture_actor, []

    def capture(request, user):
        actor = original_capture(request, user)
        captured.append(actor)
        return actor

    monkeypatch.setattr(context_module, "capture_actor", capture)
    original_execute = database._Connection.execute
    reached, acquired, locked = threading.Event(), threading.Event(), []

    def execute(conn, sql, *args, **kwargs):
        begin = sql.strip().upper() == "BEGIN IMMEDIATE"
        if begin:
            reached.set()
        result = original_execute(conn, sql, *args, **kwargs)
        if begin:
            acquired.set()
            probe = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
            try:
                try:
                    probe.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc).casefold()
                    locked.append(True)
                else:
                    probe.rollback()
                    locked.append(False)
            finally:
                probe.close()
        return result

    monkeypatch.setattr(database._Connection, "execute", execute)
    before = snapshot(db)
    catalogue.clear()
    rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
    rival.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(submit, editor_client, selected)
            try:
                assert reached.wait(5), "Save did not reach its actual SQLite writer lock"
                assert not acquired.is_set(), "The rival must genuinely hold the lock while the request waits"
                assert len(captured) == 1
                assert asdict(captured[0]) == {
                    "id": editor_user["id"], "username": editor_user["username"], "role": "editor",
                    "token_version": 1, "expires_at": entry_expiry,
                }
                # Change only the test browser's cookie jar AFTER the request
                # entered as A. These values are not retroactively sent in A's
                # dispatched headers; the server cannot observe this jar edit.
                editor_client.cookies.set("access_token", b_cookie)
                editor_client.cookies.set("csrf_token", b_csrf)
                editor_client.headers["X-CSRF-Token"] = b_csrf
                if scenario == "captured_a_expired":
                    clock[0] += 61
                    assert entry_expiry <= clock[0] < form_claims["exp"]
                marker = music_review.verify(context_marker, user_id=editor_user["id"], purpose="shelf-add-context-v1")
                assert marker["exp"] > clock[0], "An expired marker must not make the original-cookie probe vacuous"
            finally:
                rival.rollback()
            response = pending.result(timeout=8)
    finally:
        rival.close()
    assert acquired.is_set() and locked == [True]
    assert len(captured) == 1 and captured[0].id == editor_user["id"] and captured[0].expires_at == entry_expiry
    no_cookie(response)
    assert editor_client.cookies.get("access_token") == b_cookie and editor_client.cookies.get("csrf_token") == b_csrf
    assert catalogue.calls == []
    if scenario == "captured_a_expired":
        context_free(response, phase="router")
        assert "location" not in response.headers and snapshot(db) == before
        return
    assert response.status_code == 303
    receipt_token = parse_qs(urlsplit(response.headers["location"]).query, strict_parsing=True)["receipt"][0]
    music_review.verify(receipt_token, user_id=editor_user["id"], purpose="shelf-add-receipt-v1")
    with pytest.raises(music_review.ReviewError):
        music_review.verify(receipt_token, user_id=b_user["id"], purpose="shelf-add-receipt-v1")
    a_client = TestClient(editor_client.app, base_url="https://testserver")
    a_client.cookies.set("access_token", entry_cookie)
    a_client.cookies.set("csrf_token", a_csrf)
    try:
        _, actual = receipt(a_client, response, editor_user["id"])
    finally:
        a_client.close()
    row = stored(db, actual.item_id)
    assert actual.status == "wishlisted" and actual.actual_mode == "wishlist" and row["owned"] == 0
    assert row["isbn"] == ISBN and row["source"] == "openlibrary" and lists.is_member(db, lists.WISHLIST, row["id"])
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and catalogue.calls == []
    assert editor_client.cookies.get("access_token") == b_cookie and editor_client.cookies.get("csrf_token") == b_csrf


@pytest.mark.parametrize("elapsed,expected", [pytest.param(59, 303, id="before_original_deadline"),
                                             pytest.param(61, 401, id="after_original_deadline")])
def test_original_sixty_second_form_session_deadline_survives_fresh_long_entry_cookie(
    editor_client, editor_user, db, catalogue, monkeypatch, clock, no_refresh, elapsed, expected,
):
    original_cookie = short_session(editor_client, editor_user, monkeypatch)
    original_expiry = jwt.decode(original_cookie, options={"verify_signature": False})["exp"]
    issued_at = clock[0]
    selected = prepared(editor_client, "book", "wishlist")
    marker = value(selected, "context")
    ticket = value(selected, "ticket")
    before = snapshot(db)
    catalogue.clear()
    clock[0] = issued_at + elapsed
    with monkeypatch.context() as patch:
        patch.setattr(auth, "JWT_EXPIRY_SECONDS", 3600)
        fresh_entry = auth.create_token(editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"])
    entry_expiry = jwt.decode(fresh_entry, options={"verify_signature": False})["exp"]
    editor_client.cookies.set("access_token", fresh_entry)
    assert entry_expiry > clock[0] + 900
    assert music_review.verify(marker, user_id=editor_user["id"], purpose="shelf-add-context-v1")["exp"] > clock[0] + 800
    assert music_review.verify(ticket, user_id=editor_user["id"], purpose="shelf-add-catalogue-review-v1")["exp"] > clock[0] + 800
    assert (clock[0] < original_expiry) is (expected == 303)
    response = submit(editor_client, selected)
    assert response.status_code == expected
    no_cookie(response)
    assert editor_client.cookies.get("access_token") == fresh_entry and catalogue.calls == []
    if expected == 401:
        context_free(response, phase="router")
        assert snapshot(db) == before and "location" not in response.headers
    else:
        from app.services import lists
        _, actual = receipt(editor_client, response, editor_user["id"])
        row = stored(db, actual.item_id)
        assert actual.status == "wishlisted" and actual.actual_mode == "wishlist"
        assert row["owned"] == 0 and row["isbn"] == ISBN and row["source"] == "openlibrary"
        assert lists.is_member(db, lists.WISHLIST, actual.item_id)
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and catalogue.calls == []


def assert_rival_title_result_preserves_local_facts(actual, row, local_row, rival_id, mode):
    from tests.test_add_media_save import assert_row_receipt

    assert actual.item_id == rival_id == row["id"]
    assert actual.status == ("promoted" if mode == "add" else "duplicate") and actual.actual_mode == mode
    assert row["owned"] == (1 if mode == "add" else 0)
    for key in local_row.keys() - {"owned", "updated_at"}:
        assert row[key] == local_row[key], key
    assert_row_receipt(actual, row)


@pytest.mark.parametrize("family", ["dvd", "video_game"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_valid_actor_rival_wishlist_commit_precedes_real_title_guard_and_prevents_second_row(
    editor_client, editor_user, db, catalogue, monkeypatch, no_refresh, family, mode,
):
    from dataclasses import replace as replace_record
    from app.services import item_copies, item_write, lists
    from app.services import tags as tags_svc
    from tests.conftest import _insert_location

    original_location = _insert_location(db, "Rival's local shelf")
    submitted_location = _insert_location(db, "Target must not move a duplicate here")
    db.commit()
    selected = prepared(editor_client, family, mode, **({"platform": "switch"} if family == "video_game" else {}))
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    actor_before = tuple(db.execute("SELECT id,username,role,token_version FROM users WHERE id=?", (editor_user["id"],)).fetchone())
    reached, acquired, locked = threading.Event(), threading.Event(), []
    original_execute = database._Connection.execute
    catalogue.clear()
    with get_db() as rival:
        rival.execute("BEGIN IMMEDIATE")

        def execute(conn, sql, *args, **kwargs):
            target_begin = conn is not rival and sql.strip().upper() == "BEGIN IMMEDIATE"
            if target_begin:
                reached.set()
            result = original_execute(conn, sql, *args, **kwargs)
            if target_begin:
                acquired.set()
                probe = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
                try:
                    try:
                        probe.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        assert "locked" in str(exc).casefold()
                        locked.append(True)
                    else:
                        probe.rollback()
                        locked.append(False)
                finally:
                    probe.close()
            return result

        monkeypatch.setattr(database._Connection, "execute", execute)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(submit, editor_client, selected, location_id=submitted_location,
                                      tags="Must not apply target tag", reading_status="reading")
            try:
                assert reached.wait(5), "Target never attempted its actual native writer lock"
                assert not acquired.is_set(), "A real rival SQLite transaction must hold the writer lock"
                title = "Selected Movie" if family == "dvd" else "Selected Game"
                rival_id = item_write.insert_item(
                    rival, title=title, media_type=family, platform="switch" if family == "video_game" else None,
                    authors="Rival local creator", publisher="Rival local publisher", publish_year=1999,
                    description="Rival local description", source="manual", location_id=original_location,
                    notes="Rival notes must survive", cover_path="covers/rival-local.jpg", reading_status="read",
                    owned=0, wishlisted=True,
                )
                tags_svc.attach_tags(rival, rival_id, ["Rival local tag"])
                item_copies.add_copy(rival, rival_id, {"location_id": original_location, "condition": "VG+",
                                                      "acquisition_source": "Rival record shop"})
                local_row = dict(rival.execute("SELECT * FROM items_live WHERE id=?", (rival_id,)).fetchone())
                local_copies = [tuple(row) for row in rival.execute("SELECT * FROM item_copies WHERE item_id=? ORDER BY id", (rival_id,))]
                assert len(local_copies) == 2 and lists.is_member(rival, lists.WISHLIST, rival_id)
                (config.COVERS_DIR / "rival-local.jpg").write_bytes(b"rival's preserved local artwork")
                # This is the actual native-funnel rival transaction commit.
                # No duplicate result or authority decision is substituted.
                rival.commit()
            finally:
                if rival.in_transaction:
                    rival.rollback()
            response = pending.result(timeout=8)
    assert acquired.is_set() and locked == [True]
    assert tuple(db.execute("SELECT id,username,role,token_version FROM users WHERE id=?", (editor_user["id"],)).fetchone()) == actor_before
    _, actual = receipt(editor_client, response, editor_user["id"])
    after = stored(db, rival_id)
    assert_rival_title_result_preserves_local_facts(actual, after, local_row, rival_id, mode)
    assert lists.is_member(db, lists.WISHLIST, rival_id) is (mode == "wishlist")
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id=? ORDER BY id", (rival_id,))] == local_copies
    assert [tag["name"] for tag in tags_svc.get_item_tags(db, rival_id)] == ["Rival local tag"]
    assert (config.COVERS_DIR / "rival-local.jpg").read_bytes() == b"rival's preserved local artwork"
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and catalogue.calls == []
    # Detector controls derive from actual committed native output and never
    # enter application routes as fabricated successful responses.
    with pytest.raises(AssertionError):
        assert_rival_title_result_preserves_local_facts(replace_record(actual, item_id=int(rival_id) + 1), after, local_row, rival_id, mode)
    with pytest.raises(AssertionError):
        assert_rival_title_result_preserves_local_facts(replace_record(actual, authors=None), {**after, "authors": None}, local_row, rival_id, mode)


def context_free_oracle_envelope(phase):
    """Metadata-only refusal fragments for real helper controls, not app pages."""
    from tests.test_add_media_search import oracle_envelope_response

    if phase == "capture":
        import json
        return oracle_envelope_response(401, json.dumps({"code": "actor_changed", "message": EARLY_ERROR_TEXT["actor_changed"]}), media_type="application/json")
    assert phase == "router"
    return oracle_envelope_response(401, f'<p role="alert" data-testid="add-media-error" data-error-code="actor_changed">{ROUTER_ERROR_TEXT["actor_changed"]}</p>')


@pytest.mark.parametrize("phase", ["router", "capture"])
def test_context_free_helper_metadata_pins_known_phase_and_rejects_other_phase_or_success(phase):
    response = context_free_oracle_envelope(phase)
    context_free(response, phase=phase)
    with pytest.raises(AssertionError):
        context_free(response, phase="capture" if phase == "router" else "router")
    response.status_code = 200
    with pytest.raises(AssertionError):
        context_free(response, phase=phase)


@pytest.mark.parametrize("phase", ["router", "capture"])
@pytest.mark.parametrize("fault", ["wrong_code", "wrong_message", "cookie", "redirect", "form", "signature", "account", "provider", "canary"])
def test_context_free_helper_metadata_rejects_wrong_payload_paragraph_and_private_or_action_context(phase, fault):
    import json
    from tests.test_add_media_search import oracle_envelope_response

    response = context_free_oracle_envelope(phase)
    if fault == "cookie":
        response.headers["Set-Cookie"] = "access_token=metadata-only-cookie"
    elif fault == "redirect":
        response.headers["Location"] = "/login"
    elif phase == "capture":
        payload = {"code": "actor_changed", "message": EARLY_ERROR_TEXT["actor_changed"]}
        if fault == "wrong_code":
            payload["code"] = "stale_review"
        elif fault == "wrong_message":
            payload["message"] = "Your session changed. Reload this page."
        else:
            payload[fault] = "metadata-only-private-value"
        response = oracle_envelope_response(401, json.dumps(payload), media_type="application/json")
    else:
        messages = {
            "wrong_code": ('stale_review', ROUTER_ERROR_TEXT["actor_changed"]),
            "wrong_message": ('actor_changed', "Your session changed. Reload this page."),
            "form": ('actor_changed', '<form><input name="context" value="signed-action"></form>'),
            "signature": ('actor_changed', 'Your session changed. eyJtZXRhZGF0YSI.eyJhY3Rpb24i.stub'),
            "account": ('actor_changed', 'Your session changed for Alice Example.'),
            "provider": ('actor_changed', 'Your session changed. Meat Loaf / Hits Out of Hell.'),
            "canary": ('actor_changed', 'Your session changed. PRIVATE_CANARY_49b2.'),
        }
        code, message = messages[fault]
        response = oracle_envelope_response(401, f'<p role="alert" data-testid="add-media-error" data-error-code="{code}">{message}</p>')
    with pytest.raises(AssertionError):
        context_free(response, phase=phase)


def test_marker_409_and_original_session_401_oracle_metadata_are_not_interchangeable():
    from tests.test_add_media_search import oracle_envelope_response

    marker = oracle_envelope_response(409, '<p role="alert">Preview this selection again.</p>',
                                      context={"add": {"error_code": "stale_review", "provider_outcome": None}})
    session = context_free_oracle_envelope("router")
    error(marker, 409, "stale_review")
    context_free(session, phase="router")
    with pytest.raises(AssertionError):
        context_free(marker, phase="router")
    with pytest.raises(AssertionError):
        error(session, 409, "stale_review")


@pytest.mark.parametrize("path", ["/add/search", "/add/preview", "/add/save", "/add/manual"])
@pytest.mark.parametrize("fault", ["content_type", "csrf"])
def test_viewer_role_redirect_transport_and_csrf_refusals_remain_distinct(viewer_client, db, io, no_refresh, path, fault):
    before = snapshot(db)
    if fault == "content_type":
        response = viewer_client.post(path, content=b'{}', headers={"Content-Type": "application/json"}, follow_redirects=False)
        error(response, 415, "unsupported_media_type")
    else:
        viewer_client.headers["X-CSRF-Token"] = "wrong-viewer-csrf-header"
        response = post(viewer_client, path, [("_csrf", viewer_client.cookies.get("csrf_token"))])
        assert response.status_code == 403
        no_cookie(response)
    assert "location" not in response.headers and snapshot(db) == before and io.calls == []


@pytest.mark.parametrize("position", ["prefix", "inline", "suffix"])
def test_context_free_helper_metadata_rejects_processing_instructions_at_any_position(position):
    """Metadata-only HARNESS counterexamples, never application responses."""
    from tests.test_add_media_search import oracle_envelope_response

    envelope = context_free_oracle_envelope("router")
    instruction = ('<?leak account="Alice Example" ticket="eyJtZXRhZGF0YSI.eyJhY3Rpb24i.stub" '
                   'canary="PRIVATE_CANARY_49b2"?>')
    body = {
        "prefix": instruction + envelope.text,
        "inline": envelope.text.replace(">", ">" + instruction, 1),
        "suffix": envelope.text + instruction,
    }[position]
    response = oracle_envelope_response(envelope.status_code, body)
    with pytest.raises(AssertionError):
        context_free(response, phase="router")


def assert_native_shell_add_entry(html, page_url, *, present):
    """Witness actual shell markup; fragment controls never enter app handlers."""
    from urllib.parse import urljoin
    from tests.test_add_media_search import ErrorEnvelope

    class ShellEntries(ErrorEnvelope):
        def handle_starttag(self, tag, attributes):
            super().handle_starttag(tag, attributes)
            node = self.nodes[-1]
            classes = set((node["attrs"].get("class") or "").split())
            node["hidden"] = node["hidden"] or bool(classes & {"hidden", "sr-only"})
            ancestry = {self.nodes[index]["tag"] for index in self.stack}
            node["shell"] = bool(ancestry & {"header", "nav"}) and "main" not in ancestry

    def label(node):
        return " ".join((node["attrs"].get("aria-label") or node["text"]).split()).strip("+ ").casefold()

    parsed = ShellEntries(html)
    entries = [node for node in parsed.nodes if node["tag"] == "a" and (
        label(node) == "add media" or urlsplit(node["attrs"].get("href") or "").path == "/add")]
    if not present:
        assert entries == [], "Viewer/anonymous markup must not expose an Add Media entry, even hidden"
        return None
    visible = [node for node in entries if not node["hidden"]]
    assert visible, "Missing or hidden native shell Add Media entry"
    origin, targets = urlsplit(str(page_url)), []
    for node in visible:
        assert node["shell"] and not node["duplicate_attrs"]
        assert label(node) == "add media"
        attrs = node["attrs"]
        assert attrs.get("aria-disabled", "").casefold() != "true" and "download" not in attrs
        href = attrs.get("href") or ""
        assert href and "\\" not in href and all(ord(character) > 32 for character in href)
        target = urljoin(str(page_url), href)
        address = urlsplit(target)
        assert address.scheme == origin.scheme and address.scheme in {"http", "https"}
        assert address.netloc == origin.netloc, "Add Media entry must stay on the actual page's host"
        assert address.path == "/add" and not address.query and not address.fragment
        targets.append(target)
    return targets[0]


def native_shell_client(request, role):
    if role == "anonymous":
        request.getfixturevalue("editor_user")  # Real existing account, but no session in this client.
        return request.getfixturevalue("client")
    assert role in {"editor", "admin", "viewer"}
    return request.getfixturevalue(role + "_client")


def native_ordinary_shell(client, role, monkeypatch):
    original_refresh, refresh_calls = auth.should_refresh_token, []

    def refresh(request, user):
        refresh_calls.append((request.url.path, user["role"]))
        return original_refresh(request, user)

    # Delegate ordinary-page refresh decisions unchanged; never use no_refresh here.
    monkeypatch.setattr(auth, "should_refresh_token", refresh)
    response = client.get("/login" if role == "anonymous" else "/browse", follow_redirects=False)
    assert response.status_code == 200
    assert refresh_calls == ([] if role == "anonymous" else [("/browse", role)])
    return response, refresh_calls


@pytest.mark.parametrize("role", ["editor", "admin"])
def test_native_shell_add_entry_reaches_real_canonical_add_get(request, db, io, monkeypatch, role):
    native = native_shell_client(request, role)
    before = snapshot(db)
    ordinary, refresh_calls = native_ordinary_shell(native, role, monkeypatch)
    assert io.calls == [] and snapshot(db) == before
    target = assert_native_shell_add_entry(ordinary.text, ordinary.url, present=True)
    entered = native.get(target, follow_redirects=False)
    assert entered.request.method == "GET" and entered.url.path == "/add" and entered.status_code == 200
    assert value(form(entered, "add-media-search"), "context")
    assert "location" not in entered.headers
    no_cookie(entered)
    assert refresh_calls == [("/browse", role)], "Only the ordinary page may consult optional auth refresh"
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("role", ["viewer", "anonymous"])
def test_native_shell_add_entry_is_absent_for_viewer_and_anonymous(request, db, io, monkeypatch, role):
    native = native_shell_client(request, role)
    before = snapshot(db)
    ordinary, _refresh_calls = native_ordinary_shell(native, role, monkeypatch)
    assert_native_shell_add_entry(ordinary.text, ordinary.url, present=False)
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("role,target", [("viewer", "/browse"), ("anonymous", "/login")])
def test_native_shell_add_entry_does_not_grant_direct_get_editing_authority(request, db, io, no_refresh, role, target):
    native = native_shell_client(request, role)
    before = snapshot(db)
    denied = native.get("/add", follow_redirects=False)
    assert denied.status_code == 303 and denied.headers["location"] == target
    no_cookie(denied)
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("fragment", [
    pytest.param('<nav></nav>', id="missing"),
    pytest.param('<nav><a hidden href="/add">Add Media</a></nav>', id="hidden_attribute"),
    pytest.param('<nav hidden><a href="/add">Add Media</a></nav>', id="hidden_ancestor"),
    pytest.param('<nav><a aria-hidden="true" href="/add">Add Media</a></nav>', id="aria_hidden"),
    pytest.param('<nav><a style="display: none" href="/add">Add Media</a></nav>', id="style_hidden"),
    pytest.param('<nav><a class="hidden" href="/add">Add Media</a></nav>', id="hidden_class"),
    pytest.param('<nav><a href="/browse">Add Media</a></nav>', id="wrong_href"),
    pytest.param('<nav><a href="https://untrusted.example/add">Add Media</a></nav>', id="unsafe_host"),
    pytest.param('<nav><a href="javascript:/add">Add Media</a></nav>', id="unsafe_scheme"),
    pytest.param('<main><a href="/add">Add Media</a></main>', id="outside_shell"),
])
def test_shell_add_entry_metadata_controls_reject_unreachable_or_unsafe_fragments(fragment):
    """HARNESS negatives only: no HTTP response, app route, auth or writer decision."""
    with pytest.raises(AssertionError):
        assert_native_shell_add_entry(fragment, "https://testserver/browse", present=True)


def test_nav_census_metadata_control_still_rejects_an_unclassified_top_level_get(monkeypatch, io):
    """HARNESS route metadata only; never issue a request while it is installed."""
    import re
    from types import SimpleNamespace
    from app.main import app
    from tests.test_nav import test_every_top_level_page_is_reachable_from_the_nav as census

    census()  # The actual old census must pass before the negative control.
    original_routes = app.router.routes
    orphan = "/unclassified-add-entry-guard"
    with monkeypatch.context() as patch:
        patch.setattr(app.router, "routes", [*original_routes, SimpleNamespace(path=orphan, methods={"GET"})])
        with pytest.raises(AssertionError, match=re.escape(f"EXEMPT reason: ['{orphan}']") + r"(?:\n|$)"):
            census()
    assert app.router.routes is original_routes
    census()
    assert io.calls == []


@pytest.mark.parametrize("fragment", [
    pytest.param('<main><header><a href="/add">Add Media</a></header></main>', id="main_header"),
    pytest.param('<main><nav><a href="/add">Add Media</a></nav></main>', id="main_nav"),
])
def test_shell_add_entry_metadata_rejects_page_local_header_and_nav(fragment):
    """HARNESS fragments only: page-local navigation is not the global shell."""
    with pytest.raises(AssertionError):
        assert_native_shell_add_entry(fragment, "https://testserver/browse", present=True)


@pytest.mark.parametrize("fragment", [
    pytest.param('<header><a href="/add">Add Media</a></header><main></main>', id="global_header"),
    pytest.param('<nav><a href="/add">Add Media</a></nav><main></main>', id="global_nav"),
])
def test_shell_add_entry_metadata_accepts_global_header_and_nav(fragment):
    """HARNESS fragments only; no HTTP response or application decision is faked."""
    assert assert_native_shell_add_entry(fragment, "https://testserver/browse", present=True) == "https://testserver/add"
