"""Offline music review and artwork journeys.

Only provider-derived preview fragments/images are stubbed. Apply, its signed
review verification, catalogue writes, upload, navigation and dialogs are real.
"""

import os
import sqlite3
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import _run_setup_wizard, assert_page_clean, attach_page_guard, insert_item, template_env
from tests.e2e.test_responsive import _COLLAPSE, _MEASURE, _settle

pytestmark = pytest.mark.e2e
MBID = "11111111-1111-4111-8111-111111111111"


def _artwork(colour):
    """Visible offline cover art: distinct record/label colours, valid PNGs."""
    size = 240
    pixels = bytearray()
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            distance = (x - size // 2) ** 2 + (y - size // 2) ** 2
            pixel = (235, 230, 210) if distance < 80 else colour if distance < 1800 else (25, 25, 35) if distance < 10000 else colour
            pixels.extend(pixel)
    def chunk(kind, value):
        return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


PNG = _artwork((38, 90, 130))
ALTERNATE = _artwork((150, 45, 60))


@pytest.fixture
def music_page(server_factory, browser):
    server = server_factory()
    credentials = _run_setup_wizard(browser, server["url"])
    context = browser.new_context(service_workers="block")
    context.add_init_script("localStorage.setItem('shelf-welcome-dismissed', '1');localStorage.setItem('shelf-shortcuts-seen', '1');")
    def local_only(route):
        if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}:
            route.continue_()
        else:
            route.abort()
    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(server["url"] + "/login")
        page.locator('[name="username"]').fill(credentials["username"])
        page.locator('[name="password"]').fill(credentials["password"])
        with page.expect_navigation():
            page.locator('button[type="submit"]').click()
        yield server, page
        assert_page_clean(page)
    finally:
        context.close()


def _artifact(page, width, label):
    _settle(page)
    geometry = page.evaluate(_MEASURE)
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
    assert not page.evaluate(_COLLAPSE)
    output = os.environ.get("SHELF_MUSIC_ARTIFACTS")
    if output:
        folder = Path(output) / str(width)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=folder / f"{label}.png", full_page=True)


