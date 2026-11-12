"""Digital films and TV are distinct catalog types, not physical DVD aliases."""

import re

import pytest

from app import config
from app.services import item_write, tags


CONNECTED_VIDEO_TYPES = ("digital_video", "tv_series")
TYPE_LABELS = {"digital_video": "Digital Video", "tv_series": "TV Series"}
CREATOR_LABELS = {"digital_video": "Director", "tv_series": "Creator"}


def _select(html, select_id):
    match = re.search(
        rf'<select\b[^>]*\bid="{re.escape(select_id)}"[^>]*>.*?</select>', html, re.S
    )
    assert match, f"no #{select_id} select rendered"
    return match.group(0)


def _options(select):
    return {
        value: " ".join(label.split())
        for value, label in re.findall(
            r'<option\b[^>]*\bvalue="([^"]*)"[^>]*>(.*?)</option>', select, re.S
        )
    }


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_connected_video_types_are_first_class_not_aliases(media_type):
    assert config.MEDIA_TYPES[media_type] == TYPE_LABELS[media_type]
    assert config.canonical_media_type(media_type) == media_type
    assert media_type not in config.MEDIA_TYPE_ALIASES
    assert media_type not in config.BOOK_MEDIA_TYPES
    assert media_type not in config.MUSIC_MEDIA_TYPES
    assert media_type not in config.PERIODICAL_MEDIA_TYPES


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_connected_video_types_have_watch_statuses(media_type):
    assert media_type in config.STATUS_MEDIA_TYPES
    assert config.STATUS_VERBS[media_type] is config.WATCH_LABELS
    labels = config.status_labels(media_type)
    assert labels is config.WATCH_LABELS
    assert labels.key == "watch"
    assert labels.label("want_to_read") == "Want to Watch"
    assert labels.label("reading") == "Watching"
    assert labels.label("read") == "Watched"
    assert labels.done_lower == "watched"


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_connected_video_types_have_creator_and_portrait_cover_semantics(media_type):
    assert config.creator_label(media_type) == CREATOR_LABELS[media_type]
    assert config.browse_creator_label(media_type) == CREATOR_LABELS[media_type]
    assert config.cover_shape(media_type) == "portrait"
    assert media_type not in config.SQUARE_COVER_MEDIA_TYPES


def test_physical_dvd_semantics_remain_unchanged():
    assert config.MEDIA_TYPES["dvd"] == "DVD / Blu-ray"
    assert config.creator_label("dvd") == "Director"
    assert config.status_labels("dvd") is config.WATCH_LABELS
    assert config.cover_shape("dvd") == "portrait"
    assert config.TAG_SUGGESTIONS["dvd"] == [
        "Movie", "TV series", "Kids", "Documentary", "Blu-ray", "4K",
    ]


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_connected_video_tag_starters_follow_existing_scope_rules(db, media_type):
    starters = tags.suggestions_for(db, media_type)
    names = {row["name"] for row in starters}
    assert names == set(config.TAG_SUGGESTIONS[media_type])
    assert {"Kids", "Documentary"} <= names
    assert ("Movie" if media_type == "digital_video" else "TV series") in names
    assert not {"DVD", "Blu-ray", "Sealed"} & names
    assert all(row["starter"] for row in starters)
    assert {
        row["name"] for row in tags.suggestion_payload(db)
        if row["media_type"] == media_type and row["starter"]
    } == names

    tags.get_or_create_tag(db, "My watchlist", media_type=media_type)
    assert tags.suggestions_for(db, media_type) == [
        {"name": "My watchlist", "starter": False},
    ]
    other_type = next(value for value in CONNECTED_VIDEO_TYPES if value != media_type)
    assert any(row["starter"] for row in tags.suggestions_for(db, other_type))


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_item_write_insert_keeps_connected_type_and_manual_defaults(db, media_type):
    item_id = item_write.insert_item(
        db, title="Connected video", authors="A Creator", media_type=media_type,
    )
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert row["media_type"] == media_type
    assert row["authors"] == "A Creator"
    assert row["source"] == "manual"
    assert row["owned"] == 1
    assert row["isbn"] is None
    assert row["upc"] is None
    assert row["reading_status"] is None


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
@pytest.mark.parametrize("status", ["want_to_read", "reading", "read"])
def test_item_write_applies_default_watch_status(db, media_type, status):
    with item_write.default_reading_status(status):
        item_id = item_write.insert_item(db, title="Watching", media_type=media_type)
    row = db.execute(
        "SELECT media_type, reading_status FROM items_live WHERE id = ?", (item_id,),
    ).fetchone()
    assert tuple(row) == (media_type, status)


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_item_write_accepts_connected_type_in_single_and_bulk_updates(db, media_type):
    first = item_write.insert_item(db, title="First")
    second = item_write.insert_item(db, title="Second")
    item_write.update_item_fields(db, first, {"media_type": media_type})
    assert db.execute(
        "SELECT media_type FROM items_live WHERE id = ?", (first,),
    ).fetchone()["media_type"] == media_type
    item_write.update_items_fields(
        db, [first, second], {"media_type": media_type, "reading_status": "read"},
    )
    rows = db.execute(
        "SELECT media_type, reading_status FROM items_live ORDER BY id",
    ).fetchall()
    assert [tuple(row) for row in rows] == [(media_type, "read"), (media_type, "read")]


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_manual_add_preserves_connected_type_and_watch_status(editor_client, db, media_type):
    response = editor_client.post("/api/items/manual", data={
        "title": "Manual connected video", "authors": "A Creator",
        "media_type": media_type, "reading_status": "reading",
    })
    assert response.status_code == 200
    assert 'data-scan-status="added"' in response.text
    assert TYPE_LABELS[media_type] in response.text
    row = db.execute(
        "SELECT media_type, authors, source, reading_status FROM items_live "
        "WHERE title = 'Manual connected video'",
    ).fetchone()
    assert tuple(row) == (media_type, "A Creator", "manual", "reading")


