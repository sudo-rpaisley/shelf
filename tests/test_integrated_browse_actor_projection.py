"""Private Browse state and navigation follow library authority, not global roles."""

import pytest

from tests.conftest import _create_user, _insert_item


def test_catalogue_navigation_follows_library_editor_and_bearer_caps(db):
    from app import nav
    from app.services import libraries

    editor = _create_user("local-editor", "password123", "Local Editor", "viewer")
    reader = _create_user("local-reader", "password123", "Local Reader", "editor")
    libraries.set_membership(db, 1, editor["id"], "editor")
    libraries.set_membership(db, 1, reader["id"], "viewer")
    db.commit()

    assert nav.can_edit_catalogue(editor) is True
    assert nav.can_edit_catalogue(reader) is False
    token = {**editor, "api_token_id": 3, "account_role": "admin"}
    assert nav.can_edit_catalogue(token) is False
    assert "scan" in {tab["key"] for tab in nav.visible_tabs(editor)}
    assert "scan" not in {tab["key"] for tab in nav.visible_tabs(reader)}
    assert "scan" not in {tab["key"] for tab in nav.visible_tabs(token)}
    assert "settings" not in {tab["key"] for tab in nav.visible_tabs(editor)}


def test_browse_counts_and_filters_keep_personal_wishes_separate_from_acquisition(db):
    from app import browse_filters
    from app.services import browse_counts, catalogue_security, libraries

    user = _create_user("count-reader", "password123", "Reader", "viewer")
    other = _create_user("count-other", "password123", "Other", "viewer")
    libraries.set_membership(db, 1, user["id"], "viewer")
    owned_wish = _insert_item(
        db, title="Owned personal wish", isbn=None, owned=1,
        reading_status="read", personal_user_id=user["id"],
        personal_state={"wishlist": 1, "reading_status": "reading"},
    )
    shared_wish = _insert_item(
        db, title="Acquisition only", isbn=None, owned=0, wishlisted=True,
        reading_status="read",
    )
    another_wish = _insert_item(
        db, title="Other person's wish", isbn=None, owned=0,
        reading_status="read", personal_user_id=other["id"],
        personal_state={"wishlist": 1, "reading_status": "read"},
    )

    token = catalogue_security.set_actor(user)
    try:
        catalogue_security.install_catalogue_views(db)
        values = browse_filters.values_from({})
        counts = browse_counts.filter_counts(db, values, 3)
        assert counts["owned_count"] == 1
        assert counts["wishlist_count"] == 1
        assert counts["neither_count"] == 2
        assert counts["reading_status_counts"] == {"reading": 1}
        values["owned"] = "0"
        where, params = browse_filters.build_where(values)
        assert [row[0] for row in db.execute(f"SELECT i.id FROM items_live i {where}", params)] == [owned_wish]
        values["owned"] = "none"
        where, params = browse_filters.build_where(values)
        assert {row[0] for row in db.execute(f"SELECT i.id FROM items_live i {where}", params)} == {shared_wish, another_wish}
    finally:
        catalogue_security.reset_actor(token)


@pytest.mark.parametrize("bridge_state", ["hidden", "trashed"])
def test_grouping_never_traverses_hidden_or_trashed_bridge_nodes(db, bridge_state):
    from app.services import browse_grouping, catalogue_security, libraries

    user = _create_user("graph-reader", "password123", "Reader", "viewer")
    libraries.set_membership(db, 1, user["id"], "viewer")
    hidden_library = libraries.create_library(db, "Private graph")
    first = _insert_item(db, title="Visible first", isbn=None)
    bridge = _insert_item(
        db, title="Hidden bridge", isbn=None,
        _library_id=hidden_library["id"] if bridge_state == "hidden" else 1,
        **({"deleted_at": "2026-01-01 00:00:00"} if bridge_state == "trashed" else {}),
    )
    last = _insert_item(db, title="Visible last", isbn=None)
    db.execute("INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)", (first, bridge))
    db.execute("INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)", (bridge, last))
    token = catalogue_security.set_actor(user)
    try:
        catalogue_security.install_catalogue_views(db)
        cte = browse_grouping._media_roots_cte()
        assert db.execute(f"{cte} SELECT COUNT(*) FROM media_roots").fetchone()[0] == 0
        assert {row[0] for row in db.execute("SELECT id FROM items_live")} == {first, last}
    finally:
        catalogue_security.reset_actor(token)


def test_barcode_physical_trash_lookup_keeps_visibility_and_private_status(db):
    from app.services import barcode_resolve, catalogue_security, item_copies, libraries

    user = _create_user("barcode-reader", "password123", "Reader", "viewer")
    libraries.set_membership(db, 1, user["id"], "viewer")
    hidden_library = libraries.create_library(db, "Private barcode")
    visible = _insert_item(
        db, title="Visible barcode", isbn=None, reading_status="read",
        personal_user_id=user["id"], personal_state={"reading_status": "reading"},
    )
    hidden = _insert_item(db, title="Hidden barcode", isbn=None, _library_id=hidden_library["id"])
    item_copies.insert_copy(db, {"item_id": visible, "copy_number": 1, "is_primary": 1,
                                "copy_barcode": "SHF-VISIBLE-TEST"})
    item_copies.insert_copy(db, {"item_id": hidden, "copy_number": 1, "is_primary": 1,
                                "copy_barcode": "SHF-HIDDEN-TEST"})
    db.commit()
    token = catalogue_security.set_actor(user)
    try:
        assert barcode_resolve.resolve("SHF-HIDDEN-TEST") is None
        match = barcode_resolve.resolve("SHF-VISIBLE-TEST")
        assert match["id"] == visible
        assert match["reading_status"] == "reading"
        assert match["title"] == "Visible barcode"
    finally:
        catalogue_security.reset_actor(token)
