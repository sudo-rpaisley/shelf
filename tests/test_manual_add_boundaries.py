"""Request-boundary regressions for manual catalogue adds."""


def _item_count(db, *, title=None):
    if title is None:
        return db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return db.execute("SELECT COUNT(*) FROM items WHERE title = ?", (title,)).fetchone()[0]


def test_manual_add_rejects_malformed_publish_year_without_inserting(editor_client, db):
    response = editor_client.post(
        "/api/items/manual",
        data={"title": "Bad Year Manual", "publish_year": "nineteen-eighty-four"},
    )

    assert response.status_code == 200
    assert "Invalid publish year" in response.text
    assert _item_count(db, title="Bad Year Manual") == 0


def test_manual_add_rejects_unknown_platform_without_inserting(editor_client, db):
    response = editor_client.post(
        "/api/items/manual",
        data={
            "title": "Bad Platform Manual",
            "media_type": "video_game",
            "platform": "not-a-real-platform",
        },
    )

    assert response.status_code == 200
    # The route's own check is gone; the funnel names the platform (#54).
    assert "Unknown game platform" in response.text
    assert "not-a-real-platform" in response.text
    assert _item_count(db, title="Bad Platform Manual") == 0


class TestTheRetiredKidsBookAlias:
    """`/api/items/manual` is the discriminating site for the input alias.

    Unlike `/api/scan`, this route runs no detection: the posted value goes
    straight to `_find_duplicate_item`, which keys on `media_type`. So a raw
    `kids_book` here would fail to match a row already stored as `book`,
    fall through to the insert, and only then hit the funnel — which
    canonicalises, and trips `UNIQUE(isbn, media_type)` instead of showing
    the duplicate card. That is exactly the shape G100 describes.
    """

    ISBN = "9780306406157"

    def test_a_kids_book_add_is_stored_as_a_book(self, editor_client, db):
        resp = editor_client.post(
            "/api/items/manual",
            data={"title": "Goodnight Moon", "media_type": "kids_book"},
        )
        assert resp.status_code == 200
        row = db.execute(
            "SELECT media_type FROM items WHERE title = ?", ("Goodnight Moon",)
        ).fetchone()
        assert row is not None, "the item must still be created"
        assert row["media_type"] == "book"

    def test_it_matches_an_existing_book_instead_of_colliding(
        self, editor_client, db
    ):
        from tests.conftest import _insert_item

        _insert_item(db, title="Already Here", isbn=self.ISBN, media_type="book")
        db.commit()

        resp = editor_client.post(
            "/api/items/manual",
            data={
                "title": "Goodnight Moon",
                "isbn": self.ISBN,
                "media_type": "kids_book",
            },
        )

        assert resp.status_code == 200
        assert db.execute(
            "SELECT COUNT(*) AS c FROM items WHERE isbn = ?", (self.ISBN,)
        ).fetchone()["c"] == 1, "no second row may be created"
        assert "Already Here" in resp.text, (
            "the duplicate card must name the row that already exists"
        )
