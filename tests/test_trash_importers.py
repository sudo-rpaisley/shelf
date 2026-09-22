"""CSV import restores a trashed dedup hit instead of shadowing it.

T11 of the soft-delete-collisions plan. `app/routers/items_csv.py`'s two
dedup reads (ISBN and the title/author fallback) already read the physical
`items` table, so a trashed row was found as `existing` and then treated as
a live hit: skip mode reported "skipped" while the row sat invisibly in
Trash, and update mode wrote metadata to a row nobody could see. Neither
mode restored it.

Now:

- a trashed hit is restored before anything else about the row is decided,
  in both skip and update mode (G85 — the restore is the row's first write);
- a live row always wins over a trashed one sharing the same dedup key
  (claude-R4, "live wins") — both reads carry `ORDER BY deleted_at IS NOT
  NULL, id LIMIT 1`, so a trashed row is only acted on when no live row
  matches.
"""

import io

from app.services import isbn as isbn_svc
from app.services.item_write import trash_item
from tests.conftest import _insert_item

ISBN13, ISBN10 = isbn_svc.canonical_isbn_pair("9780441013593")


def _import(client, content, mode="skip"):
    resp = client.post(
        "/api/import/csv",
        files={"file": ("export.csv", io.BytesIO(content.encode()), "text/csv")},
        data={"mode": mode},
    )
    assert resp.status_code == 200
    return resp.json()


