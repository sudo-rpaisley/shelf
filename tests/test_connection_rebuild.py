from app.services import media_groups
from tests.conftest import _insert_item


def test_rebuild_endpoint_connects_existing_safe_matches(admin_client, db):
    book = _insert_item(
        db,
        title="Dune",
        isbn="9780441172719",
        media_type="book",
        authors="Frank Herbert",
    )
    audiobook = _insert_item(
        db,
        title="Dune",
        isbn="9781427201432",
        media_type="audiobook",
        authors="Frank Herbert",
    )
    db.commit()

    assert media_groups.related_ids(db, book) == []

    response = admin_client.post("/api/items/connections/rebuild")

    assert response.status_code == 200
    assert 'data-testid="connection-rebuild-result"' in response.text
    assert "Created <strong" in response.text
    assert ">1</strong>" in response.text
    assert media_groups.related_ids(db, book) == [audiobook]


def test_rebuild_is_idempotent(db):
    book = _insert_item(
        db,
        title="The Hobbit",
        isbn="9780007525492",
        media_type="book",
        authors="J. R. R. Tolkien",
    )
    ebook = _insert_item(
        db,
        title="The Hobbit",
        isbn="9780007322602",
        media_type="ebook",
        authors="J. R. R. Tolkien",
    )

    first = media_groups.rebuild_automatic_connections(db)
    second = media_groups.rebuild_automatic_connections(db)

    assert first["created"] == 1
    assert second["created"] == 0
    assert media_groups.related_ids(db, book) == [ebook]


def test_rebuild_endpoint_requires_admin(viewer_client):
    response = viewer_client.post("/api/items/connections/rebuild")
    assert response.status_code == 403


def test_settings_data_page_exposes_connection_rebuild(admin_client):
    html = admin_client.get("/settings").text
    assert 'data-testid="connection-rebuild-panel"' in html
    assert "Find connected items" in html
    assert "does not rescan your shelves" in html
