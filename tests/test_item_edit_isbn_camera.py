"""Regression coverage for scanning an ISBN into the normal edit form."""

from pathlib import Path

from tests.conftest import _insert_item
from tests.test_item_editor_partials import EditorDOM


ROOT = Path(__file__).resolve().parents[1]


def test_item_edit_isbn_camera_uses_rendered_ui_and_shared_scanner(_isolated_db, editor_client, db):
    isbn = "9780547928227"
    item_id = _insert_item(db, title="ISBN camera", isbn=isbn)
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert response.status_code == 200
    dom = EditorDOM(response.text)
    source = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")

    # Inspect the real shared fields rendered by the registered edit route,
    # rather than requiring their Jinja calls to live in the host template.
    camera = dom.form.one("div", **{"x-data": "isbnCamera"})
    control = camera.one("input", id="isbn", name="isbn")
    assert control.attrs["type"] == "text"
    assert control.attrs["placeholder"] == "ISBN-13"
    assert control.attrs["value"] == isbn
    assert camera.one("label", **{"for": "isbn"}).text.strip() == "ISBN"
    for action in ("startCamera()", "stopCamera()"):
        assert camera.one("button", **{"@click": action}).attrs["type"] == "button"
    overlay = camera.one("div", **{"x-show": "cameraActive"})
    assert overlay.attrs["style"] == "display:none"
    assert overlay.one("div", id="edit-isbn-camera-reader").attrs["x-show"] == "!isZxingFallback"
    video = overlay.one("video", id="edit-isbn-zxing-video")
    assert video.parent.attrs["x-show"] == "isZxingFallback"
    assert {"autoplay", "muted", "playsinline"} <= video.attrs.keys()
    assert "document.createElement" not in source

    # Match the existing /scan page's vendored fallback chain and shared
    # scanner engine instead of introducing another camera implementation.
    for src in (
        "/static/vendor/html5-qrcode-2.3.8.min.js",
        "/static/vendor/zxing-browser-0.1.5.min.js",
        "/static/js/scanner-engine.js", "/static/js/item_edit.js",
    ):
        assert dom.root.one("script", src=src)
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
