"""A deliberate removal survives sync even after the retained file is gone."""

import httpx
import pytest

from app import config
from app.database import get_db
from app.services import audiobookshelf, cover_sync_policy, komga_sync, romm_sync
from tests.conftest import _insert_item


IMAGE = b"\xff\xd8\xff" + b"cover bytes" * 150


def test_removal_keeps_human_review_visible_and_blocks_all_source_fills(editor_client, db):
    item_id = _insert_item(db, isbn=None, cover_path="covers/nonexistent-old-cover.jpg", cover_review_dismissed=1)
    db.commit()
    assert editor_client.post(f"/api/items/{item_id}/cover-remove").status_code == 200
    row = db.execute("SELECT cover_path, cover_review_dismissed FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert row[:] == (None, 0)
    assert cover_sync_policy.blocked(db, item_id)
    assert audiobookshelf._cover_fill_allowed(db, item_id, "audiobookshelf:item") is False
    db.commit()
    from app.services import library_attention
    assert item_id in [row["item_id"] for row in library_attention.summary(db, category="covers")["queues"]["covers"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["komga", "romm"])
async def test_blocked_source_cover_never_starts_a_download(provider, db):
    item_id = _insert_item(db, isbn=None)
    cover_sync_policy.block(db, item_id)
    db.commit()

    def forbidden(request):
        raise AssertionError("A blocked cover must not start a provider request")

    module = komga_sync if provider == "komga" else romm_sync
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        assert await module._ingest_cover(client, f"https://{provider}.example", "fixture", item_id,
                                          "entry" if provider == "komga" else "/art.jpg") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["komga", "romm"])
async def test_explicit_allow_can_replace_a_retained_removed_file(provider, editor_client, db):
    item_id = _insert_item(db, isbn=None)
    retained = config.COVERS_DIR / f"{item_id}.jpg"
    retained.write_bytes(b"removed human cover")
    cover_sync_policy.block(db, item_id)
    db.commit()
    response = editor_client.post(f"/api/items/{item_id}/cover-sync/allow", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item_id}"
    assert cover_sync_policy.decision(db, item_id) is False
    module = komga_sync if provider == "komga" else romm_sync
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=IMAGE))) as client:
        assert await module._ingest_cover(client, f"https://{provider}.example", "fixture", item_id,
                                          "entry" if provider == "komga" else "/art.jpg") is True
    assert retained.read_bytes() == IMAGE


def test_abs_explicit_allow_overrides_old_cover_removal_evidence(editor_client, db):
    item_id = _insert_item(db, isbn=None)
    retained = config.COVERS_DIR / f"{item_id}.jpg"
    retained.write_bytes(b"previously removed")
    assert audiobookshelf._cover_fill_allowed(db, item_id, "audiobookshelf:entry") is False
    db.commit()
    assert cover_sync_policy.blocked(db, item_id)
    assert editor_client.post(f"/api/items/{item_id}/cover-sync/allow", follow_redirects=False).status_code == 303
    with get_db() as current:
        assert audiobookshelf._cover_fill_allowed(current, item_id, "audiobookshelf:entry") is True


def test_viewers_cannot_allow_cover_sync(viewer_client, db):
    item_id = _insert_item(db, isbn=None)
    cover_sync_policy.block(db, item_id)
    db.commit()
    assert viewer_client.post(f"/api/items/{item_id}/cover-sync/allow").status_code == 403
    assert cover_sync_policy.blocked(db, item_id)


def test_allow_missing_item_returns_404_and_requires_csrf(editor_client):
    assert editor_client.post("/api/items/999999/cover-sync/allow").status_code == 404
    editor_client.headers.pop("X-CSRF-Token")
    assert editor_client.post("/api/items/999999/cover-sync/allow").status_code == 403