def test_manual_form_offers_both_connected_types(editor_client):
    response = editor_client.get("/scan?add=manual")
    assert response.status_code == 200
    options = _options(_select(response.text, "manual-media-type"))
    for media_type in CONNECTED_VIDEO_TYPES:
        assert options[media_type] == TYPE_LABELS[media_type]


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_item_detail_offers_watch_status_before_one_is_set(viewer_client, db, media_type):
    item_id = item_write.insert_item(db, title="Unwatched video", media_type=media_type)
    db.commit()  # The request uses a separate connection (G48).
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    match = re.search(
        r'<div id="reading-status-section">.*?</div>', response.text, re.S,
    )
    assert match, "no status control for a connected video with no status set"
    for label in ("Want to Watch", "Watching", "Watched"):
        assert re.search(rf'>\s*{label}\s*</button>', match.group(0))


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_edit_accepts_connected_type_and_renders_its_labels(editor_client, db, media_type):
    item_id = item_write.insert_item(db, title="Editable video")
    db.commit()  # The request uses a separate connection (G48).
    response = editor_client.post(f"/api/items/{item_id}", data={
        "media_type": media_type, "authors": "A Creator", "reading_status": "read",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{item_id}"
    row = db.execute(
        "SELECT media_type, reading_status FROM items_live WHERE id = ?", (item_id,),
    ).fetchone()
    assert tuple(row) == (media_type, "read")

    edit = editor_client.get(f"/item/{item_id}/edit")
    assert edit.status_code == 200
    type_select = _select(edit.text, "media_type")
    assert _options(type_select)[media_type] == TYPE_LABELS[media_type]
    assert re.search(rf'<option value="{media_type}"\s+selected\b', type_select)
    assert re.search(
        rf'<label\b[^>]*for="authors"[^>]*>\s*{CREATOR_LABELS[media_type]}\s*</label>',
        edit.text,
    )
    assert _options(_select(edit.text, "reading_status"))["read"] == "Watched"


def test_generic_csv_import_preserves_digital_tv_and_physical_identities(admin_client, db):
    content = (
        "title,authors,media_type\n"
        "Same video,Same creator,dvd\n"
        "Same video,Same creator,digital_video\n"
        "Same video,Same creator,tv_series\n"
    )
    response = admin_client.post("/api/import/csv", files={
        "file": ("connected.csv", content.encode(), "text/csv"),
    })
    assert response.status_code == 200
    assert response.json()["imported"] == 3
    assert response.json()["errors"] == []
    rows = db.execute(
        "SELECT media_type FROM items_live WHERE title = 'Same video'",
    ).fetchall()
    assert {row["media_type"] for row in rows} == {"dvd", *CONNECTED_VIDEO_TYPES}

    repeated = admin_client.post("/api/import/csv", files={
        "file": ("connected.csv", content.encode(), "text/csv"),
    })
    assert repeated.status_code == 200
    assert repeated.json()["imported"] == 0
    assert repeated.json()["skipped"] == 3
    assert repeated.json()["errors"] == []


@pytest.mark.parametrize("media_type", CONNECTED_VIDEO_TYPES)
def test_browse_and_search_filter_connected_types_without_dvd_conflation(
    viewer_client, db, media_type,
):
    for value in ("dvd", *CONNECTED_VIDEO_TYPES):
        item_write.insert_item(
            db, title=f"Catalog {value}", media_type=value, reading_status="reading",
        )
    db.commit()  # The request uses a separate connection (G48).

    for path in ("/browse", "/api/search"):
        response = viewer_client.get(path, params={
            "media_type_filter": media_type, "reading_status": "reading", "view": "list",
        })
        assert response.status_code == 200
        assert f"Catalog {media_type}" in response.text
        assert "Catalog dvd" not in response.text
        other_type = next(value for value in CONNECTED_VIDEO_TYPES if value != media_type)
        assert f"Catalog {other_type}" not in response.text
        options = _options(_select(response.text, "reading-status-filter"))
        assert options["want_to_read"].startswith("Want to Watch")
        assert options["reading"].startswith("Watching")
        assert options["read"].startswith("Watched")
        assert re.search(
            rf'<option\b[^>]*id="sort-option-author"[^>]*>\s*{CREATOR_LABELS[media_type]}\s*</option>',
            response.text,
        )
