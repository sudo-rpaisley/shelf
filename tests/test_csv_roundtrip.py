"""Export → import must be a no-op, including for items without an ISBN.

Found in a live pass, not by the suite: re-importing Shelf's own export
reported "imported 18, skipped 54" on a 72-item library. The duplicate check
sat entirely inside `if isbn_val:`, so any row lacking an ISBN — every video
game and DVD, and any book catalogued without one, about 40% of a real
library — was inserted again with no check against the file or the database.

1,378 unit tests and 123 E2E tests were green at the time. None of them
round-tripped an export containing ISBN-less rows (G33).
"""

import csv
import io

from tests.conftest import _insert_item


def _export(client):
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    return resp.text


def _import(client, content, mode="skip"):
    resp = client.post(
        "/api/import/csv",
        files={"file": ("export.csv", io.BytesIO(content.encode()), "text/csv")},
        data={"mode": mode},
    )
    assert resp.status_code == 200
    return resp.json()


def _count(db):
    return db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]


class TestWishlistedColumn:
    def test_header_ends_owned_wishlisted_deleted_tags(self, admin_client):
        header = _export(admin_client).splitlines()[0]
        assert header.split(",")[-4:] == ["owned", "wishlisted", "deleted", "tags"]

    def test_wishlist_item_exports_one_owned_item_exports_zero(self, admin_client, db):
        _insert_item(db, title="Owned Book", isbn="9780441013593", media_type="book", owned=1)
        _insert_item(db, title="Wishlist Book", isbn="9780553283686", media_type="book", owned=0, wishlisted=True)
        db.execute("COMMIT")

        rows = list(csv.DictReader(io.StringIO(_export(admin_client))))
        owned_row = next(r for r in rows if r["title"] == "Owned Book")
        wishlist_row = next(r for r in rows if r["title"] == "Wishlist Book")

        assert owned_row["wishlisted"] == "0"
        assert wishlist_row["wishlisted"] == "1"


class TestOwnershipStatesRoundTrip:
    """#125: owned, wishlisted and neither all survive export → import."""

    def test_header_carries_owned_before_wishlisted(self, admin_client):
        header = _export(admin_client).splitlines()[0]
        assert header.split(",")[-4:] == ["owned", "wishlisted", "deleted", "tags"]

    def test_all_three_states_survive_into_a_fresh_library(self, admin_client, db):
        from app.services import lists
        from tests.conftest import _assert_ownership_partition

        _insert_item(db, title="State Owned", isbn="9780441013593", media_type="book", owned=1)
        _insert_item(db, title="State Wished", isbn="9780553283686", media_type="book",
                     owned=0, wishlisted=True)
        _insert_item(db, title="State Neither", isbn=None, authors="N. Body",
                     media_type="book", owned=0)
        db.execute("COMMIT")

        exported = _export(admin_client)
        db.execute("DELETE FROM list_items")
        db.execute("DELETE FROM items")
        db.execute("COMMIT")

        result = _import(admin_client, exported)
        assert (result["imported"], result["errors"]) == (3, [])

        def state(title):
            row = db.execute("SELECT id, owned FROM items WHERE title = ?", (title,)).fetchone()
            return row["owned"], lists.is_member(db, lists.WISHLIST, row["id"])

        assert state("State Owned") == (1, False)
        assert state("State Wished") == (0, True)
        assert state("State Neither") == (0, False)
        _assert_ownership_partition(db)


