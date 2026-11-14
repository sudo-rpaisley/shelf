from pathlib import Path

from tests.conftest import _insert_item
from tests.test_item_editor_partials import EditorDOM


ROOT = Path(__file__).resolve().parents[1]


def test_item_edit_is_sectioned_without_changing_the_save_contract(_isolated_db, editor_client, db):
    item_id = _insert_item(db, title="Sectioned editor", isbn=None, media_type="dvd")
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert response.status_code == 200
    dom = EditorDOM(response.text)
    form = dom.form
    script = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")

    nav = dom.root.one("nav", **{"data-testid": "edit-section-nav"})
    sections = ("general", "artwork", "series", "identifiers", "location", "media", "tags")
    assert [link.attrs["href"] for link in nav.all("a")] == [f"#edit-{section}" for section in sections]
    for section in sections:
        node = dom.root.one("section", id=f"edit-{section}", **{"data-testid": f"edit-section-{section}"})
        assert (node in form.all("section")) == (section != "tags")

    # The tags island sits outside the save form (it carries its own
    # hx-post/hx-delete controls; nesting forms is invalid HTML).
    tags = dom.root.one("section", id="edit-tags")
    assert tags.parent is form.parent
    assert form.parent.children.index(tags) > form.parent.children.index(form)
    assert tags.one("form").attrs["hx-post"] == f"/api/items/{item_id}/tags"
    assert nav.one("a", href="#edit-tags")
    assert all(not node.all("form") for node in dom.root.all("form"))

    # This is still the existing item edit form: sectioning must not create
    # another persistence path or make navigation itself save anything.
    assert form.attrs["action"] == f"/api/items/{item_id}"
    assert form.attrs["method"] == "post"
    assert form.attrs["enctype"] == "multipart/form-data"
    save = form.one("button", **{"data-testid": "save-btn"})
    assert save.attrs["type"] == "submit" and save.text.strip() == "Save"
    assert form.all("button", type="submit") == [save]

    # Check actual macro output from the registered page, including fields
    # whose shared media-specific partial does not match this DVD's type.
    for name, kind, label in (
        ("title", "text", "Title"), ("subtitle", "text", "Subtitle"),
        ("authors", "text", "Director"), ("publisher", "text", "Publisher"),
        ("publish_year", "number", "Year"), ("isbn", "text", "ISBN"),
        ("page_count", "number", "Pages"), ("series_name", "text", "Series"),
        ("series_position", "number", "Series #"), ("narrator", "text", "Narrator"),
        ("duration_mins", "number", "Duration (min)"), ("asin", "text", "ASIN"),
        ("date_started", "date", "Date Started"), ("date_finished", "date", "Date Finished"),
    ):
        assert form.one("input", id=name, name=name).attrs["type"] == kind
        assert form.one("label", **{"for": name}).text.strip() == label

    # Direct controls also retain their native names, types and labels.
    for name, label in (
        ("media_type", "Media Type"), ("location_id", "Location"),
        ("language", "Language"), ("platform", "Platform"), ("reading_status", "Status"),
    ):
        assert form.one("select", id=name, name=name)
        assert form.one("label", **{"for": name}).text.strip() == label
    assert form.one("input", type="hidden", name="owned").attrs["value"] == "0"
    owned = form.one("input", type="checkbox", name="owned")
    assert owned.attrs["value"] == "1" and owned.parent.tag == "label"
    assert owned.parent.text.strip() == "I own this item"
    assert form.one("input", id="manual_value", name="manual_value").attrs["type"] == "number"
    assert form.one("label", **{"for": "manual_value"}).text.strip().startswith("Value (")
    for name, label in (("description", "Description"), ("notes", "Notes")):
        assert form.one("textarea", id=name, name=name)
        assert form.one("label", **{"for": name}).text.strip() == label
    assert form.one("input", type="file", name="cover").attrs["accept"] == "image/*"

    # Media-specific groups stay in the DOM so changing Media Type can reveal
    # them immediately without changing what the form submits.
    assert form.one("section", id="edit-series").attrs["data-media-types"] == "book audiobook ebook comic manga"
    assert form.one("section", id="edit-media").attrs["data-media-types"] == "video_game audiobook"
    assert "updateEditSectionVisibility" in script
    assert "mediaSelect.addEventListener('change'" in script
    assert "kids_book" not in response.text

    # Artwork exposes the existing safe manual-cover URL route directly from
    # Edit without nesting another form inside the item-save form. The HTMX
    # action applies only the cover and deliberately stays on the edit page so
    # unsaved metadata remains in the DOM.
    assert form.one("input", id="edit-cover-url", name="url").attrs["type"] == "url"
    cover_url = form.one("button", **{"data-testid": "edit-cover-url-submit"})
    assert cover_url.attrs["type"] == "button"
    assert cover_url.attrs["hx-post"] == f"/api/items/{item_id}/cover-url"
    assert cover_url.attrs["hx-include"] == "#edit-cover-url"
    assert cover_url.attrs["hx-vals"] == '{"return_to":"edit"}'
    assert cover_url.attrs["hx-swap"] == "none"
