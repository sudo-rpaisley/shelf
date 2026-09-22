"""Tests for `app.services.tags`, the service `app/routers/tags.py` and the
tag-manager/scan-defaults plans (not yet written) call through."""

from app import config
from app.services import tags as tags_svc
from tests.conftest import _insert_item


class TestNormalizeTag:
    def test_trims_and_collapses(self):
        assert tags_svc.normalize_tag("  first   edition  ") == "first edition"

    def test_caps_length(self):
        assert len(tags_svc.normalize_tag("x" * 100)) == 40

    def test_empty(self):
        assert tags_svc.normalize_tag("   ") == ""
        assert tags_svc.normalize_tag(None) == ""


class TestGetItemTags:
    def test_ordered_nocase(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["zebra", "Apple"])
        names = [row["name"] for row in tags_svc.get_item_tags(db, item_id)]
        assert names == ["Apple", "zebra"]


class TestGetAllTags:
    def test_no_media_type_is_unscoped(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["signed"])
        rows = tags_svc.get_all_tags(db)
        assert [dict(r) for r in rows] == [
            {"id": rows[0]["id"], "name": "signed", "count": 1}
        ]

    def test_media_type_filters_to_global_and_scoped(self, db):
        global_id = tags_svc.get_or_create_tag(db, "global-tag")
        book_id = tags_svc.get_or_create_tag(db, "book-tag", media_type="book")
        tags_svc.get_or_create_tag(db, "dvd-tag", media_type="dvd")
        names = {row["name"] for row in tags_svc.get_all_tags(db, media_type="book")}
        assert names == {"global-tag", "book-tag"}
        assert "dvd-tag" not in names
        assert global_id and book_id


class TestParseTagList:
    def test_splits_and_normalizes(self):
        assert tags_svc.parse_tag_list("signed; first edition ;  book club ") == [
            "signed", "first edition", "book club",
        ]

    def test_dedupes_nocase_first_spelling_wins(self):
        assert tags_svc.parse_tag_list("Signed;signed;SIGNED") == ["Signed"]

    def test_drops_blanks(self):
        assert tags_svc.parse_tag_list("signed;;  ;book club") == ["signed", "book club"]

    def test_none_and_empty(self):
        assert tags_svc.parse_tag_list(None) == []
        assert tags_svc.parse_tag_list("") == []


class TestGetOrCreateTag:
    def test_creates_new(self, db):
        tag_id = tags_svc.get_or_create_tag(db, "signed")
        row = db.execute("SELECT name, media_type FROM tags WHERE id = ?", (tag_id,)).fetchone()
        assert row["name"] == "signed"
        assert row["media_type"] is None

    def test_scoped_create(self, db):
        tag_id = tags_svc.get_or_create_tag(db, "collector", media_type="video_game")
        row = db.execute("SELECT media_type FROM tags WHERE id = ?", (tag_id,)).fetchone()
        assert row["media_type"] == "video_game"

    def test_existing_row_wins_scope_never_updated(self, db):
        first_id = tags_svc.get_or_create_tag(db, "signed", media_type="book")
        second_id = tags_svc.get_or_create_tag(db, "signed", media_type="dvd")
        assert first_id == second_id
        row = db.execute("SELECT media_type FROM tags WHERE id = ?", (first_id,)).fetchone()
        assert row["media_type"] == "book"

    def test_existing_row_wins_case_insensitive(self, db):
        first_id = tags_svc.get_or_create_tag(db, "Signed")
        second_id = tags_svc.get_or_create_tag(db, "signed")
        assert first_id == second_id


class TestAttachDetachGc:
    def test_attach_is_additive_and_idempotent(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["signed"])
        tags_svc.attach_tags(db, item_id, ["signed", "first edition"])
        names = {row["name"] for row in tags_svc.get_item_tags(db, item_id)}
        assert names == {"signed", "first edition"}

    def test_detach_returns_removed_count(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["signed"])
        tag_id = db.execute("SELECT id FROM tags WHERE name = 'signed'").fetchone()["id"]
        assert tags_svc.detach_tags(db, item_id, [tag_id]) == 1
        assert tags_svc.detach_tags(db, item_id, [tag_id]) == 0

    def test_gc_orphans_deletes_only_when_unreferenced(self, db):
        a = _insert_item(db, isbn="9780000000101")
        b = _insert_item(db, isbn="9780000000200")
        tags_svc.attach_tags(db, a, ["shared"])
        tags_svc.attach_tags(db, b, ["shared"])
        tag_id = db.execute("SELECT id FROM tags WHERE name = 'shared'").fetchone()["id"]

        tags_svc.detach_tags(db, a, [tag_id])
        tags_svc.gc_orphans(db, [tag_id])
        assert db.execute("SELECT COUNT(*) c FROM tags WHERE id = ?", (tag_id,)).fetchone()["c"] == 1

        tags_svc.detach_tags(db, b, [tag_id])
        tags_svc.gc_orphans(db, [tag_id])
        assert db.execute("SELECT COUNT(*) c FROM tags WHERE id = ?", (tag_id,)).fetchone()["c"] == 0


