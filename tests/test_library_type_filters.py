"""Strict multi-format transport through the registered Library surfaces."""

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app import config, media_presentation
from app.services import item_copies, library_catalog
from tests.conftest import _create_user, _insert_item


BAD_CSV = (
    "all", "ALL", "family:books", "format:book", "kids_book", "BOOK", "unknown",
    " book", "book ", "book\n", "book\x00", "book, ebook", "book,", ",book", "book,,ebook",
    "book,unknown", "book') OR 1=1 --", 'book,<script>alert("types")</script>',
    "book&types=dvd", "%62ook", ",".join(["book"] * 33), "x" * 1025,
)
LIBRARY_PATHS = ("/library", "/api/library/search", "/api/library/items")


def _member_ids(text):
    return {int(value) for value in re.findall(r'data-library-member="(\d+)"', text)}


def _link(text, *, rel=None, label=None):
    for attrs, body in re.findall(r"<a\b([^>]*)>(.*?)</a>", text, re.S):
        if rel and f'rel="{rel}"' not in attrs:
            continue
        if label and html.unescape(re.sub(r"<[^>]+>", "", body)).strip() != label:
            continue
        return html.unescape(re.search(r'href="([^"]+)"', attrs).group(1))
    pytest.fail(f"Missing rendered link: rel={rel!r}, label={label!r}")


def _form_inputs(text, action):
    form = re.search(rf'<form\b[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', text, re.S)
    assert form, action
    values = {}
    for tag in re.findall(r"<input\b[^>]*>", form.group(1)):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if "name" in attrs:
            assert attrs["name"] not in values
            values[attrs["name"]] = html.unescape(attrs.get("value", ""))
    return values


def _saved_id(db, user_id):
    return db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (user_id,)).fetchone()[0]


def _sign_in(client, user):
    from app.auth import create_token

    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"], user["display_name"], token_version=1,
    ))


@pytest.mark.parametrize("path", LIBRARY_PATHS)
def test_registered_library_surfaces_filter_a_canonical_format_union(viewer_client, db, path):
    paper = _insert_item(db, title="Paper edition", isbn=None, media_type="book")
    ebook = _insert_item(db, title="Digital edition", isbn=None, media_type="ebook")
    _insert_item(db, title="Screen edition", isbn=None, media_type="dvd")
    db.commit()

    response = viewer_client.get(path, params={"types": "ebook,book,ebook", "view": "items"})
    assert response.status_code == 200
    if path == "/api/library/items":
        assert {item["id"] for item in response.json()["items"]} == {paper, ebook}
        assert response.json()["filters"]["types"] == "book,ebook"
    else:
        assert _member_ids(response.text) == {paper, ebook}


def test_shared_parser_expands_only_format_selectors_in_registry_config_order(monkeypatch):
    from app import media_type_filters

    calls = []
    expand = media_presentation.expand_selectors

    def record(selectors):
        calls.append(tuple(selectors))
        return expand(selectors)

    monkeypatch.setattr(media_presentation, "expand_selectors", record)
    assert media_type_filters.parse_types("ebook,book,ebook,audiobook") == ("book", "audiobook", "ebook")
    assert calls == [("format:ebook", "format:book", "format:ebook", "format:audiobook")]
    monkeypatch.setattr(config, "MEDIA_TYPES", dict(reversed(tuple(config.MEDIA_TYPES.items()))))
    assert media_type_filters.parse_types("book,audiobook,ebook") == ("ebook", "audiobook", "book")


@pytest.mark.parametrize("value", BAD_CSV + (None, False, 0, 7, [], ["book"], {}, {"book": True}, b"book"))
def test_shared_parser_and_library_normalizer_reject_with_a_fixed_code(value):
    from app import media_type_filters

    for parse in (media_type_filters.parse_types, lambda raw: library_catalog.normalize_filters({"types": raw})):
        with pytest.raises(ValueError, match="^invalid_types$"):
            parse(value)


def test_parser_bounds_precede_registry_expansion_and_deduplication(monkeypatch):
    from app import media_type_filters

    assert media_type_filters.parse_types(",".join(["book"] * 32)) == ("book",)

    def forbidden(*args):
        pytest.fail("Oversized transport reached registry expansion")

    monkeypatch.setattr(media_presentation, "expand_selectors", forbidden)
    for raw in (",".join(["book"] * 33), "x" * 1025):
        with pytest.raises(ValueError, match="^invalid_types$"):
            media_type_filters.parse_types(raw)


