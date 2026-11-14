"""Exact-copy HTTP rendering and independent item-fragment regressions."""

from contextlib import contextmanager
from html.parser import HTMLParser
import re

import pytest

from app import nav
from app.services import item_copies, locations
from tests.conftest import _insert_item


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_lone_unlocated_missing_only_copy_keeps_visible_badge(request, db, role):
    """No condition/acquisition/notes to accidentally select the details arm."""
    client = request.getfixturevalue(f"{role}_client")
    item_id = _insert_item(db, title="Missing only", isbn=None)
    copy_id = item_copies.insert_copy(db, {
        "item_id": item_id, "copy_number": 1, "is_primary": 1,
        "missing_since": "2026-01-02",
    })
    db.commit()

    response = client.get(f"/item/{item_id}")

    assert response.status_code == 200
    assert 'id="item-copies" class="hidden"' not in response.text
    assert f'data-testid="copy-missing-{copy_id}"' in response.text
    assert "missing since 2026-01-02" in response.text
    assert (f'data-testid="copy-found-{copy_id}"' in response.text) == (role == "editor")


class _Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _seed(db, *, location=True, position=70, missing=None):
    primary_shelf = locations.create_location(db, "Primary room")
    root = locations.create_location(db, "Actual room")
    shelf = locations.create_location(db, "Actual shelf", parent_id=root)
    item = _insert_item(db, title="Exact secondary title", isbn=None, location_id=primary_shelf)
    primary = item_copies.insert_copy(db, {
        "item_id": item, "copy_number": 1, "is_primary": 1, "location_id": primary_shelf,
    })
    secondary = item_copies.insert_copy(db, {
        "item_id": item, "copy_number": 9, "location_id": shelf if location else None,
        "position_order": position, "copy_barcode": "EXACT-NINE", "missing_since": missing,
    })
    db.commit()
    return {"item": item, "primary": primary, "copy": secondary, "root": root,
            "shelf": shelf, "primary_shelf": primary_shelf}


def _url(seed):
    return f'/item/{seed["item"]}/copies/{seed["copy"]}/locate'


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_registered_get_is_viewer_accessible_exact_and_has_no_arrange_payload(request, db, role):
    client = request.getfixturevalue(f"{role}_client")
    seed = _seed(db, missing="2026-01-02")
    response = client.get(_url(seed), params={"at": seed["shelf"], "from": "series"})
    assert response.status_code == 200
    assert response.context["target"]["copy_id"] == seed["copy"]
    assert response.context["location"]["id"] == seed["shelf"]
    html = response.text
    assert "Selected copy" in html and "Copy #9" in html and "EXACT-NINE" in html
    assert "Marked missing since 2026-01-02" in html
    assert "Copy record updated:" in html
    assert f'id="copy-{seed["copy"]}"' in html
    assert f'data-locator-copy-id="{seed["primary"]}"' not in html
    assert f'href="/browse?location_filter={seed["root"]}"' in html
    assert f'href="/locations/{seed["shelf"]}/arrange"' in html
    assert f'href="/item/{seed["item"]}?from=series#copy-locator-open-{seed["copy"]}"' in html
    assert 'href="/series"' in html
    # Check the owned surface, not base.html's account/shortcut controls.
    body = html.split('data-copy-locator>', 1)[1].split('</main>', 1)[0]
    assert 'href="/settings' not in body
    for prohibited in ("hx-post", "hx-delete", "hx-get", "<form", "data-save-order",
                       "data-auto-order", "data-location-order", "draggable", "Found</"):
        assert prohibited not in body
    assert "location-order.js" not in html
    assert "not a physical sighting" in body and "not a calculated physical rank" in body


@pytest.mark.parametrize("kind", ["wrong-item", "copy-trash", "item-trash", "zero-copy", "unknown", "huge"])
def test_non_live_or_mismatched_ids_get_identical_generic_404(viewer_client, db, kind):
    seed = _seed(db)
    other = _insert_item(db, title="DO NOT LEAK OTHER TITLE", isbn=None)
    db.commit()
    baseline = viewer_client.get(f'/item/{seed["item"]}/copies/987654321/locate')
    if kind == "wrong-item":
        seed["item"] = other
    elif kind == "copy-trash":
        item_copies.trash_copy(db, seed["copy"])
    elif kind == "item-trash":
        from app.services.item_write import trash_item
        trash_item(db, seed["item"])
    elif kind == "zero-copy":
        seed["item"] = other  # location-only seam does not invent a copy
        db.execute("UPDATE items SET location_id = ? WHERE id = ?", (seed["shelf"], other))
    elif kind == "unknown":
        seed["copy"] = 987654321
    else:
        seed["copy"] = 1 << 100
    db.commit()
    response = viewer_client.get(_url(seed), params={"at": seed["primary_shelf"]})
    assert response.status_code == baseline.status_code == 404
    assert response.json() == baseline.json() == {"detail": "Copy not found"}
    assert "TITLE" not in response.text and "Exact secondary" not in response.text
    assert "location" not in response.headers and "HX-Trigger" not in response.headers


