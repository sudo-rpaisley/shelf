"""Both-parent fixture composition: no implicit authority or personal seeding."""

import pytest

from tests.conftest import (
    _create_user, _insert_item, _insert_location, _insert_node_location, _legacy_location,
)


def test_raw_user_is_a_real_nonmember(db):
    user = _create_user("nonmember", "password123", "Nonmember", "editor")
    assert db.execute("SELECT 1 FROM library_memberships WHERE user_id = ?",
                      (user["id"],)).fetchone() is None


def test_owned_zero_is_neither_not_a_shared_or_personal_wish(db, admin_user):
    item = _insert_item(db, owned=0)
    assert db.execute("SELECT 1 FROM list_items WHERE item_id = ?", (item,)).fetchone() is None
    assert db.execute("SELECT 1 FROM user_item_state WHERE item_id = ?", (item,)).fetchone() is None


def test_explicit_personal_wish_can_coexist_with_owned_without_shared_membership(db, admin_user):
    item = _insert_item(db, owned=1, personal_user_id=admin_user["id"],
                        personal_state={"wishlist": 1})
    assert db.execute("SELECT wishlist FROM user_item_state WHERE user_id = ? AND item_id = ?",
                      (admin_user["id"], item)).fetchone()["wishlist"] == 1
    assert db.execute("SELECT 1 FROM list_items WHERE item_id = ?", (item,)).fetchone() is None


def test_personal_fixture_requires_an_actor(db):
    with pytest.raises(ValueError, match="explicit user id"):
        _insert_item(db, personal_state={"wishlist": 1})
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_later_user_does_not_inherit_a_named_users_state(db, admin_user):
    item = _insert_item(db, personal_user_id=admin_user["id"], reading_status="read")
    db.commit()
    later = _create_user("later", "password123", "Later", "viewer")
    assert db.execute("SELECT 1 FROM user_item_state WHERE user_id = ? AND item_id = ?",
                      (later["id"], item)).fetchone() is None


def test_location_fixture_deliberately_uses_different_node_and_flat_ids(db):
    node = _insert_node_location(db)
    assert _legacy_location(db, node) != node


def test_node_fixture_stays_unequal_after_an_unmapped_flat_seed(db):
    raw_flat = _insert_location(db, "Historical flat only")
    node = _insert_node_location(db, "Physical shelf")
    flat = _legacy_location(db, node)
    assert node != flat
    assert flat != raw_flat
    assert db.execute("SELECT legacy_location_id FROM location_nodes WHERE id = ?", (node,)).fetchone()[0] == flat
    assert db.execute("SELECT name FROM locations WHERE id = ?", (raw_flat,)).fetchone()[0] == "Historical flat only"