def test_parser_is_pure_and_empty_is_not_a_materialized_all_types_list(monkeypatch):
    from app import database, features, i18n, media_type_filters

    def forbidden(*args, **kwargs):
        pytest.fail("Transport parser read runtime state")

    monkeypatch.setattr(database, "get_db", forbidden)
    monkeypatch.setattr(features, "feature_enabled", forbidden)
    monkeypatch.setattr(i18n, "gettext", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert media_type_filters.parse_types() == ()
    assert media_type_filters.parse_types("") == ()
    assert media_type_filters.parse_types("cd,book,cd") == ("book", "cd")


def test_normalized_filters_and_urls_preserve_canonical_types_and_saved_empty_overrides():
    filters = library_catalog.normalize_filters({"types": "ebook,book,ebook"})
    assert filters["types"] == "book,ebook"
    assert "types" in library_catalog.FILTER_KEYS and library_catalog.DEFAULT_FILTERS["types"] == ""
    assert parse_qs(urlsplit(library_catalog.library_url(filters)).query)["types"] == ["book,ebook"]
    cleared = library_catalog.library_url(filters, types="", saved_view=7, page=2)
    assert parse_qs(urlsplit(cleared).query, keep_blank_values=True)["types"] == [""]
    with pytest.raises(ValueError, match="^invalid_types$"):
        library_catalog.library_url({"types": "family:books"})


def test_types_intersect_scalar_and_legacy_books_and_comics_on_the_same_item(db):
    paper = _insert_item(db, title="Paper", isbn=None, media_type="book")
    comic = _insert_item(db, title="Comic", isbn=None, media_type="comic")
    manga = _insert_item(db, title="Manga", isbn=None, media_type="manga")
    film = _insert_item(db, title="Film", isbn=None, media_type="dvd")
    for extra, expected in (
        ({}, {paper, comic, manga, film}),
        ({"family": "book"}, {paper, comic, manga}),
        ({"family": "book", "media_type": "comic"}, {comic}),
        ({"family": "video", "media_type": "comic"}, set()),
        ({"family": "book", "media_type": "ebook"}, set()),
    ):
        result = library_catalog.search_catalog(db, {
            "types": "dvd,manga,comic,book", "view": "items", **extra,
        })
        assert {item["id"] for item in result["items"]} == expected


def test_type_filtered_groups_keep_all_live_editions_holdings_and_match_truth(viewer_client, db):
    paper = _insert_item(db, title="Paper", isbn=None, media_type="book", owned=1)
    ebook = _insert_item(db, title="Digital", isbn=None, media_type="ebook", owned=0, wishlisted=True)
    audio = _insert_item(db, title="Audio", isbn=None, media_type="audiobook", owned=0)
    dead = _insert_item(db, title="Trashed digital", isbn=None, media_type="ebook")
    for first, second in ((paper, ebook), (ebook, audio), (audio, dead)):
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (first, second))
    item_copies.insert_copy(db, {"item_id": paper, "copy_number": 1})
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    source = db.execute(
        "INSERT INTO source_instances (name, provider, base_url) VALUES ('Digital shelf', 'opds', 'https://books.example.test')"
    ).lastrowid
    db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, availability) "
        "VALUES (?, ?, 'digital-one', 'Digital', 'ebook', 'available')", (source, ebook),
    )
    db.commit()

    response = viewer_client.get("/api/library/items?types=ebook&wishlist=yes&availability=digital")
    assert response.status_code == 200
    result = response.json()
    assert (result["matching_count"], result["group_count"]) == (1, 1)
    group = result["groups"][0]
    assert group["member_ids"] == [paper, ebook, audio]
    assert group["matching_ids"] == [ebook] and group["representative"]["id"] == ebook
    members = {item["id"]: item for item in group["members"]}
    assert {key: item["matches"] for key, item in members.items()} == {paper: False, ebook: True, audio: False}
    assert members[paper]["copy_count"] == 1 and members[paper]["owned"] is True
    assert members[ebook]["wishlisted"] is True and members[audio]["wishlisted"] is False
    for suffix in ("availability=digital", "wishlist=yes", "q=Digital"):
        failed = viewer_client.get("/api/library/items?types=book&" + suffix).json()
        assert (failed["matching_count"], failed["group_count"]) == (0, 0)
    for path in ("/library", "/api/library/search"):
        page = viewer_client.get(path, params={"types": "ebook"})
        assert _member_ids(page.text) == {paper, ebook, audio}
        assert f'data-library-member="{paper}" data-matches="false"' in page.text
        assert f'data-library-member="{ebook}" data-matches="true"' in page.text
        assert f'data-matching-ids="{ebook}"' in page.text