@pytest.mark.parametrize("at", ["-1", str(1 << 63), "not-an-id", "1.1", "1 OR 1=1", ""])
def test_location_hint_is_validated_and_bounded(viewer_client, db, at):
    seed = _seed(db)
    assert viewer_client.get(_url(seed), params={"at": at}).status_code == 422


def test_no_location_no_position_missing_are_independent_truthful_states(viewer_client, db):
    seed = _seed(db, location=False, position=None, missing="2026-02-03")
    response = viewer_client.get(_url(seed), params={"at": 0})
    assert response.status_code == 200
    assert "No location recorded for this copy." in response.text
    assert "No position recorded" in response.text
    assert "Marked missing since 2026-02-03" in response.text
    assert "No location neighbourhood is available" in response.text
    assert response.context["before"] == response.context["after"] == []
    assert "data-copy-location-changed" not in response.text
    assert '/arrange"' not in response.text
    assert f'/browse?location_filter={seed["primary_shelf"]}' not in response.text


def test_current_move_rename_reparent_and_stale_hint_without_old_location_leak(viewer_client, db):
    seed = _seed(db)
    old_html = viewer_client.get(_url(seed), params={"at": seed["shelf"]}).text
    assert "Actual room / Actual shelf" in old_html
    new_root = locations.create_location(db, "New room")
    new_shelf = locations.create_location(db, "New shelf", parent_id=new_root)
    item_copies.update_copy(db, seed["copy"], {"location_id": new_shelf})
    db.commit()
    response = viewer_client.get(_url(seed), params={"at": seed["shelf"]})
    assert "data-copy-location-changed" in response.text
    assert "New room / New shelf" in response.text and "No position recorded" in response.text
    assert "Actual room" not in response.text and "Actual shelf" not in response.text
    locations.update_location(db, new_shelf, "Renamed shelf", parent_id=seed["root"])
    db.commit()
    response = viewer_client.get(_url(seed), params={"at": new_shelf})
    assert "Actual room / Renamed shelf" in response.text
    assert "New room / New shelf" not in response.text
    assert not response.context["location_changed"]
    assert "cannot detect a rename, reparenting" in response.text
    # An arbitrary valid ID hint is compared only; it cannot expose its name.
    response = viewer_client.get(_url(seed), params={"at": seed["primary_shelf"]})
    assert "Primary room" not in response.text
    assert response.context["location_changed"]
    item_copies.update_copy(db, seed["copy"], {"location_id": None})
    db.commit()
    assert "No location recorded for this copy." in viewer_client.get(_url(seed), params={"at": new_shelf}).text


def test_previously_unlocated_zero_hint_is_distinct_from_absent_hint(viewer_client, db):
    seed = _seed(db)
    assert "data-copy-location-changed" not in viewer_client.get(_url(seed)).text
    response = viewer_client.get(_url(seed), params={"at": 0})
    assert response.context["location_changed"]
    assert "This link previously recorded no location." in response.text


def test_cycles_render_partial_current_path_not_settings_recovery(viewer_client, db):
    seed = _seed(db)
    db.execute("UPDATE locations SET parent_id = ? WHERE id = ?", (seed["shelf"], seed["root"]))
    db.commit()
    response = viewer_client.get(_url(seed))
    assert response.status_code == 200
    assert response.context["ancestry_incomplete"]
    assert len(response.context["ancestors"]) == 1
    assert "data-copy-ancestry-incomplete" in response.text


@pytest.mark.parametrize("origin", [*nav.BACK_TARGETS, "//evil.test", "https://evil.test/", "javascript:alert(1)", "../../etc"])
def test_back_and_item_urls_use_only_existing_back_target_whitelist(viewer_client, db, origin):
    seed = _seed(db)
    response = viewer_client.get(_url(seed), params={"from": origin})
    back = nav.back_target(origin)
    assert response.status_code == 200
    assert response.context["back"] == back
    assert f'href="{back["path"]}"' in response.text
    if back["key"]:
        assert f'?from={origin}#copy-locator-open-{seed["copy"]}' in response.text
    else:
        assert "evil.test" not in response.text and "javascript:alert" not in response.text
        assert "../../etc" not in response.text
        assert f'/item/{seed["item"]}?from=' not in response.text


