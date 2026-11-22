"""Browse's Status filter labels follow the Type filter.

Both status `<select>`s — the static one in `browse.html` and the OOB refresh
in `fragments/filter_counts_oob.html` — call `status_labels(active_type or
none)`. A single type picked shows that type's verb set; no type (or a mix)
shows the neutral set. Assertions are scoped to the `#reading-status-filter`
element, never the bare page, since a word like "Read" can appear elsewhere.
"""

import re

from tests.conftest import _insert_item


def _status_select(html: str) -> str:
    m = re.search(r'<select id="reading-status-filter".*?</select>', html, re.S)
    assert m, "no #reading-status-filter select rendered"
    return m.group(0)


def _status_options(html: str) -> dict[str, str]:
    """value -> visible label for the reading-status-filter select."""
    select = _status_select(html)
    return {
        value: " ".join(label.split())
        for value, label in re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', select, re.S)
    }


class TestBrowsePageStatusOptions:
    def test_dvd_type_shows_watch_words(self, viewer_client):
        html = viewer_client.get("/browse?media_type_filter=dvd").text
        options = _status_options(html)
        assert options["want_to_read"].startswith("Want to Watch")
        assert options["reading"].startswith("Watching")
        assert options["read"].startswith("Watched")

    def test_video_game_type_shows_play_words(self, viewer_client):
        html = viewer_client.get("/browse?media_type_filter=video_game").text
        options = _status_options(html)
        assert options["want_to_read"].startswith("Want to Play")
        assert options["reading"].startswith("Playing")
        assert options["read"].startswith("Played")

    def test_no_type_shows_neutral_words(self, viewer_client):
        html = viewer_client.get("/browse").text
        select = _status_select(html)
        options = _status_options(html)
        assert options["want_to_read"].startswith("Want to")
        assert options["reading"].startswith("In progress")
        assert options["read"].startswith("Finished")
        assert "Want to Read" not in select


class TestSearchOobStatusOptions:
    def test_video_game_type_shows_play_words_in_oob_swap(self, viewer_client):
        html = viewer_client.get("/api/search?media_type_filter=video_game").text
        select = _status_select(html)
        assert 'hx-swap-oob="true"' in select
        options = _status_options(html)
        assert options["reading"].startswith("Playing")

    def test_dvd_type_shows_watch_words_in_oob_swap(self, viewer_client):
        html = viewer_client.get("/api/search?media_type_filter=dvd").text
        options = _status_options(html)
        assert options["want_to_read"].startswith("Want to Watch")
        assert options["read"].startswith("Watched")


class TestStatusFilterSpansTypes:
    def test_reading_status_read_returns_every_type(self, admin_client, admin_user, db):
        _insert_item(
            db, title="Finished Book", isbn="9780000000501", media_type="book",
            reading_status="read", date_finished="2026-01-01", personal_user_id=admin_user["id"],
        )
        _insert_item(
            db, title="Finished Dvd", isbn=None, upc="000000000501", media_type="dvd",
            reading_status="read", date_finished="2026-01-02", personal_user_id=admin_user["id"],
        )
        _insert_item(
            db, title="Finished Game", isbn=None, upc="000000000502", media_type="video_game",
            reading_status="read", date_finished="2026-01-03", personal_user_id=admin_user["id"],
        )
        db.commit()  # G48

        html = admin_client.get("/api/search?reading_status=read").text
        assert "Finished Book" in html
        assert "Finished Dvd" in html
        assert "Finished Game" in html


class TestBulkEditStatusOptions:
    def test_bulk_edit_select_shows_neutral_words(self, viewer_client, editor_user):
        # Keep the original client parameter, but use a real catalogue editor
        # for the positive. A viewer's absence is pinned separately below.
        from app.auth import create_token

        viewer_client.cookies.set("access_token", create_token(
            editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"],
        ))
        html = viewer_client.get("/browse").text
        m = re.search(r'x-model="bulkStatusVal".*?</select>', html, re.S)
        assert m, "no bulk-edit status select rendered"
        select = m.group(0)
        options = dict(re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select))
        assert options["want_to_read"] == "Want to"
        assert options["reading"] == "In progress"
        assert options["read"] == "Finished"

    def test_viewer_has_no_bulk_edit_status_select(self, viewer_client):
        response = viewer_client.get("/browse")
        assert response.status_code == 200
        assert 'x-model="bulkStatusVal"' not in response.text
