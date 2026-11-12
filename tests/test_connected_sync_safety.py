"""Connected services are observations, not permission to replace local edits."""

import asyncio
import json

import httpx
import pytest
import respx

from app import config
from app.database import get_db
from app.services import audiobookshelf, komga_records, metadata_policy, romm_records
from app.services.item_write import insert_item, trash_item, update_item_fields
from tests.conftest import _insert_item, _insert_location


ABS = "https://abs.example"
ISBN = "9780441172719"


def _candidate(provider, **overrides):
    fields = {
        "title": "Remote title",
        "publish_year": 2000,
        "description": "Remote description",
    }
    if provider == "komga":
        fields.update({
            "komga_id": "one", "komga_library_id": "library",
            "komga_series_id": "series", "library_kind": "comic",
            "authors": "Remote Author", "series_name": "Remote series",
            "series_position": 1.0, "page_count": 192, "isbn": ISBN,
        })
    else:
        fields.update({
            "romm_id": "one", "romm_platform_id": "snes-id",
            "platform": "snes", "publisher": "Remote publisher",
        })
    return {**fields, **overrides}


def _persist(db, provider, **overrides):
    service = komga_records if provider == "komga" else romm_records
    return service.persist_candidate(db, _candidate(provider, **overrides))


def _item(db, item_id):
    return dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())


def _snapshot(db, item_id, key, field):
    row = db.execute(
        "SELECT value_json FROM item_metadata_provenance "
        "WHERE item_id = ? AND source_key = ? AND field_name = ?",
        (item_id, key, field),
    ).fetchone()
    assert row is not None
    return json.loads(row["value_json"])


@pytest.mark.parametrize("provider", ["komga", "romm"])
def test_provider_import_and_unedited_refresh_record_applied_metadata(db, provider):
    item_id = _persist(db, provider)["item_id"]
    assert _snapshot(db, item_id, f"{provider}:one", "title") == {
        "observed": "Remote title", "applied": "Remote title",
        "local": "Remote title", "overridden": False,
    }

    result = _persist(db, provider, title="Corrected title", publish_year=2001)

    assert result["item_id"] == item_id
    assert result["action"] == "updated"
    assert _item(db, item_id)["title"] == "Corrected title"
    assert _item(db, item_id)["publish_year"] == 2001
    assert _snapshot(db, item_id, f"{provider}:one", "title") == {
        "observed": "Corrected title", "applied": "Corrected title",
        "local": "Corrected title", "overridden": False,
    }


@pytest.mark.parametrize("provider", ["komga", "romm"])
def test_human_edits_on_provider_created_items_survive_repeated_resync(db, provider):
    item_id = _persist(db, provider)["item_id"]
    location_id = _insert_location(db, "Local shelf")
    local = {
        "title": "My title", "authors": "My Author", "publisher": "My publisher",
        "series_name": "My series", "series_position": 7.0, "publish_year": 1980,
        "description": "My description", "cover_path": "covers/my-cover.jpg",
        "notes": "Private notes", "location_id": location_id,
        "owned": 0, "reading_status": "read", "page_count": 250,
        "media_type": "ebook",
    }
    if provider == "romm":
        local["platform"] = "nes"
    update_item_fields(db, item_id, local)
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('Personal')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))

    for title in ("New remote title", "Yet another title"):
        _persist(
            db, provider, title=title, authors="Other Author",
            series_name="Other series", series_position=2.0, publish_year=2024,
            description="Other description", publisher="Other publisher", page_count=100,
        )
        row = _item(db, item_id)
        assert {name: row[name] for name in local} == local

    assert db.execute(
        "SELECT tag_id FROM item_tags WHERE item_id = ?", (item_id,),
    ).fetchone()["tag_id"] == tag_id
    assert _snapshot(db, item_id, f"{provider}:one", "title") == {
        "observed": "Yet another title", "local": "My title", "overridden": True,
    }


