"""Browser coverage for the rebuilt item-detail layout."""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def test_item_detail_cards_and_primary_actions(live_server, authed_page):
    item_id = insert_item(
        live_server["data_dir"],
        title="Browser Detail Book",
        media_type="book",
        isbn="9789000090105",
        authors="Browser Author",
        description="A browser-tested synopsis.",
        publisher="Browser Press",
    )

    authed_page.goto(f"{live_server['url']}/item/{item_id}")
    authed_page.wait_for_load_state("networkidle")

    expect(authed_page.get_by_test_id("item-detail-hero")).to_be_visible()
    expect(authed_page.get_by_test_id("item-about-card")).to_be_visible()
    expect(authed_page.get_by_test_id("item-details-card")).to_be_visible()
    expect(authed_page.get_by_test_id("item-copy-card")).to_be_visible()
    expect(authed_page.get_by_test_id("item-primary-actions")).to_be_visible()
    expect(authed_page.get_by_role("heading", name="Browser Detail Book")).to_be_visible()
    expect(authed_page.get_by_text("A browser-tested synopsis.")).to_be_visible()


def test_detail_edit_round_trip_returns_to_new_layout(live_server, authed_page):
    item_id = insert_item(
        live_server["data_dir"],
        title="Round Trip Detail",
        media_type="book",
        isbn="9789000090112",
    )

    authed_page.goto(f"{live_server['url']}/item/{item_id}")
    authed_page.get_by_test_id("item-primary-actions").get_by_role("link", name="Edit").click()
    authed_page.wait_for_url(f"{live_server['url']}/item/{item_id}/edit")

    title = authed_page.locator("input[name=title]")
    title.fill("Round Trip Detail Updated")
    authed_page.locator("button[type=submit]:has-text('Save')").click()
    authed_page.wait_for_url(f"{live_server['url']}/item/{item_id}")

    expect(authed_page.get_by_test_id("item-detail-hero")).to_be_visible()
    expect(authed_page.get_by_role("heading", name="Round Trip Detail Updated")).to_be_visible()