class TestIsbnHitRestoresInBothModes:
    def test_skip_mode_restores_and_keeps_stored_fields(self, admin_client, db):
        item_id = _insert_item(db, title="Dune", isbn=ISBN13, media_type="book",
                                publisher="Chilton")
        trash_item(db, item_id)
        db.execute("COMMIT")

        csv = f"title,authors,isbn,media_type,publisher\nDune,,{ISBN13},book,Ace Books\n"
        result = _import(admin_client, csv, mode="skip")

        assert result["restored"] == 1
        assert result["skipped"] == 0
        assert result["imported"] == 0
        row = db.execute(
            "SELECT deleted_at, publisher FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["deleted_at"] is None
        # Skip mode never writes fields — restore_item only clears deleted_at.
        assert row["publisher"] == "Chilton"

    def test_update_mode_restores_and_applies_csv_fields(self, admin_client, db):
        item_id = _insert_item(db, title="Dune", isbn=ISBN13, media_type="book",
                                publisher="Chilton")
        trash_item(db, item_id)
        db.execute("COMMIT")

        csv = f"title,authors,isbn,media_type,publisher\nDune,,{ISBN13},book,Ace Books\n"
        result = _import(admin_client, csv, mode="update")

        # Disjoint: a restored row is `restored` and nothing else, in update
        # mode exactly as in skip mode.
        assert result["restored"] == 1
        assert result["imported"] == 0
        assert result["skipped"] == 0
        row = db.execute(
            "SELECT deleted_at, publisher FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["deleted_at"] is None
        assert row["publisher"] == "Ace Books"


class TestTitleFallbackHitRestoresInBothModes:
    def test_skip_mode_restores_and_keeps_stored_fields(self, admin_client, db):
        item_id = _insert_item(db, title="Some Game", isbn=None,
                                media_type="video_game", publisher="Namco")
        trash_item(db, item_id)
        db.execute("COMMIT")

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Bandai\n"
        result = _import(admin_client, csv, mode="skip")

        assert result["restored"] == 1
        assert result["skipped"] == 0
        assert result["imported"] == 0
        row = db.execute(
            "SELECT deleted_at, publisher FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["deleted_at"] is None
        assert row["publisher"] == "Namco"

    def test_update_mode_restores_and_applies_csv_fields(self, admin_client, db):
        item_id = _insert_item(db, title="Some Game", isbn=None,
                                media_type="video_game", publisher="Namco")
        trash_item(db, item_id)
        db.execute("COMMIT")

        csv = "title,authors,isbn,media_type,publisher\nSome Game,,,video_game,Bandai\n"
        result = _import(admin_client, csv, mode="update")

        # Disjoint: a restored row is `restored` and nothing else, in update
        # mode exactly as in skip mode.
        assert result["restored"] == 1
        assert result["imported"] == 0
        assert result["skipped"] == 0
        row = db.execute(
            "SELECT deleted_at, publisher FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["deleted_at"] is None
        assert row["publisher"] == "Bandai"


class TestLiveWinsOverTrashedTwin:
    """The coexistence pin (claude-R4 / codex-R1). The trashed twin is seeded
    FIRST in both tests, so it holds the lower id and would win an
    *unordered* `LIMIT 1` — without `ORDER BY deleted_at IS NOT NULL` this
    pin would pass against the exact defect it exists to catch."""

    def test_title_only_twins_live_wins_trashed_stays_trashed(self, admin_client, db):
        trashed_id = _insert_item(db, title="Foundation", isbn=None,
                                   media_type="book", publisher="Old Press")
        trash_item(db, trashed_id)
        live_id = _insert_item(db, title="Foundation", isbn=None,
                                media_type="book", publisher="Live Press")
        db.execute("COMMIT")

        csv = "title,authors,isbn,media_type,publisher\nFoundation,,,book,New Press\n"
        result = _import(admin_client, csv, mode="update")

        assert result["restored"] == 0
        assert result["imported"] == 1
        live_row = db.execute(
            "SELECT publisher FROM items WHERE id = ?", (live_id,)
        ).fetchone()
        assert live_row["publisher"] == "New Press"
        trashed_row = db.execute(
            "SELECT deleted_at, publisher FROM items WHERE id = ?", (trashed_id,)
        ).fetchone()
        assert trashed_row["deleted_at"] is not None
        assert trashed_row["publisher"] == "Old Press"

    def test_isbn_twins_live_wins_trashed_stays_trashed(self, admin_client, db):
        # Different `isbn` values (the legacy ISBN-10 form vs the canonical
        # ISBN-13 one) so both rows can hold their UNIQUE(isbn, media_type)
        # slot at once — `isbn IN (?, ?)` matches either. Two rows sharing
        # the identical isbn+media_type can't coexist at all: a trashed row
        # keeps its slot (G107), so the DB itself refuses a second INSERT.
        trashed_id = _insert_item(db, title="Old Copy", isbn=ISBN10, media_type="book")
        trash_item(db, trashed_id)
        live_id = _insert_item(db, title="Live Copy", isbn=ISBN13, media_type="book")
        db.execute("COMMIT")

        csv = f"title,authors,isbn,media_type,publisher\nLive Copy,,{ISBN13},book,New Press\n"
        result = _import(admin_client, csv, mode="update")

        assert result["restored"] == 0
        live_row = db.execute(
            "SELECT publisher FROM items WHERE id = ?", (live_id,)
        ).fetchone()
        assert live_row["publisher"] == "New Press"
        trashed_row = db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (trashed_id,)
        ).fetchone()
        assert trashed_row["deleted_at"] is not None


class TestRestoredCounterIsZeroWithNoTrashedHit:
    def test_a_plain_import_reports_no_restores(self, admin_client, db):
        csv = "title,authors,isbn,media_type\nBrand New Book,,,book\n"
        result = _import(admin_client, csv, mode="skip")

        assert result["restored"] == 0


class TestTheCountsPartitionTheRows:
    """`restored` is counted SEPARATELY from `imported` and `skipped` — the
    plan's own word — which means the three never overlap. They did in update
    mode, where a restored row was counted as both `restored` and `imported`:
    "Imported: 2, Restored: 1" could not tell the user whether that was two
    rows or three. Pinned in both modes, so the modes cannot drift apart
    again.
    """

    def _three_rows(self, db):
        restored = _insert_item(db, title="Was Trashed", isbn=ISBN13,
                                media_type="book")
        trash_item(db, restored)
        _insert_item(db, title="Already Live", isbn=None, media_type="book",
                     authors="Someone")
        db.execute("COMMIT")
        return (
            "title,authors,isbn,media_type\n"
            f"Was Trashed,,{ISBN13},book\n"
            "Already Live,Someone,,book\n"
            "Brand New,Nobody,,book\n"
        )

    def test_skip_mode_counts_sum_to_the_rows(self, admin_client, db):
        result = _import(admin_client, self._three_rows(db), mode="skip")
        assert (result["restored"], result["skipped"], result["imported"]) == (1, 1, 1)
        assert result["restored"] + result["skipped"] + result["imported"] == 3

    def test_update_mode_counts_sum_to_the_rows(self, admin_client, db):
        result = _import(admin_client, self._three_rows(db), mode="update")
        # The live hit and the brand-new row are both `imported` in update
        # mode — that is the existing meaning of the word there.
        assert (result["restored"], result["skipped"], result["imported"]) == (1, 0, 2)
        assert result["restored"] + result["skipped"] + result["imported"] == 3


# ================================================================ archive ====
#
# T12. Risk floor: reads or writes outside data/. `apply_plan` catches
# per-item exceptions and then commits the whole import, so every assertion
# here reads the DATABASE after the call, never only the report (G85).

import json
import sqlite3
import zipfile

from app.services.archive import merge_archive, read_archive
from app.services import item_copies

_MANIFEST = json.dumps({"format": "shelf-archive", "version": 1,
                        "exported_at": "2026-08-18T00:00:00Z",
                        "app_version": None, "counts": {}})


def _archive(tmp_path, library, name="t.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _MANIFEST)
        zf.writestr("library.json", json.dumps(library))
    return path


def _merge(db, path, mode="skip"):
    with read_archive(path) as reader:
        report = merge_archive(db, reader, mode=mode)
    try:
        db.execute("COMMIT")
    except sqlite3.OperationalError as e:
        if "no transaction is active" not in str(e):
            raise
    return report


def _record(copies="absent"):
    rec = {"id": 1, "title": "Restorable", "isbn": ISBN13, "media_type": "book",
           "owned": 1, "source": "manual"}
    if copies != "absent":
        rec["copies"] = copies
    return rec


def _trashed_with_two_copies(db):
    item_id = _insert_item(db, title="Restorable", isbn=ISBN13, media_type="book")
    item_copies.add_copy(db, item_id)
    item_copies.add_copy(db, item_id)
    trash_item(db, item_id)
    db.execute("COMMIT")
    return item_id


def _copy_numbers(db, item_id):
    return sorted(r["copy_number"] for r in db.execute(
        "SELECT copy_number FROM copies_live WHERE item_id = ?", (item_id,)))


import pytest


class TestArchiveRestoresAndStaysCommitted:
    @pytest.mark.parametrize("copies", [
        "absent",
        [],
        [{"copy_number": 1, "is_primary": 1}, {"copy_number": 2, "is_primary": 0}],
    ], ids=["copies-absent", "copies-empty", "copies-two"])
    def test_a_restored_row_keeps_its_own_copies(self, db, tmp_path, copies):
        """`_import_copies` is skipped on a restored row. Its `[]` arm would
        run `delete_copies_for_item` and destroy the copies the row already
        has; its reconcile arm would collide on UNIQUE(item_id, copy_number).
        Asserted on the database, for all three shapes (G87: `copies: None`
        and `[]` are different answers — and on a restored row neither may
        touch the user's copies)."""
        item_id = _trashed_with_two_copies(db)

        report = _merge(db, _archive(tmp_path, {"items": [_record(copies)]}))

        assert report["errors"] == []
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] is None
        assert _copy_numbers(db, item_id) == [1, 2]

    def test_the_restored_record_is_not_reported_as_drifted(self, db, tmp_path):
        """G72 — plan and apply must dedupe on the same value. The plan reads
        items_live and sees `create`; apply restores through the funnel. A
        restore must not land in `drifted`."""
        _trashed_with_two_copies(db)

        report = _merge(db, _archive(tmp_path, {"items": [_record()]}))

        assert report.get("drifted", 0) == 0
        assert report["imported"] == 1

    def test_siblings_in_the_same_archive_still_import(self, db, tmp_path):
        _trashed_with_two_copies(db)
        sibling = {"id": 2, "title": "Sibling", "media_type": "book",
                   "owned": 1, "source": "manual"}

        _merge(db, _archive(tmp_path, {"items": [_record(), sibling]}))

        assert db.execute(
            "SELECT COUNT(*) AS n FROM items_live WHERE title = 'Sibling'"
        ).fetchone()["n"] == 1

    def test_an_open_loan_is_not_doubled(self, db, tmp_path):
        """claude-R6. A trashed row keeps its loans (T2 writes nothing to
        them), and the archive's history loop attaches reading_log and
        checkouts to every id in `id_map` — "newly created items only". A
        restored id entering that map would attach the archive's copy of the
        same loan beside the row's own."""
        from tests.conftest import _insert_borrower

        item_id = _insert_item(db, title="Restorable", isbn=ISBN13,
                               media_type="book")
        borrower = _insert_borrower(db, name="Bea")
        db.execute(
            "INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)",
            (item_id, borrower))
        trash_item(db, item_id)
        db.execute("COMMIT")

        _merge(db, _archive(tmp_path, {
            "items": [_record()],
            "checkouts": [{"item_id": 1, "borrower": "Bea"}],
        }))

        assert db.execute(
            "SELECT COUNT(*) AS n FROM checkouts WHERE item_id = ?", (item_id,)
        ).fetchone()["n"] == 1


class TestArchiveRestoreKeepsTheStoredCover:
    """The cover-kept arm G112 was written about: a restored row that already
    has a cover keeps it, and the archive's image is not written over
    `covers/<id>.jpg` (`claude-M3`, M22). A row that had none still gets
    the archive's."""

    _JPEG = b"\xff\xd8\xff" + b"\x00" * 200

    def _archive_with_cover(self, tmp_path):
        path = tmp_path / "c.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", _MANIFEST)
            zf.writestr("library.json", json.dumps(
                {"items": [{**_record(), "cover": "covers/1.jpg"}]}))
            zf.writestr("covers/1.jpg", self._JPEG)
        return path

    def test_a_restored_row_with_a_cover_keeps_it(self, db, tmp_path):
        from app import config

        item_id = _insert_item(db, title="Restorable", isbn=ISBN13,
                               media_type="book")
        db.execute("UPDATE items SET cover_path = 'covers/mine.jpg' "
                   "WHERE id = ?", (item_id,))
        trash_item(db, item_id)
        db.execute("COMMIT")

        report = _merge(db, self._archive_with_cover(tmp_path))

        assert report["errors"] == []
        assert db.execute(
            "SELECT cover_path FROM items WHERE id = ?", (item_id,)
        ).fetchone()["cover_path"] == "covers/mine.jpg"
        assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()

    def test_a_restored_row_without_a_cover_gets_the_archives(
        self, db, tmp_path
    ):
        item_id = _insert_item(db, title="Restorable", isbn=ISBN13,
                               media_type="book")
        trash_item(db, item_id)
        db.execute("COMMIT")

        _merge(db, self._archive_with_cover(tmp_path))

        assert db.execute(
            "SELECT cover_path FROM items WHERE id = ?", (item_id,)
        ).fetchone()["cover_path"] == f"covers/{item_id}.jpg"


class TestTheUpdatePathRefusesAndLeavesNothingBehind:
    def test_an_update_onto_a_trashed_slot_writes_no_location(self, db, tmp_path):
        """Step 3. `_apply_item_update` called `get_location_id` — a
        get-or-create — BEFORE its update_item_fields. A refusal raised by
        the update would land after that location was minted, and
        apply_plan's shared transaction would commit it beside the error.
        The preflight now runs first. The shape of
        `test_a_rejected_item_leaves_nothing_behind`.

        **The collision can only arrive through UPC.** `_dedupe_lookup` keys
        on `(isbn, media_type)`, so a record matches a row that already holds
        its ISBN slot — the update path can never move a row to a different
        ISBN or media_type slot. The design said this caller "can carry all
        three"; only `upc` can actually collide here. Matched by title (the
        row has no ISBN), then handed an archive UPC a trashed row holds.
        """
        blocker = _insert_item(db, title="Blocker", isbn=None,
                               upc="012569803121", media_type="book")
        trash_item(db, blocker)
        _insert_item(db, title="Matched", isbn=None, media_type="book",
                     location_id=None)
        db.execute("COMMIT")

        record = {"id": 9, "title": "Matched", "media_type": "book",
                  "owned": 1, "source": "manual", "upc": "012569803121",
                  "location": "Only For This Record"}
        report = _merge(db, _archive(tmp_path, {"items": [record]}),
                        mode="update")

        assert len(report["errors"]) == 1, report
        assert "Trash" in report["errors"][0]
        # The location that record alone would have created is absent.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM locations "
            "WHERE name = 'Only For This Record'"
        ).fetchone()["n"] == 0
        # And the matched row was not touched.
        assert db.execute(
            "SELECT upc FROM items WHERE title = 'Matched'"
        ).fetchone()["upc"] is None
