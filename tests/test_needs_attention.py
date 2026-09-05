"""Product coverage for the catalogue needs-attention workflow."""

from app.services import holdings
from tests.conftest import _insert_item


def test_attention_page_groups_common_catalogue_problems(viewer_client, db):
    _insert_item(db, title="Incomplete Book", isbn=None, media_type="book", owned=1)
    _insert_item(
        db,
        title="Digital Book",
        isbn=None,
        media_type="ebook",
        owned=1,
        cover_path="covers/digital.jpg",
    )
    _insert_item(
        db,
        title="Unknown Magazine Issue",
        isbn=None,
        media_type="magazine",
        owned=1,
        cover_path="covers/mag.jpg",
        authors="Editorial",
    )
    db.commit()

    response = viewer_client.get("/attention")
    assert response.status_code == 200
    html = response.text

    assert "Needs attention" in html
    assert 'data-attention-category="cover"' in html
    assert 'data-attention-category="location"' in html
    assert 'data-attention-category="creator"' in html
    assert 'data-attention-category="magazine_issue"' in html
    assert "Incomplete Book" in html


def test_location_attention_excludes_digital_media_and_uses_copy_location(viewer_client, db):
    physical_id = _insert_item(
        db,
        title="Physical Book",
        isbn=None,
        media_type="book",
        owned=1,
        cover_path="covers/book.jpg",
    )
    _insert_item(
        db,
        title="Digital Book",
        isbn=None,
        media_type="ebook",
        owned=1,
        cover_path="covers/digital.jpg",
    )
    db.commit()

    html = viewer_client.get("/attention?category=location").text
    assert "Physical Book" in html
    assert "Digital Book" not in html

    # A precise nested copy location clears the warning even though the old
    # item.location_id compatibility field remains only room-level metadata.
    with db:
        holdings.ensure_foundation(db)
        room = db.execute(
            "INSERT INTO location_nodes (name) VALUES ('Study')"
        ).lastrowid
        shelf = db.execute(
            "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Shelf 1')",
            (room,),
        ).lastrowid
        db.execute(
            "UPDATE item_copies SET location_id = ? WHERE item_id = ? AND is_primary = 1",
            (shelf, physical_id),
        )
    html = viewer_client.get("/attention?category=location").text
    assert "Physical Book" not in html


def test_magazine_attention_uses_issue_model_not_generic_publish_year(viewer_client, db):
    magazine_id = _insert_item(
        db,
        title="Issue With Year Only",
        isbn=None,
        media_type="magazine",
        owned=1,
        cover_path="covers/mag.jpg",
        authors="Editorial",
        publish_year=2026,
    )
    db.commit()

    html = viewer_client.get("/attention?category=magazine_issue").text
    assert "Issue With Year Only" in html

    with db:
        holdings.ensure_foundation(db)
        publication_id = db.execute(
            "INSERT INTO periodical_publications (title, issn) VALUES ('Example Monthly', '1234-567X')"
        ).lastrowid
        db.execute(
            "INSERT INTO periodical_issues (item_id, publication_id, issue_number) "
            "VALUES (?, ?, '42')",
            (magazine_id, publication_id),
        )
    html = viewer_client.get("/attention?category=magazine_issue").text
    assert "Issue With Year Only" not in html


def test_attention_fix_action_is_role_aware(viewer_client, editor_user, db):
    """Exercise both roles without sharing two cookie-mutating client fixtures."""
    from app.auth import create_token

    item_id = _insert_item(db, title="Fix Me", isbn=None, media_type="book", owned=1)
    db.commit()

    viewer_html = viewer_client.get("/attention").text
    assert f'href="/item/{item_id}/edit"' not in viewer_html

    editor_token = create_token(
        editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"]
    )
    viewer_client.cookies.set("access_token", editor_token)
    editor_html = viewer_client.get("/attention").text
    assert f'href="/item/{item_id}/edit"' in editor_html