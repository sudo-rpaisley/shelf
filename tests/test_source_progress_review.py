"""Independent-review regressions: delayed authority, scope and exact bindings."""

import asyncio
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.database import get_db
from app.services import media_groups, source_handoff, source_progress as svc, sources
from tests.test_availability import _item, _record, _source
from tests.test_personal_library import _save
from tests.test_source_handoff import f4_client  # noqa: F401
from tests.test_source_progress import (  # noqa: F401 — isolated HTTP/DB fixtures
    REMOTE_USER, ROOT, TOKEN, _jelly, _link, _pending, _review, _state, bound, remote,
)


def _change_scope(db, source_id):
    sources.save_settings(db, source_id, name="Re-scoped Jellyfin", base_url=ROOT,
                          browser_url="https://watch.example/proxy",
                          options={"user_id": "admin-catalogue-user", "library_ids": ["new-library"]})


@pytest.mark.asyncio
async def test_delayed_relink_cannot_resurrect_disconnected_account(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    started, release = asyncio.Event(), asyncio.Event()

    async def held_identity(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"Id": REMOTE_USER})

    remote["me"].mock(side_effect=held_identity)
    task = asyncio.create_task(_link(uid, bound["key"]))
    await asyncio.wait_for(started.wait(), 5)
    with get_db() as rival:
        rival.execute("BEGIN IMMEDIATE")
        svc.disconnect(rival, uid, account)
    release.set()
    with pytest.raises(svc.ProgressError, match="stale_link"):
        await task
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["link", "pull"])
async def test_postawait_session_revocation_prevents_link_or_progress_writes(db, viewer_user, bound, remote, operation):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"]) if operation == "pull" else None

    def revoke(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
        return httpx.Response(200, json={"Id": REMOTE_USER} if operation == "link" else _jelly())

    remote["me" if operation == "link" else "progress"].mock(side_effect=revoke)
    with pytest.raises(svc.ProgressError, match="actor_changed"):
        if operation == "link":
            await _link(uid, bound["key"])
        else:
            await svc.pull_item(uid, account, bound["item"])
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0
    if operation == "link":
        assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_same_root_scope_change_during_pull_cannot_apply_old_progress(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])

    def re_scope(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            _change_scope(rival, bound["source"])
        return httpx.Response(200, json=_jelly())

    remote["progress"].mock(side_effect=re_scope)
    with pytest.raises(svc.ProgressError, match="stale_config|relink_required|not_found"):
        await svc.pull_item(uid, account, bound["item"])
    assert db.execute("SELECT COUNT(*) FROM user_source_progress").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_same_root_scope_change_invalidates_pending_review(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    _save(db, uid, item, status="paused", progress="90")
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, item)
    pending = _pending(db, uid, item)
    before = _state(db, uid, item)
    db.execute("BEGIN IMMEDIATE")
    _change_scope(db, bound["source"])
    db.commit()
    with pytest.raises(svc.ProgressError, match="stale_review|relink_required|not_found"):
        _review(db, uid, pending)
    db.rollback()
    assert _state(db, uid, item) == before
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unicode_review_token_is_controlled_and_nonmutating(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    _save(db, uid, item, status="paused", progress="90")
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, item)
    pending = {**_pending(db, uid, item), "review_token": "é"}
    before = _state(db, uid, item)
    with pytest.raises(svc.ProgressError, match="stale_review|invalid_form"):
        _review(db, uid, pending)
    db.rollback()
    assert _state(db, uid, item) == before
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0


def test_same_source_row_selection_cannot_silently_pick_first_record(db, viewer_user):
    item = _item(db, media_type="ebook")
    source = _source(db)
    _record(db, source, item, external_id="first", open_url="https://books.example/read/first")
    _record(db, source, item, external_id="second", open_url="https://books.example/read/second")
    # Source-only is ambiguous. The rendered rows must use exact binding IDs,
    # while the separate preferred-source flow may still select/fall back.
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read", source_key=f"source:{source}") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("disconnect_latest", [False, True])
async def test_concurrent_links_latest_generation_wins_even_after_disconnect(db, viewer_user, bound, remote, existing, disconnect_latest):
    uid = viewer_user["id"]
    if existing:
        await _link(uid, bound["key"])
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def identity(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return httpx.Response(200, json={"Id": REMOTE_USER})

    remote["me"].mock(side_effect=identity)
    first = asyncio.create_task(_link(uid, bound["key"]))
    await asyncio.wait_for(started.wait(), 5)
    latest_id = await _link(uid, bound["key"])
    latest = dict(db.execute("SELECT * FROM user_source_accounts WHERE id = ?", (latest_id,)).fetchone())
    if disconnect_latest:
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            svc.disconnect(rival, uid, latest_id)
    release.set()
    with pytest.raises(svc.ProgressError, match="stale_link"):
        await first
    current = db.execute("SELECT * FROM user_source_accounts WHERE user_id = ?", (uid,)).fetchone()
    assert (dict(current) if current else None) == (None if disconnect_latest else latest)
    assert db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = ?",
                      (uid, bound["key"])).fetchone()[0]


@pytest.mark.asyncio
async def test_real_scope_change_during_link_verification_cannot_save_token(db, viewer_user, bound, remote):
    def re_scope(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            _change_scope(rival, bound["source"])
        return httpx.Response(200, json={"Id": REMOTE_USER})

    remote["me"].mock(side_effect=re_scope)
    with pytest.raises(svc.ProgressError, match="stale_config"):
        await _link(viewer_user["id"], bound["key"])
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["me", "progress"])
@pytest.mark.parametrize("change", ["version", "role", "username", "delete", "feature"])
async def test_pull_revalidates_actor_and_feature_after_each_remote_await(db, viewer_user, bound, remote, phase, change):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])

    def revoke(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            if change == "version":
                rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
            elif change == "role":
                rival.execute("UPDATE users SET role = 'editor' WHERE id = ?", (uid,))
            elif change == "username":
                rival.execute("UPDATE users SET username = 'renamed' WHERE id = ?", (uid,))
            elif change == "delete":
                rival.execute("DELETE FROM users WHERE id = ?", (uid,))
            else:
                rival.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature.connected_library', '0')")
        return httpx.Response(200, json={"Id": REMOTE_USER} if phase == "me" else _jelly())

    remote[phase].mock(side_effect=revoke)
    with pytest.raises(svc.ProgressError, match="actor_changed|disabled"):
        await svc.pull_item(uid, account, bound["item"])
    if phase == "me":
        assert not remote["progress"].called
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["link", "pull"])
def test_http_entry_session_revoked_during_remote_await_is_not_recaptured(f4_client, db, viewer_user, bound, remote, operation):
    headers = {"Accept": "application/json"}
    fields = {"source_key": bound["key"], "remote_user_id": REMOTE_USER, "token": TOKEN, "consent": "pull_v1"}
    if operation == "pull":
        account = f4_client.post("/api/source-progress/accounts/link", data=fields, headers=headers).json()["account_id"]
    uid = viewer_user["id"]

    def revoke(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
        return httpx.Response(200, json={"Id": REMOTE_USER} if operation == "link" else _jelly())

    remote["me" if operation == "link" else "progress"].mock(side_effect=revoke)
    path = "/api/source-progress/accounts/link" if operation == "link" else f"/api/source-progress/accounts/{account}/items/{bound['item']}/pull"
    response = f4_client.post(path, data=fields, headers=headers)
    assert response.status_code == 401 and response.json()["code"] == "actor_changed"
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0
    if operation == "link":
        assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_session_expiring_during_pull_cannot_apply_result(db, viewer_user, bound, remote, monkeypatch):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    actor = svc._actor(db, uid)
    actor["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp()
    future = datetime.now(timezone.utc) + timedelta(minutes=2)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)

    def expire(request):
        monkeypatch.setattr(svc, "datetime", Later)
        return httpx.Response(200, json=_jelly())

    remote["progress"].mock(side_effect=expire)
    with pytest.raises(svc.ProgressError, match="actor_changed"):
        await svc.pull_item(uid, account, bound["item"], actor=actor)
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


def test_exact_row_links_choose_each_record_and_never_format_fallback(f4_client, db, viewer_user):
    uid = viewer_user["id"]
    item, sibling = _item(db, "Selected", "ebook"), _item(db, "Sibling", "ebook")
    media_groups.link_items(db, item, sibling, link_type="format")
    source = _source(db)
    first = _record(db, source, item, external_id="first", open_url="https://books.example/read/first")
    second = _record(db, source, item, external_id="second", open_url="https://books.example/read/second")
    _record(db, source, sibling, external_id="sibling", open_url="https://books.example/read/sibling")
    db.commit()
    context = source_handoff.context(db, uid, item)
    rows = [row for row in context["rows"] if row["item_id"] == item]
    assert len({row["binding"] for row in rows}) == 2

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hrefs = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and "binding=" in attrs.get("href", ""):
                self.hrefs.append(attrs["href"])

    links = Links()
    links.feed(f4_client.get(f"/api/library/items/{item}/handoff").text)
    for row in rows:
        href = next(href for href in links.hrefs if parse_qs(urlsplit(href).query)["binding"] == [row["binding"]])
        response = f4_client.get(href, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == row["url"]
    old_binding = rows[0]["binding"]
    db.execute("DELETE FROM source_records WHERE id IN (?, ?)", (first, second))
    db.commit()
    assert source_handoff.resolve(db, uid, item, "Read", binding=old_binding) is None
    assert source_handoff.resolve(db, uid, item, "Read")["item_id"] == sibling


def test_binding_includes_record_identity_even_when_browser_urls_are_identical(db, viewer_user):
    item, source = _item(db, media_type="ebook"), _source(db)
    first = _record(db, source, item, external_id="first", open_url="https://books.example/read/shared")
    _record(db, source, item, external_id="second", open_url="https://books.example/read/shared")
    rows = source_handoff.context(db, viewer_user["id"], item)["rows"]
    assert rows[0]["binding"] != rows[1]["binding"]
    old = rows[0]["binding"]
    db.execute("DELETE FROM source_records WHERE id = ?", (first,))
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read", binding=old) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["record_unknown", "record_unavailable", "source_failed"])
async def test_cached_eligibility_is_rechecked_after_progress_request(db, viewer_user, bound, remote, change):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])

    def invalidate(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            if change == "source_failed":
                rival.execute("UPDATE source_instances SET last_status = 'error' WHERE id = ?", (bound["source"],))
            else:
                rival.execute("UPDATE source_records SET availability = ? WHERE id = ?",
                              ("unknown" if change == "record_unknown" else "unavailable", bound["record"]))
        return httpx.Response(200, json=_jelly())

    remote["progress"].mock(side_effect=invalidate)
    with pytest.raises(svc.ProgressError, match="not_found|unavailable"):
        await svc.pull_item(uid, account, bound["item"])
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0


def test_unicode_review_token_http_response_is_controlled(f4_client, db, viewer_user, bound, remote):
    headers = {"Accept": "application/json"}
    fields = {"source_key": bound["key"], "remote_user_id": REMOTE_USER, "token": TOKEN, "consent": "pull_v1"}
    account = f4_client.post("/api/source-progress/accounts/link", data=fields, headers=headers).json()["account_id"]
    _save(db, viewer_user["id"], bound["item"], status="paused", progress="90")
    f4_client.post(f"/api/source-progress/accounts/{account}/items/{bound['item']}/pull", data={"_csrf": "ignored-header-wins"}, headers=headers)
    pending = _pending(db, viewer_user["id"], bound["item"])
    response = f4_client.post(f"/api/source-progress/reviews/{pending['id']}", data={"review_token": "é", "decision": "accepted"}, headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "stale_review"
    assert _state(db, viewer_user["id"], bound["item"])["progress"] == 90
