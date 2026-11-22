"""All-or-nothing shared series edits, including primary-only legacy rows."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import _insert_item


def _member(db, name, *, title="Visible series member", library_id=1, deleted_at=None, secondary=False):
    item_id = _insert_item(
        db, title=title, isbn=None, series_name=None if secondary else name,
        series_position=None if secondary else 1, _library_id=library_id, deleted_at=deleted_at,
    )
    if secondary:
        db.execute("INSERT INTO item_series (item_id, series_name, position, is_primary) VALUES (?, ?, 2, 0)", (item_id, name))
    return item_id


def _snapshot(db):
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("items", "item_series", "series_meta", "release_watches", "library_items", "user_item_state")
    }


def _mutate(client, operation):
    if operation == "check":
        return client.get("/api/series/check", params={"name": "Shared name"})
    if operation == "watch-all":
        return client.post("/api/series/watch-all")
    payloads = {
        "description": {"description": "Must not persist"}, "complete": {"complete": "1"},
        "watch": {"watch": "1"}, "unwatch": {"watch": "0"},
        "rename": {"new_name": "Renamed"}, "remove-all": {}, "fetch-description": {},
    }
    endpoint = "watch" if operation == "unwatch" else operation
    return client.post(f"/api/series/Shared%20name/{endpoint}", data=payloads[operation])


def test_primary_only_series_are_readable_without_lazy_membership_writes(editor_client, db):
    first = _member(db, "Legacy primary", title="First primary-only member")
    second = _member(db, "Legacy primary", title="Second primary-only member")
    db.commit()
    before = _snapshot(db)

    overview = editor_client.get("/series")
    detail = editor_client.get("/series/detail", params={"name": "Legacy primary"})
    panel = editor_client.get(f"/api/items/{first}/series-rows")

    assert overview.status_code == detail.status_code == panel.status_code == 200
    assert {item["id"] for item in overview.context["series_list"][0]["items"]} == {first, second}
    assert overview.context["unassigned_total"] == 0
    assert {item["id"] for item in detail.context["series"]["items"]} == {first, second}
    assert panel.context["series_memberships"] == [{"series_name": "Legacy primary", "position": 1.0, "is_primary": 1}]
    assert _snapshot(db) == before


def test_primary_only_rename_merges_and_preserves_secondary_memberships(editor_client, db):
    first = _member(db, "Original")
    second = _member(db, "Original")
    target = _member(db, "Destination")
    db.execute("INSERT INTO item_series (item_id, series_name, position, is_primary) VALUES (?, 'Secondary', 3, 0)", (first,))
    db.commit()

    response = editor_client.post("/api/series/Original/rename", data={"new_name": "Destination"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "name": "Destination", "merged": True, "count": 2}
    assert {row[0] for row in db.execute("SELECT id FROM items WHERE series_name = 'Destination'")} == {first, second, target}
    assert db.execute("SELECT COUNT(*) FROM item_series WHERE series_name = 'Original'").fetchone()[0] == 0
    assert db.execute("SELECT position FROM item_series WHERE item_id = ? AND series_name = 'Secondary'", (first,)).fetchone()[0] == 3


def test_primary_only_disband_promotes_existing_secondary_and_clears_the_other(editor_client, db):
    first = _member(db, "Original")
    second = _member(db, "Original")
    db.execute("INSERT INTO item_series (item_id, series_name, position, is_primary) VALUES (?, 'Secondary', 3, 0)", (first,))
    db.commit()

    response = editor_client.post("/api/series/Original/remove-all")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "count": 2}
    assert tuple(db.execute("SELECT series_name, series_position FROM items WHERE id = ?", (first,)).fetchone()) == ("Secondary", 3)
    assert tuple(db.execute("SELECT series_name, series_position FROM items WHERE id = ?", (second,)).fetchone()) == (None, None)
    assert db.execute("SELECT is_primary FROM item_series WHERE item_id = ?", (first,)).fetchone()[0] == 1


@pytest.mark.parametrize("operation", [
    "description", "complete", "watch", "unwatch", "rename", "remove-all", "fetch-description", "check", "watch-all",
])
@pytest.mark.parametrize("blocker", ["hidden", "trashed"])
@pytest.mark.parametrize("secondary", [False, True])
def test_every_shared_series_mutation_refuses_the_complete_unavailable_set(
    editor_client, db, operation, blocker, secondary,
):
    from app.database import set_setting
    from app.services import libraries

    private_library = libraries.create_library(db, "Private series library")
    _member(db, "Shared name")
    _member(
        db, "Shared name", title="Undisclosed member",
        library_id=private_library["id"] if blocker == "hidden" else 1,
        deleted_at="2026-01-01 00:00:00" if blocker == "trashed" else None,
        secondary=secondary,
    )
    db.execute("INSERT INTO series_meta (name, description, hc_missing) VALUES ('Shared name', 'Stored synopsis', 0)")
    db.execute("INSERT INTO release_watches (series_name) VALUES ('Shared name')")
    set_setting(db, "hardcover_token", "test-only-series-token")
    db.commit()
    before = _snapshot(db)
    books = AsyncMock(side_effect=AssertionError("Unavailable series must refuse before network"))
    description = AsyncMock(side_effect=AssertionError("Unavailable series must refuse before network"))
    with patch("app.services.hardcover.get_series_books", books), \
         patch("app.services.hardcover.get_series_description", description):
        response = _mutate(editor_client, operation)

    assert response.status_code == (404 if blocker == "hidden" else 409)
    assert "Undisclosed member" not in response.text
    assert "Private series library" not in response.text
    books.assert_not_awaited()
    description.assert_not_awaited()
    assert _snapshot(db) == before


@pytest.mark.parametrize("blocker", ["hidden", "trashed", "read-only"])
def test_rename_checks_the_destination_name_before_touching_source(editor_client, editor_user, db, blocker):
    from app.services import libraries

    _member(db, "Original")
    restricted = libraries.create_library(db, "Restricted destination")
    if blocker == "read-only":
        libraries.set_membership(db, restricted["id"], editor_user["id"], "viewer")
    _member(
        db, "Destination", library_id=1 if blocker == "trashed" else restricted["id"],
        deleted_at="2026-01-01 00:00:00" if blocker == "trashed" else None,
    )
    db.commit()
    before = _snapshot(db)

    response = editor_client.post("/api/series/Original/rename", data={"new_name": "Destination"})

    assert response.status_code == {"hidden": 404, "trashed": 409, "read-only": 403}[blocker]
    assert _snapshot(db) == before


def test_conflicting_destination_membership_rolls_back_the_entire_rename(editor_client, db):
    first = _member(db, "Original")
    last = _member(db, "Original")
    db.execute("INSERT INTO item_series (item_id, series_name, position, is_primary) VALUES (?, 'Destination', 9, 0)", (last,))
    db.commit()
    before = _snapshot(db)

    response = editor_client.post("/api/series/Original/rename", data={"new_name": "Destination"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Series memberships conflict"
    assert _snapshot(db) == before
    assert db.execute("SELECT series_name FROM items WHERE id = ?", (first,)).fetchone()[0] == "Original"


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_item_membership_mutations_cannot_attach_to_or_edit_hidden_series(editor_client, db, operation):
    from app.services import libraries

    item_id = _member(db, "Original")
    private_library = libraries.create_library(db, "Hidden membership target")
    _member(db, "Destination", library_id=private_library["id"])
    if operation == "remove":
        db.execute("INSERT INTO item_series (item_id, series_name, position, is_primary) VALUES (?, 'Destination', 2, 0)", (item_id,))
    db.commit()
    before = _snapshot(db)
    suffix = "/remove" if operation == "remove" else ""

    response = editor_client.post(
        f"/api/items/{item_id}/series-memberships{suffix}", data={"series_name": "Destination", "position": "2"},
    )

    assert response.status_code == 404
    assert _snapshot(db) == before


def test_network_cache_writeback_rechecks_new_hidden_members(editor_client, db):
    from app.database import get_db, set_setting
    from app.services import libraries

    _member(db, "Shared name")
    private_library = libraries.create_library(db, "Late hidden library")
    set_setting(db, "hardcover_token", "test-only-series-token")
    db.commit()

    async def add_hidden_member(*args):
        with get_db() as connection:
            _member(connection, "Shared name", library_id=private_library["id"])
        return [{"hardcover_book_id": 1, "title": "Provider title", "series_position": 1}]

    with patch("app.services.hardcover.get_series_books", new=AsyncMock(side_effect=add_hidden_member)):
        response = editor_client.get("/api/series/check", params={"name": "Shared name"})

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM series_meta").fetchone()[0] == 0


@pytest.mark.parametrize("surface", ["edit", "bulk", "manual"])
def test_core_item_series_edits_also_refuse_hidden_name_collisions(editor_client, db, surface):
    from app.services import libraries

    item_id = _member(db, "Original")
    restricted = libraries.create_library(db, "Hidden core edit target")
    _member(db, "Destination", library_id=restricted["id"])
    db.commit()
    before = _snapshot(db)

    if surface == "edit":
        response = editor_client.post(f"/api/items/{item_id}", data={"series_name": "Destination"})
    elif surface == "bulk":
        response = editor_client.post("/api/items/bulk-update", json={
            "item_ids": [item_id], "updates": {"series_name": "Destination"},
        })
    else:
        response = editor_client.post("/api/items/manual", data={"title": "New item", "series_name": "Destination"})

    assert response.status_code == 404
    assert _snapshot(db) == before


@pytest.mark.parametrize("blocker", ["hidden", "trashed"])
def test_continue_does_not_cross_a_primary_only_unavailable_member(editor_client, editor_user, db, blocker):
    from app.services import libraries, user_state

    private_library = libraries.create_library(db, "Private next volume")
    first = _member(db, "Continue primary")
    middle = _member(
        db, "Continue primary", library_id=private_library["id"] if blocker == "hidden" else 1,
        deleted_at="2026-01-01 00:00:00" if blocker == "trashed" else None,
    )
    last = _member(db, "Continue primary")
    db.execute("UPDATE items SET series_position = 2 WHERE id = ?", (middle,))
    db.execute("UPDATE items SET series_position = 3 WHERE id = ?", (last,))
    user_state.set_reading_status(db, editor_user["id"], first, "read")
    db.commit()
    before = _snapshot(db)

    response = editor_client.get("/api/home/continue")

    assert response.status_code == 200
    assert response.context["continue_items"] == []
    assert _snapshot(db) == before
