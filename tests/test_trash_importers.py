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
    db.commit()
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
        """G72 — plan and apply must classify on the same value. Both now
        call `_classify`, which finds the trashed twin and says `restore`. A
        restore must not land in `drifted`.

        The counts are disjoint (plan-soft-delete-export, Decisions 6): a
        restored row is `restored`, no longer also `imported` — which this
        test asserted until the `restore` verdict existed."""
        _trashed_with_two_copies(db)

        report = _merge(db, _archive(tmp_path, {"items": [_record()]}))

        assert report.get("drifted", 0) == 0
        assert report["restored"] == 1
        assert report["imported"] == 0

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

    def _observe_cover_resources(self, db, monkeypatch):
        from app.services import archive

        register = archive.register_transaction_resource
        observed = []

        def observe(conn, **kwargs):
            assert conn is db and conn.in_transaction
            resource = register(conn, **kwargs)
            owner = conn._transaction_resources
            assert owner.conn is conn and owner.fence.fd is not None
            observed.append((resource, owner, owner.fence, owner.fence.fd))
            return resource

        monkeypatch.setattr(archive, "register_transaction_resource", observe)
        return observed

    def _assert_resource_finished(self, db, observed):
        import errno
        import os

        assert len(observed) == 1
        resource, owner, fence, fd = observed[0]
        assert not db.in_transaction
        assert db._transaction_resources is None and db._critical_resource_outcomes == []
        assert db._after_commit == db._after_rollback == []
        assert owner.entries == [] and owner.fence is None
        assert fence.fd is None and resource._active is False
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
        journal = resource._after_commit.__self__
        assert journal.entries == {} and journal.pending_undo == set() and journal.resource is None

    def test_a_restored_row_with_a_cover_keeps_it(self, db, tmp_path):
        from contextlib import closing
        from pathlib import Path

        from app import config

        stored_bytes = (Path(__file__).parent / "fixtures" / "intake" / "eleven_books.jpg").read_bytes()
        assert stored_bytes != self._JPEG
        stored_path = config.COVERS_DIR / "mine.jpg"
        stored_path.write_bytes(stored_bytes)
        item_id = _insert_item(db, title="Restorable", isbn=ISBN13,
                               media_type="book")
        db.execute("UPDATE items SET cover_path = 'covers/mine.jpg' "
                   "WHERE id = ?", (item_id,))
        trash_item(db, item_id)
        db.commit()

        report = _merge(db, self._archive_with_cover(tmp_path))

        assert report["errors"] == []
        assert (report["restored"], report["covers_installed"]) == (1, 0)
        assert report["imported"] == report["updated"] == 0
        assert db.execute(
            "SELECT cover_path FROM items WHERE id = ?", (item_id,)
        ).fetchone()["cover_path"] == "covers/mine.jpg"
        assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()
        assert stored_path.read_bytes() == stored_bytes
        assert sorted(path.name for path in config.COVERS_DIR.iterdir()) == ["mine.jpg"]
        assert not db.in_transaction and getattr(db, "_transaction_resources", None) is None
        assert db._critical_resource_outcomes == []
        with closing(sqlite3.connect(config.DATABASE_PATH)) as observer:
            assert observer.execute("SELECT id, deleted_at, cover_path FROM items").fetchall() == [
                (item_id, None, "covers/mine.jpg"),
            ]

    def test_a_restored_row_without_a_cover_gets_the_archives(
        self, db, tmp_path, monkeypatch
    ):
        from contextlib import closing

        from app import config
        from app.services import covers

        item_id = _insert_item(db, title="Restorable", isbn=ISBN13,
                               media_type="book")
        trash_item(db, item_id)
        db.commit()

        path = self._archive_with_cover(tmp_path)
        # Keep the original 203-byte fixture: it meets the documented size +
        # magic contract. The C005 failure was the raw COMMIT, not these bytes.
        assert covers.validate_uploaded_cover(self._JPEG)
        with read_archive(path) as reader:
            assert reader.read_cover("covers/1.jpg") == self._JPEG
        observed = self._observe_cover_resources(db, monkeypatch)
        report = _merge(db, path)

        assert report["errors"] == []
        assert (report["restored"], report["covers_installed"]) == (1, 1)
        assert report["imported"] == report["updated"] == 0
        self._assert_resource_finished(db, observed)
        assert db.execute(
            "SELECT cover_path FROM items WHERE id = ?", (item_id,)
        ).fetchone()["cover_path"] == f"covers/{item_id}.jpg"
        assert {path.name: path.read_bytes() for path in config.COVERS_DIR.iterdir()} == {
            f"{item_id}.jpg": self._JPEG,
        }
        with closing(sqlite3.connect(config.DATABASE_PATH)) as observer:
            assert observer.execute("SELECT id, deleted_at, cover_path FROM items").fetchall() == [
                (item_id, None, f"covers/{item_id}.jpg"),
            ]

    def test_outer_rollback_returns_restored_row_to_trash_and_removes_new_cover(
        self, db, tmp_path, monkeypatch,
    ):
        """The real restore branch retains its new file until the native outcome."""
        from contextlib import closing

        from app import config

        item_id = _insert_item(db, title="Restorable", isbn=ISBN13, media_type="book")
        trash_item(db, item_id)
        db.commit()
        before = tuple(db.iterdump())
        before_row = tuple(db.execute("SELECT id, deleted_at, cover_path FROM items").fetchone())
        assert before_row[1] is not None and before_row[2] is None
        observed = self._observe_cover_resources(db, monkeypatch)
        with read_archive(self._archive_with_cover(tmp_path)) as reader:
            assert reader.read_cover("covers/1.jpg") == self._JPEG
            report = merge_archive(db, reader)
        assert report["errors"] == []
        assert (report["restored"], report["covers_installed"]) == (1, 1)
        assert report["imported"] == report["updated"] == 0
        assert db.in_transaction and len(observed) == 1
        assert observed[0][2].fd == observed[0][3]
        assert tuple(db.execute("SELECT id, deleted_at, cover_path FROM items").fetchone()) == (
            item_id, None, f"covers/{item_id}.jpg",
        )
        assert (config.COVERS_DIR / f"{item_id}.jpg").read_bytes() == self._JPEG
        with closing(sqlite3.connect(config.DATABASE_PATH)) as observer:
            assert observer.execute("SELECT id, deleted_at, cover_path FROM items").fetchall() == [before_row]

        db.rollback()

        self._assert_resource_finished(db, observed)
        assert tuple(db.iterdump()) == before
        assert list(config.COVERS_DIR.iterdir()) == []
        with closing(sqlite3.connect(config.DATABASE_PATH)) as observer:
            assert observer.execute("SELECT id, deleted_at, cover_path FROM items").fetchall() == [before_row]


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


