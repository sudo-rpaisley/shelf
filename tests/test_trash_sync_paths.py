"""A machine re-syncing leaves a trashed twin alone.

T10 of the soft-delete collision plan. Four adapters, one decision: the
external-id matcher reads the physical relation and sees `deleted_at`; a
trashed match is skipped **before any write** and counted as `in_trash`; and
the insert passes `restore_trashed=False`, so a trashed ISBN twin is refused
by the funnel and counted the same way. Never a 500, never a rolled-back
block.

Every row is trashed through `item_write.trash_item` — no route writes
`deleted_at` in this release — and every assertion that says "nothing was
written" reads the database, not the stats (G85).
"""

import pytest

from app.database import get_db
from app.services import komga_records, komga_sync, romm_records, romm_sync
from app.services.item_write import insert_item, trash_item

ISBN = "9780441013593"


def _items(db):
    return db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


# --------------------------------------------------------------- Komga ----

def _komga_candidate(komga_id="book-1", *, title="Example Comic", isbn=None):
    return {
        "komga_id": komga_id, "komga_library_id": "comics",
        "komga_series_id": "series-1", "library_kind": "comic",
        "title": title, "authors": "Example Author", "isbn": isbn,
        "series_name": None, "series_position": None, "publish_year": 2024,
        "description": None, "page_count": 24,
    }


class TestKomgaLeavesATrashedRecordAlone:
    def test_a_trashed_record_does_not_trip_the_pk_or_roll_back(self, db):
        """Halt 7. Through the old view-joined matcher the trashed item's
        record was invisible, so the item was inserted again and the record
        INSERT tripped `komga_id`'s PRIMARY KEY — rolling the whole block
        back. Now the record is seen, and skipped."""
        first = komga_records.persist_candidate(db, _komga_candidate())
        trash_item(db, first["item_id"])

        result = komga_records.persist_candidate(db, _komga_candidate())

        assert result == {"item_id": first["item_id"], "action": "in_trash"}
        assert _items(db) == 1
        assert db.execute(
            "SELECT COUNT(*) AS n FROM komga_records"
        ).fetchone()["n"] == 1
        # Still trashed — a machine does not resurrect it.
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (first["item_id"],)
        ).fetchone()["deleted_at"] is not None

    def test_the_return_carries_item_id_so_the_loop_does_not_keyerror(self, db):
        """claude-R3. As the plan found it, both sync loops read
        result["item_id"] AFTER counting the action, so a bare
        {"action": "in_trash"} would land the count and then KeyError into
        `except Exception` — errors += 1, per trashed item, per sync.

        **This is the pin that holds the return shape, and it is the only
        one.** T10's own G85 guard later removed the loop's read: an
        in_trash result now skips `_ingest_cover`, which was the call that
        read item_id. So the loop no longer needs the key, and a mutation
        dropping it leaves the loop test green — correctly. The key is kept
        because a caller naming the blocking row is the useful shape, and it
        is pinned here, where it is actually observable."""
        first = komga_records.persist_candidate(db, _komga_candidate())
        trash_item(db, first["item_id"])

        result = komga_records.persist_candidate(db, _komga_candidate())

        assert "item_id" in result

    def test_a_trashed_isbn_twin_is_refused_not_restored(self, db):
        blocker = insert_item(db, title="Owned Comic", source="manual",
                              isbn=ISBN, media_type="comic")
        trash_item(db, blocker)

        result = komga_records.persist_candidate(
            db, _komga_candidate("book-2", isbn=ISBN))

        assert result["action"] == "in_trash"
        assert result["item_id"] == blocker
        assert db.execute(
            "SELECT COUNT(*) AS n FROM komga_records"
        ).fetchone()["n"] == 0


