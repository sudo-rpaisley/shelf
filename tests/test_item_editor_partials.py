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


# Coverage-only follow-up: the original 65 cases above remain unchanged. These
# cases additionally submit ordered multipart controls without the fixture CSRF
# header, against populated domains. Encoding and csrf.js injection are simulated;
# no browser or execution of that JavaScript is claimed.
NATIVE_DOMAIN_TABLES = (*DOMAIN_TABLES, "reading_log", "user_item_state", "user_item_events")


@dataclass(frozen=True)
class NativeControl:
    name: str
    value: str | bytes
    filename: str | None = None


def _native_successful_controls(dom):
    """Document-order controls, including the browser's unselected file part."""
    controls = []
    for node in dom.form.all():
        attrs = node.attrs
        name = attrs.get("name")
        if not name or "disabled" in attrs:
            continue
        if node.tag == "input":
            kind = attrs.get("type", "text")
            if kind in {"submit", "button", "reset"}:
                continue
            if kind in {"checkbox", "radio"} and "checked" not in attrs:
                continue
            if kind == "file":
                controls.append(NativeControl(name, b"", filename=""))
            else:
                controls.append(NativeControl(name, attrs.get("value", "")))
        elif node.tag == "textarea":
            controls.append(NativeControl(name, node.text))
        elif node.tag == "select":
            options = node.all("option")
            selected = [option for option in options if "selected" in option.attrs]
            for option in selected or options[:1]:
                controls.append(NativeControl(name, option.attrs.get("value", option.text)))
    return controls


