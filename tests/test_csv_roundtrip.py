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
    def test_header_ends_owned_wishlisted_tags(self, admin_client):
        header = _export(admin_client).splitlines()[0]
        assert header.split(",")[-3:] == ["owned", "wishlisted", "tags"]

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
        assert header.split(",")[-3:] == ["owned", "wishlisted", "tags"]

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