class TestKomgaUpdatesOntoATrashedSlotAreRefusedCleanly:
    """Design §3 names both update-funnel callers in `komga_records`. Each
    must turn `IdentifierInTrash` into the module's own
    `KomgaPersistenceError` — the loop's per-candidate contract — rather
    than let a bare `ItemValueError` escape (`claude-M3`, M27/M28)."""

    def test_a_reclassify_onto_a_trashed_slot_is_a_persistence_error(self, db):
        from app.services import item_write

        blocker = insert_item(db, title="Trashed Manga", source="manual",
                              isbn=ISBN, media_type="manga")
        trash_item(db, blocker)
        first = komga_records.persist_candidate(
            db, _komga_candidate(isbn=ISBN))

        with pytest.raises(komga_records.KomgaPersistenceError) as exc:
            komga_records.persist_candidate(
                db, {**_komga_candidate(isbn=ISBN), "library_kind": "manga"})

        assert not isinstance(exc.value, item_write.IdentifierInTrash)
        assert db.execute(
            "SELECT media_type FROM items WHERE id = ?", (first["item_id"],)
        ).fetchone()["media_type"] == "comic"

    def test_a_refresh_onto_a_trashed_isbn_is_a_persistence_error(self, db):
        from app.services import item_write

        first = komga_records.persist_candidate(db, _komga_candidate())
        blocker = insert_item(db, title="Trashed Comic", source="manual",
                              isbn=ISBN, media_type="comic")
        trash_item(db, blocker)

        with pytest.raises(komga_records.KomgaPersistenceError) as exc:
            komga_records.persist_candidate(db, _komga_candidate(isbn=ISBN))

        assert not isinstance(exc.value, item_write.IdentifierInTrash)
        assert db.execute(
            "SELECT isbn FROM items WHERE id = ?", (first["item_id"],)
        ).fetchone()["isbn"] is None


class TestResolveMissingCoverSkipsATrashedIsbn:
    """Design §3: `resolve_missing_cover` treats a trashed ISBN holder as
    "taken" and carries on with the cover. Uncaught, the cover worker
    raises and the item gets no cover (`claude-M3`, M06)."""

    def test_the_isbn_is_not_stored_and_the_cover_still_is(self, db):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from app.routers import items_common

        blocker = insert_item(db, title="Holder", source="manual", isbn=ISBN)
        trash_item(db, blocker)
        live = insert_item(db, title="No ISBN yet", source="manual",
                           authors="A")
        db.commit()

        with patch.object(items_common, "_search_isbn_for_item",
                          new=AsyncMock(return_value=(ISBN, "http://x/c.jpg"))), \
             patch.object(items_common.covers, "download_cover",
                          new=AsyncMock(return_value=f"covers/{live}.jpg")):
            out = asyncio.run(items_common.resolve_missing_cover(live, None))

        assert out == f"covers/{live}.jpg"
        row = db.execute("SELECT isbn, cover_path FROM items WHERE id = ?",
                         (live,)).fetchone()
        assert row["isbn"] is None
        assert row["cover_path"] == f"covers/{live}.jpg"


@pytest.mark.asyncio
class TestKomgaSyncLoop:
    async def test_a_trashed_match_completes_with_no_errors_and_no_cover(
        self, db, monkeypatch
    ):
        """A trashed match completes the loop cleanly: no error, no cover,
        and the sibling in the same batch still commits.

        NOT a pin on the item_id in the return shape, though it was written
        as one. Mutation-checked (G108): with the key dropped this stays
        green, because the in_trash arm no longer reads it — see the persist
        -level test above, which is where that shape is held."""
        komga_sync.save_configuration(url="https://komga.example", api_key="k")
        komga_sync.save_library_selection(
            [{"id": "comics", "included": True, "kind": "comic"}])

        with get_db() as seed:
            first = komga_records.persist_candidate(seed, _komga_candidate())
            trash_item(seed, first["item_id"])

        async def fake_libraries(client, url, key, configured=None):
            return [{"id": "comics", "name": "Comics", "kind": "comic",
                     "explicit_kind": True}]

        async def fake_candidates(client, url, key, *, library_id, kind):
            return [_komga_candidate(), _komga_candidate("book-9", title="Sibling")]

        covered = []

        async def spy_cover(client, server, key, item_id, komga_id):
            covered.append(item_id)
            return False

        monkeypatch.setattr(komga_sync.komga_libraries, "fetch_libraries", fake_libraries)
        monkeypatch.setattr(komga_sync.komga_books, "fetch_library_candidates",
                            fake_candidates)
        monkeypatch.setattr(komga_sync, "_ingest_cover", spy_cover)

        stats = await komga_sync.sync()

        assert stats["errors"] == 0
        assert stats["in_trash"] == 1
        assert stats["created"] == 1          # the sibling still committed
        assert first["item_id"] not in covered


# ---------------------------------------------------------------- RomM ----

def _romm_candidate(romm_id="rom-1", title="Example Game"):
    return {"romm_id": romm_id, "romm_platform_id": 1, "platform": "snes",
            "title": title, "publish_year": 1992, "description": None}


