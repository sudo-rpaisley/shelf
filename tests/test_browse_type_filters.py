"""Multi-format Browse filtering, generated HTMX state and pagination."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from starlette.datastructures import QueryParams

from app import browse_filters as bf, config
from tests.conftest import _insert_item


BAD_CSV = (
    "all", "family:books", "format:book", "kids_book", "BOOK", "unknown", "book,unknown",
    " book", "book ", "book\n", "book, ebook", "book,", ",book", "book,,ebook",
    "book') OR 1=1 --", 'book,<script>alert("types")</script>', "book&owned=1",
    ",".join(["book"] * 33), "x" * 1025,
)


def _item_ids(text):
    return {int(value) for value in re.findall(r'data-item-id="(\d+)"', text)}


class _Controls(HTMLParser):
    """Serialize the controls the real template rendered, including empty inputs."""

    def __init__(self, source):
        super().__init__()
        self.controls = []
        self.select = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"input", "select"} and attrs.get("name"):
            control = {"tag": tag, "attrs": attrs, "value": attrs.get("value", "")}
            self.controls.append(control)
            if tag == "select":
                self.select = control
        elif tag == "option" and self.select is not None:
            if not self.select.get("has_option") or "selected" in attrs:
                self.select["value"] = attrs.get("value", "")
            self.select["has_option"] = True

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def _control(controls, name):
    found = [control for control in controls if control["attrs"]["name"] == name]
    assert len(found) == 1, (name, found)
    return found[0]


def _serialize_change(controls, name, value):
    trigger = _control(controls, name)
    trigger["value"] = value
    includes = re.findall(r"\[name='([^']+)'\]", trigger["attrs"]["hx-include"])
    assert name not in includes
    assert trigger["attrs"]["hx-target"] == "#item-grid"
    return trigger["attrs"]["hx-get"], [
        (control["attrs"]["name"], control["value"])
        for control in controls if control["attrs"]["name"] in {name, *includes}
    ]


def _apply_oob_controls(controls, fragment):
    replacements = {
        control["attrs"]["id"]: control for control in _Controls(fragment).controls
        if control["attrs"].get("hx-swap-oob") == "true"
    }
    assert "owned-filter" in replacements and "location-filter" in replacements
    controls[:] = [replacements.get(control["attrs"].get("id"), control) for control in controls]


def _load_more_url(text):
    tags = re.findall(r'<(?:div|tr)\b[^>]*id="load-more"[^>]*>', text, re.S)
    assert tags, "Expected the real pagination sentinel"
    urls = {html.unescape(re.search(r'hx-get="([^"]+)"', tag).group(1)) for tag in tags}
    assert len(urls) == 1
    for tag in tags:
        assert 'hx-include="[name=\'view\']"' in tag  # Only one types value, in the URL.
    return urls.pop()


def _options(text, select_id):
    select = re.search(rf'<select\b[^>]*id="{select_id}"[^>]*>(.*?)</select>', text, re.S)
    assert select, select_id
    return {
        html.unescape(value): " ".join(html.unescape(label).split())
        for value, label in re.findall(r'<option\b[^>]*value="([^"]*)"[^>]*>(.*?)</option>', select.group(1), re.S)
    }


@pytest.mark.parametrize("path", ["/browse", "/api/search?page=0", "/api/search?page=1"])
def test_browse_first_paint_and_search_filter_the_format_union(viewer_client, db, path):
    paper = _insert_item(db, title="Paper edition", isbn=None, media_type="book")
    ebook = _insert_item(db, title="Digital edition", isbn=None, media_type="ebook")
    _insert_item(db, title="Screen edition", isbn=None, media_type="dvd")
    db.commit()

    response = viewer_client.get(path + ("&" if "?" in path else "?") + "types=ebook,book,ebook")
    assert response.status_code == 200
    assert _item_ids(response.text) == {paper, ebook}


def test_types_is_one_registry_owned_filter_with_generated_includes_and_client_state():
    matches = [entry for entry in bf.FILTERS if entry.name == "types"]
    assert len(matches) == 1
    entry = matches[0]
    assert entry.prefix == "Formats" and entry.quote_in_qs and entry.condition is not None
    assert entry.default == "" and entry.clear_to == "" and entry.chip and entry.in_url
    for other in bf.FILTERS:
        included = bf.filter_includes(other.name)
        assert ("[name='types']" in included) is (other.name != "types")
    config_entry = next(value for value in bf.client_config() if value["name"] == "types")
    assert config_entry == {
        "name": "types", "prefix": "Formats", "default": "", "chip": True, "clearTo": "", "inUrl": True,
    }
    assert bf.has_active_filters({"types": "book,ebook"})


def test_condition_uses_bound_in_parameters_and_the_same_shared_parser_as_library(monkeypatch):
    from app import media_type_filters
    from app.services import library_catalog

    parse = media_type_filters.parse_types
    calls = []

    def record(value=""):
        calls.append(value)
        return parse(value)

    monkeypatch.setattr(media_type_filters, "parse_types", record)
    assert bf.build_where({"types": "ebook,book,ebook"}) == ("WHERE i.media_type IN (?, ?)", ["book", "ebook"])
    assert library_catalog.normalize_filters({"types": "cd,book"})["types"] == "book,cd"
    assert calls == ["ebook,book,ebook", "cd,book"]
    where, params = bf.build_where({"types": "ebook,book", "media_type_filter": "dvd", "owned": "1"})
    assert where.count("?") == len(params) == 3 and "i.owned = 1" in where
    assert bf.build_where({"types": "book"}, exclude="types") == ("", [])


@pytest.mark.parametrize("raw", BAD_CSV + (None, False, 0, [], ["book"], {}, b"book"))
def test_invalid_or_nonscalar_types_are_a_nonmatching_condition_not_none(raw):
    assert bf.build_where({"types": raw}) == ("WHERE 1 = 0", [])
    values = bf.values_from({"types": raw})
    assert values["types"] and bf.build_where(values) == ("WHERE 1 = 0", [])
    qs = bf.querystring({"types": raw})
    replay = bf.values_from(QueryParams(qs))
    assert bf.build_where(replay) == ("WHERE 1 = 0", [])


def test_values_from_canonicalizes_types_rejects_new_duplicates_and_preserves_old_last_value():
    assert bf.values_from(QueryParams("types=ebook,book,ebook"))["types"] == "book,ebook"
    assert bf.values_from({})["types"] == "" and bf.build_where({"types": ""}) == ("", [])
    for query in ("types=book&types=", "types=&types=", "types=ebook&types=book", "types=&types=book"):
        values = bf.values_from(QueryParams(query))
        assert values["types"] and bf.build_where(values) == ("WHERE 1 = 0", [])
    old = bf.values_from(QueryParams("q=first&q=last&owned=0&owned=1&types=book"))
    assert old["q"] == "last" and old["owned"] == "1" and old["types"] == "book"


def test_querystring_encodes_values_and_canonicalizes_types_without_parameter_injection():
    qs = bf.querystring({"q": "Needle & types=cd", "types": "ebook,book,ebook", "tag": "x&owned=1"})
    assert parse_qs(qs) == {"q": ["Needle & types=cd"], "types": ["book,ebook"], "tag": ["x&owned=1"]}
    assert "types=book%2Cebook" in qs
    assert bf.querystring({"types": ""}) == ""


@pytest.mark.parametrize("path", ["/browse", "/api/search?page=0", "/api/search?page=1"])
def test_invalid_duplicate_and_oversized_type_urls_are_empty_200_not_all(viewer_client, db, path):
    _insert_item(db, title="An otherwise visible item", isbn=None)
    db.commit()
    assert _item_ids(viewer_client.get(path).text)
    for raw in BAD_CSV:
        response = viewer_client.get(path + ("&" if "?" in path else "?") + urlencode({"types": raw}))
        assert response.status_code == 200, raw
        assert _item_ids(response.text) == set() and "No items match your filters" in response.text
        assert "<script>alert" not in response.text
    for pairs in (("book", ""), ("book", "ebook"), ("", ""), ("", "book")):
        response = viewer_client.get(path + ("&" if "?" in path else "?") + urlencode([("types", value) for value in pairs]))
        assert response.status_code == 200 and not _item_ids(response.text)


def test_missing_and_empty_types_include_legacy_but_explicit_all_known_does_not(viewer_client, db):
    paper = _insert_item(db, title="Known", isbn=None)
    legacy = _insert_item(db, title="Legacy", isbn=None, media_type="legacy_disc")
    db.commit()
    for path in ("/browse", "/api/search"):
        assert _item_ids(viewer_client.get(path).text) == {paper, legacy}
        assert _item_ids(viewer_client.get(path, params={"types": ""}).text) == {paper, legacy}
        assert _item_ids(viewer_client.get(path, params={"types": ",".join(config.MEDIA_TYPES)}).text) == {paper}


def test_type_intersection_trash_wishlist_and_cross_filter_counts_stay_truthful(viewer_client, db):
    paper = _insert_item(db, title="Owned paper", isbn=None, media_type="book", owned=1)
    ebook = _insert_item(db, title="Wishlisted ebook", isbn=None, media_type="ebook", owned=0, wishlisted=True)
    bare = _insert_item(db, title="Neither ebook", isbn=None, media_type="ebook", owned=0)
    _insert_item(db, title="Excluded disc", isbn=None, media_type="cd", owned=1)
    _insert_item(db, title="Trashed book", isbn=None, media_type="book", deleted_at="2026-01-01")
    db.commit()
    first = viewer_client.get("/browse?types=ebook,book&owned=0")
    fragment = viewer_client.get("/api/search?types=ebook,book&owned=0")
    for response in (first, fragment):
        assert response.status_code == 200 and _item_ids(response.text) == {ebook}
        assert _options(response.text, "owned-filter") == {
            "": "All (3)", "1": "Owned (1)", "0": "Wishlist (1)", "none": "Not owned or wishlisted (1)",
        }
        assert _options(response.text, "type-filter") == {"": "All Types (1)", "ebook": "eBook (1)"}
    for scalar, expected in (("book", {paper}), ("ebook", {ebook, bare}), ("cd", set())):
        for path in ("/browse", "/api/search"):
            assert _item_ids(viewer_client.get(path, params={
                "types": "ebook,book", "media_type_filter": scalar,
            }).text) == expected


def test_rendered_hidden_types_and_oob_includes_survive_sequential_changes_and_chip_clear(viewer_client, db):
    location = db.execute("INSERT INTO locations (name) VALUES ('Shelf A')").lastrowid
    paper = _insert_item(db, title="Owned paper", isbn=None, location_id=location)
    ebook = _insert_item(db, title="Owned ebook", isbn=None, media_type="ebook")
    film = _insert_item(db, title="Owned film", isbn=None, media_type="dvd", location_id=location)
    _insert_item(db, title="Wishlist ebook", isbn=None, media_type="ebook", owned=0, wishlisted=True)
    db.commit()
    page = viewer_client.get("/browse?types=ebook,book,ebook")
    controls = _Controls(page.text).controls
    hidden = _control(controls, "types")
    assert hidden["attrs"]["type"] == "hidden" and hidden["value"] == "book,ebook"
    assert hidden["attrs"]["hx-trigger"] == "change" and hidden["attrs"]["hx-get"] == "/api/search"
    assert hidden["attrs"]["hx-include"] == str(bf.filter_includes("types"))
    client_defs = json.loads(re.search(
        r'<script type="application/json" id="browse-filter-config">(.*?)</script>', page.text, re.S,
    ).group(1))
    assert next(entry for entry in client_defs if entry["name"] == "types")["chip"] is True

    url, params = _serialize_change(controls, "owned", "1")
    assert [value for name, value in params if name == "types"] == ["book,ebook"]
    first = viewer_client.get(url, params=params)
    assert _item_ids(first.text) == {paper, ebook}
    _apply_oob_controls(controls, first.text)
    url, params = _serialize_change(controls, "location_filter", str(location))
    assert [value for name, value in params if name == "types"] == ["book,ebook"]
    second = viewer_client.get(url, params=params)
    assert _item_ids(second.text) == {paper}
    _apply_oob_controls(controls, second.text)
    # Clearing another chip must still carry the hidden format union.
    url, params = _serialize_change(controls, "location_filter", "")
    assert [value for name, value in params if name == "types"] == ["book,ebook"]
    cleared = viewer_client.get(url, params=params)
    assert _item_ids(cleared.text) == {paper, ebook}
    _apply_oob_controls(controls, cleared.text)
    # Clearing the new chip itself sends one explicit empty, intentionally mixed.
    url, params = _serialize_change(controls, "types", "")
    assert [value for name, value in params if name == "types"] == [""]
    assert _item_ids(viewer_client.get(url, params=params).text) == {paper, ebook, film}


@pytest.mark.parametrize("view", ["grid", "list"])
def test_actual_load_more_links_preserve_canonical_types_through_all_pages(viewer_client, db, view):
    expected = {
        _insert_item(db, title=f"Selected {n:03}", isbn=None, media_type="book" if n % 2 else "ebook")
        for n in range(config.DEFAULT_PAGE_SIZE * 2 + 1)
    }
    _insert_item(db, title="Excluded film", isbn=None, media_type="dvd")
    _insert_item(db, title="Trashed selected", isbn=None, media_type="book", deleted_at="2026-01-01")
    db.commit()
    current = viewer_client.get("/browse", params={"types": "ebook,book,ebook", "view": view})
    seen = _item_ids(current.text)
    assert len(seen) == config.DEFAULT_PAGE_SIZE
    for page in (2, 3):
        url = _load_more_url(current.text)
        query = parse_qs(urlsplit(url).query)
        assert query["types"] == ["book,ebook"] and query["page"] == [str(page)]
        assert "view" not in query
        current = viewer_client.get(url + "&view=" + view)
        assert current.status_code == 200
        ids = _item_ids(current.text)
        assert ids and not seen.intersection(ids)
        seen.update(ids)
    assert seen == expected and 'id="load-more"' not in current.text