def _native_multipart(controls):
    """Encode a list, never a mapping: duplicate names retain their wire order."""
    import re

    boundary = "shelf-native-editor-control-boundary"
    body = bytearray()
    for control in controls:
        assert re.fullmatch(r"[a-z_]+", control.name)
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{control.name}"'.encode())
        if control.filename is not None:
            assert control.filename == "" and control.value == b""
            body.extend(b'; filename=""\r\nContent-Type: application/octet-stream')
        body.extend(b"\r\n\r\n")
        # Native form submission canonicalizes text line endings to CRLF.
        value = control.value
        if isinstance(value, str):
            value = re.sub(r"\r\n|\r|\n", "\r\n", value).encode("utf-8")
        body.extend(value)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _native_domain_snapshot(db):
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(NATIVE_DOMAIN_TABLES) <= present
    snapshot = {}
    for table in NATIVE_DOMAIN_TABLES:
        columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        key_columns = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
        assert key_columns
        # The existing author index replaces identical composite-key rows on
        # save. SQLite rowid insertion order is not their persisted identity or
        # author display order (position is). Preserve every SELECT * value and
        # row count, comparing in the actual schema's primary-key order.
        order = ", ".join(f'"{name}"' for name in key_columns)
        snapshot[table] = [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    return snapshot


def _native_cover_snapshot():
    from hashlib import sha256

    from app import config

    return {
        path.relative_to(config.COVERS_DIR).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(config.COVERS_DIR.rglob("*")) if path.is_file()
    }


@pytest.fixture
def native_populated_editor(editor_client, editor_user, viewer_user, db):
    """Consistent scratch catalogue/copy seams plus real account/history rows."""
    from PIL import Image

    from app import config
    from app.services import (
        author_index, item_copies, item_write, locations, media_groups,
        music_catalog, periodical_records, personal_library, tags,
    )

    db.execute("BEGIN IMMEDIATE")
    home = locations.create_location(db, "Home <b> & %")
    primary_location = locations.create_location(db, "Primary shelf", parent_id=home)
    secondary_location = locations.create_location(db, "Other shelf", parent_id=home)
    ids = {}
    primary_copies = {}
    # Raw insertion through the sanctioned fixture is deliberate ONLY for the
    # legacy-invalid identifiers. Their compatibility exemption is the subject
    # of the native save; derived authors/copies are seeded through their funnels.
    for key, media_type, isbn, upc in (
        ("book", "book", "9780441172710", "1234567890120"),
        ("audiobook", "audiobook", "B00EXAMPLE", "1234567890129"),
    ):
        ids[key] = _insert_item(
            db, title=f"Native {key} " + RAW, isbn=isbn, isbn10="legacy10",
            upc=upc, media_type=media_type, authors="Native author " + RAW,
            subtitle=None if key == "book" else "Audio subtitle " + RAW,
            publisher="Native publisher " + RAW, publish_year=0,
            page_count=0 if key == "book" else None,
            series_name="Native series " + RAW, series_position=0 if key == "book" else 2.5,
            narrator="Narrator " + RAW if key == "audiobook" else None,
            duration_mins=0 if key == "audiobook" else None,
            asin="B0D1234567" if key == "audiobook" else None,
            language="tlh", reading_status=None, date_started="2025-01-02",
            date_finished=None, description="Description\r\n" + RAW,
            notes="Catalogue notes\r\n" + RAW, manual_value=0,
            location_id=primary_location, owned=1,
            cover_path=f"covers/native-{key}.png", cover_edition_isbn="9780547928227",
            cover_edition_language="it", updated_at="2001-01-01 00:00:00",
        )
        author_index.reindex_item(db, ids[key], "Native author " + RAW)
        # Primary/default copy shape and location exactly match the catalogue
        # seam before any POST. No missing primary is left for Save to invent.
        primary_copies[key] = item_copies.insert_copy(db, {
            "item_id": ids[key], "copy_number": 1, "is_primary": 1,
            "location_id": primary_location, "position_order": 7,
            "condition": "Good", "notes": "Primary copy " + RAW,
            "acquired_date": "2020-01-02", "acquisition_source": "Fixture shop",
            "acquisition_price": 0, "provenance": "Primary provenance " + RAW,
            "copy_barcode": f"native-{key}-primary", "updated_at": "2001-01-01 00:00:00",
        })
        secondary = item_copies.add_copy(db, ids[key], {
            "location_id": secondary_location, "position_order": 23,
            "condition": "New", "notes": "Additional copy " + RAW,
            "acquired_date": "2021-03-04", "acquisition_source": "Fixture gift",
            "acquisition_price": 12.5, "provenance": "Secondary provenance " + RAW,
            "copy_barcode": f"native-{key}-secondary",
        })
        copies = item_copies.copies_for_item(db, ids[key])
        assert [(row["id"], row["copy_number"], row["is_primary"], row["location_id"]) for row in copies] == [
            (primary_copies[key], 1, 1, primary_location), (secondary, 2, 0, secondary_location),
        ]
        tags.attach_tags(db, ids[key], ["Signed <b> & %", f"Native {key}"])
        Image.new("RGB", (4, 6), color=(40, 80, 120)).save(config.COVERS_DIR / f"native-{key}.png")

    for key, wishlisted in (("wishlist", True), ("neither", False)):
        ids[key] = item_write.insert_item(
            db, title=f"Native {key} " + RAW, authors="Unowned author " + RAW,
            media_type="book", owned=0, wishlisted=wishlisted,
            reading_status="reading", date_started="2025-03-04", date_finished=None,
            notes="Unowned notes\r\n" + RAW, updated_at="2001-01-01 00:00:00",
        )
        tags.attach_tags(db, ids[key], ["Signed <b> & %"])
        assert item_copies.copies_for_item(db, ids[key]) == []

    music_id = item_write.insert_item(db, title="Native release " + RAW, authors="Artist", media_type="vinyl")
    music_catalog.save_release(db, music_id, {
        "title": "Native release " + RAW, "artist_credit": "Artist",
        "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
        "musicbrainz_release_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "catalog_number": "CAT-1", "label": "Label " + RAW, "country": "GB",
        "media": [{"position": 1, "format": "12\" Vinyl", "tracks": [
            {"position": 1, "number": "A1", "title": "Track " + RAW, "duration_ms": 0},
        ]}],
    })
    music_catalog.add_identifier(db, music_id, "matrix_runout", "MATRIX-1", "Side A")
    periodical_id = item_write.insert_item(db, title="Native issue " + RAW, media_type="magazine")
    publication_id = periodical_records.upsert_publication(db, title="Native publication " + RAW, issn="01617370")
    periodical_records.link_issue(db, item_id=periodical_id, publication_id=publication_id,
                                  issue_number="1", volume="2", issue_date="2025-01-01")
    assert media_groups.link_items(db, ids["book"], ids["audiobook"], link_type="format")
    assert media_groups.link_items(db, ids["book"], music_id, link_type="related")
    assert media_groups.link_items(db, ids["book"], periodical_id, link_type="related")

    for item_id in ids.values():
        for user in (editor_user, viewer_user):
            for status, progress in (("in_progress", "0"), ("finished", "100")):
                state = personal_library.save(db, user["id"], item_id, personal_library.parse_form({
                    "status": status, "progress": progress, "rating": "4", "favourite": "1",
                    "on_list": "1", "notes": f"{user['username']} personal " + RAW,
                }))
                assert state is not None and state["last_event_id"] > 0
    db.commit()

    # Populate legacy/shared reading history through the actual status route.
    # No provider identity is attached, so this schedules no provider push.
    for key in ("book", "audiobook"):
        for _ in range(2):
            response = editor_client.post(f"/api/items/{ids[key]}/reading-status", data={"status": "read"})
            assert response.status_code == 200
        item = db.execute("SELECT * FROM items_live WHERE id = ?", (ids[key],)).fetchone()
        copy = db.execute("SELECT * FROM copies_live WHERE id = ?", (primary_copies[key],)).fetchone()
        assert item["location_id"] == copy["location_id"] == primary_location
        assert copy["updated_at"] == "2001-01-01 00:00:00"
    snapshot = _native_domain_snapshot(db)
    assert all(snapshot.values()), "every domain included in this follow-up must actually contain rows"
    assert len(snapshot["reading_log"]) == 4
    assert len(snapshot["user_item_state"]) == 8 and len(snapshot["user_item_events"]) == 16
    assert len(_native_cover_snapshot()) == 2
    return {"ids": ids, "primary_copies": primary_copies}


def _native_post_without_header(client, dom, token_mode, record_property):
    from email import policy
    from email.parser import BytesParser
    from hashlib import sha256
    from http.cookies import SimpleCookie
    import json

    controls = _native_successful_controls(dom)
    assert not any(control.name == "_csrf" for control in controls), "GET still relies on csrf.js injection"
    assert [control for control in controls if control.filename is not None] == [NativeControl("cover", b"", "")]
    assert not any(control.name in {"name", "user_id", "last_event_id"} for control in controls)
    cookie_token = client.cookies.get("csrf_token")
    assert cookie_token
    client.headers.pop("X-CSRF-Token")
    assert "X-CSRF-Token" not in client.headers
    # This is explicitly simulated csrf.js injection, NOT JavaScript execution.
    if token_mode != "missing":
        controls.append(NativeControl("_csrf", cookie_token if token_mode == "matching" else cookie_token + "-wrong"))
    body, content_type = _native_multipart(controls)
    response = client.post(dom.form.attrs["action"], content=body,
                           headers={"Content-Type": content_type}, follow_redirects=False)
    request = response.request
    assert "X-CSRF-Token" not in request.headers and "HX-Request" not in request.headers
    assert request.content == body and request.headers["content-type"] == content_type
    cookies = SimpleCookie()
    cookies.load(request.headers["cookie"])
    assert cookies["csrf_token"].value == cookie_token
    wire = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content,
    )
    parts = list(wire.iter_parts())
    assert [part.get_param("name", header="content-disposition") for part in parts] == [control.name for control in controls]
    for part, control in zip(parts, controls, strict=True):
        assert part.get_filename() == control.filename
        expected = control.value if isinstance(control.value, bytes) else control.value.encode("utf-8")
        assert part.get_payload(decode=True) == expected
    submitted_tokens = [part.get_payload(decode=True).decode() for part in parts
                        if part.get_param("name", header="content-disposition") == "_csrf"]
    assert submitted_tokens == ([] if token_mode == "missing" else [cookie_token if token_mode == "matching" else cookie_token + "-wrong"])
    ordered_boxes = [(control.name, control.value) for control in controls if control.name in {"owned", "wishlisted"}]
    record_property("native_wire", json.dumps({
        "body_sha256": sha256(body).hexdigest(), "token_mode": token_mode,
        "header_csrf_present": False, "ordered_boxes": ordered_boxes,
        "part_names": [control.name for control in controls], "empty_filename_cover": True,
    }))
    return response, controls