class TestDeletedColumn:
    """T3: the `deleted` column carries Trash state for editor/admin exports.
    A viewer export omits the trashed row entirely and its `deleted` cells
    are all 0 — the Trash page is editor+, and the CSV must not be the one
    place a viewer sees a trashed row (design plan-soft-delete-export,
    Decision 1). The file shape (19 columns) is identical for every role."""

    def _seed_live_and_trashed(self, db):
        from app.services import item_write
        from app.services import tags as tags_svc

        live_id = _insert_item(db, title="Live Book", isbn="9780441013593", media_type="book")
        trashed_id = _insert_item(db, title="Trashed Book", isbn="9780553283686", media_type="book")
        db.commit()
        tags_svc.attach_tags(db, trashed_id, ["Signed"])
        item_write.trash_item(db, trashed_id)
        db.commit()
        return live_id, trashed_id

    def test_admin_export_includes_trashed_row_with_deleted_flag_and_tags(self, admin_client, db):
        self._seed_live_and_trashed(db)

        rows = list(csv.DictReader(io.StringIO(_export(admin_client))))
        live_row = next(r for r in rows if r["title"] == "Live Book")
        trashed_row = next(r for r in rows if r["title"] == "Trashed Book")

        assert live_row["deleted"] == "0"
        assert trashed_row["deleted"] == "1"
        assert trashed_row["tags"] == "Signed"

    def test_editor_export_includes_trashed_row_with_deleted_flag(self, editor_client, db):
        self._seed_live_and_trashed(db)

        rows = list(csv.DictReader(io.StringIO(_export(editor_client))))
        live_row = next(r for r in rows if r["title"] == "Live Book")
        trashed_row = next(r for r in rows if r["title"] == "Trashed Book")

        assert live_row["deleted"] == "0"
        assert trashed_row["deleted"] == "1"

    def test_viewer_export_omits_trashed_row_and_keeps_the_nineteen_column_header(self, viewer_client, db):
        self._seed_live_and_trashed(db)

        exported = _export(viewer_client)
        header = exported.splitlines()[0].split(",")
        assert len(header) == 19
        assert header[-4:] == ["owned", "wishlisted", "deleted", "tags"]

        rows = list(csv.DictReader(io.StringIO(exported)))
        titles = {r["title"] for r in rows}
        assert "Trashed Book" not in titles
        live_row = next(r for r in rows if r["title"] == "Live Book")
        assert live_row["deleted"] == "0"


