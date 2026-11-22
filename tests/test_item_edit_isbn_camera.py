"""Regression coverage for scanning an ISBN into the normal edit form."""

from pathlib import Path

from tests.conftest import _insert_item
from tests.test_ui_phase4_contracts_20261121 import _Markup


ROOT = Path(__file__).resolve().parents[1]


def test_item_edit_isbn_camera_uses_rendered_ui_and_shared_scanner(editor_client, db):
    item_id = _insert_item(db, title="ISBN camera", isbn="9780306406157")
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    root = _Markup(response.text).root
    form = root.one("form", id="item-edit-form")
    source = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")

    # The scanner UI belongs to the server-rendered edit template rather than
    # being assembled imperatively in JavaScript. Pin the actual successful
    # field and explicit save action rather than the macro's quote spelling.
    camera = form.one(**{"x-data": "isbnCamera"})
    assert camera.one("label", **{"for": "isbn"}).text == "ISBN"
    field = camera.one("input", id="isbn", name="isbn")
    assert field.attrs["value"] == "9780306406157"
    assert field.attrs["placeholder"] == "ISBN-10 or ISBN-13"
    assert "disabled" not in field.attrs
    assert camera.one("div", id="edit-isbn-camera-reader")
    assert camera.one("video", id="edit-isbn-zxing-video")
    assert camera.one("button", **{"@click": "startCamera()"}).attrs["type"] == "button"
    assert form.attrs["action"] == f"/api/items/{item_id}"
    assert form.attrs["method"] == "post"
    assert form.one("button", **{"data-testid": "save-btn"}).attrs["type"] == "submit"
    assert "document.createElement" not in source

    # Match the existing /scan page's vendored fallback chain and shared
    # scanner engine instead of introducing another camera implementation.
    assert root.one("script", src="/static/vendor/html5-qrcode-2.3.8.min.js")
    assert root.one("script", src="/static/vendor/zxing-browser-0.1.5.min.js")
    assert root.one("script", src="/static/js/scanner-engine.js")
    assert "window.createBarcodeScanner" in source
    assert "qrbox: { width: 280, height: 100 }" in source
    assert "aspectRatio: 1.5" in source

    # A scan only fills the ISBN field. Normal save and server validation stay
    # authoritative, and non-Bookland barcodes remain rejected in the camera UI.
    assert "digits.length !== 13" in source
    assert "digits.slice(0, 3) !== '978'" in source
    assert "digits.slice(0, 3) !== '979'" in source
    assert "input.value = digits" in source
    assert ".submit(" not in source
    assert "fetch(" not in source
