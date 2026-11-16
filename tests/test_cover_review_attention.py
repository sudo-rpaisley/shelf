"""Needs Attention and human cover review agree without automatic repair."""

from unittest.mock import AsyncMock

import pytest

from app.services import item_write, library_attention


@pytest.fixture(autouse=True)
def no_cover_provider_io(monkeypatch):
    import httpx

    from app.routers import items_common
    from app.services import covers

    blocked = []
    for owner, name in (
        (covers, "search_covers"),
        (items_common, "resolve_missing_cover"),
        (items_common, "_search_isbn_for_item"),
        (httpx.AsyncClient, "request"),
    ):
        mock = AsyncMock(side_effect=AssertionError(f"Unexpected cover I/O: {name}"))
        monkeypatch.setattr(owner, name, mock)
        blocked.append(mock)
    yield
    # Record calls too: a swallowed exception must not make this pin vacuous.
    for mock in blocked:
        mock.assert_not_called()


@pytest.mark.parametrize("cover_path", [None, "", "   "], ids=["null", "empty", "spaces"])
@pytest.mark.parametrize("media_type", ["book", "dvd", "video_game", "cd"])
def test_missing_cover_from_attention_is_reviewable(
    editor_client, db, cover_path, media_type
):
    item_id = item_write.insert_item(
        db, title="Needs a human cover decision", media_type=media_type,
        cover_path=cover_path,
    )
    db.commit()
    before = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())

    context = library_attention.summary(db, category="covers")
    assert context["counts"]["covers"] == 1
    assert [row["item_id"] for row in context["queues"]["covers"]] == [item_id]

    attention = editor_client.get("/needs-attention?category=covers")
    assert attention.status_code == 200
    assert attention.context["counts"]["covers"] == 1
    assert [row["item_id"] for row in attention.context["queues"]["covers"]] == [item_id]
    assert f'href="/item/{item_id}"' in attention.text
    assert 'href="/cover-review"' in attention.text

    review = editor_client.get("/cover-review")
    assert review.status_code == 200
    assert review.context["total"] == 1
    assert review.context["item"]["id"] == item_id
    assert review.context["item"]["cover_path"] == cover_path
    assert f'data-item-id="{item_id}"' in review.text
    assert "1 of 1" in review.text

    skipped = editor_client.get(
        f"/api/covers/review/next?after={item_id}&pos=1&total=1"
    )
    assert skipped.status_code == 200
    assert skipped.context["remaining"] == 1
    assert 'data-testid="cover-review-exhausted"' in skipped.text
    assert 'data-testid="cover-review-empty"' not in skipped.text
    after = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())
    assert after == before, "Viewing and skipping must not repair or normalize stored data"


def test_covered_dismissed_and_trashed_items_stay_out_of_both_queues(editor_client, db):
    for index, cover_path in enumerate((None, "", "   ")):
        item_write.insert_item(
            db, title=f"Dismissed {index}", media_type="dvd",
            cover_path=cover_path, cover_review_dismissed=1,
        )
        trashed = item_write.insert_item(
            db, title=f"Trashed {index}", cover_path=cover_path,
        )
        assert item_write.trash_item(db, trashed)
    for index, cover_path in enumerate(("covers/kept.jpg", "  covers/kept.jpg  ")):
        item_write.insert_item(db, title=f"Covered {index}", cover_path=cover_path)
    db.commit()

    context = library_attention.summary(db, category="covers")
    assert context["counts"]["covers"] == 0
    assert context["queues"]["covers"] == []
    attention = editor_client.get("/needs-attention?category=covers")
    assert attention.status_code == 200
    assert attention.context["counts"]["covers"] == 0
    assert attention.context["queues"]["covers"] == []

    review = editor_client.get("/cover-review")
    assert review.status_code == 200
    assert review.context["total"] == 0
    assert review.context["item"] is None
    assert 'data-testid="cover-review-empty"' in review.text
    skipped = editor_client.get("/api/covers/review/next")
    assert skipped.status_code == 200
    assert skipped.context["remaining"] == 0
    assert 'data-testid="cover-review-empty"' in skipped.text


def test_mixed_missing_paths_keep_all_media_and_newest_first_keyset_order(editor_client, db):
    oldest = item_write.insert_item(
        db, title="Alpha wishlist disc", media_type="cd", cover_path="",
        owned=0, wishlisted=True, updated_at="2026-01-01",
    )
    book = item_write.insert_item(
        db, title="Zulu book", cover_path=None, updated_at="2026-01-02",
    )
    disc = item_write.insert_item(
        db, title="Beta film", media_type="dvd", cover_path="", updated_at="2026-01-03",
    )
    game = item_write.insert_item(
        db, title="Gamma game", media_type="video_game", cover_path="   ",
        updated_at="2026-01-03",
    )
    db.commit()
    before = [dict(row) for row in db.execute("SELECT * FROM items_live ORDER BY id")]
    expected = [game, disc, book, oldest]  # Equal timestamps break ties by descending id.
    context = library_attention.summary(db, category="covers")
    assert context["counts"]["covers"] == len(expected)
    assert {row["item_id"] for row in context["queues"]["covers"]} == set(expected)

    response = editor_client.get("/cover-review")
    assert response.context["total"] == len(expected)
    for pos, item_id in enumerate(expected, start=1):
        assert response.status_code == 200
        assert response.context["item"]["id"] == item_id
        assert f'data-item-id="{item_id}"' in response.text
        assert f"{pos} of {len(expected)}" in response.text
        response = editor_client.get(
            f"/api/covers/review/next?after={item_id}&pos={pos}&total={len(expected)}"
        )
    assert response.status_code == 200
    assert response.context["remaining"] == len(expected)
    assert 'data-testid="cover-review-exhausted"' in response.text
    assert 'data-testid="cover-review-empty"' not in response.text
    assert editor_client.get("/cover-review").context["item"]["id"] == game
    assert [dict(row) for row in db.execute("SELECT * FROM items_live ORDER BY id")] == before


