"""A disc's or game's status stays in Shelf, even when the row still carries
Hardcover IDs from before it was retyped out of the book family."""
import asyncio
from unittest.mock import AsyncMock, patch

from app.database import get_db
from app.services import hardcover as hardcover_service
from tests.conftest import _insert_item


def test_a_book_retyped_to_dvd_never_pushes_its_status_to_hardcover(admin_client, db):
    item_id = _insert_item(
        db, title="Linked Book Becomes DVD", isbn="9780000000910", media_type="book",
        hardcover_book_id=91, hardcover_user_book_id=92,
    )
    db.commit()

    changed = admin_client.post(
        f"/api/items/{item_id}", data={"media_type": "dvd"}, follow_redirects=False,
    )
    assert changed.status_code == 303, changed.text
    with get_db() as check_db:
        row = check_db.execute(
            "SELECT media_type, hardcover_user_book_id FROM items WHERE id = ?", (item_id,),
        ).fetchone()
    assert (row["media_type"], row["hardcover_user_book_id"]) == ("dvd", 92)

    with patch("app.routers.items._push_status_to_hardcover", new=AsyncMock()) as push:
        resp = admin_client.post(
            f"/api/items/{item_id}/reading-status", data={"status": "read"},
        )

    assert resp.status_code == 200
    assert "Status: Watched" in resp.headers["HX-Trigger"]
    assert push.call_count == 0


def test_a_linked_book_still_pushes_its_status(admin_client, admin_user, db):
    item_id = _insert_item(
        db, title="Linked Book", isbn="9780000000927", media_type="book",
        hardcover_book_id=93, hardcover_user_book_id=94,
    )
    db.execute("INSERT INTO settings (key, value) VALUES ('hardcover_token', 'local-test-token')")
    db.execute("INSERT INTO settings (key, value) VALUES (?, '94')",
               (f"hardcover_user_book:{admin_user['id']}:{item_id}",))
    db.commit()

    with patch("app.routers.items._push_status_to_hardcover", new=AsyncMock()) as push:
        admin_client.post(f"/api/items/{item_id}/reading-status", data={"status": "read"})

    assert push.call_count == 1


def test_the_hardcover_pull_never_overwrites_a_retyped_dvd(db, monkeypatch, viewer_user):
    dvd_id = _insert_item(
        db, title="Retyped DVD", isbn="9780000000934", media_type="dvd",
        hardcover_book_id=42, reading_status="want_to_read",
        personal_user_id=viewer_user["id"],
    )
    book_id = _insert_item(
        db, title="Linked Book", isbn="9780000000941", media_type="book",
        hardcover_book_id=43, reading_status="want_to_read",
        personal_user_id=viewer_user["id"],
    )
    db.commit()
    monkeypatch.setattr(hardcover_service, "get_user_id", AsyncMock(return_value=7))
    monkeypatch.setattr(
        hardcover_service, "get_user_books",
        AsyncMock(return_value=[
            {"hardcover_book_id": 42, "reading_status": "read"},
            {"hardcover_book_id": 43, "reading_status": "read"},
        ]),
    )

    result = asyncio.run(hardcover_service.sync_reading_statuses("token"))

    with get_db() as check_db:
        status = {
            r["id"]: r["reading_status"]
            for r in check_db.execute(
                "SELECT item_id AS id, reading_status FROM user_item_state WHERE user_id = ?",
                (viewer_user["id"],),
            ).fetchall()
        }
        assert {r[0] for r in check_db.execute("SELECT reading_status FROM items")} == {"want_to_read"}
    assert status[dvd_id] == "want_to_read"
    assert status[book_id] == "read"
    assert result["updated"] == 1
