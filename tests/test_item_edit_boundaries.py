"""Request-boundary regressions for single-item edit and quick status writes."""

from app.routers import items
from app.services import periodical_records
from tests.conftest import _insert_item


def _row(db, item_id):
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def _forbid_cover_save(*args, **kwargs):
    raise AssertionError("cover storage must not run for a rejected edit")


def test_edit_rejects_blank_title_without_mutation(editor_client, db):
    item_id = _insert_item(db, title="Keep Title", isbn="9780441172719")
    db.commit()

    response = editor_client.post(f"/api/items/{item_id}", data={"title": ""})

    assert response.status_code == 400
    assert response.text == "Title is required"
    assert _row(db, item_id)["title"] == "Keep Title"


def test_edit_rejects_malformed_integer_without_mutation(editor_client, db):
    item_id = _insert_item(
        db,
        title="Keep Year",
        isbn=None,
        media_type="dvd",
        publish_year=1965,
    )
    db.commit()

    # A hand-entered or camera-populated UPC-A is accepted by the edit wrapper
    # and stored in Shelf's canonical EAN-13 form.
    saved = editor_client.post(
        f"/api/items/{item_id}/edit",
        data={"upc": "036000291452"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert _row(db, item_id)["upc"] == "0036000291452"

    # Ordinary edit validation still runs before a newly supplied barcode can
    # replace the existing one, so a failed save cannot partially alter it.
    response = editor_client.post(
        f"/api/items/{item_id}/edit",
        data={"publish_year": "nineteen-sixty-five", "upc": "042100005264"},
    )

    assert response.status_code == 400
    assert response.text == "Invalid publish year"
    row = _row(db, item_id)
    assert row["publish_year"] == 1965
    assert row["upc"] == "0036000291452"


def test_magazine_issue_barcode_can_be_typed_or_split_from_full_scan(editor_client, db):
    item_id = _insert_item(db, title="Test Magazine", isbn=None, media_type="magazine")
    publication_id = periodical_records.upsert_publication(db, title="Test Magazine")
    periodical_records.link_issue(
        db,
        item_id=item_id,
        publication_id=publication_id,
        issue_number="1",
        barcode_ean="9770953616115",
        barcode_supplement="01",
    )
    db.commit()

    edit_page = editor_client.get(f"/item/{item_id}/edit")
    assert edit_page.status_code == 200
    assert 'name="magazine_barcode_ean"' in edit_page.text
    assert 'value="9770953616115"' in edit_page.text
    assert 'name="magazine_barcode_supplement"' in edit_page.text
    assert 'value="01"' in edit_page.text

    # A scanner may return carrier + add-on as one decoded string. The edit
    # boundary splits it while retaining the add-on as an opaque discriminator.
    saved = editor_client.post(
        f"/api/items/{item_id}/edit",
        data={
            "magazine_barcode_ean": "977095361611504",
            "magazine_barcode_supplement": "04",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    issue = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert issue["barcode_ean"] == "9770953616115"
    assert issue["barcode_supplement"] == "04"


def test_magazine_issue_barcode_mismatch_rejected_before_item_edit(editor_client, db):
    item_id = _insert_item(db, title="Keep Magazine", isbn=None, media_type="magazine")
    publication_id = periodical_records.upsert_publication(db, title="Keep Magazine")
    periodical_records.link_issue(
        db,
        item_id=item_id,
        publication_id=publication_id,
        barcode_ean="9770953616115",
        barcode_supplement="01",
    )
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/edit",
        data={
            "title": "Do Not Save",
            "magazine_barcode_ean": "977095361611504",
            "magazine_barcode_supplement": "01",
        },
    )

    assert response.status_code == 400
    assert response.text == "Embedded barcode add-on does not match the add-on field"
    assert _row(db, item_id)["title"] == "Keep Magazine"
    issue = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert issue["barcode_ean"] == "9770953616115"
    assert issue["barcode_supplement"] == "01"


def test_edit_rejects_malformed_float_without_mutation(editor_client, db):
    item_id = _insert_item(db, title="Keep Value", isbn="9780441172719", manual_value=12.5)
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}", data={"manual_value": "twelve-pounds"}
    )

    assert response.status_code == 400
    assert response.text == "Invalid manual value"
    assert _row(db, item_id)["manual_value"] == 12.5


def test_edit_rejects_unknown_media_type_without_mutation(editor_client, db):
    item_id = _insert_item(db, title="Keep Type", isbn="9780441172719", media_type="book")
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}", data={"media_type": "laserdisc"}
    )

    assert response.status_code == 400
    assert response.text == "Invalid media type"
    assert _row(db, item_id)["media_type"] == "book"


