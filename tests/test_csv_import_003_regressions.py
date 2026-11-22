"""CSV remediation regressions; real-app execution belongs to the frozen gate.

All provider work is stubbed. Successful cover stubs write to the real pending
id, and suspended provider tests exercise the actual authority revalidation.
"""

import asyncio
from contextvars import Context, copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import _insert_item, _seed_personal_state
from tests.test_reading_imports import GOODREADS_HEADER, _gr_row, _post_csv


@pytest.mark.parametrize("isbn,media,error", [
    ("9780441172710", "book", "Invalid ISBN: 9780441172710"),
    ("9780441013593", "widget", "Unknown media type: 'widget'"),
])
def test_native_value_prevalidation_is_read_only_and_next_row_can_use_its_location(
    admin_client, db, monkeypatch, isbn, media, error,
):
    from app.services import csv_import

    validate = csv_import.validate_item_fields
    checks = []

    def checked(conn, fields):
        # The validator must run before any row writer, not merely rely on a
        # rollback after a get-or-create or restore has already happened.
        assert conn.execute("SELECT 1 FROM items").fetchone() is None
        assert conn.execute("SELECT 1 FROM locations WHERE name='CSV validation shelf'").fetchone() is None
        checks.append(dict(fields))
        return validate(conn, fields)

    monkeypatch.setattr(csv_import, "validate_item_fields", checked)
    header = "title,isbn,media_type,location,tags\n"
    bad = f"Invalid row,{isbn},{media},CSV validation shelf,Must not survive\n"
    before = "\n".join(db.iterdump())
    refused = _post_csv(admin_client, header + bad).json()
    assert refused["errors"] == [f"Row 2: {error}"]
    assert (refused["imported"], refused["skipped"], refused["restored"], refused["trashed"], refused["error_count"]) == (0, 0, 0, 0, 1)
    assert refused["locations_created"] == [] and refused["dropped"] == []
    assert "\n".join(db.iterdump()) == before

    good = "Valid row,9780441013593,book,CSV validation shelf,Kept tag\n"
    result = _post_csv(admin_client, header + bad + good).json()
    assert result["errors"] == [f"Row 2: {error}"]
    assert (result["imported"], result["skipped"], result["restored"], result["trashed"], result["error_count"]) == (1, 0, 0, 0, 1)
    assert len(checks) == 3
    assert checks[0]["isbn"] == isbn and checks[0]["media_type"] == media
    assert result["locations_created"] == ["CSV validation shelf"]
    assert result["dropped"] == []
    assert [tuple(r) for r in db.execute("SELECT title,isbn FROM items")] == [("Valid row", "9780441013593")]
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1
    assert db.execute("SELECT name FROM tags").fetchone()[0] == "Kept tag"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_tracker_update_preserves_legacy_and_another_actors_existing_state(
    admin_client, admin_user, viewer_user, db,
):
    item_id = _insert_item(
        db, title="Dune", isbn="9780441013593", owned=1,
        reading_status="reading", date_started="2001-01-02", date_finished="2001-02-03",
    )
    _seed_personal_state(db, viewer_user["id"], item_id, reading_status="want_to_read",
                         date_finished="2002-03-04", personal_notes="other actor only", wishlist=1)
    db.commit()
    other_before = tuple(db.execute(
        "SELECT * FROM user_item_state WHERE user_id=? AND item_id=?", (viewer_user["id"], item_id),
    ).fetchone())
    assert db.execute(
        "SELECT 1 FROM user_item_state WHERE user_id=? AND item_id=?", (admin_user["id"], item_id),
    ).fetchone() is None

    result = _post_csv(admin_client, GOODREADS_HEADER + "\n" + _gr_row(), mode="update").json()
    assert (result["imported"], result["errors"]) == (1, [])
    personal = db.execute(
        "SELECT user_id,item_id,reading_status,date_started,date_finished,wishlist "
        "FROM user_item_state WHERE user_id=? AND item_id=?", (admin_user["id"], item_id),
    ).fetchone()
    assert tuple(personal) == (admin_user["id"], item_id, "read", None, "2023-08-15", 0)
    assert tuple(db.execute(
        "SELECT * FROM user_item_state WHERE user_id=? AND item_id=?", (viewer_user["id"], item_id),
    ).fetchone()) == other_before
    assert db.execute(
        "SELECT reading_status,date_started,date_finished,owned FROM items WHERE id=?", (item_id,),
    ).fetchone()[:] == ("reading", "2001-01-02", "2001-02-03", 0)
    assert db.execute("SELECT 1 FROM list_items WHERE item_id=?", (item_id,)).fetchone() is None


