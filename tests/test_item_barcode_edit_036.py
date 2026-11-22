from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import upc
from tests.conftest import _insert_item
from tests.test_ui_phase4_contracts_20261121 import _Markup


UPC_A = "078073003501"
EAN_13 = "4006381333931"


def _item(db, *, title="Disc", media_type="dvd", upc_value=None):
    return _insert_item(db, title=title, isbn=None, media_type=media_type,
                        owned=1, upc=upc_value, source="manual", _library_id=1)


def test_editable_upc_canonicalises_upca_to_ean13():
    ok, value = upc.canonical_retail_barcode(UPC_A)
    assert ok is True
    assert value == "0" + UPC_A


def test_editable_upc_accepts_valid_non_book_ean_and_rejects_isbn():
    assert upc.canonical_retail_barcode(EAN_13) == (True, EAN_13)
    assert upc.canonical_retail_barcode("9780306406157") == (False, None)
    assert upc.canonical_retail_barcode("4006381333932") == (False, None)


def test_item_edit_can_save_upc_without_metadata_lookup(editor_client, db):
    item_id = _item(db)
    db.commit()
    with patch("app.routers.items_common._lookup_metadata", new=AsyncMock()) as lookup:
        response = editor_client.post(
            f"/api/items/{item_id}",
            data={"upc": UPC_A},
            follow_redirects=False,
        )
    assert response.status_code == 303
    lookup.assert_not_called()
    lookup.assert_not_awaited()
    row = db.execute("SELECT upc FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["upc"] == "0" + UPC_A


def test_item_edit_refuses_invalid_upc_on_edit_form(editor_client, db):
    item_id = _item(db)
    db.commit()
    response = editor_client.post(
        f"/api/items/{item_id}",
        data={"upc": "4006381333932"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=invalid_upc" in response.headers["location"]
    row = db.execute("SELECT upc FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["upc"] is None


def test_item_edit_refuses_same_upc_on_same_media_type(editor_client, db):
    _item(db, title="Existing", upc_value=EAN_13)
    item_id = _item(db, title="Other")
    db.commit()
    response = editor_client.post(
        f"/api/items/{item_id}",
        data={"media_type": "dvd", "upc": EAN_13},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=upc_conflict" in response.headers["location"]
    row = db.execute("SELECT upc FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["upc"] is None


def test_upc_error_banners_are_rendered_by_item_edit_template(editor_client, db):
    item_id = _item(db)
    db.commit()
    for error, message in (
        ("invalid_upc", "That UPC / EAN isn't a valid retail barcode — correct it or clear the field. Nothing was saved."),
        ("upc_conflict", "That identifier is unavailable. Nothing was saved."),
    ):
        response = editor_client.get(f"/item/{item_id}/edit?error={error}")
        assert response.status_code == 200
        banner = _Markup(response.text).root.one(**{"data-testid": "edit-error"})
        assert banner.attrs["role"] == "alert"
        assert banner.text == message


def test_upc_camera_is_scan_only_and_reuses_shared_engine(editor_client, db):
    item_id = _item(db, upc_value=EAN_13)
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    root = _Markup(response.text).root
    form = root.one("form", id="item-edit-form")
    source = Path("static/js/item_edit.js").read_text()
    camera = form.one(**{"data-upc-editor": None})
    assert camera.attrs["x-data"] == "upcCamera"
    assert camera.one("label", **{"for": "upc"}).text == "UPC / EAN"
    field = camera.one("input", id="upc", name="upc")
    assert field.attrs["value"] == EAN_13
    assert "disabled" not in field.attrs
    assert camera.one("div", id="edit-upc-camera-reader")
    assert camera.one("video", id="edit-upc-zxing-video")
    assert camera.one("button", **{"@click": "startCamera()"}).attrs["type"] == "button"
    assert form.attrs["action"] == f"/api/items/{item_id}"
    assert form.one("button", **{"data-testid": "save-btn"}).attrs["type"] == "submit"
    assert "window.createBarcodeScanner" in source
    assert "edit-upc-camera-reader" in source
    assert "input.value = canonical" in source
    assert "document.createElement" not in source
    assert "fetch(" not in source
    # The camera handler must not submit or call a metadata route. Saving stays
    # the user's explicit action on the existing item-edit form.
    camera_tail = source.split("function upcCamera", 1)[1]
    assert ".submit()" not in camera_tail
    assert "/api/scan" not in camera_tail