# ============================================ archive — the restore verdict ==
#
# plan-soft-delete-export T4. `_classify` holds the rule table; plan_archive
# and apply_plan both call it. Each rule-table row with a LIVE incoming
# record, in both modes, asserted on the plan AND on the database after apply
# (G85: never on the report alone).

from app.services.archive import apply_plan, plan_archive

UPC = "012569803121"


def _plan_apply(db, path, mode="skip", selection=None, between=None):
    with read_archive(path) as reader:
        plan = plan_archive(db, reader, mode=mode)
    if between is not None:
        between(db)
    with read_archive(path) as reader:
        report = apply_plan(db, reader, plan, selection)
    try:
        db.execute("COMMIT")
    except sqlite3.OperationalError as e:
        if "no transaction is active" not in str(e):
            raise
    return plan, report


def _live(db, item_id):
    return db.execute(
        "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()["deleted_at"] is None


def _rec(**kw):
    rec = {"id": 1, "title": "Restorable", "authors": "A. Author",
           "media_type": "book", "owned": 1, "source": "manual"}
    rec.update(kw)
    return rec


MODES = pytest.mark.parametrize("mode", ["skip", "update"])


class TestTheRestoreVerdict:
    @MODES
    def test_no_twin_creates(self, db, tmp_path, mode):
        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}), mode)
        assert plan["items"][0]["verdict"] == "create"
        assert plan["summary"]["restore"] == 0
        assert report["imported"] == 1 and report["restored"] == 0

    @MODES
    def test_a_trashed_isbn_twin_is_restored(self, db, tmp_path, mode):
        twin = _insert_item(db, title="Restorable", isbn=ISBN13)
        trash_item(db, twin)
        db.execute("COMMIT")

        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}), mode)

        assert plan["items"][0]["verdict"] == "restore"
        assert plan["items"][0]["basis"] == "isbn"
        assert plan["summary"]["restore"] == 1
        # Live matches only — a trashed twin is not "already in your library".
        assert plan["summary"]["by_basis"] == {"isbn": 0, "title_authors": 0}
        assert (report["restored"], report["imported"], report["drifted"]) == (1, 0, 0)
        assert _live(db, twin)

    @MODES
    def test_a_trashed_title_author_twin_is_restored_not_duplicated(
        self, db, tmp_path, mode
    ):
        """Corrections 3. The insert funnel restores only on ISBN/UPC, so
        before `_trashed_twin_lookup` this record planned `create` and landed
        as a second live row beside its trashed twin. Verified red on the
        pre-T4 classifier: the plan said `create` and two rows existed."""
        twin = _insert_item(db, title="Restorable", isbn=None,
                            authors="A. Author")
        trash_item(db, twin)
        db.execute("COMMIT")

        plan, report = _plan_apply(db, _archive(tmp_path, {"items": [_rec()]}), mode)

        assert plan["items"][0]["verdict"] == "restore"
        assert plan["items"][0]["basis"] == "title_authors"
        assert report["restored"] == 1
        assert db.execute(
            "SELECT COUNT(*) AS n FROM items WHERE title = 'Restorable'"
        ).fetchone()["n"] == 1
        assert _live(db, twin)

    @MODES
    def test_a_trashed_upc_twin_is_restored(self, db, tmp_path, mode):
        twin = _insert_item(db, title="Some Film", isbn=None, upc=UPC)
        trash_item(db, twin)
        db.execute("COMMIT")

        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(upc=UPC)]}), mode)

        assert plan["items"][0]["verdict"] == "restore"
        assert plan["items"][0]["basis"] == "upc"
        assert report["restored"] == 1 and report["drifted"] == 0
        assert _live(db, twin)
        # The row keeps its own fields: a restore is not a metadata update.
        assert db.execute(
            "SELECT title FROM items WHERE id = ?", (twin,)
        ).fetchone()["title"] == "Some Film"

    @MODES
    def test_a_live_twin_wins_and_nothing_is_trashed(self, db, tmp_path, mode):
        live = _insert_item(db, title="Restorable", isbn=ISBN13)
        db.execute("COMMIT")

        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}), mode)

        assert plan["items"][0]["verdict"] == mode
        assert report["restored"] == 0
        assert _live(db, live)
        assert db.execute(
            "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NOT NULL"
        ).fetchone()["n"] == 0


