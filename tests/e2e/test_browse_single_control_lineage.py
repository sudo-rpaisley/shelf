"""D6: the single search control is real at both responsive breakpoints."""

import re

import pytest
from playwright.sync_api import expect


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [375, 1280])
def test_one_real_browse_search_control_round_trips(live_server, authed_page, width):
    authed_page.set_viewport_size({"width": width, "height": 900})
    authed_page.goto(f"{live_server['url']}/browse")
    search = authed_page.locator('input[type="search"][name="q"]')
    expect(search).to_have_count(1)
    expect(search).to_be_visible()
    search.fill("single-control-lineage-query")
    search.press("Enter")
    expect(authed_page).to_have_url(re.compile("q=single-control-lineage-query"))
    expect(search).to_have_value("single-control-lineage-query")
