"""Future fresh-synthetic journeys; virtual-engine cases are not camera validation.

Authored only. Execution requires a separately approved server/browser/resource
plan. Reuses the real Add-host fixture; no replacement HTML or success endpoint.
"""

import base64
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.test_add_media import add_server, add_page_factory, _rows, _snapshot, _calls
from tests.e2e.conftest import assert_page_clean


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize('width', (360, 768, 1280))
@pytest.mark.parametrize('javascript', (False, True))
def test_simple_advanced_manual_is_native_and_persists_closed_optional_fields(add_server, add_page_factory, width, javascript):
    page = add_page_factory(width=width, javascript=javascript)
    page.goto(add_server['url'] + '/add')
    expect(page.locator('h1')).to_have_text('Add / Scan')
    manual = page.get_by_test_id('add-media-manual')
    expect(manual.locator('[name="media_type"]')).to_have_value('')
    manual.locator('[name="media_type"]').select_option('audiobook')
    manual.locator('[name="mode"]').select_option('wishlist')
    manual.locator('[name="title"]').fill('Synthetic narrated edition')
    advanced = page.get_by_test_id('manual-advanced')
    advanced.locator('summary').click()
    manual.locator('[name="narrator"]').fill('Synthetic narrator')
    manual.locator('[name="duration_mins"]').fill('321')
    manual.locator('[name="notes"]').fill('Line one\nLine two 星')
    manual.locator('[name="date_finished"]').fill('2026-10-31')
    advanced.locator('summary').click()
    expect(manual.locator('[name="title"]')).to_have_value('Synthetic narrated edition')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    with page.expect_navigation():
        manual.get_by_role('button', name='Save manual item', exact=True).click()
    expect(page.get_by_test_id('add-media-receipt')).to_contain_text('Added to your wishlist.')
    rows = _rows(add_server, "SELECT title, media_type, narrator, duration_mins, notes, date_finished, owned FROM items WHERE title = ?", ('Synthetic narrated edition',))
    assert rows == [{'title': 'Synthetic narrated edition', 'media_type': 'audiobook', 'narrator': 'Synthetic narrator',
                     'duration_mins': 321, 'notes': 'Line one\nLine two 星', 'date_finished': '2026-10-31', 'owned': 0}]
    assert _calls(add_server) == []
    assert_page_clean(page)


def test_virtual_barcode_capture_only_fills_search_and_restores_focus(add_server, add_page_factory):
    page = add_page_factory(javascript=True)
    page.goto(add_server['url'] + '/add')
    before, calls = _snapshot(add_server), _calls(add_server)
    page.locator('#add-media-type').select_option('book')
    page.evaluate("""() => {
        window.synthetic = {starts: 0, stops: 0, decode: null};
        window.createBarcodeScanner = opts => ({engine:'zxing',
            start() { synthetic.starts++; synthetic.decode = opts.onDecode; return Promise.resolve(); },
            stop() { synthetic.stops++; return Promise.resolve(); }
        });
    }""")
    assert page.evaluate('synthetic.starts') == 0
    button = page.get_by_role('button', name='Scan a barcode into the search field')
    button.click()
    dialog = page.locator('[data-add-camera-dialog]')
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-camera-status]')).to_have_text('Camera ready.')
    page.evaluate("synthetic.decode('9780441172719')")
    expect(dialog).to_be_hidden()
    expect(page.locator('#add-media-query')).to_have_value('9780441172719')
    expect(page.locator('#add-media-method')).to_have_value('isbn')
    expect(page.locator('#add-media-query')).to_be_focused()
    assert _snapshot(add_server) == before and _calls(add_server) == calls
    assert not page._add_media_state['posts']
    assert page.evaluate('synthetic.starts') == 1
    page.wait_for_function('synthetic.stops === 1')
    assert_page_clean(page)


def test_virtual_late_camera_start_is_stopped_after_escape_without_filling_or_reopening(add_server, add_page_factory):
    page = add_page_factory(javascript=True)
    page.goto(add_server['url'] + '/add')
    page.evaluate("""() => {
        window.synthetic = {stops:0};
        window.createBarcodeScanner = opts => ({engine:'zxing',
            start() { synthetic.decode=opts.onDecode; return new Promise(resolve => { synthetic.finish=resolve; }); },
            stop() { synthetic.stops++; return Promise.resolve(); }
        });
    }""")
    opener = page.get_by_role('button', name='Scan a barcode into the search field')
    opener.click()
    page.wait_for_function('!!synthetic.finish')
    page.keyboard.press('Escape')
    expect(page.locator('[data-add-camera-dialog]')).to_be_hidden()
    page.evaluate("synthetic.decode('9780441172719'); synthetic.finish()")
    page.wait_for_function('synthetic.stops === 1')
    expect(page.locator('#add-media-query')).to_have_value('')
    assert not page._add_media_state['posts']
    assert_page_clean(page)


def test_explicit_cover_file_previews_and_saves_without_recognition(add_server, add_page_factory):
    page = add_page_factory(javascript=True)
    page.goto(add_server['url'] + '/add')
    manual = page.get_by_test_id('add-media-manual')
    manual.locator('[name="media_type"]').select_option('book')
    manual.locator('[name="title"]').fill('Synthetic cover upload')
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN6kAAAAASUVORK5CYII=') + bytes(100)
    manual.locator('[data-cover-input]').set_input_files({'name':'cover.png', 'mimeType':'image/png', 'buffer':png})
    expect(manual.locator('[data-cover-preview]')).to_be_visible()
    assert manual.locator('[data-cover-preview]').get_attribute('src').startswith('blob:')
    assert not page._add_media_state['posts'] and _calls(add_server) == []
    with page.expect_navigation():
        manual.get_by_role('button', name='Save manual item', exact=True).click()
    expect(page.get_by_test_id('add-media-receipt')).to_contain_text('Added to your collection.')
    rows = _rows(add_server, "SELECT cover_path FROM items WHERE title = ?", ('Synthetic cover upload',))
    assert len(rows) == 1 and rows[0]['cover_path'].startswith('covers/')
    assert (add_server['data_dir'] / rows[0]['cover_path']).read_bytes() == png
    assert _calls(add_server) == []
    assert_page_clean(page)
