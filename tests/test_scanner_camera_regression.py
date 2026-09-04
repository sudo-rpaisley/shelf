from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_camera_scanner_requires_a_confirmed_read_and_keeps_html5_qrcode_off_ios():
    source = (ROOT / "static/js/scanner-engine.js").read_text(encoding="utf-8")

    assert "var CONFIRM_WINDOW_MS = 1800;" in source
    assert "decodedText === candidate" in source
    assert "createDecodeGuard(opts.onDecode)" in source
    assert "? createZxingEngine(guardedOpts)" in source
    assert ": createHtml5Engine(guardedOpts);" in source


def test_scan_page_keeps_original_compact_barcode_framing_rectangle():
    source = (ROOT / "static/js/scan.js").read_text(encoding="utf-8")

    assert "qrbox: { width: 280, height: 100 }" in source
    assert "aspectRatio: 1.5" in source