@pytest.mark.parametrize("provider", ["komga", "romm"])
def test_legacy_baseline_is_unknown_even_if_first_observation_matches(db, provider):
    item_id = insert_item(
        db, title="Remote title", authors="Local Author", source=provider,
        media_type="comic" if provider == "komga" else "video_game",
        platform="snes" if provider == "romm" else None,
        publish_year=1980, description="Local description",
    )
    if provider == "komga":
        db.execute(
            "INSERT INTO komga_records (komga_id, item_id, library_id, kind) "
            "VALUES ('one', ?, 'library', 'comic')", (item_id,),
        )
    else:
        db.execute(
            "INSERT INTO romm_records (romm_id, item_id, platform_id) "
            "VALUES ('one', ?, 'snes-id')", (item_id,),
        )

    _persist(db, provider)
    assert _snapshot(db, item_id, f"{provider}:one", "title") == {
        "observed": "Remote title", "local": "Remote title", "overridden": False,
    }
    _persist(db, provider, title="Remote correction", authors="Changed Author")

    row = _item(db, item_id)
    assert row["title"] == "Remote title"
    assert row["authors"] == "Local Author"
    assert row["publish_year"] == 1980
    assert row["description"] == "Local description"


@pytest.mark.parametrize("provider", ["komga", "romm"])
def test_missing_metadata_is_filled_and_then_refreshable_without_claiming_local_fields(db, provider):
    item_id = _persist(db, provider, description=None)["item_id"]
    update_item_fields(db, item_id, {"authors": "Explicit Local Author"})
    _persist(db, provider, authors="Other Author", description="Filled description")
    _persist(db, provider, authors="Third Author", description="Refreshed description")

    row = _item(db, item_id)
    assert row["authors"] == "Explicit Local Author"
    assert row["description"] == "Refreshed description"
    assert _snapshot(db, item_id, f"{provider}:one", "description") == {
        "observed": "Refreshed description", "applied": "Refreshed description",
        "local": "Refreshed description", "overridden": False,
    }


@pytest.mark.parametrize("provider,field", [
    ("komga", "description"), ("komga", "series_name"), ("romm", "description"),
])
def test_provider_metadata_clears_survive_two_refreshes_without_touching_the_item(db, provider, field):
    item_id = _persist(db, provider)["item_id"]
    update_item_fields(db, item_id, {field: ""})
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))

    for incoming in ("Remote correction", "Another correction"):
        result = _persist(db, provider, **{field: incoming})
        assert result["item_id"] == item_id
        assert result["action"] == "updated"  # Holding refresh, as before.
        assert _item(db, item_id)[field] == ""
        assert _item(db, item_id)["updated_at"] == "2000-01-01"
        assert _snapshot(db, item_id, f"{provider}:one", field) == {
            "observed": incoming, "local": "", "overridden": True,
        }


def test_komga_adoption_never_claims_matching_manual_metadata(db):
    item_id = insert_item(
        db, title="Remote title", isbn=ISBN, media_type="comic",
        authors="Remote Author", series_name="My series", source="manual",
    )
    assert _persist(db, "komga")["action"] == "adopted"
    assert _snapshot(db, item_id, "komga:one", "title") == {
        "observed": "Remote title", "local": "Remote title", "overridden": False,
    }
    assert _snapshot(db, item_id, "komga:one", "authors") == {
        "observed": "Remote Author", "local": "Remote Author", "overridden": False,
    }

    _persist(db, "komga", title="Changed title", authors="Other Author",
             series_name="Other series", description="Changed description")

    row = _item(db, item_id)
    assert row["source"] == "manual"
    assert row["title"] == "Remote title"
    assert row["authors"] == "Remote Author"
    assert row["series_name"] == "My series"
    # Adopted manual rows retain Komga's stricter fill-only contract, even
    # for a description that Komga filled on the first observation.
    assert row["description"] == "Remote description"


