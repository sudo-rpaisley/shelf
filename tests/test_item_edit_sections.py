from pathlib import Path

import json

from tests.conftest import _insert_item
from tests.test_ui_phase4_contracts_20261121 import _Markup


ROOT = Path(__file__).resolve().parents[1]


def test_item_edit_is_sectioned_without_changing_the_save_contract(editor_client, db):
    item_id = _insert_item(db, title="Sectioned editor", isbn=None)
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    root = _Markup(response.text).root
    form = root.one("form", id="item-edit-form")
    script = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")

    nav = root.one("nav", **{"data-testid": "edit-section-nav"})
    for section in ("general", "artwork", "series", "identifiers", "location", "media", "tags"):
        panel = root.one("section", id=f"edit-{section}", **{"data-testid": f"edit-section-{section}"})
        assert panel.within(form) == (section != "tags")
        link = nav.one("a", href=f"#edit-{section}")
        assert link.attrs["@click.prevent"] == f"show('{section if section != 'location' else 'copies'}')"
        assert "hx-post" not in link.attrs

    # The tags island sits outside the save form (it carries its own
    # hx-post/hx-delete controls; nesting forms is invalid HTML).
    tags = root.one(id="edit-tags")
    assert not tags.within(form)
    assert len(form.find_all("form")) == 1
    tag_form = tags.one("form")
    assert tag_form.attrs["hx-post"] == f"/api/items/{item_id}/tags"
    assert tag_form.attrs["hx-target"] == "#item-tags"
    assert tag_form.attrs["hx-swap"] == "outerHTML"
    assert nav.one("a", href="#edit-tags")
    assert tags.one("p", **{"data-tags-label": None}).text == "Tags"
    assert [style.text for style in root.find_all("style") if "#edit-tags" in style.text] == [
        "#edit-tags [data-tags-label] { display: none; }",
    ]

    # This is still the existing item edit form: sectioning must not create
    # another persistence path or make navigation itself save anything.
    assert form.attrs["action"] == f"/api/items/{item_id}"
    assert form.attrs["method"] == "post"
    assert form.attrs["enctype"] == "multipart/form-data"
    assert form.one("button", **{"data-testid": "save-btn"}).attrs["type"] == "submit"

    # Text/number/date fields remain real successful controls after rendering.
    for field in (
        "title", "subtitle", "authors", "publisher", "publish_year", "isbn",
        "page_count", "series_name", "series_position", "narrator",
        "duration_mins", "asin", "date_started", "date_finished",
    ):
        control = form.one("input", id=field, name=field)
        assert "disabled" not in control.attrs

    # These controls are written directly in the template.
    for field in (
        "media_type", "location_id", "owned", "language", "manual_value",
        "platform", "reading_status", "description", "notes", "cover",
    ):
        controls = form.find_all(name=field)
        assert controls, field
        assert any("disabled" not in control.attrs for control in controls), field

    # Media-specific groups stay in the DOM so changing Media Type can reveal
    # them immediately without changing what the form submits.
    assert form.one(**{"x-data": "isbnCamera"}).attrs["data-media-types"].split() == [
        "book", "kids_book", "audiobook", "ebook", "comic", "digital_comic", "manga", "digital_manga",
    ]
    assert form.one(id="platform").parent.attrs["data-media-types"].split() == ["video_game", "digital_game"]
    assert form.one(id="asin").parent.parent.attrs["data-media-types"] == "audiobook"
    assert "updateEditSectionVisibility" in script
    assert "mediaSelect.addEventListener('change'" in script
    # Legacy kids_book stays readable; the full rendered 17-type matrix is
    # separately pinned in test_ui_phase4_contracts_20261121.
    assert "fetch(" not in script
    assert ".submit(" not in script

    # Artwork exposes the existing safe manual-cover URL route directly from
    # Edit without nesting another form inside the item-save form. The HTMX
    # action applies only the cover and deliberately stays on the edit page so
    # unsaved metadata remains in the DOM.
    assert form.one("input", id="edit-cover-url").attrs["type"] == "url"
    use_url = form.one("button", **{"data-testid": "edit-cover-url-submit"})
    assert use_url.attrs["type"] == "button"
    assert use_url.attrs["hx-post"] == f"/api/items/{item_id}/cover-url"
    assert use_url.attrs["hx-include"] == "#edit-cover-url"
    assert json.loads(use_url.attrs["hx-vals"]) == {"return_to": "edit"}
