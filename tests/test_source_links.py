"""Manual availability corrections never remove catalogue entries."""

import pytest

from app.services import item_write, source_links
from app.services.source_connectors.base import ConnectorError
from tests.conftest import _insert_item


def _seed(db, provider="links"):
    item_id = _insert_item(db, isbn=None)
    source_id = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES (?, 'External', '')", (provider,)).lastrowid
    record_id = db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, 'external-id', 'Title', 'ebook')", (source_id, item_id)).lastrowid
    db.commit()
    return item_id, source_id, record_id


def test_detach_removes_only_the_manual_availability_binding(db):
    item_id, source_id, record_id = _seed(db)
    db.execute("BEGIN IMMEDIATE")
    source_links.detach(db, source_id, record_id)
    assert db.execute("SELECT 1 FROM source_records WHERE id = ?", (record_id,)).fetchone() is None
    assert db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone() is not None
    assert db.execute("SELECT 1 FROM source_instances WHERE id = ?", (source_id,)).fetchone() is not None


def test_machine_synced_records_are_not_silently_detached(db):
    _, source_id, record_id = _seed(db, provider="opds")
    with pytest.raises(ConnectorError, match="manual_only"):
        source_links.detach(db, source_id, record_id)
    assert db.execute("SELECT 1 FROM source_records WHERE id = ?", (record_id,)).fetchone()


def test_detach_cannot_modify_a_trashed_items_restorable_bindings(db):
    item_id, source_id, record_id = _seed(db)
    item_write.trash_item(db, item_id)
    with pytest.raises(ConnectorError, match="not_found"):
        source_links.detach(db, source_id, record_id)


@pytest.mark.parametrize("client_name,allowed", [("admin_client", True), ("editor_client", False), ("viewer_client", False)])
def test_manual_link_removal_is_admin_only(client_name, allowed, request, db):
    client = request.getfixturevalue(client_name)
    item_id, source_id, record_id = _seed(db)
    response = client.post(f"/api/sources/{source_id}/records/{record_id}/detach", headers={"Content-Type": "application/json"})
    assert response.status_code == (200 if allowed else 403)
    assert bool(db.execute("SELECT 1 FROM source_records WHERE id = ?", (record_id,)).fetchone()) is not allowed
    assert db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone()


def test_manual_link_removal_requires_csrf(admin_client, db):
    _, source_id, record_id = _seed(db)
    admin_client.headers.pop("X-CSRF-Token")
    assert admin_client.post(f"/api/sources/{source_id}/records/{record_id}/detach").status_code == 403
    assert db.execute("SELECT 1 FROM source_records WHERE id = ?", (record_id,)).fetchone()


@pytest.mark.parametrize("item_id", ["0", "-1", "9223372036854775808"])
def test_availability_rejects_unbindable_path_ids(viewer_client, item_id):
    assert viewer_client.get(f"/api/library/items/{item_id}/availability").status_code == 422


@pytest.mark.parametrize("suffix", ["", "/settings", "/test", "/sync", "/disable", "/records"])
def test_source_routes_reject_unbindable_path_ids(admin_client, suffix):
    url = "/sources/9223372036854775808" if not suffix else "/api/sources/9223372036854775808" + suffix
    response = admin_client.get(url) if not suffix else admin_client.post(url)
    assert response.status_code == 422