def _finish_background_inline(coro, *, name=None, context=None):
    """Honor create_task's context; never hide a newly introduced suspension."""
    execution_context = context if context is not None else copy_context()
    try:
        execution_context.run(coro.send, None)
    except StopIteration as done:
        return SimpleNamespace(result=lambda value=done.value: value)
    else:
        raise AssertionError("CSV producer unexpectedly suspended; use a real task test")
    finally:
        execution_context.run(coro.close)


def test_csv_inline_background_stub_honors_empty_context_and_refuses_suspension():
    from app.services import catalogue_security

    observed = []

    async def synchronous_producer():
        observed.append(catalogue_security.current_actor())

    async def suspending_producer():
        await asyncio.sleep(0)

    token = catalogue_security.set_actor({"id": 999, "role": "admin"})
    try:
        _finish_background_inline(synchronous_producer(), context=Context())
        assert observed == [None]
        assert catalogue_security.current_actor()["id"] == 999
        with pytest.raises(AssertionError, match="unexpectedly suspended"):
            _finish_background_inline(suspending_producer(), context=Context())
    finally:
        catalogue_security.reset_actor(token)


def test_csv_queue_producer_runs_without_request_context_but_retains_exact_authority(
    admin_client, admin_user, db, monkeypatch,
):
    from app.routers import items_common, items_csv
    from app.services import catalogue_security, cover_queue

    monkeypatch.delenv("SHELF_DISABLE_COVER_ENRICH", raising=False)
    produce = items_common._enrich_import_covers
    observations = []

    async def observed(item_ids, *, user, library_ids):
        observations.append((catalogue_security.current_actor(), list(item_ids), dict(user), dict(library_ids)))
        await produce(item_ids, user=user, library_ids=library_ids)

    monkeypatch.setattr(items_common, "_enrich_import_covers", observed)
    monkeypatch.setattr(items_csv.asyncio, "create_task", _finish_background_inline)
    result = _post_csv(admin_client, "title,isbn\nBound CSV book,9780441013593\n", enrich_covers="1").json()
    assert (result["imported"], result["errors"], result["covers_queued"]) == (1, [], 1)
    item_id = db.execute("SELECT id FROM items WHERE title='Bound CSV book'").fetchone()[0]
    assert len(observations) == 1
    actor, ids, user, parents = observations[0]
    assert actor is None and ids == [item_id]
    assert user["id"] == admin_user["id"] and parents == {item_id: 1}
    job = cover_queue._get_queue().get_nowait()
    assert job.item_id == item_id
    authority = job.hints["_cover_authority"]
    assert isinstance(authority, items_common._QueuedCoverAuthority)
    assert authority.user == user and authority.library_id == 1


