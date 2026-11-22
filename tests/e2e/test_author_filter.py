"""E2E: author links on the item page and the Browse author filter (#117b).

The live server booted once for the session, so its boot-time index build has
already run. Seeded rows are indexed here through `author_index.reindex_item`
itself — the same code the write funnel calls — on the E2E database.
"""
import re
import sqlite3
import uuid

import pytest
from playwright.sync_api import expect

from app.services import author_index
from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "mobile": {"width": 390, "height": 844},
}


def _index(data_dir, item_id, authors, trashed=False):
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        author_index.reindex_item(conn, item_id, authors)
        if trashed:
            conn.execute(
                "UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()


def _seed(data_dir):
    """Two live items sharing a translator, a trashed one by the same person,
    and an unrelated one. Names carry a per-test token: the session's database
    is shared, and an author id must be this test's alone."""
    tok = uuid.uuid4().hex[:8]
    s = {
        "tok": tok,
        "writer": f"Cixin Liu {tok}",
        "translator": f"Ken Liu {tok}",
        "both": f"Three-Body {tok}",
        "solo": f"Paper Menagerie {tok}",
        "trashed": f"Trashed Liu {tok}",
        "other": f"Unrelated {tok}",
    }
    both = insert_item(data_dir, title=s["both"],
                       authors=f"{s['writer']}, {s['translator']} - translator")
    _index(data_dir, both, f"{s['writer']}, {s['translator']} - translator")
    solo = insert_item(data_dir, title=s["solo"], authors=s["translator"])
    _index(data_dir, solo, s["translator"])
    trashed = insert_item(data_dir, title=s["trashed"], authors=s["translator"])
    _index(data_dir, trashed, s["translator"], trashed=True)
    insert_item(data_dir, title=s["other"], authors=f"Someone {tok}")
    s["both_id"] = both
    return s


def _pill(page, label):
    return page.locator("span.inline-flex", has_text=label)


@pytest.mark.parametrize("viewport", VIEWPORTS.values(), ids=VIEWPORTS.keys())
def test_author_link_filters_browse_and_the_chip_clears(live_server, authed_page, viewport):
    page = authed_page
    page.set_viewport_size(viewport)
    base = live_server["url"]
    s = _seed(live_server["data_dir"])

    page.goto(f"{base}/item/{s['both_id']}")
    page.wait_for_load_state("networkidle")
    links = page.locator("a[href^='/browse?author_filter=']")
    expect(links).to_have_count(2)
    expect(links.nth(0)).to_have_text(s["writer"])
    expect(links.nth(1)).to_have_text(s["translator"])
    expect(page.locator("body")).to_contain_text("· translator")
    assert page.locator("a[href*='/browse?q=']").count() == 0

    # Click the second author: a full navigation to the filtered Browse.
    with page.expect_navigation(url=re.compile(r"/browse\?author_filter=\d+")):
        links.nth(1).click()
    chip = _pill(page, f"Author: {s['translator']}")
    expect(chip).to_be_visible()
    grid = page.locator("#item-grid")
    expect(grid).to_contain_text(s["both"])
    expect(grid).to_contain_text(s["solo"])
    expect(grid).not_to_contain_text(s["trashed"])
    expect(grid).not_to_contain_text(s["other"])
    filtered_url = page.url

    # Remove the chip: the hidden control fires its own request (G54). Wait
    # on that response, armed before the click (G83).
    with page.expect_response(lambda r: "/api/search" in r.url):
        chip.locator("button").click()
    expect(grid).to_contain_text(s["other"])
    expect(_pill(page, "Author:")).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"author_filter="))

    # The kept URL, loaded directly, still labels the chip with the name.
    page.goto(filtered_url)
    page.wait_for_load_state("networkidle")
    expect(_pill(page, f"Author: {s['translator']}")).to_be_visible()
    expect(page.locator("#item-grid")).not_to_contain_text(s["other"])


@pytest.mark.parametrize("viewport", VIEWPORTS.values(), ids=VIEWPORTS.keys())
def test_session_restore_of_an_author_filter_shows_the_name(
    live_server, authed_page, viewport
):
    """A bare /browse replays the stored querystring as a fragment, which
    never asks the server for the author's name — so an author filter
    restores as a full page load (codex-R2) and the chip still reads the name,
    never the id."""
    page = authed_page
    page.set_viewport_size(viewport)
    base = live_server["url"]
    s = _seed(live_server["data_dir"])

    page.goto(f"{base}/item/{s['both_id']}")
    page.wait_for_load_state("networkidle")
    with page.expect_navigation(url=re.compile(r"/browse\?author_filter=\d+")):
        page.locator("a[href^='/browse?author_filter=']").nth(1).click()
    # Arriving by a link is a full page load, which does not write the
    # session copy; any swap while the author is active does (updateUrl runs
    # after every swap). The view toggle is visible at both widths, where the
    # filter selects are folded away on mobile.
    with page.expect_response(lambda r: "/api/search" in r.url):
        page.locator("[data-testid='view-list']").click()
    with page.expect_response(lambda r: "/api/search" in r.url):
        page.locator("[data-testid='view-grid']").click()
    assert "author_filter=" in page.evaluate(
        "() => sessionStorage.getItem('shelf-browse-qs') || ''")

    page.goto(f"{base}/series")
    page.wait_for_load_state("networkidle")
    page.goto(f"{base}/browse")
    expect(page).to_have_url(re.compile(r"/browse\?.*author_filter=\d+"))
    page.wait_for_load_state("networkidle")
    chip = _pill(page, "Author:")
    expect(chip).to_have_count(1)
    expect(chip).to_contain_text(s["translator"])
    expect(chip).not_to_contain_text(re.compile(r"Author: \d+"))
    grid = page.locator("#item-grid")
    expect(grid).to_contain_text(s["solo"])
    expect(grid).not_to_contain_text(s["other"])
