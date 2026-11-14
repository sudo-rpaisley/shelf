from pathlib import Path

from app.services import upc
from tests.test_item_editor_partials import EditorDOM


UPC_A = "078073003501"
EAN_13 = "4006381333931"


def _item(db, *, title="Disc", media_type="dvd", upc_value=None):
    return db.execute(
        "INSERT INTO items (title, media_type, owned, upc) VALUES (?, ?, 1, ?)",
        (title, media_type, upc_value),
    ).lastrowid


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
    response = editor_client.post(
        f"/api/items/{item_id}",
        data={"upc": UPC_A},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
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
    assert response.status_code in (302, 303)
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
    assert response.status_code in (302, 303)
    assert "error=upc_conflict" in response.headers["location"]
    row = db.execute("SELECT upc FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["upc"] is None


def test_upc_error_banners_are_rendered_by_item_edit_template():
    template = Path("app/templates/item_edit.html").read_text()
    assert 'error == "invalid_upc"' in template
    assert 'error == "upc_conflict"' in template


def test_upc_camera_is_scan_only_and_reuses_shared_engine(_isolated_db, editor_client, db):
    item_id = _item(db, upc_value=EAN_13)
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert response.status_code == 200
    dom = EditorDOM(response.text)
    source = Path("static/js/item_edit.js").read_text()
    camera = dom.form.one("div", **{"x-data": "upcCamera"})
    assert "data-upc-editor" in camera.attrs
    control = camera.one("input", id="upc", name="upc")
    assert control.attrs["type"] == "text"
    assert control.attrs["placeholder"] == "UPC-A or EAN-13"
    assert control.attrs["value"] == EAN_13
    assert camera.one("label", **{"for": "upc"}).text.strip() == "UPC / EAN"
    for action in ("startCamera()", "stopCamera()"):
        assert camera.one("button", **{"@click": action}).attrs["type"] == "button"
    overlay = camera.one("div", **{"x-show": "cameraActive"})
    assert overlay.attrs["style"] == "display:none"
    assert overlay.one("div", id="edit-upc-camera-reader").attrs["x-show"] == "!isZxingFallback"
    video = overlay.one("video", id="edit-upc-zxing-video")
    assert video.parent.attrs["x-show"] == "isZxingFallback"
    assert {"autoplay", "muted", "playsinline"} <= video.attrs.keys()
    for src in (
        "/static/vendor/html5-qrcode-2.3.8.min.js",
        "/static/vendor/zxing-browser-0.1.5.min.js",
        "/static/js/scanner-engine.js", "/static/js/item_edit.js",
    ):
        assert dom.root.one("script", src=src)
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
