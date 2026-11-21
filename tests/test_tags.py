"""Tests for custom tags (routers/tags.py + browse/search filtering)."""
import re

from app.routers.tags import normalize_tag
from tests.conftest import _insert_item


def _add_tag(client, item_id, name):
    return client.post(f"/api/items/{item_id}/tags", data={"name": name})


class TestNormalizeTag:
    def test_trims_and_collapses(self):
        assert normalize_tag("  first   edition  ") == "first edition"

    def test_caps_length(self):
        assert len(normalize_tag("x" * 100)) == 40

    def test_empty(self):
        assert normalize_tag("   ") == ""
        assert normalize_tag(None) == ""


class TestTagEndpoints:
    def test_add_and_render(self, admin_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        resp = _add_tag(admin_client, item_id, "signed")
        assert resp.status_code == 200
        assert "signed" in resp.text
        row = db.execute(
            "SELECT t.name FROM item_tags it JOIN tags t ON it.tag_id = t.id WHERE it.item_id = ?",
            (item_id,),
        ).fetchone()
        assert row["name"] == "signed"

    def test_add_reuses_existing_tag_case_insensitive(self, admin_client, db):
        a = _insert_item(db, isbn="9789000004010")
        b = _insert_item(db, isbn="9789000004188")
        db.execute("COMMIT")
        _add_tag(admin_client, a, "Signed")
        _add_tag(admin_client, b, "signed")
        count = db.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"]
        assert count == 1

    def test_add_duplicate_is_idempotent(self, admin_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        _add_tag(admin_client, item_id, "signed")
        _add_tag(admin_client, item_id, "signed")
        count = db.execute(
            "SELECT COUNT(*) as c FROM item_tags WHERE item_id = ?", (item_id,)
        ).fetchone()["c"]
        assert count == 1

    def test_add_blank_rejected(self, admin_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        resp = _add_tag(admin_client, item_id, "   ")
        assert resp.status_code == 400

    def test_add_missing_item(self, admin_client):
        resp = _add_tag(admin_client, 99999, "signed")
        assert resp.status_code == 404

    def test_remove_and_orphan_gc(self, admin_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        _add_tag(admin_client, item_id, "to-sell")
        tag = db.execute("SELECT id FROM tags WHERE name = 'to-sell'").fetchone()
        resp = admin_client.delete(f"/api/items/{item_id}/tags/{tag['id']}")
        assert resp.status_code == 200
        assert db.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"] == 0

    def test_remove_keeps_shared_tag(self, admin_client, db):
        a = _insert_item(db, isbn="9789000004256")
        b = _insert_item(db, isbn="9789000004324")
        db.execute("COMMIT")
        _add_tag(admin_client, a, "book-club")
        _add_tag(admin_client, b, "book-club")
        tag = db.execute("SELECT id FROM tags WHERE name = 'book-club'").fetchone()
        admin_client.delete(f"/api/items/{a}/tags/{tag['id']}")
        assert db.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"] == 1

    def test_viewer_cannot_edit_tags(self, viewer_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        resp = _add_tag(viewer_client, item_id, "signed")
        assert resp.status_code in (401, 403)

    def test_item_delete_keeps_the_tag_for_restore_and_purge_cascades(self, admin_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        _add_tag(admin_client, item_id, "signed")
        admin_client.delete(f"/api/items/{item_id}")

        def links():
            return db.execute(
                "SELECT COUNT(*) as c FROM item_tags WHERE item_id = ?", (item_id,)
            ).fetchone()["c"]

        # Delete moves the item to Trash; its tag link waits for a restore.
        assert links() == 1
        # Delete permanently is where the cascade fires.
        assert admin_client.delete(f"/api/trash/items/{item_id}").status_code == 200
        assert links() == 0


class TestTagFiltering:
    def _seed(self, admin_client, db):
        a = _insert_item(db, title="Signed Book", isbn="9789000004492")
        b = _insert_item(db, title="Plain Book", isbn="9789000004560")
        db.execute("COMMIT")
        _add_tag(admin_client, a, "signed")
        return a, b

    def test_search_filters_by_tag(self, admin_client, db):
        self._seed(admin_client, db)
        html = admin_client.get("/api/search", params={"tag": "signed"}).text
        assert "Signed Book" in html
        assert "Plain Book" not in html

    def test_browse_filters_by_tag(self, admin_client, db):
        self._seed(admin_client, db)
        html = admin_client.get("/browse", params={"tag": "signed"}).text
        assert "Signed Book" in html
        assert "Plain Book" not in html

    def test_browse_shows_tag_dropdown_only_when_tags_exist(self, admin_client, db):
        _insert_item(db, title="Untagged", isbn="9789000004638")
        db.execute("COMMIT")
        assert 'id="tag-filter"' not in admin_client.get("/browse").text
        item = db.execute("SELECT id FROM items").fetchone()
        _add_tag(admin_client, item["id"], "signed")
        assert 'id="tag-filter"' in admin_client.get("/browse").text

    def test_item_detail_shows_tag_chip(self, admin_client, db):
        a, _ = self._seed(admin_client, db)
        html = admin_client.get(f"/item/{a}").text
        assert "/browse?tag=signed" in html


class TestEditPageTagsIsland:
    def test_unknown_item_redirects_to_browse(self, editor_client):
        resp = editor_client.get("/item/999999/edit", follow_redirects=False)
        assert resp.status_code == 307  # RedirectResponse default on this GET route
        assert resp.headers["location"] == "/browse"

    def test_trashed_item_redirects_to_browse(self, editor_client, db):
        from app.services.item_write import trash_item

        item_id = _insert_item(db)
        db.execute("COMMIT")
        trash_item(db, item_id)
        db.execute("COMMIT")
        resp = editor_client.get(f"/item/{item_id}/edit", follow_redirects=False)
        assert resp.status_code == 307  # RedirectResponse default on this GET route
        assert resp.headers["location"] == "/browse"

    def test_edit_page_renders_the_island_with_the_items_chip(self, editor_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        _add_tag(editor_client, item_id, "signed")
        html = editor_client.get(f"/item/{item_id}/edit").text
        assert 'id="item-tags"' in html
        assert "/browse?tag=signed" in html

    def test_edit_page_has_no_related_media_loader_detail_page_has_exactly_one(
        self, editor_client, db
    ):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        edit_html = editor_client.get(f"/item/{item_id}/edit").text
        assert "/api/related-media/" not in edit_html

        detail_html = editor_client.get(f"/item/{item_id}").text
        assert detail_html.count("/api/related-media/") == 1

    def test_edit_page_suggestions_are_scoped_to_the_items_media_type(self, editor_client, db):
        book_id = _insert_item(db, title="A Book", isbn="9789100000109", media_type="book")
        db.execute("COMMIT")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('global tag', NULL)")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('game tag', 'video_game')")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('book tag', 'book')")
        db.execute("COMMIT")

        html = editor_client.get(f"/item/{book_id}/edit").text
        suggestions = re.search(
            r'<datalist id="tag-suggestions">(.*?)</datalist>', html, re.S
        ).group(1)
        assert "global tag" in suggestions
        assert "book tag" in suggestions
        assert "game tag" not in suggestions

    def test_add_tag_re_renders_the_fragment_with_the_same_scoped_suggestions(
        self, editor_client, db
    ):
        book_id = _insert_item(db, isbn="9789100000123", media_type="book")
        db.execute("COMMIT")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('global tag', NULL)")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('game tag', 'video_game')")
        db.execute("INSERT INTO tags (name, media_type) VALUES ('book tag', 'book')")
        db.execute("COMMIT")

        resp = _add_tag(editor_client, book_id, "signed")
        assert resp.status_code == 200
        suggestions = re.search(
            r'<datalist id="tag-suggestions">(.*?)</datalist>', resp.text, re.S
        ).group(1)
        assert "global tag" in suggestions
        assert "book tag" in suggestions
        assert "game tag" not in suggestions


def _seed_tagged(db, name="signed", isbn="9789100000017", media_type="book"):
    item_id = _insert_item(db, isbn=isbn, media_type=media_type)
    db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))
    return item_id, tag_id


def _tag(db, tag_id):
    return db.execute("SELECT name, media_type FROM tags WHERE id = ?", (tag_id,)).fetchone()


def _update(client, tag_id, name, media_type=""):
    return client.post(f"/api/tags/{tag_id}/update",
                       data={"name": name, "media_type": media_type}, follow_redirects=False)


def _delete(client, tag_id):
    return client.post(f"/api/tags/{tag_id}/delete", follow_redirects=False)


class TestTagManager:
    def test_rename_keeps_associations(self, admin_client, db):
        item_id, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        resp = _update(admin_client, tag_id, "Signed copy")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"
        assert _tag(db, tag_id)["name"] == "Signed copy"
        assert db.execute("SELECT 1 FROM item_tags WHERE item_id = ? AND tag_id = ?",
                          (item_id, tag_id)).fetchone()

    def test_case_only_rename_of_the_same_tag(self, admin_client, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        resp = _update(admin_client, tag_id, "Signed")
        assert resp.headers["location"] == "/settings"
        assert _tag(db, tag_id)["name"] == "Signed"

    def test_rename_onto_another_tag_is_refused_and_changes_nothing(self, admin_client, db):
        _, a = _seed_tagged(db, "signed")
        _, b = _seed_tagged(db, "first edition", isbn="9789100000024")
        db.execute("COMMIT")
        resp = _update(admin_client, b, "SIGNED")
        assert resp.headers["location"] == "/settings?tag_error=duplicate"
        assert _tag(db, a)["name"] == "signed"
        assert _tag(db, b)["name"] == "first edition"

    def test_blank_name_refused(self, admin_client, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        resp = _update(admin_client, tag_id, "   ")
        assert resp.headers["location"] == "/settings?tag_error=blank"
        assert _tag(db, tag_id)["name"] == "signed"

    def test_unknown_id_refused(self, admin_client):
        assert _update(admin_client, 999999, "x").headers["location"] == \
            "/settings?tag_error=missing"
        assert _delete(admin_client, 999999).headers["location"] == \
            "/settings?tag_error=missing"

    def test_unknown_scope_refused(self, admin_client, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        resp = _update(admin_client, tag_id, "signed", "not_a_type")
        assert resp.headers["location"] == "/settings?tag_error=scope"
        assert _tag(db, tag_id)["media_type"] is None

    def test_scope_is_advisory_and_blank_scope_stores_null(self, admin_client, db):
        book_id, tag_id = _seed_tagged(db, "classic")
        db.execute("COMMIT")
        resp = _update(admin_client, tag_id, "classic", "video_game")
        assert resp.headers["location"] == "/settings"
        assert _tag(db, tag_id)["media_type"] == "video_game"
        # A book already carrying the tag keeps it.
        assert db.execute("SELECT 1 FROM item_tags WHERE item_id = ? AND tag_id = ?",
                          (book_id, tag_id)).fetchone()
        _update(admin_client, tag_id, "classic", "")
        assert _tag(db, tag_id)["media_type"] is None

    def test_rename_posting_the_stored_scope_keeps_it(self, admin_client, db):
        """G36: the rendered form posts the current scope back with the name."""
        _, tag_id = _seed_tagged(db, "co-op")
        db.execute("COMMIT")
        _update(admin_client, tag_id, "co-op", "video_game")
        _update(admin_client, tag_id, "Co-op", "video_game")
        assert dict(_tag(db, tag_id)) == {"name": "Co-op", "media_type": "video_game"}

    def test_delete_removes_the_tag_from_every_item_including_trashed(self, admin_client, db):
        from app.services.item_write import trash_item

        live_id, tag_id = _seed_tagged(db, "gone")
        trashed_id, _ = _seed_tagged(db, "gone", isbn="9789100000031")
        trash_item(db, trashed_id)
        db.execute("COMMIT")
        resp = _delete(admin_client, tag_id)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"
        assert _tag(db, tag_id) is None
        assert db.execute("SELECT COUNT(*) AS n FROM item_tags WHERE tag_id = ?",
                          (tag_id,)).fetchone()["n"] == 0

    def test_editor_cannot_update_or_delete(self, editor_client, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        assert _update(editor_client, tag_id, "hacked").status_code == 403
        assert _delete(editor_client, tag_id).status_code == 403
        assert _tag(db, tag_id)["name"] == "signed"

    def test_viewer_cannot_update_or_delete(self, viewer_client, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        assert _update(viewer_client, tag_id, "hacked").status_code == 403
        assert _delete(viewer_client, tag_id).status_code == 403
        assert _tag(db, tag_id)["name"] == "signed"

    def test_anonymous_redirected_to_login(self, client, admin_user, db):
        _, tag_id = _seed_tagged(db)
        db.execute("COMMIT")
        for resp in (_update(client, tag_id, "x"), _delete(client, tag_id)):
            assert resp.status_code == 303
            assert resp.headers["location"] == "/login"


class TestTagManagerPanel:
    """The Settings > Library tag manager panel (fragments/settings/tags.html)."""

    def test_lists_rows_with_counts_and_out_of_scope(self, admin_client, db):
        _, book_tag = _seed_tagged(db, "classic", isbn="9789100000048", media_type="book")
        _, game_tag = _seed_tagged(db, "co-op", isbn="9789100000055", media_type="video_game")
        db.execute("UPDATE tags SET media_type = 'video_game' WHERE id = ?", (game_tag,))
        # A second, book-typed item also carries the video_game-scoped tag —
        # it falls outside that tag's scope.
        book_id = _insert_item(db, isbn="9789100000062", media_type="book")
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (book_id, game_tag))
        db.execute("COMMIT")

        html = admin_client.get("/settings").text
        assert f'data-testid="tag-row-{book_tag}"' in html
        assert f'data-testid="tag-row-{game_tag}"' in html
        assert "1 outside Video Game" in html

    def test_delete_confirm_exact_string_and_count_is_live_only(self, admin_client, db):
        from app.services.item_write import trash_item

        live_id, tag_id = _seed_tagged(db, "gone", isbn="9789100000079")
        trashed_id, _ = _seed_tagged(db, "gone", isbn="9789100000086")
        trash_item(db, trashed_id)
        db.execute("COMMIT")

        html = admin_client.get("/settings").text
        assert "1 active item(s)" in html
        # G28: exact attribute bytes, not a presence check — the apostrophes
        # around the name are literal template characters (untouched by
        # autoescaping, since the tag name itself carries no special chars).
        assert (
            "data-confirm=\"Delete tag 'gone' from every item, including "
            "items in Trash?\""
        ) in html

        resp = _delete(admin_client, tag_id)
        assert resp.status_code == 303
        for item_id in (live_id, trashed_id):
            assert not db.execute(
                "SELECT 1 FROM item_tags WHERE item_id = ? AND tag_id = ?",
                (item_id, tag_id),
            ).fetchone()

    def test_round_trip_posts_the_rendered_scope_field(self, admin_client, db):
        """G36: post back exactly what the update form rendered, scraped
        from the page — not a hand-picked subset of fields."""
        _, tag_id = _seed_tagged(db, "vintage", isbn="9789100000093")
        db.execute("UPDATE tags SET media_type = 'video_game' WHERE id = ?", (tag_id,))
        db.execute("COMMIT")

        html = admin_client.get("/settings").text
        name_match = re.search(rf'id="tag-name-{tag_id}"[^>]*value="([^"]*)"', html)
        select_match = re.search(
            rf'<select id="tag-scope-{tag_id}"[^>]*>(.*?)</select>', html, re.S
        )
        assert name_match and select_match
        rendered_name = name_match.group(1)
        assert rendered_name == "vintage"
        selected = re.search(r'<option value="([^"]*)"\s+selected', select_match.group(1))
        assert selected
        rendered_scope = selected.group(1)
        assert rendered_scope == "video_game"

        resp = admin_client.post(
            f"/api/tags/{tag_id}/update",
            data={"name": "Vintage", "media_type": rendered_scope},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/settings"
        assert dict(_tag(db, tag_id)) == {"name": "Vintage", "media_type": "video_game"}

    def test_known_error_code_renders_banner_unknown_code_is_not_reflected(self, admin_client):
        html = admin_client.get("/settings", params={"tag_error": "duplicate"}).text
        assert 'data-testid="tag-error-banner"' in html
        assert "A tag with that name already exists — merging tags is not supported yet." in html

        html = admin_client.get(
            "/settings", params={"tag_error": "<script>alert(1)</script>"}
        ).text
        assert 'data-testid="tag-error-banner"' not in html
        assert "<script>alert(1)</script>" not in html

    def test_empty_state_with_no_tags(self, admin_client):
        html = admin_client.get("/settings").text
        assert "No tags yet. Add one to an item, or from the Browse bulk bar." in html