@pytest.mark.parametrize("cover_path", ["", "   "], ids=["empty", "spaces"])
def test_dismissing_a_blank_cover_advances_and_updates_both_queues(editor_client, db, cover_path):
    first = item_write.insert_item(
        db, title="First decision", cover_path=cover_path, updated_at="2026-01-02",
    )
    second = item_write.insert_item(
        db, title="Second decision", cover_path=cover_path, updated_at="2026-01-01",
    )
    db.commit()
    initial = editor_client.get("/cover-review")
    assert initial.status_code == 200
    assert initial.context["total"] == 2
    assert initial.context["item"]["id"] == first

    dismissed = editor_client.post(
        f"/api/covers/review/{first}/dismiss", data={"pos": "1", "total": "2"},
    )
    assert dismissed.status_code == 200
    assert dismissed.context["item"]["id"] == second
    assert "2 of 2" in dismissed.text
    row = db.execute(
        "SELECT cover_path, cover_review_dismissed FROM items_live WHERE id = ?", (first,),
    ).fetchone()
    assert row["cover_path"] == cover_path
    assert row["cover_review_dismissed"] == 1
    attention = editor_client.get("/needs-attention?category=covers")
    assert attention.status_code == 200
    assert attention.context["counts"]["covers"] == 1
    assert [row["item_id"] for row in attention.context["queues"]["covers"]] == [second]
    review = editor_client.get("/cover-review")
    assert review.status_code == 200
    assert review.context["total"] == 1
    assert review.context["item"]["id"] == second


@pytest.mark.parametrize("cover_path", [None, "", "   "], ids=["null", "empty", "spaces"])
@pytest.mark.parametrize("surface", ["initial", "swapped"])
def test_missing_cover_picker_has_no_current_image_or_remove_control(
    editor_client, db, cover_path, surface
):
    first = item_write.insert_item(
        db, title="Initial missing cover", cover_path=cover_path, updated_at="2026-01-02",
    )
    second = item_write.insert_item(
        db, title="Next missing cover", cover_path=cover_path, updated_at="2026-01-01",
    )
    db.commit()
    before = [dict(row) for row in db.execute("SELECT * FROM items_live ORDER BY id")]

    response = editor_client.get("/cover-review")
    expected_id = first
    if surface == "swapped":
        response = editor_client.get(
            f"/api/covers/review/next?after={first}&pos=1&total=2"
        )
        expected_id = second
    assert response.status_code == 200
    assert response.context["item"]["id"] == expected_id
    assert response.context["item"]["cover_path"] == cover_path
    assert f'id="cover-picker-{expected_id}"' in response.text
    assert 'data-testid="cover-upload"' in response.text
    assert "No cover" in response.text
    assert 'data-testid="current-cover"' not in response.text
    assert 'alt="Current cover"' not in response.text
    assert f'src="/{cover_path or ""}"' not in response.text
    assert 'data-testid="cover-remove"' not in response.text
    assert [dict(row) for row in db.execute("SELECT * FROM items_live ORDER BY id")] == before


@pytest.mark.parametrize(
    "cover_path",
    ["covers/kept.jpg", "  covers/kept.jpg  ", "\t", "\u00a0"],
    ids=["real", "padded-real", "tab-not-sqlite-space", "nbsp-not-sqlite-space"],
)
def test_nonmissing_paths_stay_out_of_queue_and_keep_existing_picker_preview(
    editor_client, db, cover_path
):
    from app.routers import cover_review

    item_id = item_write.insert_item(db, title="Existing cover value", cover_path=cover_path)
    db.commit()
    before = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())
    assert library_attention.summary(db, category="covers")["counts"]["covers"] == 0
    page = editor_client.get("/cover-review")
    assert page.status_code == 200
    assert page.context["total"] == 0
    assert page.context["item"] is None

    # A write failure can render an existing item directly. Only SQLite-space-
    # empty paths lose their display value; a nonmissing preview stays verbatim.
    item = db.execute(
        f"SELECT {cover_review._QUEUE_COLUMNS} FROM items_live i WHERE i.id = ?", (item_id,),
    ).fetchone()
    preview = cover_review.render_card(page.context["request"], db, item, pos=1, total=1)
    body = preview.body.decode()
    assert preview.status_code == 200
    assert preview.context["cover_path"] == cover_path
    assert 'data-testid="current-cover"' in body
    assert f'src="/{cover_path}"' in body
    assert 'alt="Current cover"' in body
    assert 'data-testid="cover-remove"' in body
    after = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())
    assert after == before
