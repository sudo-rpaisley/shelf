"""Unrepresentable HTTP IDs refuse before SQLite binding, not as database faults."""

import pytest

from tests.conftest import _insert_item


class NoSql:
    def execute(self, *args, **kwargs):
        raise AssertionError("out-of-range ID reached SQLite")


@pytest.mark.parametrize("value", [2**63, 2**100])
def test_location_bounds_are_native_validation_errors_before_sql(value):
    from app.services.location_tree import validated_node_id
    from app.services.write_targets import UnknownLocationError, validated_location_id

    with pytest.raises(UnknownLocationError) as flat:
        validated_location_id(NoSql(), value)
    assert str(flat.value) == f"Location {value} not found"
    with pytest.raises(UnknownLocationError) as node:
        validated_node_id(NoSql(), value)
    assert str(node.value) == "Location not found"
    assert validated_location_id(NoSql(), -value) is None  # Existing FLAT sentinel.


@pytest.mark.parametrize("value", [-(2**100), 2**63, 2**100])
def test_unrepresentable_parent_and_library_authority_do_not_bind_sql(editor_user, value):
    from fastapi import HTTPException
    from app.services import catalogue_security

    token = catalogue_security.set_actor(editor_user)
    try:
        with pytest.raises(HTTPException) as parent:
            catalogue_security.require_item_role(NoSql(), value, "editor")
        assert parent.value.status_code == 404 and parent.value.detail == "Item not found"
        with pytest.raises(HTTPException) as library:
            catalogue_security.require_library_role(NoSql(), value, "editor")
        assert library.value.status_code == 404 and library.value.detail == "Library not found"
    finally:
        catalogue_security.reset_actor(token)


@pytest.mark.parametrize("value", [-(2**100), 2**63, 2**100])
def test_unrepresentable_detail_and_edit_gets_have_native_missing_destination(editor_client, value):
    for path in (f"/item/{value}", f"/item/{value}/edit"):
        response = editor_client.get(path, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/browse"


@pytest.mark.parametrize("target", ["item", "library"])
def test_explicit_recovery_refuses_unrepresentable_ids_without_effects(admin_client, db, target):
    item_id = _insert_item(db, title="Keep orphan untouched", isbn=None, _library_id=None)
    db.commit()
    before = list(db.iterdump())
    response = admin_client.post(f"/api/libraries/recovery/items/{2**63 if target == 'item' else item_id}",
                                 data={"library_id": 2**63 if target == "library" else 1})
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found" if target == "item" else "Library not found"}
    assert list(db.iterdump()) == before
