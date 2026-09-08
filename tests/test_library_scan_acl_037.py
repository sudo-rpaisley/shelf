"""Library and personal-state boundaries for scanner workflows."""

from unittest.mock import AsyncMock, patch

from app.auth import create_token
from app.services import libraries, provider_result
from tests.conftest import _insert_item, _insert_location


def _private_item(db, *, title: str, isbn: str | None = None, upc: str | None = None, **fields):
    library = libraries.create_library(db, f"Private {title}")
    item_id = _insert_item(
        db,
        title=title,
        isbn=isbn,
        _library_id=library["id"],
        upc=upc,
        **fields,
    )
    return library, item_id


def test_hidden_isbn_duplicate_does_not_disclose_title_or_item_id(
    db, editor_client
):
    _, hidden = _private_item(
        db,
        title="Secret Barcode Book",
        isbn="9780000015002",
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={"isbn": "9780000015002", "media_type": "book", "mode": "add"},
    )

    assert response.status_code == 200
    assert "Secret Barcode Book" not in response.text
    assert f"/item/{hidden}" not in response.text
    assert "cannot be added here" in response.text


def test_hidden_upc_duplicate_does_not_disclose_title_or_item_id(
    db, editor_client
):
    _, hidden = _private_item(
        db,
        title="Secret Disc",
        isbn=None,
        upc="04006381333931",
        media_type="dvd",
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={"isbn": "4006381333931", "media_type": "dvd", "mode": "add"},
    )

    assert response.status_code == 200
    assert "Secret Disc" not in response.text
    assert f"/item/{hidden}" not in response.text
    assert "cannot be added here" in response.text


def test_hidden_lookup_behaves_as_not_in_collection(db, editor_client):
    _, hidden = _private_item(
        db,
        title="Secret Lookup Book",
        isbn="9780000015019",
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={"isbn": "9780000015019", "mode": "lookup"},
    )

    assert response.status_code == 200
    assert "Secret Lookup Book" not in response.text
    assert f"/item/{hidden}" not in response.text
    assert "Not in your collection" in response.text


def test_shared_scan_mutation_requires_library_editor_role(
    db, editor_client, editor_user
):
    location = _insert_location(db, "Secure Shelf")
    item_id = _insert_item(
        db,
        title="Visible But Read Only",
        isbn="9780000015026",
    )
    libraries.set_membership(
        db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], "viewer"
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={
            "isbn": "9780000015026",
            "mode": "move",
            "location_id": str(location),
        },
    )

    assert response.status_code == 403
    row = db.execute(
        "SELECT location_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["location_id"] is None


def test_quick_rate_is_personal_and_never_mutates_legacy_shared_state(
    db, editor_client, editor_user
):
    item_id = _insert_item(
        db,
        title="Personal Scan Rating",
        isbn="9780000015033",
    )
    # Personal state only needs visibility; library viewer is sufficient even
    # though the scanner itself still has the legacy global editor gate.
    libraries.set_membership(
        db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], "viewer"
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={"isbn": "9780000015033", "mode": "quick_rate"},
    )

    assert response.status_code == 200
    legacy = db.execute(
        "SELECT reading_status, date_finished FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    personal = db.execute(
        "SELECT reading_status, date_finished FROM user_item_state "
        "WHERE user_id = ? AND item_id = ?",
        (editor_user["id"], item_id),
    ).fetchone()
    assert legacy["reading_status"] is None
    assert legacy["date_finished"] is None
    assert personal["reading_status"] == "read"
    assert personal["date_finished"] is not None


def test_wishlist_scan_of_visible_existing_item_is_personal(
    db, editor_client, editor_user
):
    item_id = _insert_item(
        db,
        title="Existing Wishlist Candidate",
        isbn="9780000015040",
        owned=1,
    )
    db.commit()

    response = editor_client.post(
        "/api/scan",
        data={"isbn": "9780000015040", "media_type": "book", "mode": "wishlist"},
    )

    assert response.status_code == 200
    assert "Existing Wishlist Candidate" in response.text
    state = db.execute(
        "SELECT wishlist FROM user_item_state WHERE user_id = ? AND item_id = ?",
        (editor_user["id"], item_id),
    ).fetchone()
    owned = db.execute("SELECT owned FROM items WHERE id = ?", (item_id,)).fetchone()["owned"]
    assert state["wishlist"] == 1
    assert owned == 1


