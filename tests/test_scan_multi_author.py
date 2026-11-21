"""Issue #117a — a real /api/scan add stores every author, not just the first.

Every other book-scan test patches `items_common._lookup_metadata` or
`openlibrary.lookup` wholesale, so none of them exercise the real Open
Library client's author-joining path end to end. That gap is why a
co-authored book's scan used to store only `authors[0]` (fixed in
`app/services/openlibrary.py` — `_author_keys`, `_resolve_authors`,
`MAX_AUTHORS`, joined via `authors.join_names`). These tests drive the real
client through the route, mocking only HTTP with respx, and assert the
STORED `items.authors` column (G45) — not the response text, not the call
count.
"""

from unittest.mock import patch

import httpx
import pytest
import respx

import app.config

_OL = "https://openlibrary.org"

# Distinct valid ISBN-13s (978-0 registration group -> no national provider,
# see app/services/national.py PREFIX_PROVIDERS), one per test so each test
# stands alone.
TWO_AUTHOR_ISBN = "9780132350884"
ONE_AUTHOR_ISBN = "9780321146533"
NO_AUTHOR_ISBN = "9780596007126"


@pytest.fixture(autouse=True)
def _unpaced_openlibrary(monkeypatch):
    """`outbound.acquire` really sleeps 0.34s per openlibrary.org request
    under test; zero it so these tests run at normal speed."""
    monkeypatch.setitem(app.config.HOST_RATE_LIMITS, "openlibrary.org", 0.0)


def _mock_edition(isbn, work_key="/works/OL117AW"):
    return respx.get(f"{_OL}/isbn/{isbn}.json").mock(return_value=httpx.Response(
        200,
        json={
            "title": "Refactoring in Company",
            "works": [{"key": work_key}],
        },
    ))


def _mock_work(work_key, author_keys, description="A book about working together."):
    return respx.get(f"{_OL}{work_key}.json").mock(return_value=httpx.Response(
        200,
        json={
            "description": description,
            "authors": [{"author": {"key": key}} for key in author_keys],
        },
    ))


def _mock_author(author_key, name):
    return respx.get(f"{_OL}{author_key}.json").mock(
        return_value=httpx.Response(200, json={"name": name})
    )


class TestScanStoresEveryAuthor:
    @respx.mock
    def test_two_authors_are_stored_comma_joined_in_order(self, admin_client, db):
        work_key = "/works/OL117AW"
        _mock_edition(TWO_AUTHOR_ISBN, work_key)
        _mock_work(work_key, ["/authors/OL1A", "/authors/OL2A"])
        _mock_author("/authors/OL1A", "Martin Fowler")
        _mock_author("/authors/OL2A", "Kent Beck")

        with patch("app.routers.items.cover_queue.enqueue"):
            response = admin_client.post(
                "/api/scan",
                data={"isbn": TWO_AUTHOR_ISBN, "media_type": "book", "mode": "add"},
            )

        assert response.status_code == 200
        row = db.execute(
            "SELECT authors FROM items WHERE isbn = ?", (TWO_AUTHOR_ISBN,)
        ).fetchone()
        assert row is not None
        assert row["authors"] == "Martin Fowler, Kent Beck"

    @respx.mock
    def test_one_author_is_stored_alone(self, admin_client, db):
        work_key = "/works/OL117BW"
        _mock_edition(ONE_AUTHOR_ISBN, work_key)
        _mock_work(work_key, ["/authors/OL1A"])
        _mock_author("/authors/OL1A", "Martin Fowler")

        with patch("app.routers.items.cover_queue.enqueue"):
            response = admin_client.post(
                "/api/scan",
                data={"isbn": ONE_AUTHOR_ISBN, "media_type": "book", "mode": "add"},
            )

        assert response.status_code == 200
        row = db.execute(
            "SELECT authors FROM items WHERE isbn = ?", (ONE_AUTHOR_ISBN,)
        ).fetchone()
        assert row is not None
        assert row["authors"] == "Martin Fowler"

    @respx.mock
    def test_no_authors_stores_null_not_empty_string(self, admin_client, db):
        work_key = "/works/OL117CW"
        _mock_edition(NO_AUTHOR_ISBN, work_key)
        _mock_work(work_key, [])

        with patch("app.routers.items.cover_queue.enqueue"):
            response = admin_client.post(
                "/api/scan",
                data={"isbn": NO_AUTHOR_ISBN, "media_type": "book", "mode": "add"},
            )

        assert response.status_code == 200
        row = db.execute(
            "SELECT authors FROM items WHERE isbn = ?", (NO_AUTHOR_ISBN,)
        ).fetchone()
        assert row is not None
        assert row["authors"] is None