class TestRomMLeavesATrashedRecordAlone:
    def test_a_trashed_record_does_not_trip_the_pk_or_roll_back(self, db):
        """RomM had no IntegrityError handler at all, so this rollback was
        unconditional. RomM rows carry no ISBN or UPC, so the funnel could
        never refuse one — the matcher skip is the whole fix."""
        first = romm_records.persist_candidate(db, _romm_candidate())
        trash_item(db, first["item_id"])

        result = romm_records.persist_candidate(db, _romm_candidate())

        assert result == {"item_id": first["item_id"], "action": "in_trash"}
        assert _items(db) == 1
        assert db.execute(
            "SELECT COUNT(*) AS n FROM romm_records"
        ).fetchone()["n"] == 1


@pytest.mark.asyncio
class TestRomMSyncLoop:
    async def test_a_trashed_match_completes_with_no_errors_and_no_cover(
        self, db, monkeypatch
    ):
        romm_sync.save_configuration(url="https://romm.example", token="t")

        with get_db() as seed:
            first = romm_records.persist_candidate(seed, _romm_candidate())
            trash_item(seed, first["item_id"])

        async def fake_platforms(client, server, token):
            return [{"id": 1, "name": "SNES", "slug": "snes"}]

        async def fake_candidates(client, server, token, platform):
            for c in (_romm_candidate(), _romm_candidate("rom-9", "Sibling")):
                yield c

        covered = []

        async def spy_cover(client, server, token, item_id, cover_url):
            covered.append(item_id)
            return False

        monkeypatch.setattr(romm_sync.romm_client, "fetch_platforms", fake_platforms)
        monkeypatch.setattr(romm_sync.romm_client, "iter_rom_candidates",
                            fake_candidates)
        monkeypatch.setattr(romm_sync, "_ingest_cover", spy_cover)

        stats = await romm_sync.sync()

        assert stats["errors"] == 0
        assert stats["in_trash"] == 1
        assert stats["created"] == 1
        assert first["item_id"] not in covered


# --------------------------------------------------------- Audiobookshelf ----

import asyncio

import httpx
import respx

from app.services import audiobookshelf as abs_svc

ABS = "http://abs.example:13378"


def _mock_abs(*items):
    respx.get(f"{ABS}/api/libraries").mock(return_value=httpx.Response(
        200, json={"libraries": [
            {"id": "lib_audio", "name": "Audiobooks", "mediaType": "book"}]}))
    respx.get(f"{ABS}/api/libraries/lib_audio/items").mock(
        return_value=httpx.Response(200, json={"results": [
            {"id": abs_id,
             "media": {"metadata": {"title": title, "isbn": isbn},
                       "numAudioFiles": 1, "duration": 3600}}
            for abs_id, title, isbn in items
        ]}))
    for abs_id, _t, _i in items:
        respx.get(f"{ABS}/api/items/{abs_id}/cover").mock(
            return_value=httpx.Response(404))