@pytest.mark.parametrize("format", ["audible_cli", "libation"])
def test_csv_audible_hint_jobs_retain_importer_and_parent(admin_client, admin_user, db, monkeypatch, format):
    from app.routers import items_common
    from app.services import cover_queue

    monkeypatch.delenv("SHELF_DISABLE_COVER_ENRICH", raising=False)
    if format == "audible_cli":
        content = ("Title,ASIN,Runtime Length Min,Cover URL\n"
                   "Bound Audible book,B012345678,120,https://covers.example/audible.jpg\n")
    else:
        content = ("Title,Audible Product Id,Content Type,Length In Minutes\n"
                   "Bound Audible book,B012345678,Product,120\n")
    result = _post_csv(admin_client, content, enrich_covers="1").json()
    assert (result["imported"], result["errors"], result["covers_queued"]) == (1, [], 1)
    item_id = db.execute("SELECT id FROM items WHERE title='Bound Audible book'").fetchone()[0]
    job = cover_queue._get_queue().get_nowait()
    assert job.item_id == item_id and job.hints["skip_title_search"] is True
    authority = job.hints["_cover_authority"]
    assert isinstance(authority, items_common._QueuedCoverAuthority)
    assert authority.user["id"] == admin_user["id"] and authority.library_id == 1
    if format == "audible_cli":
        assert job.hints["cover_url"] == "https://covers.example/audible.jpg"
    else:
        assert "cover_url" not in job.hints


@pytest.mark.asyncio
@pytest.mark.parametrize("change,status", [("move", 404), ("revoke", 403)])
async def test_csv_bound_cover_job_revalidates_after_real_provider_suspension(
    db, editor_user, monkeypatch, change, status,
):
    from fastapi import HTTPException
    from app import config
    from app.routers import items_common
    from app.services import catalogue_security, cover_queue, covers, libraries

    # An ordinary editor models a captured HTTP principal without manufacturing
    # admin authority or clearing the actor to make a hidden write succeed.
    target = _insert_item(db, title="Suspended CSV cover", isbn="9780441013593")
    other = libraries.create_library(db, "Other CSV cover parent")["id"]
    libraries.set_membership(db, other, editor_user["id"], "editor")
    db.commit()
    token = catalogue_security.set_actor(editor_user)
    try:
        await items_common._enrich_import_covers([target], user=dict(editor_user), library_ids={target: 1})
    finally:
        catalogue_security.reset_actor(token)
    job = cover_queue._get_queue().get_nowait()
    before_item = tuple(db.execute("SELECT * FROM items WHERE id=?", (target,)).fetchone())
    final = config.COVERS_DIR / f"{target}.jpg"
    final.write_bytes(b"existing bytes must not be overwritten")
    entered, release = asyncio.Event(), asyncio.Event()
    pending_paths = []

    async def download(download_id, isbn, cover_url, cover_id, client, **kwargs):
        assert catalogue_security.current_actor() is None
        assert download_id < 0 and download_id != target
        pending = config.COVERS_DIR / f"{download_id}.jpg"
        pending.write_bytes(b"pending provider bytes")
        pending_paths.append(pending)
        entered.set()
        await release.wait()  # a genuine suspension, not an inline fake await
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "download_cover", download)
    search = AsyncMock(side_effect=AssertionError("unexpected provider fallback"))
    monkeypatch.setattr(items_common.openlibrary, "search_by_title_author", search)
    task = asyncio.create_task(items_common.resolve_missing_cover(target, None, job.hints), context=Context())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done() and pending_paths[0].exists()
        if change == "move":
            db.execute("UPDATE library_items SET library_id=? WHERE item_id=?", (other, target))
        else:
            libraries.set_membership(db, 1, editor_user["id"], "viewer")
        db.commit()
        release.set()
        with pytest.raises(HTTPException) as refused:
            await task
        assert refused.value.status_code == status
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    search.assert_not_awaited()
    assert tuple(db.execute("SELECT * FROM items WHERE id=?", (target,)).fetchone()) == before_item
    assert final.read_bytes() == b"existing bytes must not be overwritten"
    assert all(not pending.exists() for pending in pending_paths)
    assert list(config.COVERS_DIR.iterdir()) == [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["generic", "audible_cli"])