class TestTagsForItems:
    def test_empty_ids(self, db):
        assert tags_svc.tags_for_items(db, []) == {}

    def test_maps_item_id_to_nocase_sorted_names(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["zebra", "Apple"])
        result = tags_svc.tags_for_items(db, [item_id])
        assert result == {item_id: ["Apple", "zebra"]}

    def test_chunks_over_500_ids(self, db):
        ids = []
        for n in range(600):
            item_id = _insert_item(db, isbn=None, title=f"Item {n}")
            ids.append(item_id)
            tags_svc.attach_tags(db, item_id, ["bulk"])

        result = tags_svc.tags_for_items(db, ids)
        assert len(result) == 600
        assert all(result[item_id] == ["bulk"] for item_id in ids)


class TestListTagsWithCounts:
    def test_counts_and_out_of_scope(self, db):
        book = _insert_item(db, media_type="book", isbn="9780000000316")
        dvd = _insert_item(db, media_type="dvd", isbn=None)
        tag_id = tags_svc.get_or_create_tag(db, "scoped", media_type="book")
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (book, tag_id))
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (dvd, tag_id))

        rows = {r["id"]: r for r in tags_svc.list_tags_with_counts(db)}
        row = rows[tag_id]
        assert row["count"] == 2
        assert row["out_of_scope_count"] == 1

    def test_global_tag_has_zero_out_of_scope(self, db):
        item_id = _insert_item(db)
        tag_id = tags_svc.get_or_create_tag(db, "global")
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))
        rows = {r["id"]: r for r in tags_svc.list_tags_with_counts(db)}
        assert rows[tag_id]["out_of_scope_count"] == 0

    def test_trashed_item_absent_from_count_and_out_of_scope(self, db):
        book = _insert_item(db, media_type="book", isbn="9780000000415")
        dvd = _insert_item(db, media_type="dvd", isbn=None)
        tag_id = tags_svc.get_or_create_tag(db, "scoped2", media_type="book")
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (book, tag_id))
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (dvd, tag_id))

        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (dvd,))

        rows = {r["id"]: r for r in tags_svc.list_tags_with_counts(db)}
        row = rows[tag_id]
        assert row["count"] == 1
        assert row["out_of_scope_count"] == 0


class TestSuggestionsFor:
    def test_starters_offered_with_no_user_tags(self, db):
        suggestions = tags_svc.suggestions_for(db, "book")
        assert suggestions
        assert all(s["starter"] for s in suggestions)
        names = {s["name"] for s in suggestions}
        assert "Fiction" in names

    def test_user_tags_come_first_not_marked_starter(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["My Tag"])
        suggestions = tags_svc.suggestions_for(db, "book")
        assert suggestions[0] == {"name": "My Tag", "starter": False}

    def test_no_starters_once_a_tag_is_scoped_to_the_type(self, db):
        tags_svc.get_or_create_tag(db, "House Tag", media_type="book")
        suggestions = tags_svc.suggestions_for(db, "book")
        assert all(not s["starter"] for s in suggestions)
        assert suggestions == [{"name": "House Tag", "starter": False}]

    def test_starters_still_offered_for_a_different_media_type(self, db):
        tags_svc.get_or_create_tag(db, "House Tag", media_type="book")
        suggestions = tags_svc.suggestions_for(db, "dvd")
        assert any(s["starter"] for s in suggestions)

    def test_starter_already_present_is_not_duplicated(self, db):
        item_id = _insert_item(db)
        tags_svc.attach_tags(db, item_id, ["fiction"])  # NOCASE match to "Fiction" starter
        suggestions = tags_svc.suggestions_for(db, "book")
        names_lower = [s["name"].lower() for s in suggestions]
        assert names_lower.count("fiction") == 1


class TestTagSuggestionsData:
    def test_every_starter_is_its_own_normalize_tag(self):
        for media_type, starters in config.TAG_SUGGESTIONS.items():
            for starter in starters:
                assert tags_svc.normalize_tag(starter) == starter, (
                    f"{media_type!r} starter {starter!r} is not typeable as-is"
                )

    def test_no_list_repeats_a_name_nocase(self):
        for media_type, starters in config.TAG_SUGGESTIONS.items():
            lowered = [s.casefold() for s in starters]
            assert len(lowered) == len(set(lowered)), (
                f"{media_type!r} has a NOCASE duplicate in TAG_SUGGESTIONS"
            )

    def test_every_key_is_a_known_media_type(self):
        assert set(config.TAG_SUGGESTIONS) <= set(config.MEDIA_TYPES)
