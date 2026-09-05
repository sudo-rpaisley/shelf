"""Regression coverage for personal state carried by reading-tracker imports."""

import io

from app.services import user_state
from tests.conftest import _insert_item


GOODREADS_HEADER = (
    "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,"
    "Average Rating,Publisher,Binding,Number of Pages,Year Published,"
    "Original Publication Year,Date Read,Date Added,Bookshelves,"
    "Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,"
    "Private Notes,Read Count,Owned Copies"
)


def _goodreads_read_row() -> str:
    return (
        '123,Dune,Frank Herbert,"Herbert, Frank",,'
        '"=""0441013597""","=""9780441013593""",5,4.25,'
        'Imported Publisher,Paperback,412,2005,1965,2024/01/05,2023/01/02,'
        'favorites,favorites (#1),read,,,,1,1'
    )


def test_skip_duplicate_still_imports_acting_users_reading_state(
    admin_client, admin_user, db
):
    """Skip protects shared metadata, not the importing user's personal data."""
    item_id = _insert_item(
        db,
        title="Existing Dune",
        isbn="9780441013593",
        media_type="book",
        publisher="Keep This Publisher",
        owned=1,
        reading_status=None,
    )
    db.commit()

    csv_content = GOODREADS_HEADER + "\n" + _goodreads_read_row()
    response = admin_client.post(
        "/api/import/csv",
        files={"file": ("goodreads.csv", io.BytesIO(csv_content.encode()), "text/csv")},
        data={"mode": "skip"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["format"] == "goodreads"
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == []

    shared = db.execute(
        "SELECT title, publisher, owned, reading_status FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert shared["title"] == "Existing Dune"
    assert shared["publisher"] == "Keep This Publisher"
    assert shared["owned"] == 1
    assert shared["reading_status"] is None

    personal = user_state.get_state(db, admin_user["id"], item_id)
    assert personal["reading_status"] == "read"
    assert personal["date_finished"] == "2024-01-05"
    assert personal["wishlist"] == 0
