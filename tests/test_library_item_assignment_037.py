"""New catalogue rows must enter a Shelf security library atomically."""

import pytest

from app.services import libraries
from app.services.item_write import insert_item


def test_insert_item_defaults_to_main_library(db):
    item_id = insert_item(db, title="New Main Library Item", media_type="book")

    assert libraries.item_library_id(db, item_id) == libraries.DEFAULT_LIBRARY_ID


def test_insert_item_accepts_explicit_library_target(db):
    other = libraries.create_library(db, "Other Library")

    item_id = insert_item(
        db,
        title="Explicit Library Item",
        media_type="book",
        library_id=other["id"],
    )

    assert libraries.item_library_id(db, item_id) == other["id"]


def test_invalid_explicit_library_target_inserts_nothing(db):
    before = db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]

    with pytest.raises(LookupError, match="Library not found"):
        insert_item(
            db,
            title="Must Not Become Orphaned",
            media_type="book",
            library_id=999999,
        )

    after = db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    assert after == before


def test_unknown_pseudo_field_is_still_rejected(db):
    with pytest.raises(ValueError, match="not on the items table"):
        insert_item(db, title="Strict Funnel", media_type="book", library_target=1)
