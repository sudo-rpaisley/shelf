"""Settings → Library → Trash: the retention-days round trip and the
cache reset it must trigger on save (G13's third invalidation site).

Seeds trashed rows via `app.services.item_write.insert_item` +
`item_write.trash_item`, backdating with a raw `UPDATE` the same way
`tests/test_trash_service.py` does — `deleted_at` is spelled once, in
`app/`, and a test may post-date it directly.
"""

import pytest

from app.services import trash
from app.services.item_write import insert_item, trash_item


def _item(db, title="Trash Settings Item"):
    return insert_item(db, title=title, source="test")


def _trash_and_backdate(db, item_id, days):
    trash_item(db, item_id)
    db.execute(
        "UPDATE items SET deleted_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", item_id),
    )
    db.commit()


class TestSettingsPageShowsTheCard:
    def test_default_value_when_unset(self, admin_client):
        html = admin_client.get("/settings").text
        assert 'id="trash_retention_days"' in html
        assert 'value="180"' in html

    def test_stored_value_is_reflected(self, admin_client, db):
        db.execute("INSERT INTO settings (key, value) VALUES ('trash_retention_days', '45')")
        db.commit()
        html = admin_client.get("/settings").text
        assert 'id="trash_retention_days"' in html
        assert 'value="45"' in html


class TestSaveTrashSettings:
    def test_saves_the_value(self, admin_client, db):
        response = admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "90"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()
        assert row["value"] == "90"

    def test_invalidates_the_cached_count(self, admin_client, db):
        # Prime the cache so trash._cache is populated before the save.
        assert trash.expired_count() == 0
        assert trash._cache is not None

        admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "90"},
            follow_redirects=False,
        )
        assert trash._cache is None

    def test_saving_a_shorter_window_moves_the_count_at_once(self, admin_client, db):
        item_id = _item(db)
        _trash_and_backdate(db, item_id, 100)

        # At the default 180-day window, a row backdated 100 days isn't
        # expired yet — prime the cache on that answer.
        assert trash.expired_count() == 0
        assert trash._cache is not None

        response = admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "90"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        assert trash.expired_count() == 1

    def test_non_numeric_value_is_refused_and_the_row_is_untouched(self, admin_client, db):
        db.execute("INSERT INTO settings (key, value) VALUES ('trash_retention_days', '30')")
        db.commit()

        response = admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "abc"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "message": "Retention days must be a whole number",
        }
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()
        assert row["value"] == "30"

    @pytest.mark.parametrize("value", ["²", "١٨٠", "-1", "1.5", "36501", "9" * 30])
    def test_values_int_cannot_bound_are_refused(self, admin_client, db, value):
        response = admin_client.post(
            "/api/settings/trash", data={"trash_retention_days": value},
        )
        assert response.json()["ok"] is False
        assert db.execute(
            "SELECT 1 FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone() is None

    def test_the_largest_window_is_stored(self, admin_client, db):
        response = admin_client.post(
            "/api/settings/trash", data={"trash_retention_days": "36500"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert db.execute(
            "SELECT value FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()["value"] == "36500"

    def test_zero_is_stored(self, admin_client, db):
        response = admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()
        assert row["value"] == "0"

    def test_empty_submit_stores_the_default(self, admin_client, db):
        response = admin_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()
        assert row["value"] == "180"

    def test_editor_is_refused(self, editor_client):
        response = editor_client.post(
            "/api/settings/trash",
            data={"trash_retention_days": "90"},
        )
        assert response.status_code == 403