@pytest.mark.parametrize("initially_missing", [False, True])
def test_komga_adopted_fields_stay_cleared_whether_initially_manual_or_filled(db, initially_missing):
    item_id = insert_item(
        db, title="Manual title", isbn=ISBN, media_type="comic", source="manual",
        description=None if initially_missing else "Manual description",
        series_name=None if initially_missing else "Manual series", authors="Manual Author",
    )
    assert _persist(db, "komga")["action"] == "adopted"
    for field, manual, filled in (
        ("description", "Manual description", "Remote description"),
        ("series_name", "Manual series", "Remote series"),
    ):
        snapshot = _snapshot(db, item_id, "komga:one", field)
        assert snapshot["local"] == (filled if initially_missing else manual)
        assert ("applied" in snapshot) is initially_missing

    cleared = {"description": None, "series_name": "", "authors": ""}
    update_item_fields(db, item_id, cleared)
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))
    for incoming in ("Remote correction", "Another correction"):
        result = _persist(db, "komga", description=incoming, series_name=incoming, authors=incoming)
        assert result == {"item_id": item_id, "action": "updated", "adopted": True}
        row = _item(db, item_id)
        assert {name: row[name] for name in cleared} == cleared
        assert row["source"] == "manual"
        assert row["updated_at"] == "2000-01-01"
        for field, local in cleared.items():
            assert _snapshot(db, item_id, "komga:one", field) == {
                "observed": incoming, "local": local, "overridden": True,
            }


@pytest.mark.parametrize("trashed_blocker", [False, True])
def test_komga_does_not_reclaim_a_cleared_isbn_or_force_an_identifier_collision(db, trashed_blocker):
    item_id = _persist(db, "komga")["item_id"]
    update_item_fields(db, item_id, {"isbn": None})
    blocker = insert_item(db, title="Other edition", isbn=ISBN, media_type="comic")
    if trashed_blocker:
        trash_item(db, blocker)
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))

    for _ in range(2):
        assert _persist(db, "komga")["action"] == "updated"
        row = _item(db, item_id)
        assert row["isbn"] is None and row["isbn10"] is None
        assert row["updated_at"] == "2000-01-01"
        assert _snapshot(db, item_id, "komga:one", "isbn") == {
            "observed": ISBN, "local": None, "overridden": True,
        }
    blocker_row = db.execute("SELECT isbn, deleted_at FROM items WHERE id = ?", (blocker,)).fetchone()
    assert blocker_row["isbn"] == ISBN
    assert (blocker_row["deleted_at"] is not None) is trashed_blocker


def test_two_komga_holdings_do_not_share_metadata_ownership(db):
    item_id = _persist(db, "komga")["item_id"]
    second = _persist(db, "komga", komga_id="two", title="Second holding title")
    assert second["action"] == "adopted"
    assert second["item_id"] == item_id
    assert _item(db, item_id)["title"] == "Remote title"

    _persist(db, "komga", title="First holding correction")
    _persist(db, "komga", komga_id="two", title="Second holding correction")

    assert _item(db, item_id)["title"] == "First holding correction"
    assert _snapshot(db, item_id, "komga:one", "title") == {
        "observed": "First holding correction", "applied": "First holding correction",
        "local": "First holding correction", "overridden": False,
    }
    assert _snapshot(db, item_id, "komga:two", "title") == {
        "observed": "Second holding correction", "local": "First holding correction",
        "overridden": True,
    }


def _mock_abs(*, cover_status=404, **overrides):
    metadata = {
        "title": "Remote title", "authorName": "Remote Author", "isbn": ISBN,
        "seriesName": "Remote series", "narratorName": "Remote Narrator",
        "publisher": "Remote publisher", "publishedYear": "2000",
        "description": "Remote description",
        **overrides,
    }
    respx.get(f"{ABS}/api/libraries").respond(200, json={"libraries": [
        {"id": "audio", "name": "Audiobooks", "mediaType": "book"},
    ]})
    respx.get(f"{ABS}/api/libraries/audio/items").respond(200, json={"results": [
        {"id": "one", "media": {"metadata": metadata, "numAudioFiles": 1, "duration": 3600}},
    ]})
    return respx.get(f"{ABS}/api/items/one/cover").respond(cover_status)


def _abs_sync():
    return asyncio.run(audiobookshelf.sync(ABS, "test-token"))


@respx.mock
def test_abs_import_and_unedited_refresh_record_applied_metadata(db):
    _mock_abs()
    assert _abs_sync()["added"] == 1
    item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]
    assert _snapshot(db, item_id, "audiobookshelf:one", "title") == {
        "observed": "Remote title", "applied": "Remote title",
        "local": "Remote title", "overridden": False,
    }

    _mock_abs(title="Corrected title", seriesName="Corrected series", publishedYear="2001")
    assert _abs_sync()["updated"] == 1
    row = _item(db, item_id)
    assert row["title"] == "Corrected title"
    assert row["series_name"] == "Corrected series"
    assert row["publish_year"] == 2001

    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))
    db.commit()
    assert _abs_sync()["unchanged"] == 1
    assert _item(db, item_id)["updated_at"] == "2000-01-01"


