from app.services import media_groups
from tests.conftest import _insert_item


def test_item_detail_shows_group_platforms_and_related_entries(admin_client, db):
    snes = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="snes", publish_year=1994, romm_id="11",
    )
    gba = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="gba", publish_year=1995, romm_id="12",
    )
    pc = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="pc", publish_year=1996, romm_id="13",
    )
    media_groups.auto_link_family(db, "game")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('romm_url', 'http://romm:8080')"
    )
    db.execute("COMMIT")

    response = admin_client.get(f"/item/{snes}")
    assert response.status_code == 200
    assert 'data-testid="related-media-panel"' in response.text
    assert "Platforms:" in response.text
    assert "SNES" in response.text
    assert "Game Boy Advance" in response.text
    assert "PC" in response.text
    assert response.text.count('data-testid="related-media-item"') == 2
    # The current SNES item has its own Open in RomM action; both other
    # platform versions retain independent RomM deep links in the group.
    assert response.text.count("Also in RomM (Digital Game)") == 2


def test_multiple_abs_narrator_editions_keep_separate_links(admin_client, db):
    book = _insert_item(
        db, title="Example Novel", isbn=None, media_type="book",
        authors="Example Author",
    )
    _insert_item(
        db, title="Example Novel", isbn=None, media_type="audiobook",
        authors="Example Author", narrator="Narrator One", abs_id="abs-one",
    )
    _insert_item(
        db, title="Example Novel", isbn=None, media_type="audiobook",
        authors="Example Author", narrator="Narrator Two", abs_id="abs-two",
    )
    media_groups.auto_link_family(db, "book")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('abs_url', 'http://abs:13378')"
    )
    db.execute("COMMIT")

    response = admin_client.get(f"/item/{book}")
    assert response.status_code == 200
    assert "Narrator One" in response.text
    assert "Narrator Two" in response.text
    assert response.text.count("Also in Audiobookshelf (Audiobook)") == 2
    assert "abs-one" in response.text
    assert "abs-two" in response.text


def test_cross_media_manual_group_renders_formats(admin_client, db):
    book = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn=None,
        media_type="book", authors="J. K. Rowling",
    )
    audio = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn=None,
        media_type="audiobook", authors="J. K. Rowling", narrator="Stephen Fry",
    )
    game = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn=None,
        media_type="video_game", platform="pc",
    )
    media_groups.link_items(db, book, audio, "format")
    media_groups.link_items(db, book, game, "related")
    db.execute("COMMIT")

    response = admin_client.get(f"/item/{book}")
    assert response.status_code == 200
    assert "Formats:" in response.text
    assert "Book" in response.text
    assert "Audiobook" in response.text
    assert "Video Game" in response.text
    assert "Stephen Fry" in response.text
    assert "Add related media" in response.text


def test_related_search_and_link_endpoint(admin_client, db):
    book = _insert_item(db, title="Dune", isbn=None, media_type="book", authors="Frank Herbert")
    game = _insert_item(db, title="Dune", isbn=None, media_type="video_game", platform="pc")
    db.execute("COMMIT")

    search = admin_client.get(f"/api/items/{book}/related/search?q=Dune")
    assert search.status_code == 200
    assert f"/api/items/{book}/related/{game}" in search.text
    assert "Video Game" in search.text

    linked = admin_client.post(f"/api/items/{book}/related/{game}", follow_redirects=False)
    assert linked.status_code == 303
    with db:
        assert media_groups.related_ids(db, book) == [game]


def test_viewer_cannot_manage_related_media(viewer_client, db):
    book = _insert_item(db, title="Dune", isbn=None, media_type="book")
    game = _insert_item(db, title="Dune", isbn=None, media_type="video_game", platform="pc")
    db.execute("COMMIT")

    response = viewer_client.post(f"/api/items/{book}/related/{game}", follow_redirects=False)
    assert response.status_code in (401, 403)

    detail = viewer_client.get(f"/item/{book}")
    assert "Add related media" not in detail.text