@pytest.mark.parametrize("width", [320, 375, 768, 1280, 1366])
def test_existing_vinyl_review_keeps_local_data_then_chooses_artwork(music_page, monkeypatch, width):
    from app import auth
    from app.services import music_review

    server, page = music_page
    page.set_viewport_size({"width": width, "height": 900})
    root = server["data_dir"]
    (root / "covers" / "owned.png").write_bytes(PNG)
    item_id = insert_item(root, title="My vinyl with a hand-edited title", authors="Local artist credit", media_type="vinyl",
                          cover_path="covers/owned.png", notes="Keep these collector notes", isbn=None)
    db = sqlite3.connect(root / "shelf.db")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL")
        item = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
        user = dict(db.execute("SELECT * FROM users WHERE username = 'e2eadmin'").fetchone())
        fingerprint = music_review.fingerprint(db, item_id)
    finally:
        db.close()
    csrf = next(cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "csrf_token")
    request = SimpleNamespace(cookies={"csrf_token": csrf})
    release = {"musicbrainz_release_id": MBID, "title": "Provider edition title", "artist_credit": "Provider artist",
               "label": "The independent record label", "release_date": "2026-10-01", "catalog_number": "CAT-2026-42",
               "country": "GB", "format_summary": '12" Vinyl', "source": "musicbrainz",
               "media": [{"position": 1, "format": '12" Vinyl', "tracks": [{"position": 1, "number": "A1", "title": "A long track title with side numbering"}]}]}
    candidate = {"kind": "musicbrainz", "source_url": f"https://musicbrainz.org/release/{MBID}", "release": release,
                 "fields": music_review.fields_for(release), "images": [f"https://coverartarchive.org/release/{MBID}/{i}.jpg" for i in (1, 2)]}
    # This is the disposable server's key, never a production key. Sign a fixed
    # provider observation so Apply still verifies an actual server-bound token.
    with monkeypatch.context() as patch:
        patch.setattr(auth, "get_secret_key", lambda: (root / "signing.key").read_text().strip())
        candidate["ticket"] = music_review.sign(candidate, user_id=user["id"], item_id=item_id, fingerprint=fingerprint)
        candidate["image_tickets"] = [music_review.sign({"kind": "musicbrainz", "images": [image]}, user_id=user["id"], item_id=item_id,
                                                       fingerprint=fingerprint, purpose="shelf-music-image-v1") for image in candidate["images"]]
    env = template_env()
    preview = env.get_template("fragments/music_matching.html").render(item=item, candidates=[candidate], request=request,
                field_labels=music_review.FIELD_LABELS, show_match_search=True, match_fields={}, music_media_types={"vinyl": "Vinyl"})
    page.route("**/api/music/preview", lambda route: route.fulfill(body=preview, content_type="text/html"))
    page.route("**/api/music/preview-image?*", lambda route: route.fulfill(body=ALTERNATE if candidate["image_tickets"][1] in route.request.url else PNG, content_type="image/png"))
    page.route("https://coverartarchive.org/**", lambda route: route.fulfill(body=ALTERNATE if route.request.url.endswith('/2.jpg') else PNG, content_type="image/png"))

    page.goto(server["url"] + f"/item/{item_id}")
    page.get_by_role("link", name="Music details and matching", exact=True).click()
    expect(page).to_have_url(server["url"] + f"/music/item/{item_id}")
    expect(page.get_by_test_id("music-matching")).to_be_visible()
    page.locator("#music-source-url").fill(candidate["source_url"])
    page.get_by_role("button", name="Preview link", exact=True).click()
    expect(page.get_by_test_id("music-match-preview")).to_be_visible()
    expect(page.get_by_role("radio", name="Keep current artwork", exact=True)).to_be_checked()
    form = page.get_by_test_id("music-apply-match")
    expect(form.locator('[name="fields"][value="title"]')).not_to_be_checked()
    form.locator('[name="fields"][value="publisher"]').check()
    _artifact(page, width, "release-preview")
    with page.expect_navigation():
        form.get_by_role("button", name="Apply selected match", exact=True).click()
    expect(page).to_have_url(server["url"] + f"/music/item/{item_id}")
    expect(page.get_by_role("heading", level=1)).to_have_text(item["title"])
    expect(page.get_by_text("A long track title with side numbering", exact=True)).to_be_visible()

    gallery = env.get_template("fragments/cover_search.html").render(item_id=item_id, query=item["title"], media_type="vinyl",
                cover_path="covers/owned.png", gallery=True, return_to="music", candidates=[
                    {"url": image, "thumbnail": image, "source": "Cover Art Archive", "label": "Front" if i == 0 else "Back"}
                    for i, image in enumerate(candidate["images"])])
    page.route(f"**/api/items/{item_id}/cover-search?*", lambda route: route.fulfill(body=gallery, content_type="text/html"))
    choose = page.get_by_role("button", name="Choose artwork", exact=True)
    choose.click()
    dialog = page.get_by_role("dialog", name="Choose artwork", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Use this artwork:", exact=False)).to_have_count(2)
    assert dialog.bounding_box()["width"] >= min(width - 32, 1100)
    _artifact(page, width, "artwork-gallery")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(choose).to_be_focused()
    choose.click()
    expect(dialog).to_be_visible()
    upload = dialog.get_by_test_id("cover-upload")
    upload.locator('[type="file"]').set_input_files({"name": "chosen.png", "mimeType": "image/png", "buffer": PNG})
    with page.expect_navigation():
        upload.get_by_role("button", name="Upload", exact=True).click()
    expect(page).to_have_url(server["url"] + f"/music/item/{item_id}")
    db = sqlite3.connect(root / "shelf.db")
    db.row_factory = sqlite3.Row
    try:
        after = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert after["title"] == item["title"] and after["authors"] == item["authors"]
        assert after["notes"] == item["notes"] and after["owned"] == item["owned"] and after["location_id"] == item["location_id"]
        assert after["publisher"] == release["label"] and after["cover_path"] != item["cover_path"]
        assert db.execute("SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()[0] == MBID
    finally:
        db.close()
    assert_page_clean(page)


@pytest.mark.parametrize('media_type', ['book', 'dvd', 'video_game'])
def test_other_managed_items_use_the_same_artwork_dialog(music_page, media_type):
    server, page = music_page
    page.set_viewport_size({'width': 1280, 'height': 900})
    item_id = insert_item(server['data_dir'], title='Existing item artwork', media_type=media_type, isbn=None)
    gallery = template_env().get_template('fragments/cover_search.html').render(
        item_id=item_id, query='Existing item artwork', media_type=media_type, gallery=True,
        candidates=[{'url': 'https://coverartarchive.org/example.jpg', 'thumbnail': 'https://coverartarchive.org/example.jpg',
                     'source': 'Offline artwork fixture'}])
    page.route(f'**/api/items/{item_id}/cover-search?*', lambda route: route.fulfill(body=gallery, content_type='text/html'))
    page.route('https://coverartarchive.org/**', lambda route: route.fulfill(body=ALTERNATE, content_type='image/png'))
    page.goto(server['url'] + f'/item/{item_id}')
    page.get_by_role('button', name='Find cover', exact=True).click()
    dialog = page.get_by_role('dialog', name='Choose artwork', exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('button', name='Use this artwork:', exact=False)).to_have_count(1)
    upload = dialog.get_by_test_id('cover-upload')
    upload.locator('[type="file"]').set_input_files({'name': 'poster.png', 'mimeType': 'image/png', 'buffer': ALTERNATE})
    with page.expect_navigation():
        upload.get_by_role('button', name='Upload', exact=True).click()
    expect(page).to_have_url(server['url'] + f'/item/{item_id}')
    expect(page.get_by_role('heading', level=1)).to_have_text('Existing item artwork')
    with sqlite3.connect(server['data_dir'] / 'shelf.db') as db:
        row = db.execute('SELECT media_type, cover_path FROM items WHERE id = ?', (item_id,)).fetchone()
        assert row[0] == media_type and row[1]
    assert_page_clean(page)
