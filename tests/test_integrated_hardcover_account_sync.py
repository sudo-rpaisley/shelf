"""Hardcover account boundaries; execute only after the parent freezes writers.

Real disposable schema/writers, explicit canonical accounts and stubbed provider
functions. No live provider calls, dependency overrides or replacement app.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.database import get_db, set_setting
from app.services import catalogue_security, hardcover, item_merge, libraries, user_state
from tests.conftest import _insert_item


REFUSED = {"updated": 0, "unchanged": 0, "total": 0,
           "error": "Hardcover sync is not authorized"}


def _account(db, user):
    return dict(db.execute(
        "SELECT id, username, role, display_name, token_version, language FROM users WHERE id = ?",
        (user["id"],),
    ).fetchone())


def _new_account(db, name, role="viewer"):
    user_id = db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, 'unused', ?, ?)",
        (name, name, role),
    ).lastrowid
    return _account(db, {"id": user_id})


def _book(db, title="Account book", *, hc_id=101, library_id=1, **fields):
    return _insert_item(db, title=title, isbn=None, _library_id=library_id,
                        hardcover_book_id=hc_id, **fields)


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("items", "library_items", "library_memberships", "user_item_state",
                          "user_reading_log", "reading_log")}


def _provider(monkeypatch, books=(), *, during_fetch=None):
    identity = AsyncMock(return_value=700)

    async def fetch(_token, provider_user_id, **_kwargs):
        assert provider_user_id == 700
        if during_fetch is not None:
            during_fetch()
        return list(books)

    library = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(hardcover, "get_user_id", identity)
    monkeypatch.setattr(hardcover, "get_user_books", library)
    # Any accidentally introduced lower-level provider path must fail offline.
    monkeypatch.setattr(hardcover, "_graphql", AsyncMock(side_effect=AssertionError("No network")))
    return identity, library


def test_single_account_legacy_call_updates_private_not_global_status(db, admin_user, monkeypatch):
    item = _book(db, reading_status="read", date_started="2001-01-01")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    _provider(monkeypatch, [{"hardcover_book_id": 101, "reading_status": "read"}])

    result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token"))

    # Private blank != legacy shared 'read': this must count as an actual update.
    assert result == {"updated": 1, "unchanged": 0, "total": 1}
    state = user_state.get_state(db, admin_user["id"], item)
    assert state["reading_status"] == "read"
    assert state["date_started"] is None
    assert tuple(db.execute("SELECT reading_status, date_started FROM items WHERE id = ?",
                            (item,)).fetchone()) == ("read", "2001-01-01")


@pytest.mark.parametrize("account_count", [0, 2])
def test_missing_owner_never_guesses_first_admin_or_calls_provider(db, monkeypatch, account_count):
    for index in range(account_count):
        _new_account(db, f"account-{index}", "admin" if index == 0 else "viewer")
    _book(db, title="PRIVATE OWNER MUST NOT BE GUESSED", reading_status="reading")
    db.commit()
    before = _snapshot(db)
    identity, library = _provider(monkeypatch)

    assert asyncio.run(hardcover.sync_reading_statuses("synthetic-token")) == REFUSED

    identity.assert_not_awaited()
    library.assert_not_awaited()
    assert _snapshot(db) == before


def test_explicit_viewer_sync_is_visibility_scoped_and_never_inherits_shared_state(
    db, viewer_user, admin_user, monkeypatch,
):
    owner = _account(db, viewer_user)
    hidden_library = libraries.create_library(db, "Hidden Hardcover library")
    book = _book(db, reading_status="read", date_started="2000-01-01")
    kids = _book(db, "Retained kids book", hc_id=102, media_type="kids_book", reading_status="reading")
    unmatched = _book(db, "No remote status", hc_id=103, media_type="ebook", reading_status="read")
    hidden = _book(db, "NEVER EXPOSE THIS BOOK", hc_id=104, library_id=hidden_library["id"])
    dvd = _book(db, "Retyped DVD", hc_id=105, media_type="dvd", reading_status="reading")
    game = _book(db, "Retyped game", hc_id=106, media_type="video_game", reading_status="reading")
    trashed = _book(db, "Trashed linked book", hc_id=107, deleted_at="2026-01-01")
    unmapped = _book(db, "Unmapped linked book", hc_id=108, library_id=None)
    user_state.save_state(db, admin_user["id"], book, reading_status="reading", personal_notes="Not yours")
    user_state.save_state(db, owner["id"], kids, reading_status="want_to_read", rating=4)
    before_items = [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")]
    db.commit()
    _provider(monkeypatch, [{"hardcover_book_id": bid, "reading_status": "read"}
                            for bid in (101, 102, 104, 105, 106, 107, 108)])

    result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner))

    assert result == {"updated": 2, "unchanged": 1, "total": 3}
    assert user_state.get_state(db, owner["id"], book)["reading_status"] == "read"
    assert user_state.get_state(db, owner["id"], book)["date_started"] is None
    assert user_state.get_state(db, owner["id"], kids)["reading_status"] == "read"
    assert user_state.get_state(db, owner["id"], kids)["rating"] == 4
    assert user_state.get_state(db, owner["id"], unmatched)["persisted"] is False
    assert user_state.get_state(db, admin_user["id"], book)["reading_status"] == "reading"
    assert user_state.get_state(db, admin_user["id"], book)["personal_notes"] == "Not yours"
    for item in (hidden, dvd, game, trashed, unmapped):
        assert db.execute("SELECT 1 FROM user_item_state WHERE user_id = ? AND item_id = ?",
                          (owner["id"], item)).fetchone() is None
    assert [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")] == before_items
    assert db.execute("SELECT media_type FROM items WHERE id = ?", (kids,)).fetchone()[0] == "kids_book"


def test_omitted_user_in_request_is_only_the_canonical_actor(db, viewer_user, admin_user, monkeypatch):
    owner = _account(db, viewer_user)
    item = _book(db)
    db.commit()
    _provider(monkeypatch, [{"hardcover_book_id": 101, "reading_status": "reading"}])
    actor_token = catalogue_security.set_actor(owner)
    try:
        result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token"))
    finally:
        catalogue_security.reset_actor(actor_token)
    assert result == {"updated": 1, "unchanged": 0, "total": 1}
    assert user_state.get_state(db, viewer_user["id"], item)["reading_status"] == "reading"
    assert user_state.get_state(db, admin_user["id"], item)["persisted"] is False


@pytest.mark.parametrize("invalid", ["missing", "version", "username", "bearer"])
def test_stale_or_bearer_explicit_principal_refuses_before_provider(db, admin_user, monkeypatch, invalid):
    owner = _account(db, admin_user)
    _book(db)
    db.commit()
    if invalid == "missing":
        owner["id"] += 10000
    elif invalid == "version":
        owner["token_version"] += 1
    elif invalid == "username":
        owner["username"] = "different-account"
    else:
        owner.update(api_token_id=123, account_role="admin")
    before = _snapshot(db)
    identity, library = _provider(monkeypatch)

    assert asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner)) == REFUSED

    identity.assert_not_awaited()
    library.assert_not_awaited()
    assert _snapshot(db) == before


@pytest.mark.parametrize("actor_kind", ["bearer", "different-account"])
def test_explicit_owner_cannot_replace_or_uncap_request_actor(
    db, admin_user, viewer_user, monkeypatch, actor_kind,
):
    owner = _account(db, admin_user)
    actor = (dict(owner, api_token_id=123, account_role="admin", role="viewer")
             if actor_kind == "bearer" else _account(db, viewer_user))
    _book(db)
    db.commit()
    before = _snapshot(db)
    identity, library = _provider(monkeypatch)
    actor_token = catalogue_security.set_actor(actor)
    try:
        result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner))
    finally:
        catalogue_security.reset_actor(actor_token)
    assert result == REFUSED
    identity.assert_not_awaited()
    library.assert_not_awaited()
    assert _snapshot(db) == before


@pytest.mark.parametrize("change", ["membership", "token_version", "deleted_account", "move",
                                    "relink", "retype", "trash", "merge"])
def test_awaited_revocation_or_identity_change_refuses_the_whole_batch_without_effects(
    db, viewer_user, monkeypatch, change,
):
    owner = _account(db, viewer_user)
    second_library = libraries.create_library(db, "Second target library")
    third_library = libraries.create_library(db, "Still readable destination")
    for library in (second_library, third_library):
        libraries.set_membership(db, library["id"], owner["id"], "viewer")
    first = _book(db, "First would otherwise be changed", reading_status="want_to_read")
    second = _book(db, "Second planned target", hc_id=102, library_id=second_library["id"])
    survivor = _book(db, "Merge survivor", hc_id=None, library_id=second_library["id"])
    db.commit()
    after_external_change = {}

    def revoke():
        # This callback is reached only after the pre-await selection. Observe
        # the real independent transaction, not a fake security/writer result.
        with get_db() as changed:
            assert changed.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
            if change == "membership":
                libraries.remove_membership(changed, second_library["id"], owner["id"])
            elif change == "token_version":
                changed.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (owner["id"],))
            elif change == "deleted_account":
                changed.execute("DELETE FROM users WHERE id = ?", (owner["id"],))
            elif change == "move":
                libraries.assign_item(changed, second, third_library["id"])
            elif change == "relink":
                changed.execute("UPDATE items SET hardcover_book_id = 999 WHERE id = ?", (second,))
            elif change == "retype":
                changed.execute("UPDATE items SET media_type = 'ebook' WHERE id = ?", (second,))
            elif change == "trash":
                changed.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (second,))
            else:
                item_merge.reparent_children(changed, survivor, second)
                changed.execute("DELETE FROM items WHERE id = ?", (second,))
            after_external_change.update(_snapshot(changed))

    _provider(monkeypatch, [{"hardcover_book_id": bid, "reading_status": "read"} for bid in (101, 102)],
              during_fetch=revoke)
    result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner))

    assert result == REFUSED
    assert _snapshot(db) == after_external_change
    assert db.execute("SELECT reading_status FROM items WHERE id = ?", (first,)).fetchone()[0] == "want_to_read"
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


@pytest.mark.parametrize("scheduler_handoff", [False, True])
def test_sole_account_compatibility_is_rechecked_after_provider_await(db, admin_user, monkeypatch, scheduler_handoff):
    _book(db)
    db.commit()
    owner = hardcover.scheduled_account(db) if scheduler_handoff else None

    def add_account():
        with get_db() as changed:
            _new_account(changed, "new-household-member")

    _provider(monkeypatch, [{"hardcover_book_id": 101, "reading_status": "read"}], during_fetch=add_account)
    assert asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner)) == REFUSED
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


def test_newly_visible_links_do_not_expand_the_captured_plan(db, viewer_user, monkeypatch):
    owner = _account(db, viewer_user)
    first = _book(db)
    db.commit()
    added = []

    def add_link():
        with get_db() as changed:
            added.append(_book(changed, "New link during fetch", hc_id=102))

    _provider(monkeypatch, [{"hardcover_book_id": bid, "reading_status": "read"} for bid in (101, 102)],
              during_fetch=add_link)
    result = asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner))
    assert result == {"updated": 1, "unchanged": 0, "total": 1}
    assert user_state.get_state(db, owner["id"], first)["reading_status"] == "read"
    assert user_state.get_state(db, owner["id"], added[0])["persisted"] is False


def test_late_private_write_refusal_rolls_back_earlier_writes_and_redacts_error(db, viewer_user, monkeypatch):
    owner = _account(db, viewer_user)
    first, second = _book(db), _book(db, "Second", hc_id=102)
    db.commit()
    before = _snapshot(db)
    _provider(monkeypatch, [{"hardcover_book_id": bid, "reading_status": "read"} for bid in (101, 102)])
    save = user_state.save_state
    written = []

    def refuse_second(connection, user_id, item_id, **changes):
        assert connection.in_transaction
        written.append(item_id)
        if item_id == second:
            raise HTTPException(404, "HIDDEN INFORMATION MUST NOT APPEAR")
        return save(connection, user_id, item_id, **changes)

    monkeypatch.setattr(user_state, "save_state", refuse_second)
    assert asyncio.run(hardcover.sync_reading_statuses("synthetic-token", user=owner)) == REFUSED
    assert written == [first, second]
    assert _snapshot(db) == before


def test_schedule_captures_current_admin_not_legacy_or_previous_owner_and_off_clears(
    db, admin_client, admin_user,
):
    other = _new_account(db, "previous-owner", "admin")
    set_setting(db, "hc_sync_user_id", str(other["id"]))
    set_setting(db, "hc_sync_token_version", str(other["token_version"]))
    set_setting(db, "hardcover_user_id", "999999")  # Provider identity is not a Shelf owner.
    db.commit()

    response = admin_client.post("/api/hardcover/schedule", data={"interval": "daily"}, follow_redirects=False)
    assert response.status_code == 303
    assert hardcover.scheduled_account(db) == _account(db, admin_user)
    settings = dict(db.execute("SELECT key, value FROM settings WHERE key LIKE 'hc_sync_%'").fetchall())
    assert settings == {"hc_sync_interval": "daily", "hc_sync_user_id": str(admin_user["id"]),
                        "hc_sync_token_version": str(_account(db, admin_user)["token_version"])}
    response = admin_client.post("/api/hardcover/schedule", data={"interval": "off"}, follow_redirects=False)
    assert response.status_code == 303
    assert dict(db.execute("SELECT key, value FROM settings WHERE key LIKE 'hc_sync_%'").fetchall()) == {
        "hc_sync_interval": "off",
    }
    assert hardcover.scheduled_account(db) is None  # Still multi-account, never guess.


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_schedule_configuration_remains_global_admin_only(db, request, role):
    client = request.getfixturevalue(f"{role}_client")
    user = request.getfixturevalue(f"{role}_user")
    libraries.set_membership(db, 1, user["id"], "editor")
    db.commit()
    response = client.post("/api/hardcover/schedule", data={"interval": "daily"}, follow_redirects=False)
    assert response.status_code == 403
    assert db.execute("SELECT 1 FROM settings WHERE key LIKE 'hc_sync_%'").fetchone() is None


def test_legacy_schedule_sole_account_compatibility_never_adopts_household_owner(db, admin_user):
    set_setting(db, "hc_sync_interval", "daily")
    set_setting(db, "hardcover_user_id", "999999")
    assert hardcover.scheduled_account(db) == _account(db, admin_user)
    actor_token = catalogue_security.set_actor(_account(db, admin_user))
    try:
        assert hardcover.scheduled_account(db) is None  # No HTTP legacy fallback.
    finally:
        catalogue_security.reset_actor(actor_token)
    _new_account(db, "second-account")
    assert hardcover.scheduled_account(db) is None


@pytest.mark.parametrize("invalid", ["version", "deleted", "owner_only", "version_only", "malformed"])
def test_invalid_configured_schedule_owner_never_falls_back_even_if_one_account_remains(db, admin_user, invalid):
    owner = _new_account(db, "configured-owner", "admin")
    set_setting(db, "hc_sync_user_id", str(owner["id"]))
    set_setting(db, "hc_sync_token_version", str(owner["token_version"]))
    assert hardcover.scheduled_account(db) == owner
    if invalid == "version":
        db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (owner["id"],))
    elif invalid == "deleted":
        db.execute("DELETE FROM users WHERE id = ?", (owner["id"],))
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    elif invalid == "owner_only":
        db.execute("DELETE FROM settings WHERE key = 'hc_sync_token_version'")
    elif invalid == "version_only":
        db.execute("DELETE FROM settings WHERE key = 'hc_sync_user_id'")
    else:
        set_setting(db, "hc_sync_user_id", "not-an-account")
    assert hardcover.scheduled_account(db) is None


def test_private_user_book_keys_and_blank_state_never_fall_back_to_legacy_http_link(
    db, admin_user, viewer_user,
):
    from app.routers import hardcover as routes

    first, second = _account(db, admin_user), _account(db, viewer_user)
    libraries.set_membership(db, 1, second["id"], "editor")
    item_id = _book(db, hardcover_user_book_id=999, reading_status="read", date_started="2000-01-01")
    user_state.save_state(db, first["id"], item_id, reading_status="reading")
    set_setting(db, routes._private_link_key(first["id"], item_id), "123")
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert routes._private_link_key(second["id"], item_id) == f"hardcover_user_book:{second['id']}:{item_id}"
    actor_token = catalogue_security.set_actor(second)
    try:
        account_item = routes._account_item(db, item, second)
        assert account_item["hardcover_user_book_id"] is None
        assert account_item["reading_status"] is None
        assert account_item["date_started"] is None
        routes._save_hc_ids(db, item_id, {"hardcover_user_book_id": 456}, user=second)
        assert routes._account_item(db, item, second)["hardcover_user_book_id"] == 456
    finally:
        catalogue_security.reset_actor(actor_token)
    actor_token = catalogue_security.set_actor(first)
    try:
        account_item = routes._account_item(db, item, first)
        assert account_item["hardcover_user_book_id"] == 123
        assert account_item["reading_status"] == "reading"
    finally:
        catalogue_security.reset_actor(actor_token)
    assert db.execute("SELECT hardcover_user_book_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 999