class TestDeletedRecordsClassify:
    """The deleted-incoming half of the rule table, as the plan states it.
    What apply then does with a deleted create is T5's."""

    @MODES
    def test_deleted_with_a_trashed_twin_is_left_alone(self, db, tmp_path, mode):
        twin = _insert_item(db, title="Restorable", isbn=ISBN13)
        trash_item(db, twin, at="2026-01-02 03:04:05")
        db.execute("COMMIT")

        plan, report = _plan_apply(db, _archive(tmp_path, {"items": [
            _rec(isbn=ISBN13, deleted_at="2026-05-05 05:05:05")]}), mode)

        assert plan["items"][0]["verdict"] == "skip"
        assert plan["items"][0]["deleted"] is True
        assert report["skipped"] == 1 and report["restored"] == 0
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (twin,)
        ).fetchone()["deleted_at"] == "2026-01-02 03:04:05"

    @MODES
    def test_deleted_with_a_live_twin_never_trashes_it(self, db, tmp_path, mode):
        live = _insert_item(db, title="Restorable", isbn=ISBN13)
        db.execute("COMMIT")

        plan, _report = _plan_apply(db, _archive(tmp_path, {"items": [
            _rec(isbn=ISBN13, deleted_at="2026-05-05 05:05:05")]}), mode)

        assert plan["items"][0]["verdict"] == mode
        assert _live(db, live)

    def test_deleted_with_no_twin_plans_a_create_into_trash(self, db, tmp_path):
        plan, _report = _plan_apply(db, _archive(tmp_path, {"items": [
            _rec(isbn=ISBN13, deleted_at="2026-05-05 05:05:05"),
            {**_rec(isbn=None, title="Other"), "id": 2},
        ]}))
        s = plan["summary"]
        assert s["create"] == 2 and s["create_in_trash"] == 1
        assert [r["deleted"] for r in plan["items"]] == [True, False]

    def test_a_v1_record_without_the_key_is_live(self, db, tmp_path):
        twin = _insert_item(db, title="Restorable", isbn=ISBN13)
        trash_item(db, twin)
        db.execute("COMMIT")
        plan, _report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}))
        assert plan["items"][0]["deleted"] is False
        assert plan["items"][0]["verdict"] == "restore"