@pytest.mark.parametrize("field,remote_field", [
    ("description", "description"), ("series_name", "seriesName"),
])
@pytest.mark.parametrize("cleared", [None, ""])
@respx.mock
def test_abs_metadata_clears_survive_two_remote_updates_with_unchanged_counters(db, field, remote_field, cleared):
    _mock_abs()
    assert _abs_sync()["added"] == 1
    item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]
    update_item_fields(db, item_id, {field: cleared, "cover_path": "covers/selected.jpg"})
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))
    db.commit()

    for incoming in ("Remote correction", "Another correction"):
        _mock_abs(**{remote_field: incoming})
        assert _abs_sync() == {"added": 0, "updated": 0, "unchanged": 1, "skipped": 0, "errors": 0}
        assert _item(db, item_id)[field] == cleared
        assert _item(db, item_id)["updated_at"] == "2000-01-01"
        assert _snapshot(db, item_id, "audiobookshelf:one", field) == {
            "observed": incoming, "local": cleared, "overridden": True,
        }


@pytest.mark.parametrize("initially_missing", [False, True])
@respx.mock
def test_abs_adopted_fields_stay_cleared_whether_initially_manual_or_filled(db, initially_missing):
    item_id = insert_item(
        db, title="Manual title", isbn=ISBN, media_type="audiobook", source="manual",
        description=None if initially_missing else "Manual description",
        series_name=None if initially_missing else "Manual series", authors="Manual Author",
    )
    db.commit()
    _mock_abs()
    assert _abs_sync()["updated"] == 1
    for field, manual, filled in (
        ("description", "Manual description", "Remote description"),
        ("series_name", "Manual series", "Remote series"),
    ):
        snapshot = _snapshot(db, item_id, "audiobookshelf:one", field)
        assert snapshot["local"] == (filled if initially_missing else manual)
        assert ("applied" in snapshot) is initially_missing

    cleared = {"description": None, "series_name": "", "authors": ""}
    update_item_fields(db, item_id, {**cleared, "cover_path": "covers/selected.jpg"})
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))
    db.commit()
    for incoming in ("Remote correction", "Another correction"):
        _mock_abs(description=incoming, seriesName=incoming, authorName=incoming)
        assert _abs_sync() == {"added": 0, "updated": 0, "unchanged": 1, "skipped": 0, "errors": 0}
        row = _item(db, item_id)
        assert {name: row[name] for name in cleared} == cleared
        assert row["source"] == "manual"
        assert row["updated_at"] == "2000-01-01"
        for field, local in cleared.items():
            assert _snapshot(db, item_id, "audiobookshelf:one", field) == {
                "observed": incoming, "local": local, "overridden": True,
            }


@respx.mock
def test_abs_cleared_isbn_does_not_attempt_to_take_a_trashed_items_identifier(db):
    _mock_abs()
    assert _abs_sync()["added"] == 1
    item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]
    update_item_fields(db, item_id, {"isbn": "", "cover_path": "covers/selected.jpg"})
    blocker = insert_item(db, title="Other edition", isbn=ISBN, media_type="audiobook")
    trash_item(db, blocker)
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))
    db.commit()

    for _ in range(2):
        assert _abs_sync() == {"added": 0, "updated": 0, "unchanged": 1, "skipped": 0, "errors": 0}
        row = _item(db, item_id)
        assert row["isbn"] is None and row["isbn10"] is None
        assert row["updated_at"] == "2000-01-01"
        assert _snapshot(db, item_id, "audiobookshelf:one", "isbn") == {
            "observed": ISBN, "local": None, "overridden": True,
        }
    blocker_row = db.execute("SELECT isbn, deleted_at FROM items WHERE id = ?", (blocker,)).fetchone()
    assert blocker_row["isbn"] == ISBN
    assert blocker_row["deleted_at"] is not None


