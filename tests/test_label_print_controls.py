"""The sheet's native layout controls must never broaden its label selection."""

import re
from html import unescape
from html.parser import HTMLParser

import pytest

from app.services import copy_labels, item_copies
from tests.conftest import _insert_item, _insert_location
from tests.test_features_gate import off


class _Controls(HTMLParser):
    """Read the actual successful controls, not a hand-written query subset."""

    def __init__(self, text):
        super().__init__()
        self.forms = []
        self.controls = []
        self.options = []
        self.label_targets = []
        self.attributes = []
        self._inside = False
        self.feed(text)
        assert len(self.forms) == 1, "expected one sheet layout form"
        self.form = self.forms[0]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("data-testid") == "labels-sheet-controls":
            self.forms.append(attrs)
            self._inside = True
        if not self._inside:
            return
        self.attributes.append(attrs)
        if tag in {"input", "select"} and "name" in attrs:
            self.controls.append((tag, attrs))
        elif tag == "option":
            self.options.append(attrs)
        elif tag == "label":
            self.label_targets.append(attrs.get("for"))

    def handle_endtag(self, tag):
        if tag == "form":
            self._inside = False

    def values(self):
        values = {}
        for tag, attrs in self.controls:
            assert "disabled" not in attrs
            name = attrs["name"]
            assert name not in values, f"duplicate successful control: {name}"
            if tag == "select":
                selected = [option for option in self.options if "selected" in option]
                assert len(selected) == 1
                values[name] = selected[0]["value"]
            else:
                values[name] = attrs.get("value", "")
        return values


def _copies(db, codes, *, title="Selected", location_id=None, isbn=None):
    item_id = _insert_item(db, title=title, isbn=isbn)
    for number, code in enumerate(codes, 1):
        item_copies.insert_copy(db, {
            "item_id": item_id, "copy_number": number, "is_primary": int(number == 1),
            "copy_barcode": code, "location_id": location_id,
        })
    db.commit()
    return item_id


def _catalogue_state(db):
    return tuple(
        tuple(tuple(row) for row in db.execute(f"SELECT * FROM {view} ORDER BY id"))
        for view in ("items_live", "copies_live")
    )


def _codes(text):
    return [unescape(code) for code in re.findall(r'data-label="([^"]+)"', text)]


def _href(text, testid):
    match = re.search(rf'href="([^"]+)"[^>]*data-testid="{testid}"', text)
    assert match, f"missing {testid} link"
    return unescape(match[1])


def _submit(client, controls, **changes):
    assert controls.form["method"].lower() == "get"
    assert controls.form["action"] == "/labels/sheet"
    response = client.get(controls.form["action"], params={**controls.values(), **changes})
    assert response.status_code == 200
    return response


def test_assigned_range_survives_stock_and_start_changes(editor_client, db):
    location = _insert_location(db, "New labels only")
    _copies(db, ["SHF-000004", "HAND-TYPED"], title="Already labelled")
    _copies(db, [None, None], location_id=location)
    assigned = editor_client.post("/api/labels/assign", data={"scope": "location", "location_id": location})
    assert assigned.status_code == 200
    sheet_url = _href(assigned.text, "labels-assigned-sheet")
    _copies(db, ["SHF-000007", None], title="Not in the assigned batch")
    before = _catalogue_state(db)

    first = editor_client.get(sheet_url)
    assert first.status_code == 200
    controls = _Controls(first.text)
    selector = {"from": "SHF-000005", "to": "SHF-000006"}
    assert controls.values() == {**selector, "preset": "avery5160", "start": "1"}

    a4 = _submit(editor_client, controls, preset="averyl7160", start="5")
    assert _codes(a4.text) == ["SHF-000005", "SHF-000006"]
    assert a4.text.count("data-label-blank") == 4
    assert "size: A4" in a4.text
    controls = _Controls(a4.text)
    assert controls.values() == {**selector, "preset": "averyl7160", "start": "5"}

    letter = _submit(editor_client, controls, preset="avery5160", start="30")
    assert _codes(letter.text) == ["SHF-000005", "SHF-000006"]
    assert letter.text.count("data-label-blank") == 29
    assert "size: letter" in letter.text
    assert _Controls(letter.text).values() == {**selector, "preset": "avery5160", "start": "30"}
    assert _catalogue_state(db) == before


