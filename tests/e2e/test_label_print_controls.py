"""Change a newly assigned batch's print layout without changing its selection."""

import re
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_labels import _insert_location, _seed_copies

pytestmark = pytest.mark.e2e


def _catalogue_state(data_dir):
    with closing(sqlite3.connect(str(data_dir / "shelf.db"))) as db:
        return tuple(
            tuple(db.execute(f"SELECT * FROM {table} ORDER BY id"))
            for table in ("items", "item_copies")
        )


def _apply_layout(page, form, preset, start, selector):
    form.get_by_label("Label stock", exact=True).select_option(preset)
    form.get_by_label("Start at label", exact=True).fill(start)
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/labels/sheet"
        and response.request.is_navigation_request()
    ) as navigation:
        form.get_by_role("button", name="Apply layout", exact=True).click()
    response = navigation.value
    assert response.status == 200
    request = response.request
    assert request.method == "GET" and request.resource_type == "document"
    assert request.post_data is None and "hx-request" not in request.headers
    assert parse_qs(urlsplit(request.url).query) == {
        **selector, "preset": [preset], "start": [start],
    }
    expect(form.get_by_label("Label stock", exact=True)).to_have_value(preset)
    expect(form.get_by_label("Start at label", exact=True)).to_have_value(start)


@pytest.mark.parametrize("size", [(1280, 800), (390, 844)], ids=["desktop", "phone"])
def test_assigned_batch_layout_uses_native_get_and_survives_reload(live_server, authed_page, size):
    data_dir = live_server["data_dir"]
    location = _insert_location(data_dir, f"Print Controls Shelf {size[0]}")
    _seed_copies(data_dir, f"Print Controls Batch {size[0]}", None, location, [None, None])
    other_location = _insert_location(data_dir, f"Other Print Controls Shelf {size[0]}")
    _seed_copies(data_dir, f"Other Print Controls Book {size[0]}", None, other_location,
                 [f"MANUAL-CONTROLS-{size[0]}"])
    page = authed_page
    page.set_viewport_size({"width": size[0], "height": size[1]})
    csp_errors = []
    page.on("console", lambda message: csp_errors.append(message.text)
            if message.type == "error" and "Content Security Policy" in message.text else None)

    page.goto(f"{live_server['url']}/labels")
    assign = page.locator("#labels-assign-form")
    assign.locator('input[name="scope"][value="location"]').check()
    assign.locator('select[name="location_id"]').select_option(str(location))
    expect(page.get_by_test_id("labels-count")).to_contain_text("2 copies would get a code")
    page.get_by_test_id("labels-assign").click()
    expect(page.get_by_test_id("labels-assigned")).to_contain_text("2 codes assigned")
    page.get_by_test_id("labels-assigned-sheet").click()
    page.wait_for_url(re.compile(r"/labels/sheet\?"))
    selector = parse_qs(urlsplit(page.url).query)
    assert set(selector) == {"from", "to"}
    expected_codes = [selector["from"][0], selector["to"][0]]
    expect(page.locator("[data-label] .label-code")).to_have_text(expected_codes)

    # A later code and an unlabelled copy must not leak into the assigned batch.
    prefix, _, last = expected_codes[-1].rpartition("-")
    _seed_copies(data_dir, f"Later Print Controls Book {size[0]}", None, other_location,
                 [f"{prefix}-{int(last) + 1:06d}", None])
    before = _catalogue_state(data_dir)
    # Saved /labels defaults must not overwrite this URL's explicit layout.
    page.evaluate("""() => {
        localStorage.setItem('shelf-label-preset', 'avery5160');
        localStorage.setItem('shelf-label-start', '29');
    }""")
    form = page.get_by_role("form", name="Label print settings", exact=True)
    expect(form).to_be_visible()
    _apply_layout(page, form, "averyl7160", "5", selector)
    expect(page.locator("[data-label] .label-code")).to_have_text(expected_codes)
    expect(page.locator("[data-label] svg")).to_have_count(2)
    expect(page.locator("[data-label-blank]")).to_have_count(4)
    assert any("size: A4" in style for style in page.locator("style").all_text_contents())

    response = page.reload(wait_until="domcontentloaded")
    assert response.status == 200 and response.request.method == "GET"
    assert parse_qs(urlsplit(page.url).query) == {**selector, "preset": ["averyl7160"], "start": ["5"]}
    expect(form.get_by_label("Label stock", exact=True)).to_have_value("averyl7160")
    expect(form.get_by_label("Start at label", exact=True)).to_have_value("5")
    expect(page.locator("[data-label] .label-code")).to_have_text(expected_codes)
    page.emulate_media(media="print")
    expect(form).to_be_hidden()
    expect(page.locator("[data-print-page]")).to_be_hidden()
    expect(page.locator("[data-label]").first).to_be_visible()
    page.emulate_media(media="screen")

    # Switching to the larger stock accepts position 30 in the same submission.
    _apply_layout(page, form, "avery5160", "30", selector)
    expect(page.locator("[data-label] .label-code")).to_have_text(expected_codes)
    expect(page.locator("[data-label-blank]")).to_have_count(29)
    expect(page.locator("[data-sheet]")).to_have_count(2)
    assert _catalogue_state(data_dir) == before
    assert_page_clean(page)
    assert csp_errors == []