@respx.mock
def test_abs_human_metadata_and_cover_edits_survive_remote_resync(db):
    _mock_abs()
    _abs_sync()
    item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]
    local = {
        "title": "My title", "authors": "My Author", "narrator": "My Narrator",
        "series_name": "My series", "publisher": "My publisher",
        "description": "My description", "publish_year": 1980,
        "duration_mins": 90, "media_type": "ebook", "isbn": "9780441013593",
        "asin": "B00STAMPED", "cover_path": f"covers/{item_id}.jpg",
        "cover_edition_isbn": "9780441013593", "cover_edition_language": "en",
        "notes": "My notes", "owned": 0, "reading_status": "read",
    }
    update_item_fields(db, item_id, local)
    cover_file = config.COVERS_DIR / f"{item_id}.jpg"
    cover_file.write_bytes(b"my selected cover")
    db.commit()

    for title in ("New remote title", "Another remote title"):
        cover_route = _mock_abs(
            title=title, authorName="Other Author", seriesName="Other series",
            narratorName="Other Narrator", publisher="Other publisher",
            description="Other description", publishedYear="2024", asin="B00REMOTE1",
        )
        stats = _abs_sync()
        assert stats["errors"] == 0
        row = _item(db, item_id)
        assert {name: row[name] for name in local} == local
        assert cover_route.call_count == 1  # Only the first import's 404 request.
        assert cover_file.read_bytes() == b"my selected cover"
    assert _snapshot(db, item_id, "audiobookshelf:one", "title") == {
        "observed": "Another remote title", "local": "My title", "overridden": True,
    }


@respx.mock
def test_abs_adopts_without_claiming_manual_title_or_nonempty_authors(db):
    item_id = insert_item(
        db, title="Remote title", authors="Remote Author", isbn=ISBN,
        media_type="audiobook", source="manual", series_name="My series",
    )
    db.commit()
    _mock_abs()
    assert _abs_sync()["updated"] == 1
    assert _snapshot(db, item_id, "audiobookshelf:one", "title") == {
        "observed": "Remote title", "local": "Remote title", "overridden": False,
    }
    assert _snapshot(db, item_id, "audiobookshelf:one", "authors") == {
        "observed": "Remote Author", "local": "Remote Author", "overridden": False,
    }
    assert _item(db, item_id)["description"] == "Remote description"

    _mock_abs(title="Remote correction", authorName="Other Author", seriesName="Other series")
    _abs_sync()
    row = _item(db, item_id)
    assert row["title"] == "Remote title"
    assert row["authors"] == "Remote Author"
    assert row["series_name"] == "My series"
    assert row["source"] == "manual"


@respx.mock
def test_abs_local_cover_survives_an_unedited_provider_title_refresh(db):
    _mock_abs()
    _abs_sync()
    item_id = db.execute("SELECT id FROM items_live").fetchone()["id"]
    cover_file = config.COVERS_DIR / f"{item_id}.jpg"
    cover_file.write_bytes(b"selected cover")
    update_item_fields(db, item_id, {"cover_path": f"covers/{item_id}.jpg"})
    db.commit()
    route = _mock_abs(title="Corrected remote title")
    route.respond(200, content=b"different remote cover" * 100)

    assert _abs_sync()["updated"] == 1
    assert _item(db, item_id)["title"] == "Corrected remote title"
    assert route.call_count == 1  # The original import only, not this refresh.
    assert cover_file.read_bytes() == b"selected cover"


@respx.mock
def test_abs_unknown_baseline_preserves_existing_metadata_and_fills_gaps(db):
    item_id = insert_item(
        db, title="Local legacy title", authors="Explicit Local Author",
        isbn=ISBN, media_type="audiobook", source="audiobookshelf", abs_id="one",
        abs_library_id="audio", description="Local description", narrator=None,
    )
    db.commit()
    _mock_abs()
    _abs_sync()
    _mock_abs(title="Changed remote", authorName=None, description=None, narratorName="New Narrator")
    _abs_sync()
    row = _item(db, item_id)
    assert row["title"] == "Local legacy title"
    assert row["authors"] == "Explicit Local Author"
    assert row["description"] == "Local description"
    assert row["narrator"] == "New Narrator"
    assert _snapshot(db, item_id, "audiobookshelf:one", "authors") == {
        "observed": None, "local": "Explicit Local Author", "overridden": False,
    }