class TestRoundTrip:
    def test_reimporting_an_export_adds_nothing(self, admin_client, db):
        """The reported bug, end to end."""
        _insert_item(db, title="With ISBN", isbn="9780441013593", media_type="book")
        _insert_item(db, title="No ISBN Book", isbn=None, authors="A. Writer", media_type="book")
        _insert_item(db, title="Some Game", isbn=None, media_type="video_game")
        _insert_item(db, title="Some Movie", isbn=None, media_type="dvd")
        db.execute("COMMIT")
        before = _count(db)

        result = _import(admin_client, _export(admin_client))

        assert _count(db) == before, (
            f"round trip created {_count(db) - before} duplicate row(s); "
            f"import reported {result}"
        )
        assert result["imported"] == 0
        assert result["skipped"] == before

    def test_isbnless_rows_are_deduped_against_the_database(self, admin_client, db):
        _insert_item(db, title="Katamari Damacy", isbn=None, media_type="video_game")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type\nKatamari Damacy,,,video_game\n"
        result = _import(admin_client, csv)

        assert _count(db) == before
        assert result["skipped"] == 1 and result["imported"] == 0

    def test_isbnless_rows_are_deduped_within_one_file(self, admin_client, db):
        before = _count(db)
        csv = (
            "title,authors,isbn,media_type\n"
            "Repeated Game,,,video_game\n"
            "Repeated Game,,,video_game\n"
        )
        result = _import(admin_client, csv)

        assert _count(db) == before + 1
        assert result["imported"] == 1 and result["skipped"] == 1

    def test_matching_ignores_case_and_surrounding_space(self, admin_client, db):
        _insert_item(db, title="The Hobbit", authors="J.R.R. Tolkien",
                     isbn=None, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type\n  the hobbit  ,  J.R.R. TOLKIEN ,,book\n"
        result = _import(admin_client, csv)

        assert _count(db) == before
        assert result["skipped"] == 1


class TestFallbackStaysNarrow:
    def test_same_title_different_media_type_is_not_a_duplicate(self, admin_client, db):
        """A book and its film adaptation are different items."""
        _insert_item(db, title="Dune", isbn=None, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        result = _import(admin_client, "title,authors,isbn,media_type\nDune,,,dvd\n")

        assert _count(db) == before + 1
        assert result["imported"] == 1

    def test_same_title_different_authors_is_not_a_duplicate(self, admin_client, db):
        _insert_item(db, title="Selected Poems", authors="W. B. Yeats",
                     isbn=None, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type\nSelected Poems,Emily Dickinson,,book\n"
        result = _import(admin_client, csv)

        assert _count(db) == before + 1
        assert result["imported"] == 1

    def test_isbnless_row_does_not_collapse_onto_an_edition_that_has_one(self, admin_client, db):
        """A CSV row with no ISBN must not be swallowed by a different edition
        of the same title that does have one — that would lose the copy."""
        _insert_item(db, title="Neuromancer", authors="William Gibson",
                     isbn="9780441569595", media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type\nNeuromancer,William Gibson,,book\n"
        result = _import(admin_client, csv)

        assert _count(db) == before + 1
        assert result["imported"] == 1

    def test_isbn_rows_still_dedupe_on_isbn_not_title(self, admin_client, db):
        """The strong key is unchanged: a retitled row with a known ISBN is
        still the same item."""
        _insert_item(db, title="Dune", isbn="9780441013593", media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type\nDune (Deluxe Edition),,9780441013593,book\n"
        result = _import(admin_client, csv)

        assert _count(db) == before
        assert result["skipped"] == 1


class TestUpdateMode:
    def test_update_mode_refreshes_an_isbnless_match(self, admin_client, db):
        """mode=update must reach rows found by the fallback key too."""
        _insert_item(db, title="Some Game", isbn=None, media_type="video_game",
                     publisher=None)
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Namco\n"
        result = _import(admin_client, csv, mode="update")

        assert _count(db) == before
        assert result["imported"] == 1
        row = db.execute(
            "SELECT publisher FROM items WHERE title = 'Some Game'"
        ).fetchone()
        assert row["publisher"] == "Namco"


class TestIsbnFormMatching:
    """A CSV row is matched by its canonical ISBN-13, regardless of whether
    the row (or the library row) holds the ISBN-10 or ISBN-13 form, or a
    hyphenated one."""

    ISBN13 = "9780441172719"
    ISBN10 = "0441172717"

    def test_isbn10_row_matches_isbn13_twin_skip_mode(self, admin_client, db):
        _insert_item(db, title="Dune Messiah", isbn=self.ISBN13, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = f"title,authors,isbn,media_type\nDune Messiah,,{self.ISBN10},book\n"
        result = _import(admin_client, csv, mode="skip")

        assert _count(db) == before
        assert result["skipped"] == 1 and result["imported"] == 0
        assert not any("UNIQUE constraint" in e for e in result["errors"])

    def test_isbn10_row_matches_isbn13_twin_update_mode(self, admin_client, db):
        _insert_item(db, title="Dune Messiah", isbn=self.ISBN13, media_type="book",
                     publisher=None)
        db.execute("COMMIT")
        before = _count(db)

        csv = (
            f"title,authors,isbn,media_type,publisher\n"
            f"Dune Messiah,,{self.ISBN10},book,Ace Books\n"
        )
        result = _import(admin_client, csv, mode="update")

        assert _count(db) == before
        assert result["imported"] == 1 and result["skipped"] == 0
        assert not any("UNIQUE constraint" in e for e in result["errors"])
        row = db.execute(
            "SELECT publisher FROM items WHERE title = 'Dune Messiah'"
        ).fetchone()
        assert row["publisher"] == "Ace Books"

    def test_hyphenated_isbn_matches_unhyphenated_twin(self, admin_client, db):
        _insert_item(db, title="Dune Messiah", isbn=self.ISBN13, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        hyphenated = "978-0-441-17271-9"
        csv = f"title,authors,isbn,media_type\nDune Messiah,,{hyphenated},book\n"
        result = _import(admin_client, csv, mode="skip")

        assert _count(db) == before
        assert result["skipped"] == 1 and result["imported"] == 0

    def test_legacy_isbn10_in_isbn_column_matched_by_isbn13_csv_row(self, admin_client, db):
        """A row seeded directly (bypassing the funnel) can hold an ISBN-10
        in `isbn` with `isbn10` NULL — a real state for pre-0.28.0 rows."""
        _insert_item(db, title="Dune Messiah", isbn=self.ISBN10, media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv = f"title,authors,isbn,media_type\nDune Messiah,,{self.ISBN13},book\n"
        result = _import(admin_client, csv, mode="skip")

        assert _count(db) == before
        assert result["skipped"] == 1 and result["imported"] == 0

    def test_two_forms_of_one_isbn_in_one_file_dedupe_against_each_other(self, admin_client, db):
        before = _count(db)
        csv = (
            "title,authors,isbn,media_type\n"
            f"Dune Messiah,,{self.ISBN10},book\n"
            f"Dune Messiah,,{self.ISBN13},book\n"
        )
        result = _import(admin_client, csv, mode="skip")

        assert _count(db) == before + 1
        assert result["imported"] == 1 and result["skipped"] == 1

    def test_bad_checksum_isbn_reports_invalid_isbn_and_next_row_still_imports(self, admin_client, db):
        before = _count(db)
        csv = (
            "title,authors,isbn,media_type\n"
            "Bad Checksum Book,,9999999999999,book\n"
            "Good Row,,,book\n"
        )
        result = _import(admin_client, csv, mode="skip")

        assert _count(db) == before + 1
        assert result["imported"] == 1
        assert len(result["errors"]) == 1
        assert "Invalid ISBN" in result["errors"][0]
        row = db.execute(
            "SELECT id FROM items WHERE title = 'Good Row'"
        ).fetchone()
        assert row is not None


class TestUnknownMode:
    """An unrecognized mode must be rejected before the file is even read —
    no rows inserted, updated, or skipped-and-counted; the seeded row must
    come back byte-identical."""

    def test_wrong_case_mode_is_rejected(self, admin_client, db):
        _insert_item(db, title="Some Game", isbn=None, media_type="video_game",
                     publisher="Original Publisher")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Namco\n"
        result = _import(admin_client, csv, mode="Skip")

        assert result.get("error")
        assert result["imported"] == 0
        assert result["skipped"] == 0
        assert _count(db) == before
        row = db.execute(
            "SELECT publisher FROM items WHERE title = 'Some Game'"
        ).fetchone()
        assert row["publisher"] == "Original Publisher"

    def test_unrecognized_mode_name_is_rejected(self, admin_client, db):
        _insert_item(db, title="Some Game", isbn=None, media_type="video_game",
                     publisher="Original Publisher")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Namco\n"
        result = _import(admin_client, csv, mode="merge")

        assert result.get("error")
        assert result["imported"] == 0
        assert result["skipped"] == 0
        assert _count(db) == before
        row = db.execute(
            "SELECT publisher FROM items WHERE title = 'Some Game'"
        ).fetchone()
        assert row["publisher"] == "Original Publisher"

    def test_omitted_mode_defaults_to_skip(self, admin_client, db):
        _insert_item(db, title="Some Game", isbn=None, media_type="video_game",
                     publisher="Original Publisher")
        db.execute("COMMIT")
        before = _count(db)

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Namco\n"
        resp = admin_client.post(
            "/api/import/csv",
            files={"file": ("export.csv", io.BytesIO(csv.encode()), "text/csv")},
        )
        assert resp.status_code == 200
        result = resp.json()

        assert not result.get("error")
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert _count(db) == before
        row = db.execute(
            "SELECT publisher FROM items WHERE title = 'Some Game'"
        ).fetchone()
        assert row["publisher"] == "Original Publisher"


class TestTagsRoundTrip:
    """T8: tags survive export -> import, additively — the import never
    removes a tag — and a `kids_book` row earns the `Kids` tag on the way
    in, the same statement _retire_kids_book makes at boot for a live row."""

    def test_tags_survive_export_into_a_fresh_library(self, admin_client, db):
        from app.services import tags as tags_svc

        item_id = _insert_item(db, title="Tagged Book", isbn="9780441013593", media_type="book")
        db.execute("COMMIT")
        tags_svc.attach_tags(db, item_id, ["Signed", "First Edition"])
        db.execute("COMMIT")

        exported = _export(admin_client)
        db.execute("DELETE FROM item_tags")
        db.execute("DELETE FROM list_items")
        db.execute("DELETE FROM items")
        db.execute("DELETE FROM tags")
        db.execute("COMMIT")

        result = _import(admin_client, exported)
        assert (result["imported"], result["errors"]) == (1, [])

        new_id = db.execute(
            "SELECT id FROM items WHERE title = 'Tagged Book'"
        ).fetchone()["id"]
        names = {row["name"] for row in tags_svc.get_item_tags(db, new_id)}
        assert names == {"Signed", "First Edition"}

    def test_reimporting_an_export_with_tags_is_idempotent(self, admin_client, db):
        """update mode on the same export must not duplicate the association
        or the tag row itself."""
        from app.services import tags as tags_svc

        item_id = _insert_item(db, title="Idempotent Book", isbn="9780553283686",
                               media_type="book")
        db.execute("COMMIT")
        tags_svc.attach_tags(db, item_id, ["Signed"])
        db.execute("COMMIT")

        exported = _export(admin_client)
        result = _import(admin_client, exported, mode="update")
        assert result["errors"] == []

        rows = tags_svc.get_item_tags(db, item_id)
        assert [r["name"] for r in rows] == ["Signed"]
        count = db.execute(
            "SELECT COUNT(*) c FROM tags WHERE name = 'Signed'"
        ).fetchone()["c"]
        assert count == 1

    def test_no_tags_column_imports_as_before_and_removes_nothing(self, admin_client, db):
        """A CSV with no `tags` column at all (pre-#T8 export, or a
        hand-edited file) must not strip an existing row's tags on an
        update-mode reimport — absent and empty are the same answer here."""
        from app.services import tags as tags_svc

        item_id = _insert_item(db, title="Untouched Book", isbn="9780441172719",
                               media_type="book")
        db.execute("COMMIT")
        tags_svc.attach_tags(db, item_id, ["Signed"])
        db.execute("COMMIT")

        csv_content = "title,authors,isbn,media_type\nUntouched Book,,9780441172719,book\n"
        result = _import(admin_client, csv_content, mode="update")
        assert (result["imported"], result["errors"]) == (1, [])

        rows = tags_svc.get_item_tags(db, item_id)
        assert [r["name"] for r in rows] == ["Signed"]

    def test_kids_book_row_lands_as_book_plus_kids_tag(self, admin_client, db):
        from app.services import tags as tags_svc

        csv_content = "title,authors,isbn,media_type\nAlice,,9780441172719,kids_book\n"
        result = _import(admin_client, csv_content)
        assert (result["imported"], result["errors"]) == (1, [])

        item = db.execute(
            "SELECT id, media_type FROM items WHERE isbn = '9780441172719'"
        ).fetchone()
        assert item["media_type"] == "book"
        names = {r["name"] for r in tags_svc.get_item_tags(db, item["id"])}
        assert "Kids" in names

    def test_kids_book_row_dedupes_against_an_existing_book_isbn(self, admin_client, db):
        """The alias must land in normalize_generic, above the dedupe guard
        (G100) — a kids_book row for an ISBN already stored as book is a
        twin, not a new row."""
        _insert_item(db, title="Existing", isbn="9780316769488", media_type="book")
        db.execute("COMMIT")
        before = _count(db)

        csv_content = "title,authors,isbn,media_type\nExisting,,9780316769488,kids_book\n"
        result = _import(admin_client, csv_content)

        assert _count(db) == before
        assert result["skipped"] == 1 and result["imported"] == 0
        assert result["errors"] == []

    def test_overlong_tags_cell_is_a_row_error_and_writes_nothing(self, admin_client, db):
        before = _count(db)
        long_tags = "A" * 1001
        csv_content = (
            f"title,authors,isbn,media_type,tags\nOverlong,,9780441013593,book,{long_tags}\n"
        )
        result = _import(admin_client, csv_content)

        assert _count(db) == before
        assert result["imported"] == 0
        assert any("tags too long" in e for e in result["errors"])

    def test_storygraph_tags_column_imports_fine_and_attaches_nothing(self, admin_client, db):
        """StoryGraph's own Tags column survives header lowercasing into
        row["tags"] — normalize_storygraph never reads it, so a populated
        Tags cell must not fail the length check or attach anything."""
        from app.services import tags as tags_svc
        from tests.test_reading_imports import STORYGRAPH_HEADER

        long_tags = "fantasy;" * 200  # well over 1000 chars, in the Tags column
        row = (
            "Piranesi,Susanna Clarke,,9781635575637,digital,read,2023-01-15,"
            f"2023-02-20,,1,4.5,,{long_tags},Yes"
        )
        csv_content = STORYGRAPH_HEADER + "\n" + row
        result = _import(admin_client, csv_content)

        assert result["imported"] == 1
        assert result["errors"] == []
        item = db.execute(
            "SELECT id FROM items WHERE isbn = '9781635575637'"
        ).fetchone()
        assert tags_svc.get_item_tags(db, item["id"]) == []


class TestUploadReadIsBounded:
    """G55: bound the read before validating. `items_csv.py` used to call
    `await csv_file.read()` with no argument, buffering the entire upload
    before the 50 MB ceiling was ever consulted."""

    def test_read_is_called_with_the_ceiling_plus_one(self, admin_client, monkeypatch):
        from starlette.datastructures import UploadFile

        from app.routers import items_csv

        calls = []
        original_read = UploadFile.read

        async def spy_read(self, size=-1):
            calls.append(size)
            return await original_read(self, size)

        monkeypatch.setattr(UploadFile, "read", spy_read)

        csv_content = "title,authors,isbn,media_type\nSome Book,Someone,9780441013593,book\n"
        result = _import(admin_client, csv_content)

        assert result["imported"] == 1
        assert calls == [items_csv.MAX_CSV_UPLOAD_SIZE + 1]


# The 19-column export header (#145), for hand-written generic files below.
HEADER = ("title,authors,isbn,media_type,platform,publisher,publish_year,page_count,"
          "language,series_name,series_position,location,source,estimated_value,"
          "manual_value,owned,wishlisted,deleted,tags")
ROUND_TRIP = ("platform", "language", "series_position", "location",
              "estimated_value", "manual_value")


def _csv(*rows):
    """A generic file: each row is a dict of the cells it sets; others blank."""
    cols = HEADER.split(",")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(cols)
    for row in rows:
        writer.writerow([row.get(c, "") for c in cols])
    return out.getvalue()


def _item(db, title):
    return db.execute(
        "SELECT i.*, l.name AS location_name FROM items i "
        "LEFT JOIN locations l ON i.location_id = l.id WHERE i.title = ?",
        (title,),
    ).fetchone()


def _location_count(db):
    return db.execute("SELECT COUNT(*) AS c FROM locations").fetchone()["c"]


def _node_count(db):
    return db.execute("SELECT COUNT(*) FROM location_nodes").fetchone()[0]


def _wipe_library(db):
    for table in ("item_tags", "list_items", "item_copies", "items"):
        db.execute(f"DELETE FROM {table}")
    # This fixture promises a genuinely fresh receiving catalogue. NODE trees
    # are independent of flat aliases and must also be removed, leaf-first.
    from app.services import location_tree
    for node in reversed(location_tree.flattened_tree(db)):
        db.execute("DELETE FROM location_nodes WHERE id = ?", (node["id"],))
    db.execute("DELETE FROM locations")
    db.execute("COMMIT")


def _platform(db):
    from app.database import get_game_platforms
    return next(iter(get_game_platforms(db)))


class TestRoundTripFields:
    """#145: every column the export writes, the import reads (except source)."""

    def _seed(self, db, location):
        from app.services import item_copies, locations
        from app.services.item_write import insert_item
        loc_id = None
        for label in location.split(" / "):
            loc_id = locations.create_location(db, label, parent_id=loc_id)
        item_id = insert_item(
            db, title="Round Trip Game", isbn=None, media_type="video_game",
            platform=_platform(db), language="ja", series_position=3.5,
            manual_value=40.0, estimated_value=25.5,
        )
        primary = db.execute(
            "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,),
        ).fetchone()
        assert primary is not None
        item_copies.update_copy(db, primary["id"], {"location_id": loc_id})
        assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None
        db.execute("COMMIT")
        return item_id

    @staticmethod
    def _cells(exported, title="Round Trip Game"):
        row = next(r for r in csv.DictReader(io.StringIO(exported)) if r["title"] == title)
        return {c: row[c] for c in ROUND_TRIP}

    def test_six_fields_survive_into_a_fresh_library(self, admin_client, db):
        from app.services import location_tree

        self._seed(db, "Game Shelf")
        first = _export(admin_client)
        before = self._cells(first)
        assert before["language"] == "ja" and before["location"] == "Game Shelf"
        _wipe_library(db)
        assert _node_count(db) == _location_count(db) == 0

        result = _import(admin_client, first)
        assert (result["imported"], result["errors"]) == (1, [])
        assert result["locations_created"] == ["Game Shelf"]
        assert not [d for d in result["dropped"] if d["column"] != "source"]

        assert self._cells(_export(admin_client)) == before
        row = _item(db, "Round Trip Game")
        assert (row["series_position"], row["manual_value"], row["estimated_value"]) == (3.5, 40.0, 25.5)
        primary = db.execute(
            "SELECT location_id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (row["id"],),
        ).fetchone()
        assert primary["location_id"] == location_tree.legacy_location_to_node(db, row["location_id"])
        assert location_tree.node_to_legacy_location(db, primary["location_id"]) == row["location_id"]

    def test_nested_location_resolves_when_the_target_has_the_hierarchy(self, admin_client, db):
        from app.services import location_tree, locations
        self._seed(db, "Office / Shelf 2")
        exported = _export(admin_client)
        assert self._cells(exported)["location"] == "Office / Shelf 2"
        _wipe_library(db)
        office = locations.create_location(db, "Office")
        shelf = locations.create_location(db, "Shelf 2", parent_id=office)
        db.execute("COMMIT")
        nodes_before = _node_count(db)

        result = _import(admin_client, exported)
        assert (result["imported"], result["errors"]) == (1, [])
        assert result["locations_created"] == []
        item = _item(db, "Round Trip Game")
        assert item["location_id"] is None
        assert location_tree.node_to_legacy_location(db, shelf) is None
        assert db.execute(
            "SELECT location_id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item["id"],),
        ).fetchone()[0] == shelf
        assert _node_count(db) == nodes_before and _location_count(db) == 0

    def test_reimporting_creates_nothing_in_either_mode(self, admin_client, db):
        self._seed(db, "Game Shelf")
        exported = _export(admin_client)
        locs = _location_count(db)
        nodes = _node_count(db)
        copies_before = [tuple(r) for r in db.execute("SELECT * FROM item_copies ORDER BY id")]
        for mode in ("skip", "update"):
            result = _import(admin_client, exported, mode=mode)
            assert result["errors"] == []
            assert result["locations_created"] == []
        assert _location_count(db) == locs
        assert _node_count(db) == nodes
        assert [tuple(r) for r in db.execute("SELECT * FROM item_copies ORDER BY id")] == copies_before
        assert _count(db) == 1

    def test_new_location_is_listed_once_across_many_rows(self, admin_client, db):
        result = _import(admin_client, _csv(
            {"title": "A", "location": "Attic"},
            {"title": "B", "location": "attic"},
            {"title": "C", "location": "Attic"},
        ))
        assert (result["imported"], result["errors"]) == (3, [])
        assert result["locations_created"] == ["Attic"]
        assert _location_count(db) == 1


class TestRoundTripUpdateMode:
    def test_update_relocates_the_primary_and_leaves_a_second_copy(self, admin_client, db):
        from app.services import item_copies, location_tree, locations
        from app.services.item_write import insert_item
        old = locations.create_location(db, "Old Shelf")
        other = locations.create_location(db, "Other Shelf")
        item_id = insert_item(db, title="Located", isbn=None, authors="X")
        primary_id = db.execute(
            "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,),
        ).fetchone()[0]
        item_copies.update_copy(db, primary_id, {"location_id": old})
        second = item_copies.add_copy(db, item_id, {"location_id": other, "position_order": 27})
        db.execute("COMMIT")
        second_before = tuple(db.execute("SELECT * FROM item_copies WHERE id = ?", (second,)).fetchone())

        result = _import(admin_client, _csv(
            {"title": "Located", "authors": "X", "location": "New Shelf"}), mode="update")
        assert (result["imported"], result["errors"]) == (1, [])
        assert result["locations_created"] == ["New Shelf"]
        new = db.execute("SELECT id FROM locations WHERE name = 'New Shelf'").fetchone()["id"]
        new_node = location_tree.legacy_location_to_node(db, new)
        assert _item(db, "Located")["location_id"] == new
        copies = {r["id"]: (r["is_primary"], r["location_id"]) for r in db.execute(
            "SELECT id, is_primary, location_id FROM item_copies WHERE item_id = ?", (item_id,))}
        assert copies[second] == (0, other)
        assert copies[primary_id] == (1, new_node)
        assert [loc for primary, loc in copies.values() if primary] == [new_node]
        assert tuple(db.execute("SELECT * FROM item_copies WHERE id = ?", (second,)).fetchone()) == second_before

    def test_update_fills_an_unlocated_item(self, admin_client, db):
        from app.services import location_tree, locations
        from app.services.item_write import insert_item
        loc = locations.create_location(db, "Hall")
        item_id = insert_item(db, title="Unlocated", isbn=None, authors="Y")
        db.execute("COMMIT")

        result = _import(admin_client, _csv(
            {"title": "Unlocated", "authors": "Y", "location": "hall"}), mode="update")
        assert (result["imported"], result["errors"], result["locations_created"]) == (1, [], [])
        assert _item(db, "Unlocated")["location_id"] is None
        assert location_tree.node_to_legacy_location(db, loc) is None
        assert db.execute(
            "SELECT location_id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()["location_id"] == loc

    def test_update_applies_values_but_never_estimated_value(self, admin_client, db):
        plat = _platform(db)
        _insert_item(db, title="Valued", isbn=None, authors="Z", estimated_value=10.0,
                     media_type="video_game")
        db.execute("COMMIT")

        result = _import(admin_client, _csv({
            "title": "Valued", "authors": "Z", "media_type": "video_game", "platform": plat,
            "language": "Japanese", "series_position": "2", "manual_value": "$12.50",
            "estimated_value": "99",
        }), mode="update")
        assert (result["imported"], result["errors"]) == (1, [])
        row = _item(db, "Valued")
        assert (row["platform"], row["language"], row["series_position"], row["manual_value"]) == (
            plat, "ja", 2.0, 12.5)
        assert row["estimated_value"] == 10.0

    def test_estimated_value_is_applied_on_insert(self, admin_client, db):
        result = _import(admin_client, _csv({"title": "Fresh", "estimated_value": "7.25"}))
        assert result["errors"] == []
        assert _item(db, "Fresh")["estimated_value"] == 7.25

    def test_empty_cells_leave_every_field_alone(self, admin_client, db):
        from app.services import item_copies, locations
        from app.services.item_write import insert_item
        plat = _platform(db)
        loc = locations.create_location(db, "Kept")
        item_id = insert_item(
            db, title="Full", isbn=None, authors="W", media_type="video_game", platform=plat,
            language="fr", series_position=4.0, manual_value=5.0, estimated_value=6.0,
        )
        primary = db.execute(
            "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,),
        ).fetchone()[0]
        item_copies.update_copy(db, primary, {"location_id": loc})
        db.execute("COMMIT")
        copies_before = [tuple(r) for r in db.execute("SELECT * FROM item_copies ORDER BY id")]

        result = _import(admin_client, _csv(
            {"title": "Full", "authors": "W", "media_type": "video_game"}), mode="update")
        assert (result["imported"], result["errors"]) == (1, [])
        row = _item(db, "Full")
        assert (row["platform"], row["language"], row["series_position"], row["manual_value"],
                row["estimated_value"], row["location_id"]) == (plat, "fr", 4.0, 5.0, 6.0, None)
        assert [tuple(r) for r in db.execute("SELECT * FROM item_copies ORDER BY id")] == copies_before
        assert db.execute("SELECT location_id FROM item_copies WHERE id = ?", (primary,)).fetchone()[0] == loc

    def test_skip_mode_hit_with_a_new_location_creates_none(self, admin_client, db):
        _insert_item(db, title="Skipped", isbn=None, authors="V")
        db.execute("COMMIT")
        result = _import(admin_client, _csv(
            {"title": "Skipped", "authors": "V", "location": "Nowhere Yet"}))
        assert (result["skipped"], result["locations_created"]) == (1, [])
        assert _location_count(db) == 0

    def test_librarything_update_leaves_series_position_alone(self, admin_client, db):
        _insert_item(db, title="Guards! Guards!", isbn="9780061020643", series_name="Discworld",
                     series_position=8.0)
        db.execute("COMMIT")
        cols = ["Book Id", "Title", "Primary Author", "ISBN", "Series", "Media"]
        tsv = "\t".join(cols) + "\n" + "\t".join(
            ["1", "Guards! Guards!", "Terry Pratchett", "9780061020643", "Discworld (99)",
             "Paperback"]) + "\n"
        result = _import(admin_client, tsv, mode="update")
        assert result["format"] == "librarything", result
        assert result["errors"] == []
        assert _item(db, "Guards! Guards!")["series_position"] == 8.0


class TestRoundTripRowErrors:
    """A bad cell is a row error, and the row writes nothing — no item, no
    location (G85). Asserted on the database, not the response alone."""

    def _assert_nothing_written(self, admin_client, db, row, expect):
        items, locs = _count(db), _location_count(db)
        result = _import(admin_client, _csv({"title": "Bad Row", **row}))
        assert result["error_count"] == 1, result
        assert result["errors"][0].startswith("Row 2: ") and expect in result["errors"][0]
        assert result["locations_created"] == []
        assert (_count(db), _location_count(db)) == (items, locs)

    def test_non_numeric_series_position(self, admin_client, db):
        self._assert_nothing_written(admin_client, db, {"series_position": "vol. 3"},
                                     'series_position "vol. 3" is not a number')

    def test_manual_value_that_is_not_a_price(self, admin_client, db):
        self._assert_nothing_written(admin_client, db, {"manual_value": "£5 (2 for 1)"},
                                     "is not a price")

    def test_negative_value(self, admin_client, db):
        self._assert_nothing_written(admin_client, db, {"estimated_value": "-4"},
                                     'estimated_value "-4" is not a price')

    def test_unmatched_nested_location(self, admin_client, db):
        self._assert_nothing_written(admin_client, db, {"location": "A / B"},
                                     'location "A / B" not found')

    def test_invalid_isbn_with_a_new_location(self, admin_client, db):
        self._assert_nothing_written(admin_client, db,
                                     {"isbn": "9780000000000", "location": "Brand New"},
                                     "Invalid ISBN")

    def test_unknown_platform_with_a_new_location(self, admin_client, db):
        self._assert_nothing_written(
            admin_client, db,
            {"media_type": "video_game", "platform": "no-such-console", "location": "Brand New"},
            "Unknown game platform")
