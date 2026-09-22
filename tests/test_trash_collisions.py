"""The insert funnel's collision-with-Trash rule.

`insert_item` is the only `INSERT INTO items` in the codebase, so the rule for
what happens when a write claims a slot a trashed row still holds lives in
exactly one place. A person re-adding restores; a machine re-syncing refuses.

Every test here trashes rows by calling `item_write.trash_item` directly — no
route writes `deleted_at` in this release — and asserts on the **database**
rather than on a return value wherever the claim is "nothing was written"
(G85: the `db` fixture is one `get_db()` block, and `pytest.raises` swallows
the exception inside it, so a report-level assertion would not notice a write
that had already landed).
"""

import json
import sqlite3

import pytest

from app.services import item_write
from app.services.item_write import (
    IdentifierInTrash,
    ItemId,
    insert_item,
    trash_item,
    was_restored,
)
from app.services.isbn import canonical_isbn_pair

# Every literal here goes through `canonical_isbn_pair` in the test that uses
# it, per G71 — a checksum-invalid literal in a plan is not a checked value.
ISBN_A = "9780000000118"
ISBN_B = "9780000000125"


def _row(db, item_id, *cols):
    return db.execute(
        f"SELECT {', '.join(cols)} FROM items WHERE id = ?", (item_id,)
    ).fetchone()


def _live(db, item_id):
    return db.execute(
        "SELECT 1 FROM items_live WHERE id = ?", (item_id,)
    ).fetchone()


def _count(db):
    return db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


class TestTheLiteralsAreReal:
    def test_the_isbns_this_file_uses_are_checksum_valid(self):
        """G71: a plan is prose and no gate reads it. Check the pair before
        building assertions on it, or a dedup pin quietly exercises the
        invalid-ISBN branch while reading as a match test."""
        for literal in (ISBN_A, ISBN_B):
            assert canonical_isbn_pair(literal) is not None, literal


class TestRestoreOnTheIsbnSlot:
    def test_re_adding_a_trashed_isbn_restores_that_row(self, db):
        original = insert_item(db, title="Dune", source="test",
                               isbn=ISBN_A, media_type="book")
        trash_item(db, original)

        again = insert_item(db, title="Dune (provider spelling)", source="scan",
                            isbn=ISBN_A, media_type="book")

        assert again == original
        assert was_restored(again) is True
        assert _live(db, original) is not None
        assert _count(db) == 1

    def test_the_restored_row_keeps_every_stored_field(self, db):
        """The row comes back as the user left it, not overwritten by
        whatever the provider says today."""
        location = db.execute(
            "INSERT INTO locations (name) VALUES ('Study')"
        ).lastrowid
        original = insert_item(db, title="Kept Title", source="manual",
                               isbn=ISBN_A, media_type="book",
                               location_id=location, notes="my note")
        trash_item(db, original)

        insert_item(db, title="Provider Title", source="scan",
                    isbn=ISBN_A, media_type="book", notes="provider note")

        row = _row(db, original, "title", "source", "location_id", "notes")
        assert row["title"] == "Kept Title"
        assert row["source"] == "manual"
        assert row["location_id"] == location
        assert row["notes"] == "my note"

    def test_an_absent_media_type_matches_the_book_default_slot(self, db):
        """G85 — the slot is judged on the *effective* row. An insert that
        names no media_type claims the `'book'` slot, because that is the
        SCHEMA default, so it must match a trashed book."""
        original = insert_item(db, title="Defaulted", source="test", isbn=ISBN_A)
        assert _row(db, original, "media_type")["media_type"] == "book"
        trash_item(db, original)

        again = insert_item(db, title="Again", source="test", isbn=ISBN_A)

        assert again == original
        assert was_restored(again) is True

    def test_a_different_media_type_on_the_same_isbn_is_a_plain_insert(self, db):
        original = insert_item(db, title="As Book", source="test",
                               isbn=ISBN_A, media_type="book")
        trash_item(db, original)

        other = insert_item(db, title="As Audiobook", source="test",
                            isbn=ISBN_A, media_type="audiobook")

        assert other != original
        assert was_restored(other) is False
        # The trashed book is still trashed — a different slot was claimed.
        assert _live(db, original) is None
        assert _count(db) == 2

    def test_a_live_twin_still_raises_integrity_error(self, db):
        """The guards did not move. A *live* duplicate is still the callers'
        own duplicate card, reached through `sqlite3.IntegrityError`, and this
        rule changed nothing about it."""
        insert_item(db, title="Live", source="test",
                    isbn=ISBN_A, media_type="book")
        with pytest.raises(sqlite3.IntegrityError):
            insert_item(db, title="Rival", source="test",
                        isbn=ISBN_A, media_type="book")