@respx.mock
def test_abs_cover_fill_rechecks_local_cover_after_the_http_request(db):
    route = _mock_abs()
    selected = {}

    def choose_cover_while_downloading(request):
        with get_db() as writer:
            item_id = writer.execute("SELECT id FROM items_live").fetchone()["id"]
            path = config.COVERS_DIR / f"{item_id}.jpg"
            path.write_bytes(b"human cover chosen during download")
            update_item_fields(writer, item_id, {"cover_path": f"covers/{item_id}.jpg"})
        selected.update(item_id=item_id, path=path)
        return httpx.Response(200, content=b"remote cover" * 200)

    route.mock(side_effect=choose_cover_while_downloading)
    assert _abs_sync()["added"] == 1
    assert selected["path"].read_bytes() == b"human cover chosen during download"
    assert _item(db, selected["item_id"])["cover_path"] == f"covers/{selected['item_id']}.jpg"


@respx.mock
def test_abs_isbn_conflict_logs_only_after_releasing_its_write_lock(db, monkeypatch):
    from app.log_handler import SQLiteHandler

    insert_item(db, title="Existing copy", isbn=ISBN, media_type="audiobook", abs_id="other")
    db.commit()
    _mock_abs()
    handler = SQLiteHandler()
    monkeypatch.setattr(audiobookshelf.logger, "handlers", [handler])

    assert _abs_sync()["skipped"] == 1
    messages = db.execute(
        "SELECT message FROM log_entries WHERE module = ?",
        (audiobookshelf.__name__,),
    ).fetchall()
    assert any("Skipping ABS item one" in row["message"] for row in messages)
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0] == 0


def test_provenance_is_per_source_key_and_observation_never_claims_a_local_edit(db):
    item_id = insert_item(db, title="Initial", media_type="comic")
    metadata_policy.record_initial(db, item_id, "komga:first", {"title": "Initial"})
    assert metadata_policy.refresh_metadata(db, item_id, "komga:second", {"title": "Other"}) == {}
    assert _snapshot(db, item_id, "komga:second", "title") == {
        "observed": "Other", "local": "Initial", "overridden": False,
    }

    update_item_fields(db, item_id, {"title": "Human edit"})
    metadata_policy.refresh_metadata(db, item_id, "komga:first", {"title": "Human edit"})
    metadata_policy.refresh_metadata(db, item_id, "komga:first", {"title": "Remote correction"})
    metadata_policy.refresh_metadata(db, item_id, "komga:second", {"title": "Another correction"})

    assert _item(db, item_id)["title"] == "Human edit"
    assert _snapshot(db, item_id, "komga:first", "title") == {
        "observed": "Remote correction", "local": "Human edit", "overridden": True,
    }
    assert _snapshot(db, item_id, "komga:second", "title") == {
        "observed": "Another correction", "local": "Human edit", "overridden": True,
    }


def test_sparse_observations_keep_last_applied_value_and_sqlite_numeric_affinity(db):
    item_id = insert_item(db, title="Title", publish_year="2000", description="Original")
    fields = {"publish_year": "2000", "description": "Original"}
    metadata_policy.record_initial(db, item_id, "source:first", fields)
    assert _snapshot(db, item_id, "source:first", "publish_year")["applied"] == 2000
    metadata_policy.refresh_metadata(db, item_id, "source:first", {"description": None})
    assert _snapshot(db, item_id, "source:first", "description") == {
        "observed": None, "applied": "Original", "local": "Original", "overridden": False,
    }
    metadata_policy.refresh_metadata(
        db, item_id, "source:first", {"publish_year": 2001, "description": "Revised"},
    )
    assert _item(db, item_id)["publish_year"] == 2001
    assert _item(db, item_id)["description"] == "Revised"


