"""Route/UI regressions for hierarchical physical locations (#98)."""

import re
from html import unescape

from app.services import locations as location_svc


def _select_block(html: str, select_id: str) -> str:
    """Slice out one <select id="..."> ... </select> block.

    A bare substring check over the whole page passes for the wrong reason —
    the row headers print every full path (G69) — so assertions about a
    single select's offered options must be scoped to that select.
    """
    node_id = int(select_id.removeprefix("location-parent-"))
    form = _form_block(html, f"/api/location-tree/{node_id}/update")
    match = re.search(r'<select name="parent_id"[^>]*>.*?</select>', form, re.DOTALL)
    assert match, f'no <select id="{select_id}"> found'
    return match.group(0)


def _form_block(html: str, action: str) -> str:
    """Slice out one <form action="..." method="post"> ... </form> block."""
    match = re.search(
        rf'<form action="{re.escape(action)}" method="post".*?</form>', html, re.DOTALL
    )
    assert match, f'no <form action="{action}"> found'
    return match.group(0)


def _display(db, node_id):
    return next(row for row in location_svc.location_tree(db) if row["id"] == node_id)


def _breadcrumb(html):
    match = re.search(r'<nav[^>]*aria-label="Breadcrumb"[^>]*>(.*?)</nav>', html, re.S)
    assert match, "the selected node must show its unambiguous ancestor path"
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", match[1])).split())