class TestRestoreOnTheUpcSlot:
    def test_re_adding_a_trashed_upc_restores_that_row(self, db):
        original = insert_item(db, title="Disc One", source="test",
                               upc="012569803121", media_type="dvd")
        trash_item(db, original)

        again = insert_item(db, title="Disc One again", source="scan",
                            upc="012569803121", media_type="dvd")

        assert again == original
        assert was_restored(again) is True

    def test_an_empty_string_upc_claims_a_real_slot(self, db):
        """`idx_items_upc_type` is partial — `WHERE upc IS NOT NULL` — so ''
        occupies a slot and NULL occupies none. The lookup therefore tests
        `is not None`, not truthiness."""
        original = insert_item(db, title="Blank UPC", source="test",
                               upc="", media_type="dvd")
        trash_item(db, original)

        again = insert_item(db, title="Blank again", source="test",
                            upc="", media_type="dvd")

        assert again == original
        assert was_restored(again) is True

    def test_no_identifier_means_no_lookup_and_no_restore(self, db):
        """Rows 5, 6, 12 and 15 of the call-site table: a path whose insert
        carries neither identifier cannot restore, and must not raise."""
        original = insert_item(db, title="Same Title", source="test",
                               media_type="video_game")
        trash_item(db, original)

        fresh = insert_item(db, title="Same Title", source="test",
                            media_type="video_game")

        assert fresh != original
        assert was_restored(fresh) is False
        assert _live(db, original) is None

    def test_isbn_wins_when_both_identifiers_hit_different_rows(self, db):
        by_isbn = insert_item(db, title="By ISBN", source="test",
                              isbn=ISBN_A, media_type="book")
        by_upc = insert_item(db, title="By UPC", source="test",
                             upc="099999999999", media_type="book")
        trash_item(db, by_isbn)
        trash_item(db, by_upc)

        again = insert_item(db, title="Both", source="test", isbn=ISBN_A,
                            upc="099999999999", media_type="book")

        assert again == by_isbn
        assert _live(db, by_upc) is None