@pytest.mark.parametrize("path", LIBRARY_PATHS)
def test_invalid_and_duplicate_types_are_fixed_400_refusals_never_unfiltered(viewer_client, db, path):
    _insert_item(db, title="Must not leak in an invalid result", isbn=None)
    db.commit()
    assert viewer_client.get(path).status_code == 200
    for raw in BAD_CSV:
        response = viewer_client.get(path, params={"types": raw})
        assert response.status_code == 400, raw
        assert response.json() == {"detail": "Invalid media formats."}
        assert "Must not leak" not in response.text
        assert "<script>" not in response.text and raw not in response.json().values()
        assert response.headers["cache-control"] == "no-store"
    for pairs in (("book", ""), ("book", "ebook"), ("", ""), ("", "book")):
        response = viewer_client.get(path, params=[("types", value) for value in pairs])
        assert response.status_code == 400 and response.json() == {"detail": "Invalid media formats."}


def test_old_duplicate_fields_still_take_the_last_value_for_get_and_saved_post(viewer_client, db):
    paper = _insert_item(db, title="Wanted paper", isbn=None, media_type="book")
    _insert_item(db, title="Wrong screen", isbn=None, media_type="dvd")
    db.commit()
    result = viewer_client.get(
        "/api/library/items?types=book,dvd&view=items&q=wrong&q=Wanted&media_type=dvd&media_type=book"
    ).json()
    assert [item["id"] for item in result["items"]] == [paper]
    response = viewer_client.post("/api/library/views", content=urlencode([
        ("name", "Ignored"), ("name", "Last name"), ("types", "book"), ("q", "Wrong"), ("q", "Wanted"),
    ]), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert response.status_code == 303
    row = db.execute("SELECT name, filters_json FROM library_saved_views").fetchone()
    assert row["name"] == "Last name" and json.loads(row["filters_json"])["q"] == "Wanted"


def test_missing_and_explicit_empty_types_include_unknown_legacy_media(viewer_client, db):
    paper = _insert_item(db, title="Known", isbn=None)
    legacy = _insert_item(db, title="Legacy", isbn=None, media_type="legacy_disc")
    db.commit()
    for suffix in ("", "&types="):
        result = viewer_client.get("/api/library/items?view=items" + suffix).json()
        assert {item["id"] for item in result["items"]} == {paper, legacy}
    explicit = viewer_client.get("/api/library/items", params={
        "view": "items", "types": ",".join(config.MEDIA_TYPES),
    }).json()
    assert [item["id"] for item in explicit["items"]] == [paper]


def test_rendered_pagination_and_view_links_keep_canonical_union_and_group_counts(viewer_client, db):
    pairs = []
    for n in range(4):
        paper = _insert_item(db, title=f"Title {n}", isbn=None, media_type="book")
        ebook = _insert_item(db, title=f"Title {n}", isbn=None, media_type="ebook")
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (paper, ebook))
        pairs.append({paper, ebook})
    _insert_item(db, title="Excluded film", isbn=None, media_type="dvd")
    db.commit()
    first = viewer_client.get("/library?types=ebook,book,ebook&page_size=2")
    assert first.status_code == 200 and "8 matching items in 4 groups" in first.text
    next_url = _link(first.text, rel="next")
    assert parse_qs(urlsplit(next_url).query)["types"] == ["book,ebook"]
    second = viewer_client.get(next_url)
    assert second.status_code == 200 and "8 matching items in 4 groups" in second.text
    assert _member_ids(first.text) == set.union(*pairs[:2])
    assert _member_ids(second.text) == set.union(*pairs[2:])
    item_url = _link(second.text, label="Individual items")
    assert parse_qs(urlsplit(item_url).query)["types"] == ["book,ebook"]
    assert len(_member_ids(viewer_client.get(item_url).text)) == 2


