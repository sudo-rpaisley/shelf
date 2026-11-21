"""Settings → Integrations → Collection Valuation: the Price alerts block and
POST /api/settings/price-alerts."""
import re

import pytest

from app.database import get_db


def _block(html):
    """The rendered price-alerts block, so assertions cannot match elsewhere."""
    start = html.index('data-testid="price-alerts-block"')
    end = html.index("</form>", start)
    return html[start:end]


def _value(block, testid):
    tag = re.search(r'<input[^>]*data-testid="%s"[^>]*>' % testid, block).group(0)
    return re.search(r'value="([^"]*)"', tag).group(1)


def _setting(key):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    db.commit()


class TestBlock:
    def test_defaults_render_when_nothing_is_saved(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert _value(block, "price-alert-threshold") == "15"
        assert _value(block, "price-alert-cap") == "100"
        assert "Last run: Never" in block

    def test_honesty_line_is_visible_text(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert ("Tracks the publisher's list price. Good for spotting reprints and price "
                "cuts, not used-market deals.") in block

    def test_saved_values_round_trip(self, admin_client):
        """G36: re-post what the form rendered, then change one field."""
        block = _block(admin_client.get("/settings").text)
        form = {
            "price_alert_threshold_pct": _value(block, "price-alert-threshold"),
            "price_alert_nightly_cap": _value(block, "price-alert-cap"),
        }
        form["price_alert_threshold_pct"] = "20"
        resp = admin_client.post("/api/settings/price-alerts", data=form, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"

        block = _block(admin_client.get("/settings").text)
        assert _value(block, "price-alert-threshold") == "20"
        assert _value(block, "price-alert-cap") == "100"

        form = {
            "price_alert_threshold_pct": _value(block, "price-alert-threshold"),
            "price_alert_nightly_cap": "250",
        }
        admin_client.post("/api/settings/price-alerts", data=form)
        block = _block(admin_client.get("/settings").text)
        assert _value(block, "price-alert-threshold") == "20"
        assert _value(block, "price-alert-cap") == "250"

    def test_last_run_renders_as_utc(self, admin_client, db):
        _set(db, "price_alert_last_run", "1790000000")  # 2026-09-21 14:13:20 UTC
        block = _block(admin_client.get("/settings").text)
        assert "Last run: 2026-09-21 14:13 UTC" in block

    def test_last_run_shows_the_failure_summary(self, admin_client, db):
        _set(db, "price_alert_last_run", "1790000000")
        _set(db, "price_alert_last_summary", '{"looked_up": 1, "failed": 1, "status": 401}')
        block = _block(admin_client.get("/settings").text)
        assert ("Last run: 2026-09-21 14:13 UTC — 1 checked, 1 failed "
                "(ISBNdb refused the API key)") in block

    def test_notify_link_shows_without_a_notify_url(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="price-alert-notify-hint"' in block
        assert 'data-testid="price-alert-notify-link"' in block
        # test-drive Obs 4: the link lands on the field, not just the tab.
        assert "goToField('library', 'notify-url-input')" in block

    def test_notify_link_hidden_once_a_notify_url_row_exists(self, admin_client, db, monkeypatch):
        monkeypatch.delenv("NOTIFY_URL", raising=False)
        _set(db, "notify_url", "https://ntfy.example/shelf")
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="price-alert-notify-hint"' in block
        assert 'data-testid="price-alert-notify-link"' not in block

    def test_feature_off_note_appears_with_the_flag_off(self, admin_client):
        assert 'data-feature="price_alerts"' not in _block(admin_client.get("/settings").text)
        admin_client.post("/api/settings/features/price_alerts", data={"enabled": "0"})
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="feature-off-note" data-feature="price_alerts"' in block


class TestSave:
    @pytest.mark.parametrize("threshold,cap", [
        ("0", "100"), ("101", "100"), ("abc", "100"), ("²", "100"), ("-5", "100"),
        ("15", "0"), ("15", "501"), ("15", "x"),
    ])
    def test_rejects_out_of_range_and_writes_nothing(self, admin_client, threshold, cap):
        resp = admin_client.post(
            "/api/settings/price-alerts",
            data={"price_alert_threshold_pct": threshold, "price_alert_nightly_cap": cap},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "whole number" in body["message"]
        assert _setting("price_alert_threshold_pct") is None
        assert _setting("price_alert_nightly_cap") is None

    def test_bounds_are_accepted(self, admin_client):
        resp = admin_client.post(
            "/api/settings/price-alerts",
            data={"price_alert_threshold_pct": "100", "price_alert_nightly_cap": "500"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _setting("price_alert_threshold_pct") == "100"
        assert _setting("price_alert_nightly_cap") == "500"

    def test_editor_gets_role_refusal(self, editor_client):
        resp = editor_client.post(
            "/api/settings/price-alerts",
            data={"price_alert_threshold_pct": "20", "price_alert_nightly_cap": "50"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _setting("price_alert_threshold_pct") is None
