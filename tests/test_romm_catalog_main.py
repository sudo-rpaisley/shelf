from app.services import romm_catalog


def _insert_romm(db, romm_id, platform_id, platform, title, *, publisher=None, year=None):
    cursor = db.execute(
        "INSERT INTO items (title, media_type, source, owned, platform, publisher, "
        "publish_year, romm_id, romm_platform_id) "
        "VALUES (?, 'digital_game', 'romm', 1, ?, ?, ?, ?, ?)",
        (title, platform, publisher, year, romm_id, platform_id),
    )
    return cursor.lastrowid


def test_platform_summaries_keep_provider_platforms_separate(db):
    _insert_romm(db, "rom-1", "p-snes", "snes", "Same Game")
    _insert_romm(db, "rom-2", "p-gba", "gba", "Same Game")

    rows = romm_catalog.platform_summaries(db)

    assert {row["platform_id"] for row in rows} == {"p-snes", "p-gba"}
    assert sum(row["game_count"] for row in rows) == 2


def test_catalog_filter_and_search_do_not_merge_same_titles(db):
    _insert_romm(db, "rom-1", "p-snes", "snes", "Shared Title", publisher="Nintendo")
    _insert_romm(db, "rom-2", "p-gba", "gba", "Shared Title", publisher="Nintendo")
    _insert_romm(db, "rom-3", "p-snes", "snes", "Other Game", publisher="Sega")

    rows, total = romm_catalog.fetch_page(db, platform_id="p-snes", query="Nintendo")

    assert total == 1
    assert [row["romm_id"] for row in rows] == ["rom-1"]


def test_catalog_paginates_stably_by_title(db):
    for number, title in enumerate(["Gamma", "Alpha", "Beta"], start=1):
        _insert_romm(db, f"rom-{number}", "p-one", "snes", title)

    first, total = romm_catalog.fetch_page(db, limit=2, offset=0)
    second, _ = romm_catalog.fetch_page(db, limit=2, offset=2)

    assert total == 3
    assert [row["title"] for row in first] == ["Alpha", "Beta"]
    assert [row["title"] for row in second] == ["Gamma"]


def test_catalog_honours_library_visibility_predicate(db):
    visible_id = _insert_romm(db, "rom-visible", "p-one", "snes", "Visible Game")
    _insert_romm(db, "rom-hidden", "p-one", "snes", "Hidden Game")

    rows, total = romm_catalog.fetch_page(
        db,
        access_sql="i.id = ?",
        access_params=[visible_id],
    )
    summaries = romm_catalog.platform_summaries(
        db,
        access_sql="i.id = ?",
        access_params=[visible_id],
    )

    assert total == 1
    assert [row["title"] for row in rows] == ["Visible Game"]
    assert sum(row["game_count"] for row in summaries) == 1