class TestWhatARestoreCarries:
    def _trashed(self, db, **kw):
        twin = _insert_item(db, title="Restorable", isbn=ISBN13, **kw)
        return twin

    def test_history_is_not_attached_and_copies_are_kept(self, db, tmp_path):
        """G27: the archive is not an undo, and a restored row kept its own
        copies, reading log and loans through the trash."""
        twin = self._trashed(db)
        item_copies.add_copy(db, twin)
        item_copies.add_copy(db, twin)
        before = _copy_numbers(db, twin)
        trash_item(db, twin)
        db.execute("COMMIT")

        _plan, report = _plan_apply(db, _archive(tmp_path, {
            "items": [_rec(isbn=ISBN13, copies=[])],
            "reading_log": [{"item_id": 1, "status": "read"}],
            "checkouts": [{"item_id": 1, "borrower": "Bea"}],
        }))

        assert report["restored"] == 1
        assert len(before) == 2 and _copy_numbers(db, twin) == before
        for table in ("reading_log", "checkouts"):
            assert db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE item_id = ?", (twin,)
            ).fetchone()["n"] == 0

    def test_tags_attach_and_ownership_moves_toward_owned(self, db, tmp_path):
        twin = self._trashed(db, owned=0, wishlisted=True)
        trash_item(db, twin)
        db.execute("COMMIT")

        _plan_apply(db, _archive(tmp_path, {"items": [
            _rec(isbn=ISBN13, owned=1, wishlisted=False, tags=["Signed"])]}))

        assert db.execute(
            "SELECT owned FROM items WHERE id = ?", (twin,)).fetchone()["owned"] == 1
        assert [r["name"] for r in db.execute(
            "SELECT tags.name FROM item_tags JOIN tags ON tags.id = item_tags.tag_id "
            "WHERE item_tags.item_id = ?", (twin,))] == ["Signed"]

    def test_an_invalid_owned_value_leaves_the_twin_in_trash(self, db, tmp_path):
        """G85: the ownership intent is checked before `restore_item`."""
        twin = self._trashed(db)
        trash_item(db, twin)
        db.execute("COMMIT")

        _plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13, owned="maybe")]}))

        assert len(report["errors"]) == 1
        assert report["restored"] == 0
        assert not _live(db, twin)

    def test_include_creates_off_deselects_a_restore(self, db, tmp_path):
        twin = self._trashed(db)
        trash_item(db, twin)
        db.execute("COMMIT")

        _plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}),
            selection={"include_creates": False})

        assert report["deselected"]["creates"] == 1
        assert report["restored"] == 0
        assert not _live(db, twin)


class TestRestoreDrift:
    def test_twin_trashed_between_plan_and_apply_is_drift(self, db, tmp_path):
        """Planned `skip` against a live row; the row went to Trash before
        apply, which now says `restore`. Nothing the user did not see."""
        live = _insert_item(db, title="Restorable", isbn=ISBN13)
        db.execute("COMMIT")

        def trash_it(db):
            trash_item(db, live)
            db.execute("COMMIT")

        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}), between=trash_it)

        assert plan["items"][0]["verdict"] == "skip"
        assert report["drifted"] == 1
        assert report["restored"] == 0 and report["skipped"] == 0
        assert not _live(db, live)

    def test_twin_restored_by_hand_between_plan_and_apply_is_drift(self, db, tmp_path):
        from app.services.item_write import restore_item

        twin = _insert_item(db, title="Restorable", isbn=ISBN13)
        trash_item(db, twin)
        db.execute("COMMIT")

        def restore_it(db):
            restore_item(db, twin)
            db.execute("COMMIT")

        plan, report = _plan_apply(
            db, _archive(tmp_path, {"items": [_rec(isbn=ISBN13)]}), between=restore_it)

        assert plan["items"][0]["verdict"] == "restore"
        assert report["drifted"] == 1 and report["restored"] == 0
        assert _live(db, twin)
