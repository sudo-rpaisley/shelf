from app.routers import items_catalog
from app.services import audiobookshelf, hardcover, igdb, openlibrary, provider_result
from app.services.item_write import insert_item
from tests.conftest import _insert_item


def test_insert_item_persists_scalar_series_immediately(db):
    item_id = insert_item(
        db,
        title="Series Book",
        isbn="9780000011001",
        media_type="book",
        series_name="Example Saga",
        series_position=2,
    )
    row = db.execute(
        "SELECT series_name, position, is_primary FROM item_series WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert row["series_name"] == "Example Saga"
    assert row["position"] == 2
    assert row["is_primary"] == 1


def test_insert_item_persists_multiple_explicit_series(db):
    item_id = insert_item(
        db,
        title="Crossover",
        isbn="9780000011002",
        media_type="book",
        series_name="Main Saga",
        series_position=3,
        series_memberships=[
            {"name": "Main Saga", "position": 3},
            {"name": "Shared World", "position": 7},
        ],
    )
    rows = db.execute(
        "SELECT series_name, position, is_primary FROM item_series "
        "WHERE item_id = ? ORDER BY series_name COLLATE NOCASE",
        (item_id,),
    ).fetchall()
    assert {row["series_name"] for row in rows} == {"Main Saga", "Shared World"}
    assert sum(row["is_primary"] for row in rows) == 1
    assert next(row for row in rows if row["series_name"] == "Shared World")["position"] == 7


def test_rebuild_reconciles_existing_legacy_series(admin_client, db):
    item_id = _insert_item(
        db,
        title="Existing Book",
        isbn="9780000011003",
        media_type="book",
        series_name="Existing Saga",
        series_position=4,
    )
    db.commit()
    assert db.execute(
        "SELECT 1 FROM item_series WHERE item_id = ?", (item_id,)
    ).fetchone() is None

    response = admin_client.post("/api/items/connections/rebuild")

    assert response.status_code == 200
    assert 'data-testid="series-rebuild-result"' in response.text
    row = db.execute(
        "SELECT series_name, position, is_primary FROM item_series WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert row["series_name"] == "Existing Saga"
    assert row["position"] == 4
    assert row["is_primary"] == 1


def test_hardcover_preserves_multiple_book_series():
    rows = hardcover._book_series_memberships({
        "book_series": [
            {"series": {"name": "Main Saga"}, "position": 2},
            {"series": {"name": "Shared World"}, "position": 8},
        ]
    })
    assert rows == [
        {"name": "Main Saga", "position": 2},
        {"name": "Shared World", "position": 8},
    ]


def test_audiobookshelf_preserves_series_sequences():
    rows = audiobookshelf._series_memberships({
        "series": [
            {"series": "Discworld", "sequence": "12"},
            {"series": "Death", "sequence": "2"},
        ]
    })
    assert rows == [
        {"name": "Discworld", "position": 12.0},
        {"name": "Death", "position": 2.0},
    ]


def test_igdb_preserves_multiple_franchises_as_series():
    parsed = igdb._parse_game({
        "id": 1,
        "name": "Example Game",
        "franchises": [{"name": "Main Franchise"}, {"name": "Shared Universe"}],
    })
    assert parsed["series_name"] == "Main Franchise"
    assert parsed["series_memberships"] == [
        {"name": "Main Franchise", "position": None},
        {"name": "Shared Universe", "position": None},
    ]


def test_openlibrary_uses_only_explicit_series_metadata():
    rows = openlibrary._series_memberships({
        "title": "Harry Potter and the Philosopher's Stone",
        "series": ["Harry Potter #1", "Wizarding World"],
    })
    assert rows == [
        {"name": "Harry Potter", "position": 1},
        {"name": "Wizarding World", "position": None},
    ]
    assert openlibrary._series_memberships({"title": "Dune Messiah"}) == []


def test_reconciliation_removes_stale_old_primary_but_keeps_secondary(db):
    item_id = insert_item(
        db,
        title="Changing Series",
        isbn="9780000011004",
        media_type="book",
        series_name="Old Primary",
        series_position=1,
        series_memberships=[{"name": "Secondary", "position": 5}],
    )
    db.execute(
        "UPDATE items SET series_name = 'New Primary', series_position = 2 WHERE id = ?",
        (item_id,),
    )
    from app.services import series_memberships as series_svc
    series_svc.sync_legacy_item(db, item_id)
    rows = db.execute(
        "SELECT series_name, is_primary FROM item_series WHERE item_id = ?",
        (item_id,),
    ).fetchall()
    assert {row["series_name"] for row in rows} == {"New Primary", "Secondary"}
    assert next(row for row in rows if row["series_name"] == "New Primary")["is_primary"] == 1


def test_dvd_add_uses_tmdb_collection_as_series(admin_client, db, monkeypatch):
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('tmdb_api_key', 'test-key')"
    )
    db.commit()

    async def fake_lookup_movie(tmdb_id, api_key, client):
        return provider_result.found("tmdb", {
            "series_name": "Example Collection",
            "series_memberships": [{"name": "Example Collection", "position": None}],
        })

    monkeypatch.setattr(items_catalog.tmdb, "lookup_movie", fake_lookup_movie)
    response = admin_client.post(
        "/api/dvds/add",
        data={
            "title": "Example Film",
            "tmdb_id": "42",
            "description": "",
            "publish_year": "2001",
            "cover_url": "",
        },
    )
    assert response.status_code == 200
    item = db.execute(
        "SELECT id, series_name FROM items WHERE title = 'Example Film'"
    ).fetchone()
    assert item["series_name"] == "Example Collection"
    membership = db.execute(
        "SELECT series_name, is_primary FROM item_series WHERE item_id = ?",
        (item["id"],),
    ).fetchone()
    assert membership["series_name"] == "Example Collection"
    assert membership["is_primary"] == 1
