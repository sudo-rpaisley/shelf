"""The single-item Retry ISBN route: book types take the recovery chain, nothing else does.

G29's fourth instance (codex-B2 on feat/cover-editions): #154 sent every
media type through `resolve_missing_cover`, whose title-search fallback has
no media-type guard. A DVD keeps its UPC in `isbn`, so Retry ISBN on a
cover-less DVD installed a novel's cover. These tests pin the route's guard.
"""
from unittest.mock import AsyncMock

import pytest

from tests.conftest import _insert_item
from tests.test_covers_phase4_004 import cover_stages as cover_stages, session_principal


@pytest.fixture
def no_recovery(monkeypatch):
    """A title search that fails the test if anything reaches it."""
    from app.routers import items_common

    async def boom(*args, **kwargs):
        raise AssertionError("title-search recovery must not run for this item")

    monkeypatch.setattr(items_common, "_search_isbn_for_item", boom)
    return boom


def test_a_dvd_gets_one_exact_lookup_and_never_the_recovery_chain(editor_client, db, monkeypatch, no_recovery, cover_stages):
    from app.routers import items_common
    from app.services import covers

    item_id = _insert_item(db, title="Dune", isbn="025192024825", media_type="dvd", cover_path=None)
    db.commit()
    exact = AsyncMock(return_value=None)
    resolve = AsyncMock(return_value="covers/novel.jpg")
    monkeypatch.setattr(covers, "download_cover", exact)
    monkeypatch.setattr(items_common, "resolve_missing_cover", resolve)

    r = editor_client.post(f"/api/items/{item_id}/retry-cover")

    assert r.status_code == 200
    assert r.json() == {"ok": False, "message": "No cover found"}
    resolve.assert_not_awaited()
    from httpx import AsyncClient
    assert len(cover_stages.clients) == 1 and isinstance(cover_stages.clients[0], AsyncClient)
    exact.assert_awaited_once_with(cover_stages.latest(item_id).download_id,
        "025192024825", None, None, cover_stages.clients[0])
    row = db.execute("SELECT cover_path FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert row["cover_path"] is None
    assert cover_stages.stores == []
    cover_stages.assert_clean()


def test_a_dvd_exact_hit_is_stored(editor_client, editor_user, db, monkeypatch, no_recovery, cover_stages):
    from app.services import covers

    item_id = _insert_item(db, title="Dune", isbn="025192024825", media_type="dvd", cover_path=None)
    db.commit()
    exact = AsyncMock(side_effect=lambda download_id, *args: cover_stages.write(item_id, download_id))
    monkeypatch.setattr(covers, "download_cover", exact)

    r = editor_client.post(f"/api/items/{item_id}/retry-cover")

    assert r.json() == {"ok": True, "cover_path": f"covers/{item_id}.jpg"}
    row = db.execute("SELECT cover_path FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert row["cover_path"] == f"covers/{item_id}.jpg"
    assert len(cover_stages.clients) == 1
    exact.assert_awaited_once_with(cover_stages.latest(item_id).download_id,
        "025192024825", None, None, cover_stages.clients[0])
    cover_stages.assert_published(db, item_id, principal=session_principal(db, editor_user),
        kwargs={"library_ids": {item_id: 1}})


@pytest.mark.parametrize("media_type", ["book", "ebook", "audiobook", "comic", "manga"])
def test_a_book_family_item_takes_the_recovery_chain(editor_client, db, monkeypatch, media_type):
    from app.routers import items_common
    from app.services import covers

    item_id = _insert_item(db, title="Dune", isbn="9780441172719", media_type=media_type, cover_path=None)
    db.commit()
    exact = AsyncMock(return_value=None)
    resolve = AsyncMock(return_value="covers/found.jpg")
    monkeypatch.setattr(covers, "download_cover", exact)
    monkeypatch.setattr(items_common, "resolve_missing_cover", resolve)

    r = editor_client.post(f"/api/items/{item_id}/retry-cover")

    assert r.json() == {"ok": True, "cover_path": "covers/found.jpg"}
    resolve.assert_awaited_once()
    assert resolve.await_args.args[0] == item_id
    exact.assert_not_awaited()


def test_an_isbnless_audiobook_with_an_asin_reaches_the_resolver(editor_client, db, monkeypatch):
    from app.routers import items_common

    item_id = _insert_item(
        db, title="Audio", isbn=None, asin="B002V0QK4C", media_type="audiobook", cover_path=None,
    )
    db.commit()
    resolve = AsyncMock(return_value="covers/audnexus.jpg")
    monkeypatch.setattr(items_common, "resolve_missing_cover", resolve)

    r = editor_client.post(f"/api/items/{item_id}/retry-cover")

    assert r.json() == {"ok": True, "cover_path": "covers/audnexus.jpg"}
    resolve.assert_awaited_once()


@pytest.mark.parametrize("media_type, asin", [("audiobook", None), ("ebook", "B002V0QK4C")])
def test_without_an_isbn_or_an_audiobook_asin_the_route_says_no_isbn(
    editor_client, db, monkeypatch, media_type, asin,
):
    from app.routers import items_common

    item_id = _insert_item(
        db, title="Nothing", isbn=None, asin=asin, media_type=media_type, cover_path=None,
    )
    db.commit()
    resolve = AsyncMock(return_value="covers/x.jpg")
    monkeypatch.setattr(items_common, "resolve_missing_cover", resolve)

    r = editor_client.post(f"/api/items/{item_id}/retry-cover")

    assert r.json() == {"ok": False, "message": "No ISBN"}
    resolve.assert_not_awaited()