def test_all_locator_metadata_is_escaped_not_markup_or_url_authority(viewer_client, db):
    seed = _seed(db)
    hostile = '<img src=x onerror="alert(1)"> & </a><script>bad()</script>'
    db.execute("UPDATE items SET title = ?, media_type = ? WHERE id = ?", (hostile, hostile, seed["item"]))
    db.execute("UPDATE item_copies SET copy_barcode = ?, missing_since = ? WHERE id = ?", (hostile, hostile, seed["copy"]))
    db.execute("UPDATE locations SET label = ?, name = ? WHERE id = ?", (hostile, hostile, seed["shelf"]))
    db.commit()
    response = viewer_client.get(_url(seed))
    assert response.status_code == 200
    assert "&lt;img" in response.text and "&lt;script&gt;bad()" in response.text
    assert hostile not in response.text and '<img src=x' not in response.text
    for tag, attrs in _Tags(response.text).tags:
        assert "onerror" not in attrs
        if tag == "a":
            assert attrs["href"].startswith(("/", "#"))


def test_get_holds_one_query_only_snapshot_without_any_data_writes(viewer_client, db, monkeypatch):
    from app.database import get_db
    from app.routers import location_order as router

    seed = _seed(db, missing="2026-01-02")
    before = list(db.iterdump())
    calls = []

    @contextmanager
    def query_only():
        with get_db() as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.set_trace_callback(calls.append)
            yield conn

    monkeypatch.setattr(router, "get_db", query_only)
    for _ in range(2):
        assert viewer_client.get(_url(seed)).status_code == 200
    assert list(db.iterdump()) == before
    assert calls.count("BEGIN") == calls.count("COMMIT") == 2
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)", sql, re.I) for sql in calls)


def test_authentication_still_required(client, viewer_user, db):
    seed = _seed(db)
    response = client.get(_url(seed), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "Exact secondary" not in response.text


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_item_links_each_exact_copy_with_stable_opener_and_preserves_existing_controls(request, db, role):
    client = request.getfixturevalue(f"{role}_client")
    seed = _seed(db, position=None, missing="2026-01-02")
    response = client.get(f'/item/{seed["item"]}?from=series')
    html = response.text
    for copy_id, location_id in ((seed["primary"], seed["primary_shelf"]), (seed["copy"], seed["shelf"])):
        tag = next(attrs for tag, attrs in _Tags(html).tags if attrs.get("id") == f"copy-locator-open-{copy_id}")
        assert tag["href"] == f'/item/{seed["item"]}/copies/{copy_id}/locate?at={location_id}&from=series#copy-{copy_id}'
        assert not any(name.startswith("hx-") for name in tag)
    assert html.count("Find this copy") == 2
    assert "position" not in html  # existing unknown-position contract, incl attributes
    assert f'/browse?location_filter={seed["shelf"]}' in html
    assert (f'hx-post="/api/items/{seed["item"]}/copies/{seed["copy"]}/found"' in html) == (role == "editor")
    if role == "editor":
        swapped = client.get(f'/api/items/{seed["item"]}/copies').text
        assert f'id="copy-locator-open-{seed["copy"]}"' in swapped
        assert f'hx-get="/api/items/{seed["item"]}/copies/{seed["copy"]}/edit" hx-target="#item-copies" hx-swap="outerHTML"' in swapped
        assert f'locate?at={seed["shelf"]}#copy-{seed["copy"]}' in swapped


def test_lone_empty_unlocated_copy_has_link_but_zero_copy_legacy_only_keeps_browse(viewer_client, db):
    item = _insert_item(db, title="Lone", isbn=None)
    copy_id = item_copies.insert_copy(db, {"item_id": item, "copy_number": 1})
    shelf = locations.create_location(db, "Legacy location only")
    legacy = _insert_item(db, title="Legacy", isbn=None, location_id=shelf)
    db.commit()
    html = viewer_client.get(f'/item/{item}').text
    assert f'locate?at=0#copy-{copy_id}' in html
    assert "Location:" not in html and "position" not in html
    assert 'id="item-copies" class="hidden"' not in html
    legacy_html = viewer_client.get(f'/item/{legacy}').text
    assert f'href="/browse?location_filter={shelf}"' in legacy_html
    assert "Find this copy" not in legacy_html
    assert item_copies.copies_for_item(db, legacy) == []