@pytest.mark.parametrize("field", ["description", "series_name", "authors", "isbn"])
@pytest.mark.parametrize("cleared", [None, "", "  "])
def test_metadata_policy_never_refills_a_known_explicit_clear(db, field, cleared):
    initial = ISBN if field == "isbn" else "Provider value"
    item_id = insert_item(db, title="Title", **{field: initial})
    metadata_policy.record_initial(db, item_id, "source:first", {field: initial})
    update_item_fields(db, item_id, {field: cleared})
    local = None if field == "isbn" else cleared
    assert _item(db, item_id)[field] == local
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item_id,))

    incoming_values = ("9780441013593", "9781974700523") if field == "isbn" else (
        "Remote correction", "Another correction",
    )
    for incoming in incoming_values:
        assert metadata_policy.refresh_metadata(db, item_id, "source:first", {field: incoming}) == {}
        assert _item(db, item_id)[field] == local
        assert _item(db, item_id)["updated_at"] == "2000-01-01"
        assert _snapshot(db, item_id, "source:first", field) == {
            "observed": incoming, "local": local, "overridden": True,
        }


def test_metadata_policy_still_fills_genuine_gaps_after_initial_empty_observations(db):
    item_id = insert_item(db, title="Manual title", description=None, series_name="")
    missing = {"description": None, "series_name": None}
    assert metadata_policy.refresh_metadata(db, item_id, "source:first", missing) == {}
    for field, local in (("description", None), ("series_name", "")):
        assert _snapshot(db, item_id, "source:first", field) == {
            "observed": None, "local": local, "overridden": False,
        }

    filled = {"description": "Filled description", "series_name": "Filled series"}
    assert metadata_policy.refresh_metadata(db, item_id, "source:first", filled) == filled
    revised = {"description": "Revised description", "series_name": "Revised series"}
    assert metadata_policy.refresh_metadata(db, item_id, "source:first", revised) == revised
    for field, value in revised.items():
        assert _snapshot(db, item_id, "source:first", field) == {
            "observed": value, "applied": value, "local": value, "overridden": False,
        }


def test_metadata_policy_records_clear_during_sparse_observation_and_keeps_override(db):
    item_id = insert_item(db, title="Title", description="Original")
    metadata_policy.record_initial(db, item_id, "source:first", {"description": "Original"})
    update_item_fields(db, item_id, {"description": ""})
    assert metadata_policy.refresh_metadata(db, item_id, "source:first", {"description": None}) == {}
    assert _snapshot(db, item_id, "source:first", "description") == {
        "observed": None, "local": "", "overridden": True,
    }
    # A recorded override must not disappear when a later local value or
    # remote observation happens to equal the original provider value.
    for local in ("Original", None, ""):
        update_item_fields(db, item_id, {"description": local})
        for incoming in (local, "Remote correction"):
            assert metadata_policy.refresh_metadata(
                db, item_id, "source:first", {"description": incoming},
            ) == {}
            assert _item(db, item_id)["description"] == local
            assert _snapshot(db, item_id, "source:first", "description") == {
                "observed": incoming, "local": local, "overridden": True,
            }


@pytest.mark.parametrize("baseline", [
    {"observed": "Original", "applied": "Original"},
    {"observed": "Original", "local": "Original", "overridden": False},
    {"observed": "Original", "overridden": True},
])
def test_metadata_policy_preserves_known_clears_with_older_snapshot_shapes(db, baseline):
    item_id = insert_item(db, title="Title", description="Original")
    db.execute(
        "INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) "
        "VALUES (?, 'source:first', 'description', ?)", (item_id, json.dumps(baseline)),
    )
    update_item_fields(db, item_id, {"description": None})

    for incoming in ("Remote correction", "Another correction"):
        assert metadata_policy.refresh_metadata(db, item_id, "source:first", {"description": incoming}) == {}
        assert _item(db, item_id)["description"] is None
        assert _snapshot(db, item_id, "source:first", "description") == {
            "observed": incoming, "local": None, "overridden": True,
        }


@pytest.mark.parametrize("field", ["cover_path", "tags", "notes", "owned", "reading_status", "location_id", "source"])
def test_metadata_policy_refuses_non_metadata_fields(db, field):
    item_id = insert_item(db, title="Title")
    with pytest.raises(ValueError, match="Not refreshable"):
        metadata_policy.refresh_metadata(db, item_id, "source:first", {field: "remote"})
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0] == 0