class TestRefuseForAMachine:
    def test_restore_trashed_false_raises_identifier_in_trash(self, db):
        original = insert_item(db, title="Deleted On Purpose", source="test",
                               isbn=ISBN_A, media_type="book")
        trash_item(db, original)

        with pytest.raises(IdentifierInTrash) as exc:
            insert_item(db, title="Sync Copy", source="abs", isbn=ISBN_A,
                        media_type="book", restore_trashed=False)

        err = exc.value
        assert err.code == "identifier_in_trash"
        assert err.field == "isbn"
        assert err.item_id == original
        assert err.title == "Deleted On Purpose"
        assert "Deleted On Purpose" in str(err)

    def test_the_refusal_names_both_ways_out(self, db):
        """"That ISBN is taken" is unactionable when the thing taking it is
        invisible, so the message must say how to make it not taken."""
        original = insert_item(db, title="X", source="test", isbn=ISBN_A)
        trash_item(db, original)

        with pytest.raises(IdentifierInTrash) as exc:
            insert_item(db, title="Y", source="abs", isbn=ISBN_A,
                        restore_trashed=False)

        message = str(exc.value).lower()
        assert "restore" in message
        assert "delete" in message and "permanently" in message

    def test_the_refusal_writes_nothing_at_all(self, db):
        """G85 — asserted on the database, not on the exception. The refusal
        runs before the first write, so a caller whose broad `except` carries
        on leaves nothing behind."""
        original = insert_item(db, title="Untouched", source="test",
                               isbn=ISBN_A, media_type="book")
        trash_item(db, original)
        before = _count(db)

        with pytest.raises(IdentifierInTrash):
            insert_item(db, title="Sync Copy", source="abs", isbn=ISBN_A,
                        media_type="book", restore_trashed=False)

        assert _count(db) == before
        # And the trashed row is still trashed — a refusal is not a restore.
        assert _live(db, original) is None
        assert _row(db, original, "title")["title"] == "Untouched"

    def test_refusal_also_fires_on_the_upc_slot(self, db):
        original = insert_item(db, title="Disc", source="test",
                               upc="012569803121", media_type="dvd")
        trash_item(db, original)

        with pytest.raises(IdentifierInTrash) as exc:
            insert_item(db, title="Sync Disc", source="romm",
                        upc="012569803121", media_type="dvd",
                        restore_trashed=False)

        assert exc.value.field == "upc"

    def test_a_machine_insert_with_no_trashed_twin_is_unaffected(self, db):
        item_id = insert_item(db, title="Fresh", source="abs", isbn=ISBN_A,
                              restore_trashed=False)
        assert was_restored(item_id) is False
        assert _live(db, item_id) is not None


class TestOwnershipIntent:
    """Ownership only ever moves toward owned (G100)."""

    def test_add_mode_over_a_trashed_wishlist_row_yields_an_owned_item(self, db):
        original = insert_item(db, title="Wanted", source="test", isbn=ISBN_A,
                               owned=0, wishlisted=True)
        trash_item(db, original)

        again = insert_item(db, title="Bought", source="scan", isbn=ISBN_A)

        assert again == original
        assert _row(db, original, "owned")["owned"] == 1

    def test_wishlist_mode_over_a_trashed_owned_row_leaves_it_owned(self, db):
        """Skipped, not refused: the user is scanning in wishlist mode and
        should not get an error about a row they cannot see."""
        original = insert_item(db, title="Owned", source="test", isbn=ISBN_A,
                               owned=1)
        trash_item(db, original)

        again = insert_item(db, title="Wanted", source="scan", isbn=ISBN_A,
                            owned=0, wishlisted=True)

        assert again == original
        assert _row(db, original, "owned")["owned"] == 1

    def test_wishlist_mode_over_a_trashed_wishlist_row_stays_wishlisted(self, db):
        from app.services import lists

        original = insert_item(db, title="Wanted", source="test", isbn=ISBN_A,
                               owned=0, wishlisted=True)
        trash_item(db, original)

        again = insert_item(db, title="Wanted", source="scan", isbn=ISBN_A,
                            owned=0, wishlisted=True)

        assert again == original
        assert _row(db, original, "owned")["owned"] == 0
        assert lists.is_member(db, lists.WISHLIST, original)

    def test_add_mode_over_a_trashed_owned_row_leaves_it_owned(self, db):
        original = insert_item(db, title="Owned", source="test", isbn=ISBN_A,
                               owned=1)
        trash_item(db, original)

        again = insert_item(db, title="Owned", source="scan", isbn=ISBN_A)

        assert again == original
        assert _row(db, original, "owned")["owned"] == 1