@pytest.mark.parametrize("change", ["demote", "delete", "version", "role_without_version", "unchanged"])
async def test_actual_csv_admin_job_rechecks_account_after_provider_suspension(
    admin_client, admin_user, db, monkeypatch, format, change,
):
    from fastapi import HTTPException
    from app import config
    from app.routers import items_common, items_csv
    from app.services import catalogue_security, cover_queue, covers, libraries

    monkeypatch.delenv("SHELF_DISABLE_COVER_ENRICH", raising=False)
    if format == "generic":
        content = "title,isbn\nSuspended administrator import,9780441013593\n"
    else:
        content = ("Title,ASIN,Runtime Length Min,Cover URL\n"
                   "Suspended administrator import,B012345678,120,https://covers.example/admin.jpg\n")
    # Obtain the actual server-typed job through authenticated CSV dispatch.
    # Restore create_task before testing the genuinely suspended resolver.
    with monkeypatch.context() as producer_patch:
        producer_patch.setattr(items_csv.asyncio, "create_task", _finish_background_inline)
        result = _post_csv(admin_client, content, enrich_covers="1").json()
    assert (result["imported"], result["errors"], result["covers_queued"]) == (1, [], 1)
    job = cover_queue._get_queue().get_nowait()
    authority = job.hints["_cover_authority"]
    assert isinstance(authority, items_common._QueuedCoverAuthority)
    assert authority.user["id"] == admin_user["id"]
    assert authority.user["role"] == "admin" and authority.user["token_version"] == 1
    assert authority.library_id == 1
    target = job.item_id
    before = tuple(db.execute("SELECT * FROM items WHERE id=?", (target,)).fetchone())
    final = config.COVERS_DIR / f"{target}.jpg"
    final.write_bytes(b"original administrator cover")
    entered, release = asyncio.Event(), asyncio.Event()
    pending_paths = []

    async def download(download_id, isbn, cover_url, cover_id, client, **kwargs):
        assert catalogue_security.current_actor() is None
        assert download_id < 0 and download_id != target
        assert isbn == ("9780441013593" if format == "generic" else None)
        assert cover_url == ("https://covers.example/admin.jpg" if format == "audible_cli" else None)
        pending = config.COVERS_DIR / f"{download_id}.jpg"
        pending.write_bytes(b"new administrator cover")
        pending_paths.append(pending)
        entered.set()
        await release.wait()
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "download_cover", download)
    fallback = AsyncMock(side_effect=AssertionError("unexpected fallback"))
    monkeypatch.setattr(items_common.openlibrary, "search_by_title_author", fallback)
    task = asyncio.create_task(items_common.resolve_missing_cover(target, None, job.hints), context=Context())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done() and pending_paths[0].exists()
        if change == "delete":
            db.execute("DELETE FROM users WHERE id=?", (admin_user["id"],))
        elif change == "version":
            db.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (admin_user["id"],))
        elif change in {"demote", "role_without_version"}:
            version_sql = ", token_version=token_version+1" if change == "demote" else ""
            db.execute(f"UPDATE users SET role='viewer'{version_sql} WHERE id=?", (admin_user["id"],))
            libraries.set_membership(db, 1, admin_user["id"], "viewer")
        db.commit()
        release.set()
        if change == "unchanged":
            assert await task == f"covers/{target}.jpg"
        else:
            with pytest.raises(HTTPException) as refusal:
                await task
            assert refusal.value.status_code == 403
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    fallback.assert_not_awaited()
    assert all(not pending.exists() for pending in pending_paths)
    assert list(config.COVERS_DIR.iterdir()) == [final]
    if change == "unchanged":
        assert final.read_bytes() == b"new administrator cover"
        assert db.execute("SELECT cover_path FROM items WHERE id=?", (target,)).fetchone()[0] == f"covers/{target}.jpg"
    else:
        assert tuple(db.execute("SELECT * FROM items WHERE id=?", (target,)).fetchone()) == before
        assert final.read_bytes() == b"original administrator cover"
