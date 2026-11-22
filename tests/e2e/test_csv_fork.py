"""E2E tests: CSV export/import round-trip."""
import csv
import io
import sqlite3
import urllib.request

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_USERNAME, insert_item

pytestmark = pytest.mark.e2e


def _get_token_cookie(live_server, browser, setup_admin) -> str:
    """Retrieve auth token from browser session."""
    from tests.e2e.conftest import _get_auth_cookies
    cookies = _get_auth_cookies(live_server, browser, setup_admin)
    return cookies.get("access_token", "")


def test_csv_export(live_server, browser, setup_admin):
    """GET /api/export/csv returns a CSV file containing seeded items."""
    insert_item(live_server["data_dir"], title="Export Test Book", media_type="book", isbn="9780000111222")

    token = _get_token_cookie(live_server, browser, setup_admin)
    req = urllib.request.Request(
        f"{live_server['url']}/api/export/csv",
        headers={"Cookie": f"access_token={token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        content_disp = resp.headers.get("Content-Disposition", "")
        assert "csv" in content_disp or "attachment" in content_disp
        body = resp.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(body))
    titles = [row.get("title", "") for row in reader]
    assert any("Export Test Book" in t for t in titles), f"Expected book not in export; got: {titles}"


def test_csv_import(live_server, authed_page):
    """Uploading a CSV file via the settings page imports items."""
    # Build a minimal CSV
    csv_content = "title,media_type,isbn\nImported Book,book,9780000222336\n"

    authed_page.goto(f"{live_server['url']}/settings")
    authed_page.wait_for_load_state("networkidle")

    # CSV import is on the "Data" tab — click it first
    authed_page.locator("button:has-text('Data')").click()

    # CSV import file input
    file_input = authed_page.locator("[x-data='csvImportPanel'] form input[type=file]")
    expect(file_input).to_be_visible()
    assert set(file_input.get_attribute("accept").split(",")) == {".csv", ".tsv", ".txt"}

    file_input.set_input_files({
        "name": "import.csv",
        "mimeType": "text/csv",
        "buffer": csv_content.encode(),
    })

    # Scope to the CSV import form to avoid matching the Hardcover import button
    submit = file_input.locator("xpath=ancestor::form").locator("button[type=submit], input[type=submit]").first

    with authed_page.expect_response(
        lambda r: r.url == f"{live_server['url']}/api/import/csv"
        and r.request.method == "POST"
    ):
        submit.click()
    result = authed_page.get_by_test_id("csv-import-result")
    expect(result).to_be_visible()
    expect(result.locator("[x-text='importResult.imported']")).to_have_text("1")

    # Navigate to browse and verify the imported item appears
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    expect(authed_page.locator("body")).to_contain_text("Imported Book")


def test_goodreads_import_via_ui(live_server, authed_page):
    """A Goodreads-shaped export uploads through the settings UI, is auto-detected,
    and lands with reading status + wishlist mapping applied."""
    header = (
        "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,"
        "Average Rating,Publisher,Binding,Number of Pages,Year Published,"
        "Original Publication Year,Date Read,Date Added,Bookshelves,"
        "Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,"
        "Private Notes,Read Count,Owned Copies"
    )
    rows = [
        '1,GR Read Book,Ann Author,"Author, Ann",,"=""""","=""9780900000010""",'
        '5,4.2,Ace,Paperback,300,2005,1999,2023/08/15,2023/01/02,,,read,,,,1,0',
        '2,GR Wishlist Book,Bob Writer,"Writer, Bob",,"=""""","=""9780900000027""",'
        '0,4.0,Bantam,Hardcover,500,1990,1989,,2023/01/02,,,to-read,,,,0,0',
    ]
    csv_content = header + "\n" + "\n".join(rows) + "\n"

    authed_page.goto(f"{live_server['url']}/settings")
    authed_page.wait_for_load_state("networkidle")
    authed_page.locator("button:has-text('Data')").click()

    file_input = authed_page.locator("[x-data='csvImportPanel'] form input[type=file]")
    expect(file_input).to_be_visible()
    file_input.set_input_files({
        "name": "goodreads_library_export.csv",
        "mimeType": "text/csv",
        "buffer": csv_content.encode(),
    })

    form = file_input.locator("xpath=ancestor::form")
    form.locator("input[name=to_read_wishlist]").check()
    with sqlite3.connect(str(live_server["data_dir"] / "shelf.db")) as conn:
        acquisition_before = conn.execute("SELECT COUNT(*) FROM list_items").fetchone()[0]

    with authed_page.expect_response("**/api/import/csv") as resp_info:
        form.locator("button[type=submit]").first.click()
    result = resp_info.value.json()
    assert result["format"] == "goodreads", result
    assert result["imported"] == 2, result

    # Detection + counts surface in the UI
    expect(authed_page.locator("text=Detected").first).to_be_visible()

    # Both items appear; personal tracker intent is not a shared acquisition.
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    expect(authed_page.locator("body")).to_contain_text("GR Read Book")
    expect(authed_page.locator("body")).to_contain_text("GR Wishlist Book")
    wish_card = authed_page.locator("#item-grid a[data-item-id]").filter(has_text="GR Wishlist Book")
    expect(wish_card.get_by_test_id("personal-wishlist-badge")).to_have_text("My wishlist")
    with sqlite3.connect(str(live_server["data_dir"] / "shelf.db")) as conn:
        states = conn.execute(
            "SELECT i.title, i.owned, s.reading_status, s.date_finished, s.wishlist "
            "FROM items i JOIN user_item_state s ON s.item_id = i.id "
            "JOIN users u ON u.id = s.user_id WHERE u.username = ? "
            "AND i.title IN ('GR Read Book', 'GR Wishlist Book') ORDER BY i.title",
            (ADMIN_USERNAME,),
        ).fetchall()
        assert conn.execute("SELECT COUNT(*) FROM list_items").fetchone()[0] == acquisition_before
    assert states == [
        ("GR Read Book", 0, "read", "2023-08-15", 0),
        ("GR Wishlist Book", 0, "want_to_read", None, 1),
    ]
