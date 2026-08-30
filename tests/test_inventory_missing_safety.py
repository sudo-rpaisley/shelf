"""Security regressions for the Inventory Missing HTMX fragment."""

from tests.conftest import _insert_item, _insert_location


def test_inventory_missing_escapes_stored_title_author_and_location(admin_client, db):
    location_name = '<img src=x onerror="alert(1)">'
    title = "<script>alert(2)</script>"
    authors = "<svg onload=alert(3)>"
    location_id = _insert_location(db, location_name)
    _insert_item(
        db,
        title=title,
        isbn="9780000999500",
        authors=authors,
        location_id=location_id,
    )
    db.commit()

    response = admin_client.post(
        "/api/inventory/missing",
        data={"location_id": str(location_id), "scanned_ids": ""},
    )

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "<svg" not in response.text
    assert '<img src=x onerror="alert(1)">' not in response.text
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in response.text
    assert "&lt;svg onload=alert(3)&gt;" in response.text
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in response.text


def test_inventory_all_accounted_escapes_location_name(admin_client, db):
    location_name = "<script>alert('location')</script>"
    location_id = _insert_location(db, location_name)
    item_id = _insert_item(
        db,
        title="Scanned",
        isbn="9780000999517",
        location_id=location_id,
    )
    db.commit()

    response = admin_client.post(
        "/api/inventory/missing",
        data={"location_id": str(location_id), "scanned_ids": str(item_id)},
    )

    assert response.status_code == 200
    assert "All items at" in response.text
    assert "<script>" not in response.text
    assert "&lt;script&gt;alert(&#39;location&#39;)&lt;/script&gt;" in response.text