def test_new_wishlist_scan_sets_shared_not_owned_and_personal_wishlist(
    db, editor_client, editor_user
):
    metadata = {
        "title": "New Wishlist Book",
        "authors": "Author",
        "publisher": "Publisher",
        "publish_year": 2026,
    }
    lookup = AsyncMock(
        return_value=(
            metadata,
            "openlibrary",
            {},
            provider_result.no_match("isbn-cascade"),
        )
    )
    with patch("app.routers.items_common._lookup_metadata", new=lookup):
        response = editor_client.post(
            "/api/scan",
            data={
                "isbn": "9780000015057",
                "media_type": "book",
                "mode": "wishlist",
            },
        )

    assert response.status_code == 200
    row = db.execute(
        "SELECT id, owned FROM items WHERE isbn = '9780000015057'"
    ).fetchone()
    assert row is not None
    assert row["owned"] == 0
    state = db.execute(
        "SELECT wishlist FROM user_item_state WHERE user_id = ? AND item_id = ?",
        (editor_user["id"], row["id"]),
    ).fetchone()
    assert state["wishlist"] == 1


def test_new_scan_refuses_main_library_without_editor_membership_before_lookup(
    db, editor_client, editor_user
):
    libraries.set_membership(
        db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], "viewer"
    )
    db.commit()
    lookup = AsyncMock()

    with patch("app.routers.items_common._lookup_metadata", new=lookup):
        response = editor_client.post(
            "/api/scan",
            data={"isbn": "9780000015064", "media_type": "book", "mode": "add"},
        )

    assert response.status_code == 403
    lookup.assert_not_awaited()
    assert db.execute(
        "SELECT 1 FROM items WHERE isbn = '9780000015064'"
    ).fetchone() is None


def test_manual_copy_suggestions_and_template_hide_private_items(
    db, editor_client
):
    visible = _insert_item(
        db,
        title="Copy Candidate Visible",
        isbn="9780000015071",
        authors="Visible Author",
    )
    _, hidden = _private_item(
        db,
        title="Copy Candidate Hidden",
        isbn="9780000015088",
        authors="Hidden Author",
    )
    db.commit()

    suggestions = editor_client.get("/api/items/suggest", params={"q": "Copy Candidate"})
    assert suggestions.status_code == 200
    assert visible in {row["id"] for row in suggestions.json()}
    assert hidden not in {row["id"] for row in suggestions.json()}

    hidden_template = editor_client.get(f"/api/items/{hidden}/copy-template")
    assert hidden_template.status_code == 404


def test_recent_scans_hide_private_linked_history(db, editor_client):
    visible = _insert_item(
        db,
        title="Visible Scan History",
        isbn="9780000015095",
    )
    _, hidden = _private_item(
        db,
        title="Hidden Scan History",
        isbn="9780000015101",
    )
    db.execute(
        "INSERT INTO scan_log (isbn, media_type, result, item_id, mode) "
        "VALUES (?, 'book', 'added', ?, 'add')",
        ("9780000015095", visible),
    )
    db.execute(
        "INSERT INTO scan_log (isbn, media_type, result, item_id, mode) "
        "VALUES (?, 'book', 'added', ?, 'add')",
        ("9780000015101", hidden),
    )
    db.commit()

    html = editor_client.get("/api/recent-scans", params={"mode": "add"}).text
    assert "Visible Scan History" in html
    assert "Hidden Scan History" not in html
    assert "9780000015101" not in html


def test_inventory_missing_lists_only_library_editable_items(
    db, editor_client, editor_user
):
    location = _insert_location(db, "Inventory Shelf")
    visible = _insert_item(
        db,
        title="Editable Missing",
        isbn="9780000015118",
        location_id=location,
    )
    private = libraries.create_library(db, "Private Inventory")
    hidden = _insert_item(
        db,
        title="Private Missing",
        isbn="9780000015125",
        location_id=location,
        _library_id=private["id"],
    )
    db.commit()

    response = editor_client.post(
        "/api/inventory/missing",
        data={"location_id": str(location), "scanned_ids": ""},
    )

    assert response.status_code == 200
    assert "Editable Missing" in response.text
    assert "Private Missing" not in response.text
    assert f"/item/{visible}" in response.text
    assert f"/item/{hidden}" not in response.text
