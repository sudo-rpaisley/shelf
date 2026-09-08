"""Library-aware Needs Attention coverage for Shelf 0.37."""

from pathlib import Path

from app.auth import create_token
from app.services import libraries
from tests.conftest import _insert_item


def test_attention_counts_and_results_do_not_leak_hidden_libraries(
    db, viewer_client, viewer_user, admin_user
):
    hidden_library = libraries.create_library(db, "Hidden health")
    _insert_item(
        db,
        title="Visible Missing Cover",
        isbn=None,
        media_type="book",
        cover_path=None,
    )
    _insert_item(
        db,
        title="Secret Missing Cover",
        isbn=None,
        media_type="book",
        cover_path=None,
        _library_id=hidden_library["id"],
    )
    db.commit()

    response = viewer_client.get("/attention")
    assert response.status_code == 200
    assert "Visible Missing Cover" in response.text
    assert "Secret Missing Cover" not in response.text
    assert 'data-attention-category="cover"' in response.text
    assert 'data-attention-count="1"' in response.text

    admin_token = create_token(
        admin_user["id"],
        admin_user["username"],
        admin_user["role"],
        admin_user["display_name"],
    )
    viewer_client.cookies.set("access_token", admin_token)
    admin_response = viewer_client.get("/attention")
    assert admin_response.status_code == 200
    assert "Visible Missing Cover" in admin_response.text
    assert "Secret Missing Cover" in admin_response.text
    assert 'data-attention-count="2"' in admin_response.text


def test_location_attention_is_copy_based_not_media_type_inference(
    db, viewer_client
):
    physical = _insert_item(
        db,
        title="Loose Physical Copy",
        isbn=None,
        media_type="book",
        cover_path="covers/physical.jpg",
    )
    _insert_item(
        db,
        title="Digital Without Copy",
        isbn=None,
        media_type="ebook",
        cover_path="covers/digital.jpg",
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)",
        (physical,),
    )
    db.commit()

    response = viewer_client.get("/attention?category=location")
    assert response.status_code == 200
    assert "Loose Physical Copy" in response.text
    assert "Digital Without Copy" not in response.text

    location_id = db.execute(
        "INSERT INTO locations (name) VALUES ('Study Shelf')"
    ).lastrowid
    db.execute(
        "UPDATE item_copies SET location_id = ? WHERE item_id = ?",
        (location_id, physical),
    )
    db.commit()

    response = viewer_client.get("/attention?category=location")
    assert "Loose Physical Copy" not in response.text


def test_creator_and_periodical_issue_categories_use_current_media_families(
    db, viewer_client
):
    _insert_item(
        db,
        title="Manga Missing Creator",
        isbn=None,
        media_type="manga",
        cover_path="covers/manga.jpg",
        authors=None,
    )
    magazine = _insert_item(
        db,
        title="Magazine Missing Issue",
        isbn=None,
        media_type="magazine",
        cover_path="covers/magazine.jpg",
        authors="Editorial",
    )
    publication_id = db.execute(
        "INSERT INTO periodical_publications (title) VALUES ('Example Monthly')"
    ).lastrowid
    db.execute(
        "INSERT INTO periodical_issues (item_id, publication_id) VALUES (?, ?)",
        (magazine, publication_id),
    )
    db.commit()

    creator = viewer_client.get("/attention?category=creator")
    assert creator.status_code == 200
    assert "Manga Missing Creator" in creator.text

    issue = viewer_client.get("/attention?category=periodical_issue")
    assert issue.status_code == 200
    assert "Magazine Missing Issue" in issue.text

    db.execute(
        "UPDATE periodical_issues SET issue_number = '42' WHERE item_id = ?",
        (magazine,),
    )
    db.commit()
    issue = viewer_client.get("/attention?category=periodical_issue")
    assert "Magazine Missing Issue" not in issue.text


def test_fix_action_uses_item_library_editor_role(
    db, viewer_client, viewer_user
):
    item_id = _insert_item(
        db,
        title="Fixable Record",
        isbn=None,
        media_type="book",
        cover_path=None,
    )
    db.commit()

    viewer_html = viewer_client.get("/attention").text
    assert f'href="/item/{item_id}/edit"' not in viewer_html

    libraries.set_membership(
        db,
        libraries.DEFAULT_LIBRARY_ID,
        viewer_user["id"],
        "editor",
    )
    db.commit()
    editor_html = viewer_client.get("/attention").text
    assert f'href="/item/{item_id}/edit"' in editor_html


def test_attention_keeps_037_schema_and_router_ownership_contracts():
    router_source = Path("app/routers/attention.py").read_text()
    assert "CREATE TABLE" not in router_source
    assert "ensure_foundation" not in router_source
    assert Path("app/routers/__init__.py").read_text() == ""

    main_source = Path("app/main.py").read_text()
    assert "attention" in main_source
    assert "app.include_router(attention.router)" in main_source
