"""The admin Trash banner: shown while Trash holds rows past the retention
window, to admins only, dismissable server-side.

Rows are trashed through the funnel and backdated on the test's connection,
then committed before the first request (G48).
"""

import sqlite3
from contextlib import contextmanager

from app.database import get_setting
from app.services import item_write, trash
from app.services.item_write import insert_item

BANNER = 'data-testid="trash-nag"'


def _expired(db, title, days=200):
    item_id = insert_item(db, title=title, source="test")
    item_write.trash_item(db, item_id)
    db.execute(
        "UPDATE items SET deleted_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", item_id),
    )
    return item_id


def _page(client):
    resp = client.get("/browse")
    assert resp.status_code == 200
    return resp.text


class TestWhoSeesIt:
    def test_admin_sees_the_banner_with_the_count_and_the_link(self, admin_client, db):
        _expired(db, "Old One")
        db.commit()
        html = _page(admin_client)
        assert BANNER in html
        assert "1 item in Trash has been there longer than your retention window." in html
        assert 'href="/trash?expired=1"' in html

    def test_plural_count(self, admin_client, db):
        _expired(db, "Old One")
        _expired(db, "Old Two")
        db.commit()
        assert "2 items in Trash have been there longer" in _page(admin_client)

    def test_no_banner_without_an_expired_row(self, admin_client, db):
        _expired(db, "Recent", days=10)
        db.commit()
        assert BANNER not in _page(admin_client)

    def test_editor_and_viewer_see_no_banner_and_reach_no_trash_code(
        self, editor_client, viewer_client, db, monkeypatch
    ):
        _expired(db, "Old One")
        db.commit()

        # Recorded, not raised: the wrapper swallows a failing read (no
        # banner rather than no page), so a raising stub could not go red.
        calls = []
        monkeypatch.setattr(trash, "nag_state", lambda *a, **k: calls.append("nag_state"))
        monkeypatch.setattr(trash, "_cached_entry", lambda *a, **k: calls.append("_cached_entry"))
        for client in (editor_client, viewer_client):
            html = _page(client)
            assert BANNER not in html
            assert "retention window" not in html
        assert calls == [], f"a non-admin render reached the Trash service: {calls}"

    def test_a_failing_read_means_no_banner_not_a_failed_page(self, admin_client, db, monkeypatch):
        _expired(db, "Old One")
        db.commit()

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(trash, "_cached_entry", boom)
        assert BANNER not in _page(admin_client)

    def test_zero_retention_never_shows_it(self, admin_client, db):
        _expired(db, "Old One")
        db.execute("INSERT INTO settings (key, value) VALUES ('trash_retention_days', '0')")
        db.commit()
        assert BANNER not in _page(admin_client)


class TestDismiss:
    def test_dismiss_hides_it_until_the_count_grows_and_restore_hides_it_again(self, admin_client, db):
        _expired(db, "Old One")
        db.commit()
        assert BANNER in _page(admin_client)

        resp = admin_client.post("/api/trash/nag/dismiss")
        assert resp.status_code == 200 and resp.text == ""
        assert get_setting(db, trash.NAG_DISMISSED_KEY) == "1"
        assert BANNER not in _page(admin_client)

        second = _expired(db, "Old Two")
        db.commit()
        trash.invalidate()  # the backdate is a raw write no funnel announces
        html = _page(admin_client)
        assert "2 items in Trash" in html

        item_write.restore_item(db, second)
        db.commit()
        assert BANNER not in _page(admin_client)

    def test_the_dismissal_survives_a_fresh_process(self, admin_client, db):
        _expired(db, "Old One")
        db.commit()
        admin_client.post("/api/trash/nag/dismiss")
        # A second process, or a restart: nothing in memory, the row remains.
        trash._cache = None
        assert BANNER not in _page(admin_client)

    def test_the_banner_returns_after_a_purge_lowered_the_count(self, admin_client, db):
        for n in range(3):
            _expired(db, f"Purged {n}")
        db.commit()
        admin_client.post("/api/trash/nag/dismiss")
        assert get_setting(db, trash.NAG_DISMISSED_KEY) == "3"

        assert admin_client.post("/api/trash/empty-expired").status_code == 200
        assert get_setting(db, trash.NAG_DISMISSED_KEY) == "0"

        _expired(db, "New One")
        _expired(db, "New Two")
        db.commit()
        trash.invalidate()  # the backdate is a raw write no funnel announces
        assert "2 items in Trash" in _page(admin_client)

    def test_a_restore_lowers_the_marker(self, admin_client, db):
        first = _expired(db, "Old One")
        _expired(db, "Old Two")
        db.commit()
        admin_client.post("/api/trash/nag/dismiss")
        assert admin_client.post(f"/api/trash/items/{first}/restore").status_code == 200
        assert get_setting(db, trash.NAG_DISMISSED_KEY) == "1"

    def test_a_longer_window_lowers_the_marker(self, admin_client, db):
        _expired(db, "Old One")
        db.commit()
        admin_client.post("/api/trash/nag/dismiss")
        admin_client.post(
            "/api/settings/trash", data={"trash_retention_days": "365"},
            follow_redirects=False,
        )
        assert get_setting(db, trash.NAG_DISMISSED_KEY) == "0"

    def test_editor_cannot_dismiss(self, editor_client):
        assert editor_client.post("/api/trash/nag/dismiss").status_code == 403

    def test_the_count_is_read_under_the_write_lock(self, admin_client, db, monkeypatch):
        """G18: no rival writer may commit between the count and the marker."""
        import app.config
        import app.routers.trash as trash_router

        _expired(db, "Old One")
        db.commit()
        probe_results = []
        real_get_db = trash_router.get_db

        class LockProbingConnection:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                result = self._conn.execute(sql, *args, **kwargs)
                if "(SELECT COUNT(*) FROM items i WHERE" in sql:
                    rival = sqlite3.connect(str(app.config.DATABASE_PATH), timeout=0)
                    try:
                        rival.execute("BEGIN IMMEDIATE")
                        probe_results.append("acquired")
                        rival.rollback()
                    except sqlite3.OperationalError as exc:
                        probe_results.append(f"locked: {exc}")
                    finally:
                        rival.close()
                return result

        @contextmanager
        def probing_get_db():
            with real_get_db() as conn:
                yield LockProbingConnection(conn)

        monkeypatch.setattr(trash_router, "get_db", probing_get_db)
        assert admin_client.post("/api/trash/nag/dismiss").status_code == 200
        assert probe_results, "the count never ran — the probe did not fire"
        assert probe_results[0].startswith("locked"), (
            f"got {probe_results[0]!r} — the dismiss read its count outside "
            "the write lock (G18)"
        )


class TestHandBuiltEnvironment:
    def test_base_renders_without_the_key(self):
        """G92's shape: `trash_nag` is context, not a global, so a hand-built
        environment that never injects it must still render base.html."""
        from jinja2 import Environment, FileSystemLoader

        from app.main import templates

        env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
        env.globals.update(templates.env.globals)
        env.filters.update(templates.env.filters)
        html = env.from_string('{% extends "base.html" %}').render(
            user=None, nav_tabs=[], request=None
        )
        assert BANNER not in html
        with_nag = env.from_string('{% extends "base.html" %}').render(
            user=None, nav_tabs=[], request=None, trash_nag={"count": 3}
        )
        assert "3 items in Trash" in with_nag