def test_library_forms_count_carry_save_and_clear_the_new_filter(viewer_client, viewer_user, db):
    page = viewer_client.get("/library?types=ebook,book,ebook")
    assert page.status_code == 200
    filters_form = _form_inputs(page.text, "/library")
    assert filters_form["types"] == "book,ebook"
    assert 'data-testid="library-active-filter-count" data-count="1"' in page.text
    clear_url = _link(page.text, label="Clear filters")
    assert parse_qs(urlsplit(clear_url).query) == {"reset": ["1"]}
    assert _form_inputs(viewer_client.get(clear_url).text, "/library")["types"] == ""
    saved_form = _form_inputs(page.text, "/api/library/views")
    assert set(library_catalog.FILTER_KEYS) <= set(saved_form)
    assert saved_form["types"] == "book,ebook"
    saved_form["name"] = "My format union"
    response = viewer_client.post("/api/library/views", data=saved_form, follow_redirects=False)
    assert response.status_code == 303
    saved = library_catalog.load_saved_view(db, viewer_user["id"], _saved_id(db, viewer_user["id"]))
    assert saved["filters"]["types"] == "book,ebook"
    payload = json.loads(db.execute("SELECT filters_json FROM library_saved_views").fetchone()[0])
    assert set(payload) == library_catalog.FILTER_KEYS and payload["types"] == "book,ebook"


def test_saved_types_roundtrip_and_explicit_empty_survive_real_links(viewer_client, viewer_user, db):
    paper = _insert_item(db, title="Paper", isbn=None)
    ebook = _insert_item(db, title="Digital", isbn=None, media_type="ebook")
    legacy = _insert_item(db, title="Legacy", isbn=None, media_type="legacy_disc")
    db.commit()
    viewer_client.post("/api/library/views", data={
        "name": "Digital", "types": "ebook,ebook", "view": "items", "page_size": "1",
    })
    view_id = _saved_id(db, viewer_user["id"])
    saved = viewer_client.get("/api/library/items", params={"saved_view": view_id}).json()
    assert [item["id"] for item in saved["items"]] == [ebook]
    assert saved["filters"]["types"] == "ebook"
    first = viewer_client.get("/library", params={"saved_view": view_id, "types": ""})
    assert "3 matching items in 3 groups" in first.text
    assert _form_inputs(first.text, "/library")["types"] == ""
    seen = _member_ids(first.text)
    current = first
    for _ in range(2):
        next_url = _link(current.text, rel="next")
        query = parse_qs(urlsplit(next_url).query, keep_blank_values=True)
        assert query["types"] == [""] and query["saved_view"] == [str(view_id)]
        current = viewer_client.get(next_url)
        seen.update(_member_ids(current.text))
        assert "3 matching items in 3 groups" in current.text
    assert seen == {paper, ebook, legacy}
    grouped = _link(current.text, label="Grouped formats")
    assert parse_qs(urlsplit(grouped).query, keep_blank_values=True)["types"] == [""]
    assert "3 matching items in 3 groups" in viewer_client.get(grouped).text


def test_reset_discards_saved_filters_before_explicit_filters_and_cannot_resurrect_them(viewer_client, viewer_user, db):
    ids = {_insert_item(db, title=title, isbn=None, media_type=kind) for title, kind in (
        ("Paper", "book"), ("Digital", "ebook"), ("Screen", "dvd"), ("Legacy", "legacy_disc"),
    )}
    db.commit()
    viewer_client.post("/api/library/views", data={
        "name": "Narrow saved default", "q": "Impossible", "types": "cd", "family": "music",
        "source": "physical", "view": "groups", "sort": "title", "page_size": "1",
    })
    view_id = _saved_id(db, viewer_user["id"])
    result = viewer_client.get("/api/library/items", params={
        "saved_view": view_id, "reset": "1", "view": "items", "sort": "recent",
    }).json()
    assert result["saved_view"] is None and {item["id"] for item in result["items"]} == ids
    assert result["filters"] == {**library_catalog.DEFAULT_FILTERS, "view": "items", "sort": "recent"}
    selected = viewer_client.get("/library", params={
        "saved_view": view_id, "reset": "1", "types": "ebook,book", "view": "items", "page_size": "1",
    })
    assert "2 matching items in 2 groups" in selected.text
    assert "saved_view" not in _form_inputs(selected.text, "/library")
    next_url = _link(selected.text, rel="next")
    query = parse_qs(urlsplit(next_url).query)
    assert query["types"] == ["book,ebook"] and "saved_view" not in query
    assert "2 matching items in 2 groups" in viewer_client.get(next_url).text