@pytest.mark.parametrize("target", ("book", "audiobook", "wishlist", "neither"))
def test_native_multipart_no_change_preserves_populated_domains(
    editor_client, db, native_populated_editor, target, record_property,
):
    from copy import deepcopy
    import json

    item_id = native_populated_editor["ids"][target]
    before = _native_domain_snapshot(db)
    covers_before = _native_cover_snapshot()
    _, dom = _get(editor_client, item_id, "?from=series")
    _assert_field_contract(dom, item_id)
    controls = _native_successful_controls(dom)
    expected_boxes = (
        [("owned", "0"), ("owned", "1"), ("wishlisted", "0")] if target in {"book", "audiobook"}
        else [("owned", "0"), ("wishlisted", "0"), ("wishlisted", "1")] if target == "wishlist"
        else [("owned", "0"), ("wishlisted", "0")]
    )
    assert [(control.name, control.value) for control in controls if control.name in {"owned", "wishlisted"}] == expected_boxes
    item_before = next(row for row in before["items"] if row["id"] == item_id)
    for name in ("isbn", "upc", "series_name", "narrator", "duration_mins", "date_started", "date_finished", "subtitle"):
        values = [control.value for control in controls if control.name == name]
        assert values == ["" if item_before[name] is None else str(item_before[name])]
    if target in {"book", "audiobook"}:
        assert dom.form.one("p", **{"data-testid": "isbn-stored-invalid"})
        assert dom.form.one("p", **{"data-testid": "upc-stored-invalid"})
        assert [control.value for control in controls if control.name == "manual_value"] == ["0.0"]
    assert _native_domain_snapshot(db) == before and _native_cover_snapshot() == covers_before
    clock_before = db.execute("SELECT datetime('now')").fetchone()[0]
    response, _ = _native_post_without_header(editor_client, dom, "matching", record_property)
    clock_after = db.execute("SELECT datetime('now')").fetchone()[0]
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item_id}?from=series"
    after = _native_domain_snapshot(db)
    # Account for ONLY the known native writer stamps on this item and its
    # existing primary copy. Every other timestamp, field, row and history/event
    # record is compared in full; additional copies are never exempted.
    expected = deepcopy(before)
    stamps = []
    stamp_targets = [("items", item_id)]
    if target in native_populated_editor["primary_copies"]:
        stamp_targets.append(("item_copies", native_populated_editor["primary_copies"][target]))
    for table, row_id in stamp_targets:
        prior = next(row for row in expected[table] if row["id"] == row_id)
        current = next(row for row in after[table] if row["id"] == row_id)
        assert clock_before <= current["updated_at"] <= clock_after
        stamps.append({"table": table, "id": row_id, "before": prior["updated_at"], "after": current["updated_at"]})
        if table == "item_copies" or target in {"wishlist", "neither"}:
            assert prior["updated_at"] == "2001-01-01 00:00:00" and current["updated_at"] != prior["updated_at"]
        prior["updated_at"] = current["updated_at"]
    assert after == expected
    assert _native_cover_snapshot() == covers_before
    record_property("known_native_stamps", json.dumps(stamps))
    record_property("domain_before", json.dumps(before, ensure_ascii=False))
    record_property("domain_after", json.dumps(after, ensure_ascii=False))
    record_property("cover_hashes_before_and_after", json.dumps(covers_before))


@pytest.mark.parametrize("target", ("book", "audiobook", "wishlist", "neither"))
@pytest.mark.parametrize("token_mode", ("missing", "bad"))
def test_native_multipart_form_csrf_refuses_without_any_domain_write(
    editor_client, db, native_populated_editor, target, token_mode, record_property,
):
    import json

    item_id = native_populated_editor["ids"][target]
    before = _native_domain_snapshot(db)
    covers_before = _native_cover_snapshot()
    _, dom = _get(editor_client, item_id)
    assert _native_domain_snapshot(db) == before
    response, _ = _native_post_without_header(editor_client, dom, token_mode, record_property)
    assert response.status_code == 403 and response.text == "CSRF validation failed"
    assert _native_domain_snapshot(db) == before
    assert _native_cover_snapshot() == covers_before
    record_property("domain_before_and_after", json.dumps(before, ensure_ascii=False))
    record_property("cover_hashes_before_and_after", json.dumps(covers_before))