class TestItemIdCarriesTheFlag:
    """The four probed facts about the `int` subclass, each pinned.

    If any of these stops holding, 18 call sites start behaving differently
    and nothing else would say so.
    """

    def test_it_binds_as_a_sqlite3_parameter(self, db):
        item_id = insert_item(db, title="Bindable", source="test")
        row = db.execute(
            "SELECT title FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["title"] == "Bindable"

    def test_it_serialises_as_a_bare_number(self, db):
        item_id = insert_item(db, title="Serialisable", source="test")
        assert json.dumps({"id": item_id}) == '{"id": %d}' % int(item_id)

    def test_it_formats_in_an_f_string(self, db):
        item_id = insert_item(db, title="Formattable", source="test")
        assert f"/item/{item_id}" == f"/item/{int(item_id)}"

    def test_arithmetic_drops_the_flag_which_is_why_you_read_it_first(self, db):
        """The documented limit, pinned so nobody relies on the opposite."""
        restored = ItemId(7, restored=True)
        assert was_restored(restored) is True
        assert was_restored(restored + 0) is False
        assert was_restored(int(restored)) is False

    def test_a_plain_int_answers_false(self):
        """A test double returning a bare int must report "added", never a
        restore that did not happen."""
        assert was_restored(5) is False


class TestTheRowcountFallThrough:
    def test_a_row_that_stops_being_trashed_falls_through_to_the_insert(
        self, db, monkeypatch
    ):
        """G18 / fact 4. Most add routes hold no `BEGIN IMMEDIATE` around
        their insert, so the row can be restored by someone else between the
        lookup and the write. `restore_item` returning False is the signal;
        the funnel must then INSERT rather than return an id it did not
        restore.

        Patched rather than raced: reproducing it with a second connection is
        awkward and the branch is what matters, not the scheduling.
        """
        original = insert_item(db, title="Racy", source="test", isbn=ISBN_A,
                               media_type="book")
        trash_item(db, original)

        monkeypatch.setattr(item_write, "restore_item", lambda db, item_id: False)

        # The INSERT is attempted — and the constraint answers, because the
        # trashed row really does still hold the slot.
        with pytest.raises(sqlite3.IntegrityError):
            insert_item(db, title="Racy again", source="scan", isbn=ISBN_A,
                        media_type="book")

    def test_the_fall_through_reaches_the_insert_for_a_free_slot(
        self, db, monkeypatch
    ):
        """The other half: when the row really has been restored *and* its
        identifier changed, the fall-through inserts normally."""
        original = insert_item(db, title="Racy", source="test", isbn=ISBN_A,
                               media_type="book")
        trash_item(db, original)

        def _lost_the_race(db_, item_id):
            # Simulate the rival restoring it and editing its ISBN away.
            db_.execute(
                "UPDATE items SET deleted_at = NULL, isbn = ? WHERE id = ?",
                (ISBN_B, item_id),
            )
            return False

        monkeypatch.setattr(item_write, "restore_item", _lost_the_race)

        fresh = insert_item(db, title="Fresh", source="scan", isbn=ISBN_A,
                            media_type="book")

        assert fresh != original
        assert was_restored(fresh) is False
        assert _count(db) == 2


class TestTheSubclassIsExported:
    def test_identifier_in_trash_is_importable_from_item_write(self):
        from app.services import item_write as mod

        assert issubclass(mod.IdentifierInTrash, ValueError)
        assert mod.IdentifierInTrash.code == "identifier_in_trash"


class TestTheUpdateFunnelRefuses:
    """The mirror rule. It cannot restore — the user is editing a *different*
    item — so it refuses, naming the trashed title and both ways out."""

    def test_moving_an_isbn_onto_a_trashed_slot_is_refused(self, db):
        blocker = insert_item(db, title="In Trash", source="test",
                              isbn=ISBN_A, media_type="book")
        trash_item(db, blocker)
        editing = insert_item(db, title="Editing", source="test",
                              isbn=ISBN_B, media_type="book")

        with pytest.raises(IdentifierInTrash) as exc:
            item_write.update_item_fields(db, editing, {"isbn": ISBN_A})

        assert exc.value.item_id == blocker
        assert exc.value.title == "In Trash"
        # Nothing moved.
        assert _row(db, editing, "isbn")["isbn"] == ISBN_B

    def test_moving_a_upc_onto_a_trashed_slot_is_refused(self, db):
        blocker = insert_item(db, title="Disc In Trash", source="test",
                              upc="012569803121", media_type="dvd")
        trash_item(db, blocker)
        editing = insert_item(db, title="Editing", source="test",
                              upc="099999999999", media_type="dvd")

        with pytest.raises(IdentifierInTrash) as exc:
            item_write.update_item_fields(db, editing, {"upc": "012569803121"})

        assert exc.value.field == "upc"
        assert _row(db, editing, "upc")["upc"] == "099999999999"

    def test_a_media_type_only_change_is_refused(self, db):
        """Halt 5. The slot is `(isbn, media_type)`, so a reclassify can reach
        a trashed slot without naming an identifier at all — which is how
        bulk_update and Komga's reclassify would otherwise hand the database
        an IntegrityError."""
        blocker = insert_item(db, title="Trashed Audiobook", source="test",
                              isbn=ISBN_A, media_type="audiobook")
        trash_item(db, blocker)
        editing = insert_item(db, title="A Book", source="test",
                              isbn=ISBN_A, media_type="book")

        with pytest.raises(IdentifierInTrash):
            item_write.update_item_fields(db, editing, {"media_type": "audiobook"})

        assert _row(db, editing, "media_type")["media_type"] == "book"

    def test_re_saving_a_rows_own_unchanged_identifiers_is_not_refused(self, db):
        """The edit form posts every field back on every save, so the row's
        own ISBN arrives unchanged each time. `AND id != ?` is what keeps that
        from being a self-collision — and this pin is what keeps the clause."""
        item_id = insert_item(db, title="Mine", source="test",
                              isbn=ISBN_A, media_type="book")
        trash_item(db, item_id)
        item_write.restore_item(db, item_id)

        item_write.update_item_fields(
            db, item_id, {"isbn": ISBN_A, "media_type": "book", "title": "Renamed"}
        )
        assert _row(db, item_id, "title")["title"] == "Renamed"

    def test_an_update_carrying_none_of_the_three_never_looks(self, db, monkeypatch):
        """A location move, a reading-status change and the wishlist writes
        must cost no lookup at all."""
        item_id = insert_item(db, title="X", source="test", isbn=ISBN_A)

        called = []
        real = item_write.refuse_trash_collision

        def _spy(db_, where, params, values):
            called.append(dict(values))
            return real(db_, where, params, values)

        monkeypatch.setattr(item_write, "refuse_trash_collision", _spy)
        item_write.update_item_fields(db, item_id, {"notes": "moved"})

        assert called == [{"notes": "moved"}]
        # The guard ran, but found nothing to look up: no identifier key.
        assert not {"isbn", "upc", "media_type"} & set(called[0])

    def test_a_bulk_update_with_one_colliding_target_moves_nothing(self, db):
        """G85 — every target is checked before the UPDATE, so a mixed
        selection refuses whole. A partial application is the worse outcome:
        the user cannot see which half landed."""
        blocker = insert_item(db, title="Blocker", source="test",
                              isbn=ISBN_A, media_type="audiobook")
        trash_item(db, blocker)
        clean = insert_item(db, title="Clean", source="test", media_type="book")
        colliding = insert_item(db, title="Colliding", source="test",
                                isbn=ISBN_A, media_type="book")

        with pytest.raises(IdentifierInTrash):
            item_write.update_items_fields(
                db, [clean, colliding], {"media_type": "audiobook"}
            )

        assert _row(db, clean, "media_type")["media_type"] == "book"
        assert _row(db, colliding, "media_type")["media_type"] == "book"

    def test_the_refusal_names_the_trashed_title(self, db):
        blocker = insert_item(db, title="The Blocking Title", source="test",
                              isbn=ISBN_A)
        trash_item(db, blocker)
        editing = insert_item(db, title="Editing", source="test", isbn=ISBN_B)

        with pytest.raises(IdentifierInTrash) as exc:
            item_write.update_item_fields(db, editing, {"isbn": ISBN_A})

        assert "The Blocking Title" in str(exc.value)
        assert "restore" in str(exc.value).lower()


class TestTrashedTitle:
    def test_it_names_a_trashed_row(self, db):
        item_id = insert_item(db, title="Gone", source="test")
        trash_item(db, item_id)
        assert item_write.trashed_title(db, item_id) == "Gone"

    def test_a_live_row_answers_none(self, db):
        item_id = insert_item(db, title="Here", source="test")
        assert item_write.trashed_title(db, item_id) is None

    def test_an_unknown_id_answers_none(self, db):
        assert item_write.trashed_title(db, 99999) is None


class TestTheUpdateRefusalOnRoutes:
    """The route-level half of T4: what each surface does with the refusal."""

    def test_the_edit_save_redirects_with_the_code_and_the_trashed_id(
        self, admin_client, db
    ):
        blocker = insert_item(db, title="Blocking Title", source="test",
                              isbn=ISBN_A, media_type="book")
        trash_item(db, blocker)
        editing = insert_item(db, title="Editing", source="test",
                              isbn=ISBN_B, media_type="book")
        db.commit()

        resp = admin_client.post(
            f"/api/items/{editing}",
            data={"title": "Editing", "isbn": ISBN_A, "media_type": "book"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        # G102: assert the refusal URL by EQUALITY. `_refused` extends the
        # success URL, so `startswith` would pass on the success redirect too.
        assert resp.headers["location"] == (
            f"/item/{editing}/edit?error=identifier_in_trash&trashed={blocker}"
        )
        assert _row(db, editing, "isbn")["isbn"] == ISBN_B

    def test_the_edit_page_names_the_trashed_title_escaped(self, admin_client, db):
        """Seeded with a `<` so the escaping is actually exercised: the router
        sends an id, the template resolves and escapes the title (G58)."""
        blocker = insert_item(db, title="<script>Bad</script>", source="test",
                              isbn=ISBN_A)
        trash_item(db, blocker)
        editing = insert_item(db, title="Editing", source="test", isbn=ISBN_B)
        db.commit()

        resp = admin_client.get(
            f"/item/{editing}/edit?error=identifier_in_trash&trashed={blocker}"
        )

        assert resp.status_code == 200
        assert "&lt;script&gt;Bad&lt;/script&gt;" in resp.text
        assert "<script>Bad</script>" not in resp.text
        assert "Restore it from Trash" in resp.text

    def test_the_edit_page_falls_back_when_the_id_names_no_trashed_row(
        self, admin_client, db
    ):
        editing = insert_item(db, title="Editing", source="test", isbn=ISBN_B)
        db.commit()

        resp = admin_client.get(
            f"/item/{editing}/edit?error=identifier_in_trash&trashed=99999"
        )

        assert resp.status_code == 200
        assert "belongs to an item in Trash" in resp.text

    def test_the_merge_refuses_before_deleting_the_husk(self, admin_client, db):
        """Fact 6. `merge_items` validates before its DELETE and writes after
        it, so the preflight has to sit beside the validation — a refusal at
        the write would arrive with the husk already gone."""
        # Three distinct rows, and the shape is forced: a live husk and a
        # trashed blocker cannot share one slot, because UNIQUE(isbn,
        # media_type) spans trashed rows — that is the premise of this whole
        # plan. So the collision arrives through the *keeper's* media_type:
        # filling the husk's ISBN onto an audiobook keeper claims
        # (ISBN_A, 'audiobook'), which the trashed blocker holds.
        blocker = insert_item(db, title="Blocker", source="test",
                              isbn=ISBN_A, media_type="audiobook")
        trash_item(db, blocker)
        keep = insert_item(db, title="Keep", source="test",
                           media_type="audiobook")
        husk = insert_item(db, title="Husk", source="test",
                           isbn=ISBN_A, media_type="book")
        db.commit()

        resp = admin_client.post(
            "/api/items/merge", json={"keep_id": keep, "merge_ids": [husk]}
        )

        body = resp.json()
        assert body["ok"] is False
        assert "Blocker" in body["message"]
        # The husk is still there — the preflight ran before the DELETE.
        assert db.execute(
            "SELECT 1 FROM items WHERE id = ?", (husk,)
        ).fetchone() is not None
        assert _row(db, keep, "isbn")["isbn"] is None

    def test_bulk_update_refuses_whole_and_names_the_title(self, admin_client, db):
        blocker = insert_item(db, title="Trashed Audiobook", source="test",
                              isbn=ISBN_A, media_type="audiobook")
        trash_item(db, blocker)
        clean = insert_item(db, title="Clean", source="test", media_type="book")
        colliding = insert_item(db, title="Colliding", source="test",
                                isbn=ISBN_A, media_type="book")
        db.commit()

        resp = admin_client.post(
            "/api/items/bulk-update",
            json={"item_ids": [clean, colliding],
                  "updates": {"media_type": "audiobook"}},
        )

        body = resp.json()
        assert body["ok"] is False
        assert "Trashed Audiobook" in body["message"]
        assert _row(db, clean, "media_type")["media_type"] == "book"
        assert _row(db, colliding, "media_type")["media_type"] == "book"


class TestWishlistIntentRidesTheInsert:
    """T6 / D1. Four callers used to insert owned and demote in a *second*
    transaction. Against a restoring funnel that is wrong twice: a
    wishlist-mode re-add of a trashed **owned** row would restore it and then
    demote something the user owns, and a wishlist-mode re-add of a trashed
    **wishlist** row would be promoted by the funnel (no `owned` key means
    effective `owned = 1`) and then demoted again.

    Each pin patches `update_item_fields` on the *module* to raise, so the
    path can only succeed if it never takes a second transaction. That is the
    assertion — "unowned and wishlisted after ONE write" — and a bare state
    check would pass against the old two-step code.
    """

    @staticmethod
    def _forbid_second_write(monkeypatch, module):
        def _boom(*args, **kwargs):
            raise AssertionError(
                "a second transaction ran — wishlist intent must ride the insert"
            )
        monkeypatch.setattr(module, "update_item_fields", _boom, raising=False)

    def test_save_item_carries_the_intent_in_one_write(self, db, monkeypatch):
        from app.routers import items_common

        self._forbid_second_write(monkeypatch, items_common)
        item_id = items_common._save_item(
            {"title": "Wanted"}, ISBN_A, "book", None, "test", {}, owned=False
        )

        row = _row(db, item_id, "owned")
        assert row["owned"] == 0
        from app.services import lists
        assert lists.is_member(db, lists.WISHLIST, item_id)

    def test_save_item_defaults_to_owned(self, db):
        from app.routers import items_common

        item_id = items_common._save_item(
            {"title": "Bought"}, ISBN_B, "book", None, "test", {}
        )
        assert _row(db, item_id, "owned")["owned"] == 1

    def test_the_insert_shape_is_what_refuse_owned_wishlist_accepts(self, db):
        """G85: `wishlisted=True` with no `owned` key is refused by the
        funnel, so the pair must travel together — which is exactly what the
        removed two-step code sent."""
        with pytest.raises(item_write.InvalidWishlisted):
            insert_item(db, title="Bad", source="test", wishlisted=True)
        # The shape _save_item actually sends is accepted.
        item_id = insert_item(db, title="Good", source="test",
                              owned=0, wishlisted=True)
        assert _row(db, item_id, "owned")["owned"] == 0

    def test_wishlist_mode_over_a_trashed_owned_row_does_not_demote_it(self, db):
        """The defect D1 describes, end to end through `_save_item`: without
        the intent on the insert, the funnel restores the owned row and the
        second transaction then demotes something the user owns."""
        from app.routers import items_common

        original = insert_item(db, title="Owned", source="test",
                               isbn=ISBN_A, media_type="book", owned=1)
        trash_item(db, original)
        # `_save_item` opens its own connection, so the seed must be
        # committed or it deadlocks on this fixture's write lock — and
        # without the commit an absence pin would pass vacuously (G48).
        db.commit()

        again = items_common._save_item(
            {"title": "Wanted"}, ISBN_A, "book", None, "test", {}, owned=False
        )

        assert again == original
        assert was_restored(again) is True
        assert _row(db, original, "owned")["owned"] == 1