def test_create_location_under_parent_builds_full_path(admin_client, db):
    parent = location_svc.create_location(db, "Living Room")
    db.commit()

    response = admin_client.post(
        "/api/location-tree",
        data={"name": "Shelf 1", "parent_id": str(parent)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = db.execute(
        "SELECT id, name, parent_id FROM location_nodes WHERE name = 'Shelf 1'"
    ).fetchone()
    assert response.headers["location"] == f"/locations/{row['id']}"
    assert (row["name"], row["parent_id"]) == ("Shelf 1", parent)
    assert _display(db, row["id"])["name"] == "Living Room / Shelf 1"


def test_same_child_name_can_exist_under_two_parents(admin_client, db):
    living = location_svc.create_location(db, "Living Room")
    bedroom = location_svc.create_location(db, "Bedroom")
    db.commit()

    for parent in (living, bedroom):
        response = admin_client.post(
            "/api/location-tree",
            data={"name": "Shelf 1", "parent_id": str(parent)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        child = db.execute("SELECT id FROM location_nodes WHERE name = 'Shelf 1' AND parent_id = ?",
                           (parent,)).fetchone()
        assert child is not None
        assert response.headers["location"] == f"/locations/{child['id']}"

    assert [
        row["name"] for row in sorted(location_svc.location_tree(db), key=lambda r: r["name"])
        if row["label"] == "Shelf 1"
    ] == ["Bedroom / Shelf 1", "Living Room / Shelf 1"]


def test_duplicate_child_name_under_same_parent_is_settings_error(admin_client, db):
    parent = location_svc.create_location(db, "Living Room")
    location_svc.create_location(db, "Shelf 1", parent_id=parent)
    db.commit()

    response = admin_client.post(
        "/api/location-tree",
        data={"name": "sHeLf 1", "parent_id": str(parent)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/locations/{parent}?error=duplicate"


def test_reparent_route_rewrites_descendant_paths(admin_client, db):
    living = location_svc.create_location(db, "Living Room")
    bedroom = location_svc.create_location(db, "Bedroom")
    case = location_svc.create_location(db, "Bookcase", parent_id=living)
    shelf = location_svc.create_location(db, "Shelf 1", parent_id=case)
    db.commit()

    response = admin_client.post(
        f"/api/location-tree/{case}/update",
        data={"name": "Bookcase", "parent_id": str(bedroom)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _display(db, case)["name"] == (
        "Bedroom / Bookcase"
    )
    assert _display(db, shelf)["name"] == (
        "Bedroom / Bookcase / Shelf 1"
    )


def test_route_rejects_moving_parent_beneath_its_child(admin_client, db):
    room = location_svc.create_location(db, "Room")
    shelf = location_svc.create_location(db, "Shelf", parent_id=room)
    db.commit()

    response = admin_client.post(
        f"/api/location-tree/{room}/update",
        data={"name": "Room", "parent_id": str(shelf)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/locations/{room}?error=cycle"
    row = db.execute("SELECT name, parent_id FROM location_nodes WHERE id = ?", (room,)).fetchone()
    assert tuple(row) == ("Room", None)


def test_route_refuses_to_delete_parent_with_children(admin_client, db):
    room = location_svc.create_location(db, "Room")
    location_svc.create_location(db, "Shelf", parent_id=room)
    db.commit()

    response = admin_client.post(
        f"/api/location-tree/{room}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/locations/{room}?error=has_children"
    assert db.execute("SELECT 1 FROM location_nodes WHERE id = ?", (room,)).fetchone()


def test_settings_location_form_offers_parent_and_uses_node_label(admin_client, db):
    room = location_svc.create_location(db, "Living Room")
    shelf = location_svc.create_location(db, "Shelf 1", parent_id=room)
    db.commit()

    response = admin_client.get(f"/locations/{shelf}")

    assert response.status_code == 200
    assert 'name="parent_id"' in response.text
    form = _form_block(response.text, f"/api/location-tree/{shelf}/update")
    assert f'value="{room}" selected' in form
    assert f'data-location-node="{shelf}"' in response.text
    assert 'value="Shelf 1"' in form
    assert _breadcrumb(response.text) == "Living Room › Shelf 1"


def test_edit_parent_select_excludes_own_subtree(admin_client, db):
    office = location_svc.create_location(db, "Office")
    bookcase = location_svc.create_location(db, "Bookcase A", parent_id=office)
    shelf3 = location_svc.create_location(db, "Shelf 3", parent_id=bookcase)
    attic = location_svc.create_location(db, "Attic")
    db.commit()

    response = admin_client.get(f"/locations/{office}")

    assert response.status_code == 200
    html = response.text

    office_select = _select_block(html, f"location-parent-{office}")
    assert f'value="{office}"' not in office_select
    assert f'value="{bookcase}"' not in office_select
    assert f'value="{shelf3}"' not in office_select
    assert f'value="{attic}"' in office_select
    assert "Top level" in office_select

    shelf3_html = admin_client.get(f"/locations/{shelf3}").text
    shelf3_select = _select_block(shelf3_html, f"location-parent-{shelf3}")
    assert f'value="{shelf3}"' not in shelf3_select
    assert f'value="{office}"' in shelf3_select
    assert f'value="{bookcase}" selected' in shelf3_select


def test_edit_and_create_selects_word_options_the_same(admin_client, db):
    office = location_svc.create_location(db, "Office")
    bookcase = location_svc.create_location(db, "Bookcase A", parent_id=office)
    shelf3 = location_svc.create_location(db, "Shelf 3", parent_id=bookcase)
    db.commit()

    response = admin_client.get(f"/locations/{shelf3}")

    assert response.status_code == 200
    html = response.text

    # Fork node creation is scoped by the selected node, not the old flat
    # Settings parent picker. Its real hidden parent and breadcrumb must name
    # exactly the same full path as the edit select's parent option.
    create_html = admin_client.get(f"/locations/{bookcase}").text
    create_forms = re.findall(r'<form action="/api/location-tree".*?</form>', create_html, re.S)
    matching = [form for form in create_forms if f'name="parent_id" value="{bookcase}"' in form]
    assert len(matching) == 1
    assert _breadcrumb(create_html) == "Office › Bookcase A"

    edit_select = _select_block(html, f"location-parent-{shelf3}")
    assert "Office › Bookcase A" in edit_select


def test_hierarchy_error_banner_uses_fixed_copy(admin_client, db):
    room = location_svc.create_location(db, "Room")
    db.commit()
    response = admin_client.get(f"/locations/{room}?error=has_children")

    assert response.status_code == 200
    assert 'data-testid="location-error"' in response.text
    assert "Move or remove this location’s child locations first." in response.text
