"""Role-aware Browse bulk-action rendering."""


def test_viewer_has_no_bulk_selection_controls(viewer_client):
    response = viewer_client.get("/browse")
    assert response.status_code == 200
    html = response.text

    assert 'data-can-select="0"' in html
    assert "Delete Selected" not in html
    assert "Move to location..." not in html
    assert 'x-model="bulkTypeVal"' not in html
    assert 'id="bulk-series"' not in html


def test_editor_can_bulk_delete_but_not_bulk_edit(editor_client):
    """The historical definition now pins D5: library editors may edit too."""
    response = editor_client.get("/browse")
    assert response.status_code == 200
    html = response.text

    assert 'data-can-select="1"' in html
    assert "Delete Selected" in html
    assert "Move to location..." in html
    assert 'x-model="bulkTypeVal"' in html
    assert 'id="bulk-series"' in html
    assert 'x-model="bulkTagVal"' in html
    assert "Select items to bulk edit or delete them." in html
    assert "Tagging is also available." in html
    assert "editor access to every selected item’s library" in html


def test_admin_gets_bulk_edit_and_delete_controls(admin_client):
    response = admin_client.get("/browse")
    assert response.status_code == 200
    html = response.text

    assert 'data-can-select="1"' in html
    assert "Delete Selected" in html
    assert "Move to location..." in html
    assert 'x-model="bulkTypeVal"' in html
    assert 'id="bulk-series"' in html
    assert "Select items to bulk edit or delete them." in html


def test_global_editor_with_only_read_only_membership_has_no_bulk_controls(editor_client, editor_user, db):
    from app.services import libraries

    libraries.set_membership(db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], "viewer")
    db.commit()
    response = editor_client.get("/browse")
    assert response.status_code == 200
    html = response.text
    assert 'data-can-select="0"' in html
    for control in ("Delete Selected", "Move to location...", 'x-model="bulkTypeVal"',
                    'id="bulk-series"', 'x-model="bulkTagVal"'):
        assert control not in html


def test_global_viewer_with_library_editor_membership_gets_all_bulk_controls(viewer_client, viewer_user, db):
    from app.services import libraries

    libraries.set_membership(db, libraries.DEFAULT_LIBRARY_ID, viewer_user["id"], "editor")
    db.commit()
    response = viewer_client.get("/browse")
    assert response.status_code == 200
    html = response.text
    assert 'data-can-select="1"' in html
    for control in ("Delete Selected", "Move to location...", 'x-model="bulkTypeVal"',
                    'id="bulk-series"', 'x-model="bulkTagVal"'):
        assert control in html
