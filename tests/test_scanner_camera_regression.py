from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_camera_scanner_requires_a_confirmed_read_and_keeps_html5_qrcode_off_ios():
    source = (ROOT / "static/js/scanner-engine.js").read_text(encoding="utf-8")

    assert "var CONFIRM_WINDOW_MS = 1800;" in source
    assert "decodedText === candidate" in source
    assert "createDecodeGuard(opts.onDecode)" in source
    assert "(opts.forceZxing || isIosDevice())" in source
    assert "? createZxingEngine(guardedOpts)" in source
    assert ": createHtml5Engine(guardedOpts);" in source


def test_scan_page_and_edit_fields_share_compact_barcode_camera_behavior():
    scan_source = (ROOT / "static/js/scan.js").read_text(encoding="utf-8")
    edit_source = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")
    edit_template = (ROOT / "app/templates/item_edit.html").read_text(encoding="utf-8")

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
    assert 'data-scan-barcode-target="{{ name }}"' in edit_template
    assert 'data-scan-barcode-supplement-target="{{ supplement_target }}"' in edit_template
    assert 'barcode_field("isbn", "ISBN"' in edit_template
    assert 'barcode_field("upc", "Barcode (UPC / EAN)"' in edit_template
    assert 'barcode_field("magazine_barcode_ean", "Magazine barcode carrier"' in edit_template
    assert 'barcode_field("magazine_barcode_supplement", "Barcode add-on"' in edit_template
    assert 'scan_mode="periodical-carrier"' in edit_template
    assert 'scan_mode="periodical-supplement"' in edit_template
    assert 'action="/api/items/{{ item.id }}/edit"' in edit_template
    assert 'src="/static/js/scanner-engine.js"' in edit_template