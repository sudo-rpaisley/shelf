"""Registered-route contracts for the shared, still full-page item editor.

This extraction does not introduce a dialog or make provider facts editable.
Successful controls are collected from the real response, including the ordered
hidden/checkbox pairs; no hand-written substitute form is rendered by these tests.
"""

from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.conftest import _insert_item, _insert_location


ROOT = Path(__file__).resolve().parents[1]
MEDIA_TYPES = (
    "book", "audiobook", "ebook", "magazine", "dvd", "digital_video",
    "tv_series", "vinyl", "cassette", "cd", "digital_music", "comic",
    "manga", "video_game",
)
FIELD_TYPES = {
    "title": "text", "subtitle": "text", "authors": "text",
    "publisher": "text", "publish_year": "number", "date_started": "date",
    "date_finished": "date", "series_name": "text", "series_position": "number",
    "page_count": "number", "isbn": "text", "upc": "text", "manual_value": "number",
    "narrator": "text", "duration_mins": "number", "asin": "text",
}
SELECT_NAMES = {"media_type", "language", "reading_status", "location_id", "platform"}
FIELD_NAMES = Counter({
    **dict.fromkeys(FIELD_TYPES, 1), **dict.fromkeys(SELECT_NAMES, 1),
    "from": 1, "description": 1, "notes": 1, "cover": 1, "url": 1,
    "owned": 2, "wishlisted": 2,
})
DOMAIN_TABLES = (
    "items", "item_copies", "locations", "lists", "list_items", "tags", "item_tags",
    "authors", "item_authors", "item_links", "music_releases", "music_media",
    "music_tracks", "music_identifiers", "periodical_publications", "periodical_issues",
)
RAW = '100%s <b>raw</b> & "quoted" {{ never_rescan }}'


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: "Element | None" = None

    def all(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if (tag is None or child.tag == tag) and all(
                child.attrs.get(key) == value for key, value in attrs.items()
            ):
                found.append(child)
            found.extend(child.all(tag, **attrs))
        return found

    def one(self, tag=None, **attrs):
        found = self.all(tag, **attrs)
        assert len(found) == 1, (tag, attrs, len(found))
        return found[0]

    @property
    def text(self):
        return "".join(child.text if isinstance(child, Element) else child for child in self.children)


class EditorDOM(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    @property
    def form(self):
        return self.root.one("form", id="item-edit-form")

    def pairs(self):
        """Native successful text controls, before CSRF's existing JS injection."""
        pairs = []
        for node in self.form.all():
            attrs = node.attrs
            name = attrs.get("name")
            if not name or "disabled" in attrs:
                continue
            if node.tag == "input":
                kind = attrs.get("type", "text")
                if kind in {"file", "submit", "button"}:
                    continue
                if kind in {"checkbox", "radio"} and "checked" not in attrs:
                    continue
                pairs.append((name, attrs.get("value", "")))
            elif node.tag == "textarea":
                pairs.append((name, node.text))
            elif node.tag == "select":
                options = node.all("option")
                selected = [option for option in options if "selected" in option.attrs]
                option = (selected or options)[0]
                pairs.append((name, option.attrs.get("value", option.text)))
        return pairs


def _snapshot(db):
    return {
        table: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')), key=repr)
        for table in DOMAIN_TABLES
    }


def _seed(db, media_type="book", *, owned=1, wishlisted=False, **overrides):
    from app.services import author_index

    values = dict(
        title=RAW, isbn=None, media_type=media_type, subtitle="Subtitle " + RAW,
        authors="Creator " + RAW, publisher="Publisher " + RAW, publish_year=0,
        description="Description\n" + RAW, notes="Notes\n" + RAW, manual_value=0,
        owned=owned, wishlisted=wishlisted,
    )
    values.update(overrides)
    item_id = _insert_item(db, **values)
    author_index.reindex_item(db, item_id, values["authors"])
    db.commit()  # Registered handlers open their own connection (G48).
    return item_id


def _get(client, item_id, query=""):
    response = client.get(f"/item/{item_id}/edit{query}", follow_redirects=False)
    assert response.status_code == 200
    return response, EditorDOM(response.text)


def _assert_field_contract(dom, item_id):
    form = dom.form
    assert form.attrs == {
        "id": "item-edit-form", "action": f"/api/items/{item_id}", "method": "post",
        "enctype": "multipart/form-data", "class": "edit-layout",
    }
    assert not form.all("form"), "the independently saving tags form must not be nested"
    assert Counter(node.attrs["name"] for node in form.all() if "name" in node.attrs) == FIELD_NAMES
    for name, kind in FIELD_TYPES.items():
        control = form.one("input", id=name, name=name)
        assert control.attrs["type"] == kind
        assert "required" not in control.attrs and "readonly" not in control.attrs
        assert form.one("label", **{"for": name}).text.strip()
    for name in SELECT_NAMES:
        assert form.one("select", id=name, name=name)
        assert form.one("label", **{"for": name}).text.strip()
    assert form.one("input", id="series_position").attrs["step"] == "any"
    value = form.one("input", id="manual_value")
    assert (value.attrs["step"], value.attrs["min"]) == ("0.01", "0")
    assert form.one("textarea", id="description").attrs["rows"] == "4"
    assert form.one("textarea", id="notes").attrs["rows"] == "3"
    assert len(form.all("button", type="submit")) == 1
    assert form.one("button", **{"data-testid": "save-btn"}).text.strip() == "Save"
    for section in ("general", "artwork", "series", "identifiers", "location", "media"):
        assert form.one("section", id=f"edit-{section}")
    tags = dom.root.one("section", id="edit-tags")
    assert tags not in form.all()
    assert tags.one("form").attrs["hx-post"] == f"/api/items/{item_id}/tags"


def test_shared_editor_fieldset_contract():
    """The intentional pre-extraction RED is a missing-partial contract, not a bug."""
    templates = ROOT / "app/templates"
    aggregate_path = templates / "fragments/item_editor_fields.html"
    common_path = templates / "fragments/item_editor_common_fields.html"
    assert aggregate_path.is_file(), "shared editor fieldset has not been extracted yet"
    assert common_path.is_file(), "common fields must have a real shared owner"
    host = (templates / "item_edit.html").read_text()
    aggregate = aggregate_path.read_text()
    common = common_path.read_text()
    assert host.count('{% include "fragments/item_editor_fields.html" %}') == 1
    assert "fragments/item_editor_common_fields.html" in aggregate
    assert "macro field(" in common
    for section in ("books", "audio", "games"):
        path = f"fragments/item_editor_sections/{section}.html"
        assert f'include "{path}"' in aggregate
        assert "field(" in (templates / path).read_text() or 'name="platform"' in (templates / path).read_text()
    for path in (aggregate_path, common_path, *templates.glob("fragments/item_editor_sections/*.html")):
        assert "<form" not in path.read_text(), "hosts own the sole save form"
    assert 'field("title"' not in host, "do not keep a parallel copy of the original fields"


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("client_name", ("admin_client", "editor_client", "viewer_client"))
def test_registered_edit_route_preserves_fields_and_permissions(request, db, media_type, client_name):
    from app.config import MEDIA_TYPES as registered_types, creator_label, status_labels

    assert set(MEDIA_TYPES) == set(registered_types) and len(MEDIA_TYPES) == 14
    client = request.getfixturevalue(client_name)
    item_id = _seed(db, media_type)
    before = _snapshot(db)
    response = client.get(f"/item/{item_id}/edit?from=series", follow_redirects=False)
    if client_name == "viewer_client":
        # Native page denials redirect; API denials use 403 (auth.require_role).
        assert response.status_code == 303 and response.headers["location"] == "/browse"
        assert not EditorDOM(response.text).root.all("form", id="item-edit-form")
    else:
        assert response.status_code == 200
        dom = EditorDOM(response.text)
        _assert_field_contract(dom, item_id)
        data = dict(dom.pairs())
        assert data["title"] == RAW and data["media_type"] == media_type
        assert data["subtitle"] == "Subtitle " + RAW
        assert data["authors"] == "Creator " + RAW
        assert data["publisher"] == "Publisher " + RAW
        assert data["description"] == "Description\n" + RAW
        assert data["notes"] == "Notes\n" + RAW
        assert data["publish_year"] == "0" and data["manual_value"] == "0.0"
        assert data["from"] == "series"
        assert dom.form.one("label", **{"for": "authors"}).text.strip() == creator_label(media_type)
        options = dom.form.one("select", name="reading_status").all("option")
        labels = status_labels(media_type)
        assert [option.text for option in options] == ["None", labels.want, labels.doing, labels.done]
        assert not dom.form.all("b"), "raw catalogue data must remain escaped, not rescanned"
        assert response.text.count('id="item-edit-form"') == 1
    assert _snapshot(db) == before


@pytest.mark.parametrize("owned,wishlisted,pairs", [
    (1, False, [("owned", "0"), ("owned", "1"), ("wishlisted", "0")]),
    (0, True, [("owned", "0"), ("wishlisted", "0"), ("wishlisted", "1")]),
    (0, False, [("owned", "0"), ("wishlisted", "0")]),
])
def test_ownership_pairs_and_full_rendered_round_trip(editor_client, db, owned, wishlisted, pairs):
    item_id = _seed(db, owned=owned, wishlisted=wishlisted)
    before = _snapshot(db)
    _, dom = _get(editor_client, item_id)
    assert [pair for pair in dom.pairs() if pair[0] in {"owned", "wishlisted"}] == pairs
    boxes = dom.form.one("div", **{"x-data": "ownershipBoxes"})
    assert boxes.attrs["data-owned"] == str(owned)
    assert boxes.attrs["data-wishlisted"] == str(int(wishlisted))
    wish = boxes.one("input", type="checkbox", name="wishlisted")
    assert ("disabled" in wish.attrs) == bool(owned)
    assert wish.attrs[":disabled"] == "owned"
    assert _snapshot(db) == before
    response = editor_client.post(dom.form.attrs["action"], data=dict(dom.pairs()), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item_id}"
    after = _snapshot(db)
    # The existing write funnel stamps updated_at even for an unchanged save.
    columns = [row["name"] for row in db.execute("PRAGMA table_info(items)")]
    updated_at = columns.index("updated_at")
    for snapshot in (before, after):
        snapshot["items"] = [row[:updated_at] + row[updated_at + 1:] for row in snapshot["items"]]
    assert after == before


def test_legacy_identifiers_and_all_media_fields_keep_values_and_scopes(editor_client, db):
    location = _insert_location(db, "Location " + RAW)
    item_id = _seed(
        db, "audiobook", isbn="9780441172710", upc="1234567890120", asin="B0D1234567",
        language="tlh", reading_status="read", date_started="2026-01-01", date_finished="2026-02-01",
        series_name="Series " + RAW, series_position=0, page_count=0, narrator="Narrator " + RAW,
        duration_mins=0, platform="ps5", location_id=location, cover_path="covers/existing.jpg",
    )
    before = _snapshot(db)
    response, dom = _get(editor_client, item_id)
    data = dict(dom.pairs())
    for name, value in {
        "isbn": "9780441172710", "upc": "1234567890120", "asin": "B0D1234567",
        "language": "tlh", "reading_status": "read", "date_started": "2026-01-01",
        "date_finished": "2026-02-01", "series_name": "Series " + RAW,
        "series_position": "0.0", "page_count": "0", "narrator": "Narrator " + RAW,
        "duration_mins": "0", "platform": "ps5", "location_id": str(location),
    }.items():
        assert data[name] == value, name
    for identifier in ("isbn", "upc"):
        assert dom.form.one("p", **{"data-testid": f"{identifier}-stored-invalid"})
        control = dom.form.one("input", name=identifier)
        assert control.parent.parent.attrs["x-data"] == f"{identifier}Camera"
        assert dom.form.one("div", id=f"edit-{identifier}-camera-reader")
        assert dom.form.one("video", id=f"edit-{identifier}-zxing-video")
    for group in dom.form.all():
        if "data-always-visible" in group.attrs:
            assert group.attrs["data-always-visible"] == "true"
    assert dom.form.one("option", value=str(location)).text == "Location " + RAW
    assert 'src="/static/js/csrf.js"' in response.text
    assert dom.form.one("div", **{"x-data": "coverDrop"}).one("input", name="cover").attrs["x-ref"] == "coverInput"
    url_button = dom.form.one("button", **{"data-testid": "edit-cover-url-submit"})
    assert {key: url_button.attrs[key] for key in ("type", "hx-post", "hx-include", "hx-vals", "hx-swap")} == {
        "type": "button", "hx-post": f"/api/items/{item_id}/cover-url", "hx-include": "#edit-cover-url",
        "hx-vals": '{"return_to":"edit"}', "hx-swap": "none",
    }
    assert _snapshot(db) == before


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_empty_conditional_groups_stay_in_the_dom(editor_client, db, media_type):
    item_id = _seed(db, media_type)
    _, dom = _get(editor_client, item_id)
    conditional = [node for node in dom.form.all() if "data-media-types" in node.attrs]
    assert len(conditional) == 9
    assert all(node.attrs["data-always-visible"] == "false" for node in conditional)
    assert not any("disabled" in node.attrs for node in dom.form.all("select"))
    assert {node.attrs["name"] for node in dom.form.all("input") if "name" in node.attrs} >= {
        "isbn", "upc", "series_name", "series_position", "page_count", "narrator", "duration_mins", "asin",
    }


@pytest.mark.parametrize("family", ("music", "periodical"))
def test_provider_side_tables_stay_outside_the_generic_editor(editor_client, db, family):
    if family == "music":
        from app.services import music_catalog

        item_id = _seed(db, "vinyl")
        music_catalog.save_release(db, item_id, {
            "title": RAW, "artist_credit": "Creator " + RAW,
            "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
            "musicbrainz_release_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "catalog_number": "CAT-1", "label": "Label " + RAW, "country": "GB",
            "media": [{"position": 1, "format": "12\" Vinyl", "tracks": [
                {"position": 1, "number": "A1", "title": "Track " + RAW, "duration_ms": 65000},
            ]}],
        })
        music_catalog.add_identifier(db, item_id, "matrix_runout", "MATRIX-1", "Side A")
    else:
        from app.services import periodical_records

        item_id = _seed(db, "magazine")
        publication = periodical_records.upsert_publication(db, title="Publication " + RAW, issn="01617370")
        periodical_records.link_issue(db, item_id=item_id, publication_id=publication,
                                      issue_number="1", volume="2", issue_date="2026-01-01")
    db.commit()
    before = _snapshot(db)
    _, dom = _get(editor_client, item_id)
    _assert_field_contract(dom, item_id)
    assert not {"musicbrainz_release_id", "musicbrainz_release_group_id", "track_title", "catalog_number",
                "publication_id", "issn", "issue_number", "issue_date", "volume"} & set(dict(dom.pairs()))
    assert _snapshot(db) == before
    # Even an unchanged full native submission must not rewrite those identities.
    response = editor_client.post(dom.form.attrs["action"], data=dict(dom.pairs()), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item_id}"
    for table in DOMAIN_TABLES:
        if table.startswith(("music_", "periodical_")):
            assert _snapshot(db)[table] == before[table]


def test_native_save_still_requires_csrf(editor_client, db):
    item_id = _seed(db)
    before = _snapshot(db)
    _, dom = _get(editor_client, item_id)
    # The existing csrf.js injects _csrf in the browser; GET HTML has no server token.
    assert "_csrf" not in dict(dom.pairs())
    del editor_client.headers["X-CSRF-Token"]
    response = editor_client.post(dom.form.attrs["action"], data=dict(dom.pairs()), follow_redirects=False)
    assert response.status_code == 403
    assert _snapshot(db) == before


def test_missing_and_trashed_items_do_not_render_an_editor(editor_client, db):
    item_id = _seed(db)
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (item_id,))
    db.commit()
    before = _snapshot(db)
    for candidate in (item_id, item_id + 1000):
        response = editor_client.get(f"/item/{candidate}/edit", follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/browse"
        assert not EditorDOM(response.text).root.all("form", id="item-edit-form")
    assert _snapshot(db) == before