def test_types_saved_views_do_not_leak_across_two_real_authenticated_accounts(viewer_client, viewer_user, db):
    other = _create_user("other-types", "password123", "Other account", "viewer")
    paper = _insert_item(db, title="Paper", isbn=None)
    disc = _insert_item(db, title="Disc", isbn=None, media_type="cd")
    db.commit()
    viewer_client.post("/api/library/views", data={"name": "My books", "types": "book", "view": "items"})
    own_id = _saved_id(db, viewer_user["id"])
    _sign_in(viewer_client, other)
    viewer_client.post("/api/library/views", data={"name": "Private discs", "types": "cd", "view": "items"})
    foreign_id = _saved_id(db, other["id"])
    assert viewer_client.get("/api/library/items", params={"saved_view": foreign_id}).json()["items"][0]["id"] == disc
    _sign_in(viewer_client, viewer_user)
    own = viewer_client.get("/api/library/items", params={"saved_view": own_id}).json()
    assert [item["id"] for item in own["items"]] == [paper]
    foreign = viewer_client.get("/api/library/items", params={"saved_view": foreign_id, "view": "items"}).json()
    assert foreign["saved_view"] is None and foreign["filters"]["types"] == ""
    assert {item["id"] for item in foreign["items"]} == {paper, disc}
    page = viewer_client.get("/library", params={"saved_view": foreign_id})
    assert "Private discs" not in page.text and "My books" in page.text


def test_invalid_types_saved_forms_refuse_without_inserting_or_replacing(viewer_client, viewer_user, db):
    viewer_client.post("/api/library/views", data={"name": "Keep", "types": "book"})
    view_id = _saved_id(db, viewer_user["id"])
    for raw in BAD_CSV:
        response = viewer_client.post("/api/library/views", data={"name": "Keep", "types": raw}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/library"
        assert "library_view_notice=invalid_form" in response.headers["set-cookie"]
    for values in (("book", ""), ("", ""), ("ebook", "book")):
        response = viewer_client.post("/api/library/views", content=urlencode([
            ("name", "Never create"), *[("types", value) for value in values],
        ]), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert "library_view_notice=invalid_form" in response.headers["set-cookie"]
    upload = viewer_client.post("/api/library/views", data={"name": "Keep"}, files={
        "types": ("types.txt", b"ebook", "text/plain"),
    }, follow_redirects=False)
    assert "library_view_notice=invalid_form" in upload.headers["set-cookie"]
    assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 1
    assert library_catalog.load_saved_view(db, viewer_user["id"], view_id)["filters"]["types"] == "book"


def test_invalid_nonempty_saved_types_are_refused_not_silently_changed_to_all(viewer_client, viewer_user, db):
    _insert_item(db, title="Must not appear after corrupt selection", isbn=None)
    view_id = db.execute(
        "INSERT INTO library_saved_views (user_id, name, filters_json) VALUES (?, 'Invalid selection', ?)",
        (viewer_user["id"], json.dumps({"types": "book,unknown"})),
    ).lastrowid
    db.commit()
    with pytest.raises(ValueError, match="^invalid_types$"):
        library_catalog.load_saved_view(db, viewer_user["id"], view_id)
    for path in LIBRARY_PATHS:
        response = viewer_client.get(path, params={"saved_view": view_id})
        assert response.status_code == 400 and "Must not appear" not in response.text
    reset = viewer_client.get("/api/library/items", params={"saved_view": view_id, "reset": "1"})
    assert reset.status_code == 200 and reset.json()["matching_count"] == 1
