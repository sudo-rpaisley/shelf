from pathlib import Path
import json
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


def test_camera_scanner_requires_a_confirmed_read_and_keeps_html5_qrcode_off_ios():
    source = (ROOT / "static/js/scanner-engine.js").read_text(encoding="utf-8")

    assert "var CONFIRM_WINDOW_MS = 1800;" in source
    assert "decodedText === candidate" in source
    assert "createDecodeGuard(opts.onDecode)" in source
    assert "(opts.forceZxing || isIosDevice())" in source
    assert "? createZxingEngine(guardedOpts)" in source
    assert ": createHtml5Engine(guardedOpts);" in source


def test_scan_page_and_edit_fields_share_compact_barcode_camera_behavior(admin_client, db):
    from app.services import periodical_records, provider_result
    from tests.conftest import _insert_item
    from tests.test_ui_phase4_contracts_20261121 import _Markup

    scan_source = (ROOT / "static/js/scan.js").read_text(encoding="utf-8")
    edit_source = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")
    artwork_source = (ROOT / "static/js/item_edit_artwork.js").read_text(encoding="utf-8")

    assert "qrbox: { width: 280, height: 100 }" in scan_source
    assert "aspectRatio: 1.5" in scan_source
    assert "qrbox: { width: 280, height: 100 }" in edit_source
    assert "window.createBarcodeScanner" in edit_source
    assert "applyScannedBarcode(target, decodedText, scanMode, supplementTargetId)" in edit_source
    assert "forceZxing: scanMode === 'periodical-supplement'" in edit_source
    assert "digits.length === 15 || digits.length === 18" in edit_source
    assert "digits.length === 14 || digits.length === 17" in edit_source
    assert "digits.length === 2 || digits.length === 5" in edit_source
    assert "fetch(" not in edit_source
    assert ".submit(" not in edit_source
    assert "requestSubmit(" not in edit_source
    assert "if (!window.isSecureContext)" in edit_source

    book_id = _insert_item(db, title="Compact camera book", isbn="9780306406157",
                           cover_path="covers/compact-camera.jpg")
    magazine_id = _insert_item(db, title="Compact camera issue", isbn=None, media_type="magazine")
    publication_id = periodical_records.upsert_publication(db, title="Compact camera publication", issn="0161-7370")
    periodical_records.link_issue(db, item_id=magazine_id, publication_id=publication_id,
                                  barcode_ean="9770161737008", barcode_supplement="05")
    db.commit()
    response = admin_client.get(f"/item/{book_id}/edit")
    assert response.status_code == 200
    root = _Markup(response.text).root
    form = root.one("form", id="item-edit-form")
    assert form.attrs["action"] == f"/api/items/{book_id}"
    assert form.attrs["method"] == "post"
    assert form.one("button", **{"data-testid": "save-btn"}).attrs["type"] == "submit"
    assert root.one("script", src="/static/js/scanner-engine.js")
    # Native ISBN/UPC components replace the retired barcode_field macro.
    # Pin their REAL inputs, labels and scan-only controls, not macro syntax.
    for name, factory, label, placeholder in (
        ("isbn", "isbnCamera", "ISBN", "ISBN-10 or ISBN-13"),
        ("upc", "upcCamera", "UPC / EAN", "UPC-A or EAN-13"),
    ):
        camera = form.one(**{"x-data": factory})
        control = camera.one("input", id=name, name=name)
        assert control.attrs["placeholder"] == placeholder
        assert "disabled" not in control.attrs
        assert camera.one("label", **{"for": name}).text == label
        assert camera.one("button", **{"@click": "startCamera()"}).attrs["type"] == "button"
        assert camera.one("div", id=f"edit-{name}-camera-reader")
        assert camera.one("video", id=f"edit-{name}-zxing-video")

    issue = admin_client.get(f"/item/{magazine_id}/edit")
    assert issue.status_code == 200
    issue_form = _Markup(issue.text).root.one("form", id="item-edit-form")
    for name, label, value, mode in (
        ("magazine_barcode_ean", "Magazine barcode carrier", "9770161737008", "periodical-carrier"),
        ("magazine_barcode_supplement", "Barcode add-on", "05", "periodical-supplement"),
    ):
        assert issue_form.one("label", **{"for": name}).text == label
        control = issue_form.one("input", id=name, name=name)
        assert control.attrs["value"] == value
        assert "disabled" not in control.attrs
        button = issue_form.one("button", **{"data-scan-barcode-target": name})
        assert button.attrs["type"] == "button"
        assert button.attrs["data-scan-barcode-mode"] == mode
        assert button.attrs["data-scan-barcode-supplement-target"] == "magazine_barcode_supplement"

    # The item editor is sectioned like a familiar media-server metadata
    # editor while preserving the scanner's isolated, no-fetch module.
    assert root.one(**{"x-data": "editSections"})
    nav = root.one("nav", **{"data-testid": "edit-section-nav"})
    for key, label in (("general", "General"), ("artwork", "Artwork"),
                       ("identifiers", "Identifiers"), ("location", "Copies & Location")):
        assert nav.one("a", href=f"#edit-{key}").text == label
    for section in ("general", "artwork", "identifiers", "copies"):
        assert form.one(**{"data-testid": f"edit-section-{section}"})
    assert form.one("button", **{"hx-get": f"/api/items/{book_id}/cover-search?editor=1"})
    assert form.one("input", id="edit-cover-url", name="url").attrs["type"] == "url"
    url_button = form.one("button", **{"data-testid": "edit-cover-url-submit"})
    assert url_button.attrs["type"] == "button"
    assert url_button.attrs["hx-post"] == f"/api/items/{book_id}/cover-url"
    assert json.loads(url_button.attrs["hx-vals"]) == {"return_to": "edit"}
    assert form.one("button", **{"data-edit-cover-remove": None}).attrs["type"] == "button"
    assert root.one("script", src="/static/js/item_edit_artwork.js")

    candidate = {"url": "https://covers.openlibrary.org/b/id/123-L.jpg",
                 "thumbnail": "https://covers.openlibrary.org/b/id/123-S.jpg", "source": "openlibrary"}
    search = AsyncMock(return_value=provider_result.found("openlibrary", [candidate]))
    with patch("app.routers.items_covers.covers.search_covers", new=search):
        picker_response = admin_client.get(f"/api/items/{book_id}/cover-search?editor=1")
    assert picker_response.status_code == 200
    search.assert_awaited_once()
    assert search.await_args.args[1] == "Compact camera book"
    picker = _Markup(picker_response.text).root
    choice = picker.one("button", **{"data-edit-cover-select-url": candidate["url"]})
    assert choice.attrs["type"] == "button"
    assert "hx-post" not in choice.attrs
    assert json.loads(choice.attrs["data-edit-cover-values"]) == {"url": candidate["url"], "return_to": "edit"}
    assert picker.find_all("form") == []
    assert "'X-CSRF-Token'" in artwork_source