def test_metadata_policy_locks_before_reads_and_does_not_commit_the_callers_work(db):
    item_id = insert_item(db, title="Original")
    metadata_policy.record_initial(db, item_id, "source:first", {"title": "Original"})
    db.commit()
    statements = []
    db.set_trace_callback(lambda statement: statements.append(statement))

    metadata_policy.refresh_metadata(db, item_id, "source:first", {"title": "Revised"})

    assert statements[0] == "BEGIN IMMEDIATE"
    assert db.in_transaction
    db.rollback()
    assert _item(db, item_id)["title"] == "Original"
    assert _snapshot(db, item_id, "source:first", "title") == {
        "observed": "Original", "applied": "Original",
        "local": "Original", "overridden": False,
    }


@pytest.mark.parametrize("missing", [None, "", "  ", ",", "—"])
@pytest.mark.parametrize("side", ["origin", "candidate"])
def test_abs_title_only_links_require_both_meaningful_creators(db, missing, side):
    origin = _insert_item(
        db, title="Dune", isbn=None, media_type="audiobook",
        authors=missing if side == "origin" else "Frank Herbert",
    )
    _insert_item(
        db, title="Dune", isbn=None, media_type="book",
        authors=missing if side == "candidate" else "Frank Herbert",
    )
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0


@pytest.mark.parametrize("media_type", [
    "dvd", "digital_video", "tv_series", "video_game", "magazine",
    "cd", "vinyl", "cassette", "digital_music",
])
@pytest.mark.parametrize("shared_isbn", [False, True])
def test_abs_never_infers_a_book_to_other_media_format_link(db, media_type, shared_isbn):
    isbn = ISBN if shared_isbn else None
    origin = _insert_item(db, title="Dune", isbn=isbn, media_type="audiobook", authors="Frank Herbert")
    _insert_item(db, title="Dune", isbn=isbn, media_type=media_type, authors="Frank Herbert")
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0


def test_abs_non_book_origin_does_not_infer_a_format_link_to_a_book(db):
    origin = _insert_item(db, title="Dune", isbn=ISBN, media_type="dvd", authors="Frank Herbert")
    _insert_item(db, title="Dune", isbn=ISBN, media_type="book", authors="Frank Herbert")
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0


@pytest.mark.parametrize("origin_title,candidate_title", [
    ("三体", "活着"), ("!!!", "!!!"), ("Dune: A Novel", "Dune: A Study Guide"),
    ("The Road", "Road"),
])
def test_abs_auto_link_does_not_collapse_unicode_empty_or_inexact_titles(db, origin_title, candidate_title):
    origin = _insert_item(db, title=origin_title, isbn=None, media_type="audiobook", authors="Same Author")
    _insert_item(db, title=candidate_title, isbn=None, media_type="book", authors="Same Author")
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0


def test_abs_exact_unicode_title_with_real_creator_overlap_can_link(db):
    origin = _insert_item(db, title="三体", isbn=None, media_type="audiobook", authors="刘慈欣")
    book = _insert_item(db, title="三体", isbn=None, media_type="book", authors="刘慈欣")
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 1
    assert [tuple(row) for row in db.execute("SELECT item_a_id, item_b_id FROM item_links")] == [
        (min(origin, book), max(origin, book)),
    ]


def test_abs_author_substring_is_not_creator_overlap(db):
    origin = _insert_item(db, title="Same title", isbn=None, media_type="audiobook", authors="Ann")
    _insert_item(db, title="Same title", isbn=None, media_type="book", authors="Joann")
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0


@pytest.mark.parametrize("author", ["Unknown", "Unknown Author", "Anonymous", "N/A"])
def test_abs_placeholder_creators_are_not_real_overlap(db, author):
    origin = _insert_item(db, title="Same title", isbn=None, media_type="audiobook", authors=author)
    _insert_item(db, title="Same title", isbn=None, media_type="book", authors=author)
    db.commit()
    assert audiobookshelf.auto_link([origin]) == 0


def test_abs_strong_isbn_still_links_two_book_format_origins_without_creators(db):
    audio = _insert_item(db, title="Audio title", isbn=ISBN, media_type="audiobook", authors=None)
    ebook = _insert_item(db, title="Digital title", isbn=ISBN, media_type="ebook", authors=None)
    db.commit()
    assert audiobookshelf.auto_link([audio, ebook]) == 1
    assert [tuple(row) for row in db.execute("SELECT item_a_id, item_b_id FROM item_links")] == [
        (min(audio, ebook), max(audio, ebook)),
    ]