@pytest.mark.parametrize("selection", ["item", "location", "no_identifier", "all", "from", "to"])
def test_native_get_keeps_the_active_selector(editor_client, db, selection):
    location = _insert_location(db)
    item_id = _copies(db, ["SHF-000001", "SHF-000003"], location_id=location)
    _copies(db, ["SHF-000002", "MANUAL-CODE"], title="Other", isbn="9780000000026")
    selectors = {
        "item": ({"item_id": str(item_id)}, ["SHF-000001", "SHF-000003"]),
        "location": ({"scope": "location", "location_id": str(location)}, ["SHF-000001", "SHF-000003"]),
        "no_identifier": ({"scope": "no_identifier"}, ["SHF-000001", "SHF-000003"]),
        "all": ({"scope": "all"}, ["MANUAL-CODE", "SHF-000001", "SHF-000002", "SHF-000003"]),
        "from": ({"from": "SHF-000002"}, ["SHF-000002", "SHF-000003"]),
        "to": ({"to": "SHF-000002"}, ["SHF-000001", "SHF-000002"]),
    }
    selector, expected = selectors[selection]
    # Inactive query parameters must not replace the router's chosen selector.
    query = {**selector, "scope": "all"} if selection == "item" else selector
    before = _catalogue_state(db)
    response = editor_client.get("/labels/sheet", params=query)
    assert response.status_code == 200
    controls = _Controls(response.text)
    assert controls.values() == {**selector, "preset": "avery5160", "start": "1"}

    response = _submit(editor_client, controls, preset="averyl7160", start="3")
    assert _codes(response.text) == expected
    assert _Controls(response.text).values() == {**selector, "preset": "averyl7160", "start": "3"}
    assert response.text.count("data-label-blank") == 2
    assert _catalogue_state(db) == before


def test_layout_changes_on_a_continuation_keep_the_current_chunk(editor_client, db):
    item_id = _copies(db, [f"SHF-{number:06d}" for number in range(1, 302)])
    first = editor_client.get("/labels/sheet", params={"item_id": item_id, "start": 3})
    assert first.status_code == 200
    second = editor_client.get(_href(first.text, "labels-sheet-next"))
    assert second.status_code == 200
    controls = _Controls(second.text)
    assert controls.values() == {"item_id": str(item_id), "offset": "300", "preset": "avery5160", "start": "3"}

    changed = _submit(editor_client, controls, preset="averyl7160", start="7")
    assert _codes(changed.text) == ["SHF-000301"]
    assert "data-label-blank" not in changed.text
    assert _Controls(changed.text).values() == {
        "item_id": str(item_id), "offset": "300", "preset": "averyl7160", "start": "7",
    }
    back = editor_client.get(_href(changed.text, "labels-sheet-prev"))
    assert back.status_code == 200
    assert _codes(back.text) == [f"SHF-{number:06d}" for number in range(1, 301)]
    assert back.text.count("data-label-blank") == 6


@pytest.mark.parametrize("preset,start,rendered_preset,rendered_start", [
    ("avery5160", "1", "avery5160", "1"),
    ("averyl7160", "9", "averyl7160", "9"),
    ("unknown", "99", "avery5160", "30"),
])
def test_controls_reflect_server_context_and_have_native_accessible_markup(
    editor_client, preset, start, rendered_preset, rendered_start,
):
    response = editor_client.get("/labels/sheet", params={"preset": preset, "start": start})
    assert response.status_code == 200
    controls = _Controls(response.text)
    assert controls.values() == {"scope": "all", "preset": rendered_preset, "start": rendered_start}
    assert controls.form["method"] == "get"
    assert controls.form["action"] == "/labels/sheet"
    assert controls.form["aria-label"] == "Label print settings"
    assert {option["value"] for option in controls.options} == set(copy_labels.PRESETS)
    visible = [attrs for _, attrs in controls.controls if attrs.get("type") != "hidden"]
    assert len(visible) == 2
    assert {attrs["id"] for attrs in visible} == set(controls.label_targets)
    number = next(attrs for _, attrs in controls.controls if attrs["name"] == "start")
    assert number["type"] == "number" and number["min"] == "1" and number["step"] == "1"
    assert "required" in number and number["aria-describedby"]
    assert not any(key.lower().startswith(("on", "hx-", "x-"))
                   for attrs in controls.attributes for key in attrs)


def test_unmatched_range_is_escaped_and_stays_empty_after_layout_changes(editor_client, db):
    _copies(db, ["SHF-000001"])
    selector = {"from": 'SHF-000001" autofocus onfocus="alert(1)'}
    response = editor_client.get("/labels/sheet", params=selector)
    assert response.status_code == 200
    controls = _Controls(response.text)
    assert controls.values() == {**selector, "preset": "avery5160", "start": "1"}
    assert not any("autofocus" in attrs or "onfocus" in attrs for attrs in controls.attributes)
    changed = _submit(editor_client, controls, preset="averyl7160", start="4")
    assert _codes(changed.text) == []
    assert 'data-testid="labels-sheet-empty"' in changed.text
    assert _Controls(changed.text).values() == {**selector, "preset": "averyl7160", "start": "4"}


def test_viewers_do_not_get_sheet_controls(viewer_client):
    response = viewer_client.get("/labels/sheet", follow_redirects=False)
    assert response.status_code in (303, 403)
    assert 'data-testid="labels-sheet-controls"' not in response.text


def test_collector_off_does_not_expose_sheet_controls(admin_client):
    off("collector")
    response = admin_client.get("/labels/sheet")
    assert response.status_code == 200
    assert 'data-testid="feature-enable-form"' in response.text
    assert 'data-testid="labels-sheet-controls"' not in response.text
