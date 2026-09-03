"""Product coverage for the catalogue needs-attention workflow."""


def test_attention_page_groups_common_catalogue_problems(viewer_client, db):
    db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Incomplete Book', 'book', 'test', 1)"
    )
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, cover_path) "
        "VALUES ('Digital Book', 'ebook', 'test', 1, 'covers/digital.jpg')"
    )
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, cover_path, authors) "
        "VALUES ('Unknown Magazine Issue', 'magazine', 'test', 1, 'covers/mag.jpg', 'Editorial')"
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


def test_location_attention_excludes_digital_media(viewer_client, db):
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, cover_path) "
        "VALUES ('Physical Book', 'book', 'test', 1, 'covers/book.jpg')"
    )
    db.execute(
        "INSERT INTO items (title, media_type, source, owned, cover_path) "
        "VALUES ('Digital Book', 'ebook', 'test', 1, 'covers/digital.jpg')"
    )
    db.commit()

    html = viewer_client.get("/attention?category=location").text
    assert "Physical Book" in html
    assert "Digital Book" not in html


def test_attention_fix_action_is_role_aware(viewer_client, editor_client, db):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Fix Me', 'book', 'test', 1)"
    ).lastrowid
    db.commit()

    viewer_html = viewer_client.get("/attention").text
    editor_html = editor_client.get("/attention").text

    assert f'href="/item/{item_id}/edit"' not in viewer_html
    assert f'href="/item/{item_id}/edit"' in editor_html