def test_edit_rejects_unknown_reading_status_without_mutation(editor_client, db):
    item_id = _insert_item(
        db, title="Keep Status", isbn="9780441172719", reading_status="reading"
    )
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}", data={"reading_status": "abandoned"}
    )

    assert response.status_code == 400
    assert response.text == "Invalid reading status"
    assert _row(db, item_id)["reading_status"] == "reading"


def test_edit_rejects_invalid_owned_value_without_mutation(editor_client, db):
    item_id = _insert_item(db, title="Keep Owned", isbn="9780441172719", owned=1)
    db.commit()

    response = editor_client.post(f"/api/items/{item_id}", data={"owned": "2"})

    assert response.status_code == 400
    assert response.text == "Owned must be 0 or 1"
    assert _row(db, item_id)["owned"] == 1


def test_edit_rejects_stale_location_before_cover_storage(editor_client, db, monkeypatch):
    item_id = _insert_item(db, title="Keep Location", isbn="9780441172719")
    db.commit()
    monkeypatch.setattr(items.covers, "save_uploaded_cover", _forbid_cover_save)

    response = editor_client.post(
        f"/api/items/{item_id}",
        data={"location_id": "999999"},
        files={"cover": ("cover.jpg", b"x" * 200, "image/jpeg")},
    )

    assert response.status_code == 400
    assert response.text == "Location not found"
    assert _row(db, item_id)["location_id"] is None


def test_edit_missing_item_rejected_before_cover_storage(editor_client, monkeypatch):
    monkeypatch.setattr(items.covers, "save_uploaded_cover", _forbid_cover_save)

    response = editor_client.post(
        "/api/items/999999",
        files={"cover": ("cover.jpg", b"x" * 200, "image/jpeg")},
    )

    assert response.status_code == 404
    assert response.text == "Not found"


def test_edit_catalogue_conflict_rejected_before_cover_storage(editor_client, db, monkeypatch):
    keep_id = _insert_item(db, title="Existing ISBN", isbn="9780441172719", media_type="book")
    edit_id = _insert_item(db, title="Edited ISBN", isbn="9780156027601", media_type="book")
    db.commit()
    monkeypatch.setattr(items.covers, "save_uploaded_cover", _forbid_cover_save)

    response = editor_client.post(
        f"/api/items/{edit_id}",
        data={"isbn": "9780441172719"},
        files={"cover": ("cover.jpg", b"x" * 200, "image/jpeg")},
    )

    assert response.status_code == 409
    assert response.text == "Update conflicts with existing catalogue data"
    assert _row(db, keep_id)["isbn"] == "9780441172719"
    assert _row(db, edit_id)["isbn"] == "9780156027601"


def test_quick_status_rejects_unknown_value_without_clearing(editor_client, db):
    item_id = _insert_item(db, title="Keep Quick Status", isbn="9780441172719")
    db.execute(
        "UPDATE items SET reading_status = 'read', date_started = '2026-01-01', "
        "date_finished = '2026-01-02' WHERE id = ?",
        (item_id,),
    )
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/reading-status", data={"status": "abandoned"}
    )

    assert response.status_code == 400
    assert response.text == "Invalid reading status"
    row = _row(db, item_id)
    assert row["reading_status"] == "read"
    assert row["date_started"] == "2026-01-01"
    assert row["date_finished"] == "2026-01-02"
    assert db.execute(
        "SELECT COUNT(*) FROM reading_log WHERE item_id = ?", (item_id,)
    ).fetchone()[0] == 0