class TestAudiobookshelfLeavesATrashedTwinAlone:
    @respx.mock
    def test_an_isbn_less_trashed_item_is_not_duplicated(self, db):
        """The design's named contract. With no ISBN, only `abs_id` can find
        the row — and through the view a trashed one was invisible, so the
        next sync inserted a second copy of something the user deleted."""
        item_id = insert_item(db, title="No ISBN Book", source="audiobookshelf",
                              media_type="audiobook", abs_id="li_1",
                              abs_library_id="lib_audio")
        trash_item(db, item_id)
        db.execute("COMMIT")

        _mock_abs(("li_1", "No ISBN Book", None))
        stats = asyncio.run(abs_svc.sync(ABS, "token"))

        assert stats["in_trash"] == 1
        assert stats["errors"] == 0
        assert _items(db) == 1
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] is not None

    @respx.mock
    def test_a_live_row_wins_over_a_trashed_row_sharing_its_abs_id(self, db):
        """The coexistence pin (claude-R4 / codex-R1). `abs_id` is a plain
        index, so two rows can share one. Without live-first ordering the
        trashed row wins the bare read, the live row is never refreshed, and
        the sync reports it as in_trash."""
        trashed = insert_item(db, title="Old Copy", source="audiobookshelf",
                              media_type="audiobook", abs_id="li_1",
                              abs_library_id="lib_audio")
        trash_item(db, trashed)
        live = insert_item(db, title="Live Copy", source="audiobookshelf",
                           media_type="audiobook", abs_id="li_1",
                           abs_library_id="lib_audio")
        db.execute("COMMIT")

        _mock_abs(("li_1", "Live Copy Renamed", None))
        stats = asyncio.run(abs_svc.sync(ABS, "token"))

        assert stats.get("in_trash", 0) == 0
        assert db.execute(
            "SELECT title FROM items WHERE id = ?", (live,)
        ).fetchone()["title"] == "Live Copy Renamed"
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (trashed,)
        ).fetchone()["deleted_at"] is not None

    @respx.mock
    def test_a_trashed_isbn_twin_is_counted_in_trash(self, db):
        blocker = insert_item(db, title="Manual Audiobook", source="manual",
                              isbn=ISBN, media_type="audiobook")
        trash_item(db, blocker)
        db.execute("COMMIT")

        _mock_abs(("li_7", "Synced", ISBN))
        stats = asyncio.run(abs_svc.sync(ABS, "token"))

        assert stats["in_trash"] == 1
        assert stats["errors"] == 0, (
            "IdentifierInTrash is an ItemValueError — the refusal arm must "
            "sit ahead of the ItemValueError arm, which counts an error"
        )
        assert _items(db) == 1

    @respx.mock
    def test_an_update_blocked_by_a_trashed_isbn_is_counted_in_trash(self, db):
        """`claude-m3`. The update arm hits the same refusal through the
        update funnel; counted as an error, it would recur on every sync
        until the user empties Trash."""
        blocker = insert_item(db, title="Manual Audiobook", source="manual",
                              isbn=ISBN, media_type="audiobook")
        trash_item(db, blocker)
        live = insert_item(db, title="Synced", source="audiobookshelf",
                           media_type="audiobook", abs_id="li_7",
                           abs_library_id="lib_audio")
        db.execute("COMMIT")

        _mock_abs(("li_7", "Synced", ISBN))
        stats = asyncio.run(abs_svc.sync(ABS, "token"))

        assert stats["in_trash"] == 1
        assert stats["errors"] == 0
        assert db.execute(
            "SELECT isbn FROM items WHERE id = ?", (live,)
        ).fetchone()["isbn"] is None

    @respx.mock
    def test_the_stats_shape_is_unchanged_when_nothing_is_in_trash(self, db):
        """`in_trash` is counted lazily, so every sync until the delete sites
        are flipped returns exactly the shape it always has."""
        _mock_abs(("li_1", "Fresh", None))
        stats = asyncio.run(abs_svc.sync(ABS, "token"))
        assert "in_trash" not in stats


# ------------------------------------------------------- Hardcover import ----

class TestHardcoverImportLeavesATrashedTwinAlone:
    def _import(self, book, title_index=None):
        from app.routers import hardcover as hc_router

        return hc_router._import_single_book_metadata(
            book, False, title_index or {})

    def test_a_trashed_hardcover_id_is_counted_in_trash(self, db):
        item_id = insert_item(db, title="HC Book", source="hardcover",
                              hardcover_book_id=4242)
        trash_item(db, item_id)
        db.commit()

        status, cover_job = self._import(
            {"title": "HC Book", "hardcover_book_id": 4242})

        assert status == "in_trash"
        assert cover_job is None
        assert _items(db) == 1

    def test_a_live_row_wins_over_a_trashed_row_sharing_its_hardcover_id(self, db):
        """The coexistence pin. `hardcover_book_id` is a plain index; every
        live strategy runs first and a trashed row counts only when all of
        them miss."""
        trashed = insert_item(db, title="Old", source="hardcover",
                              hardcover_book_id=4242)
        trash_item(db, trashed)
        live = insert_item(db, title="Live", source="hardcover",
                           hardcover_book_id=4242)
        db.commit()

        status, _ = self._import(
            {"title": "Live", "hardcover_book_id": 4242,
             "description": "filled in"})

        assert status != "in_trash"
        assert db.execute(
            "SELECT description FROM items WHERE id = ?", (live,)
        ).fetchone()["description"] == "filled in"
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (trashed,)
        ).fetchone()["deleted_at"] is not None

    def test_a_trashed_isbn_twin_is_counted_in_trash(self, db):
        blocker = insert_item(db, title="Owned", source="manual",
                              isbn=ISBN, media_type="book")
        trash_item(db, blocker)
        db.commit()

        status, _ = self._import({"title": "Owned", "isbn": ISBN})

        assert status == "in_trash"
        assert _items(db) == 1
