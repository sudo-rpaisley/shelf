"""Offline guards for scan-page fetch error handling."""

from pathlib import Path

import pytest


def _guarded_fetches(js):
    """Pin each live request's own guard BEFORE its body read, not a total."""
    boundaries = (
        ("loadRecentScans(m) {", "toggleManual() {", "/api/recent-scans?mode=", "r", "return r.text();"),
        ("installShelfFillPicker() {", "persistReadingStatus() {", "/api/shelf-fill/location-picker", "r", "return r.text();"),
        ("async onScan(code) {", "\n    };", "fetch('/api/scan'", "resp", "var html = await resp.text();"),
    )
    guards = []
    for start, end, endpoint, response, consume in boundaries:
        block = js.split(start, 1)[1].split(end, 1)[0]
        guard = f"if (!{response}.ok) throw new Error('HTTP ' + {response}.status);"
        assert endpoint in block
        assert guard in block, endpoint
        assert block.index(guard) < block.index(consume), endpoint
        guards.append((block, guard, consume))
    return guards


def test_scan_fetch_paths_reject_http_errors_before_consuming_bodies():
    js = Path(__file__).resolve().parent.parent.joinpath("static/js/scan.js").read_text()

    guards = _guarded_fetches(js)
    assert "showToast(t('Could not load recent scans'), 'error');" in js
    assert "title: t('Scan failed')" in js
    # Server-side audits own missing accounting; refresh the actual strip via
    # HTMX instead of resurrecting the retired missing-items fetch/client array.
    audit = js.split("refreshAudit() {", 1)[1].split("syncAuditFromStrip(strip) {", 1)[0]
    assert "'/api/audits/summary?audit_id='" in audit
    assert "htmx.ajax('GET', url, {target: '#audit-strip', swap: 'innerHTML'});" in audit
    assert "if (scanMode === 'inventory' && this.auditId) this.refreshAudit();" in js

    # Meaningful controls: EVERY live request loses protection if its own
    # guard is removed or moved after consumption (even with the same count).
    for block, guard, consume in guards:
        without = block.replace(guard, "", 1)
        with pytest.raises(AssertionError):
            _guarded_fetches(js.replace(block, without, 1))
        reordered = without.replace(consume, consume + "\n" + guard, 1)
        with pytest.raises(AssertionError):
            _guarded_fetches(js.replace(block, reordered, 1))
