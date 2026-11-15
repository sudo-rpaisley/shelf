"""Whole real producer footprints, not artwork in unchanged layout slots."""

from pathlib import Path
import json
import sqlite3
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_connected_library import _cached_source, _copy, _db, _holding, _item, _link
from tests.e2e.test_cover_size_integration import _domain_snapshot, _seed
from tests.e2e.test_library_phone_presentation import _painted
from tests.e2e.test_responsive import _COLLAPSE


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


def _open_producer(page, server, producer, suffix, *, width=1366):
    prefix, ids = _seed(server, suffix)
    # More than a desktop row of local releases, without contacting MusicBrainz.
    if producer == "music":
        _, extra = _seed(server, str(suffix) + "-extra")
        with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
            for media_type in ("cd", "cassette"):
                for covered in (False, True):
                    for release_ids in (ids, extra):
                        db.execute("UPDATE music_releases SET catalog_number = ?, format_summary = ? WHERE item_id = ?",
                                   ("UnbrokenCatalogueNumber" * 6, "A complete pressing and medium description",
                                    release_ids[media_type, covered]))
    path = {"home": "/", "music": "/music"}.get(producer)
    if path is None:
        path = "/library?" + urlencode({"reset": "1", "view": producer, "q": prefix})
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server["url"] + path, wait_until="load")
    card = page.locator(f'.media-card[data-item-id="{ids["cd" if producer == "music" else "book", True]}"]')
    expect(card).to_have_count(1)
    return card


def _footprint(card):
    # Deliberately use semantic/established producer containers, not the new CSS
    # markers: this oracle must detect the old picture-only implementation.
    return card.evaluate("""card => {
        const member = card.closest('[data-library-member]');
        const item = member ? member.closest('article') : card.parentElement;
        const host = item.parentElement, box = host.getBoundingClientRect();
        const slots = [...host.children];
        return {
            width: item.getBoundingClientRect().width,
            cardWidth: card.getBoundingClientRect().width,
            contentWidth: card.parentElement.clientWidth,
            columns: getComputedStyle(host).gridTemplateColumns,
            hostWidth: box.width,
            firstRow: slots.filter(n => Math.abs(n.getBoundingClientRect().top - slots[0].getBoundingClientRect().top) < 1).length,
            fullyVisible: slots.filter(n => {
                const r = n.getBoundingClientRect();
                return n.querySelector('.media-card') && r.left >= box.left - 1 && r.right <= box.right + 1;
            }).length,
            ids: [...host.querySelectorAll('.media-card')].map(n => n.dataset.itemId),
            text: host.textContent,
            links: [...host.querySelectorAll('a')].map(n => n.getAttribute('href')),
            fonts: [...host.querySelectorAll('h3,p,a,summary')].map(n => getComputedStyle(n).fontSize),
            transform: getComputedStyle(item).transform,
        };
    }""")


def _assert_density(before, after, producer):
    assert after["width"] < before["width"] - 1, "Whole outer item must shrink, not just its picture"
    if producer == "home":
        assert after["fullyVisible"] > before["fullyVisible"], "More complete shelf slots must fit"
    else:
        assert len(after["columns"].split()) > len(before["columns"].split()), "More grid columns must fit"
        assert after["firstRow"] > before["firstRow"], "More actual items must paint in the first row"
    for key in ("ids", "text", "links", "fonts", "hostWidth"):
        assert after[key] == before[key], key
    assert after["transform"] == "none"
    assert after["cardWidth"] == pytest.approx(after["contentWidth"], abs=1), "No double scaling inside a smaller tile"


@pytest.mark.parametrize("producer", ("home", "items", "groups", "music"))
def test_smaller_whole_items_fit_more_and_larger_reset_restore_the_original_layout(
    authed_page, live_server, producer, tmp_path,
):
    page = authed_page
    card = _open_producer(page, live_server, producer, "density-" + producer)
    before, domain, url = _footprint(card), _domain_snapshot(live_server), page.url
    slider = page.locator("[data-cover-size-input]")
    slider.press("Home")
    expect(slider).to_have_value("50")
    smaller = _footprint(card)
    _assert_density(before, smaller, producer)
    (tmp_path / "density-geometry.json").write_text(json.dumps({
        "producer": producer, "before": before, "compact": smaller,
    }, indent=2) + "\n")
    slider.press("End")
    expect(slider).to_have_value("100")
    assert _footprint(card) == before
    slider.press("Home")
    page.locator('[data-cover-size-action="reset"]').click()
    assert _footprint(card) == before
    assert _domain_snapshot(live_server) == domain and page.url == url
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


@pytest.mark.parametrize("producer", ("home", "items", "groups", "music"))
def test_density_oracle_rejects_a_picture_only_resize_with_frozen_outer_slots(
    authed_page, live_server, producer,
):
    page = authed_page
    card = _open_producer(page, live_server, producer, "density-mutant-" + producer)
    before = _footprint(card)
    page.locator("[data-cover-size-input]").press("Home")
    card.evaluate("""(card, before) => {
        const item = card.closest('[data-library-member]')?.closest('article') || card.parentElement;
        const host = item.parentElement;
        if (before.columns !== 'none') host.style.gridTemplateColumns = before.columns;
        else for (const slot of host.children) {
            slot.style.flexBasis = before.width + 'px'; slot.style.width = before.width + 'px';
        }
        card.style.width = '50%';
    }""", before)
    with pytest.raises(AssertionError, match="Whole outer item must shrink"):
        _assert_density(before, _footprint(card), producer)
    assert_page_clean(page)


@pytest.mark.parametrize("producer", ("home", "items", "groups", "music"))
def test_density_oracle_rejects_double_scaling_inside_a_correctly_smaller_item(authed_page, live_server, producer):
    page = authed_page
    card = _open_producer(page, live_server, producer, "density-double-" + producer)
    before = _footprint(card)
    page.locator("[data-cover-size-input]").press("Home")
    card.evaluate("n => n.style.width = '50%'")
    with pytest.raises(AssertionError, match="No double scaling"):
        _assert_density(before, _footprint(card), producer)
    assert_page_clean(page)


def test_home_terminal_slot_resizes_with_the_items_and_remains_keyboard_reachable(authed_page, live_server):
    page = authed_page
    _open_producer(page, live_server, "home", "density-terminal", width=320)
    shelf = page.locator('[data-testid="home-recent-items"] .media-shelf__items')
    slots = shelf.locator(":scope > li")
    terminal = shelf.locator(".media-shelf__view-more")
    href = terminal.get_attribute("href")
    ids = shelf.locator(".media-card").evaluate_all("nodes => nodes.map(n => n.dataset.itemId)")
    assert len(ids) == 8 and slots.count() == 9
    assert terminal.evaluate("n => parseFloat(getComputedStyle(n).minHeight)") == 240
    page.locator("[data-cover-size-input]").press("Home")
    assert all(width == pytest.approx(96, abs=1) for width in slots.evaluate_all(
        "nodes => nodes.map(n => n.getBoundingClientRect().width)"))
    assert terminal.evaluate("n => parseFloat(getComputedStyle(n).minHeight)") == 120
    assert terminal.bounding_box()["height"] >= 120
    shelf.focus()
    shelf.press("ArrowRight")
    expect(shelf).not_to_have_js_property("scrollLeft", 0)
    terminal.focus()
    expect(terminal).to_be_focused()
    assert terminal.evaluate("n => { const r=n.getBoundingClientRect(), h=n.closest('ul').getBoundingClientRect(); return r.left>=h.left && r.right<=h.right; }")
    assert terminal.get_attribute("href") == href == "/library?reset=1&view=items&sort=recent"
    assert shelf.locator(".media-card").evaluate_all("nodes => nodes.map(n => n.dataset.itemId)") == ids
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.evaluate("document.documentElement.style.fontSize = '32px'")
    assert all(width == pytest.approx(192, abs=1) for width in slots.evaluate_all(
        "nodes => nodes.map(n => n.getBoundingClientRect().width)"))
    geometry = page.evaluate("""() => ({
        client:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth,
        outside:[...document.querySelectorAll('main *')].filter(n=>{
            const r=n.getBoundingClientRect(); return r.width && (r.left<0 || r.right>document.documentElement.clientWidth);
        }).slice(0,20).map(n=>({tag:n.tagName,cls:n.className,text:n.textContent.slice(0,80),
            left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right,
            width:n.getBoundingClientRect().width,overflow:getComputedStyle(n).overflowX}))
    })""")
    assert geometry["scroll"] <= geometry["client"], geometry
    assert page.evaluate(_COLLAPSE) == []
    assert_page_clean(page)


@pytest.mark.parametrize("view", ("items", "groups"))
@pytest.mark.parametrize("width", (375, 1280))
def test_rich_outer_items_keep_holdings_editions_pagination_and_focus_at_compact_and_enlarged_sizes(
    authed_page, live_server, view, width,
):
    page = authed_page
    prefix = f"Density rich {width}-{view}"
    location_text = prefix + " / Reading room / " + "LongUnbrokenLocation" * 8
    with _db(live_server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES (?)", (location_text,)).lastrowid
        source = _cached_source(db, live_server["url"])
        db.execute("UPDATE source_instances SET name = ? WHERE id = ?", ("UnbrokenSourceName" * 12, source))
        tag = db.execute("INSERT INTO tags (name) VALUES (?)", (prefix + "UnbrokenTag" * 20,)).lastrowid
        for number in range(8):
            book = _item(db, f"{prefix} {number:02d} A complete long printed title", owned=1,
                         subtitle="UnbrokenSubtitle" * 12, location_id=location)
            db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (book, tag))
            _copy(db, book, location)
            ebook = _item(db, f"{prefix} {number:02d} The complete digital edition", "ebook")
            _link(db, book, ebook)
            _holding(db, source, ebook, live_server["url"] + f"/fixture-reader/{ebook}")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server["url"] + "/library?" + urlencode({
        "reset": "1", "view": view, "q": prefix, "page_size": "4",
    }), wait_until="load")
    results = page.get_by_test_id("library-results")
    # Reveal real secondary editions before capturing content/action ownership.
    for details in results.locator("article > details").all():
        details.locator("summary").click()
    members = results.locator("[data-library-member]")
    text = members.all_text_contents()
    ids = members.evaluate_all("nodes => nodes.map(n => n.dataset.libraryMember)")
    counts = page.get_by_test_id("library-counts").inner_text()
    pagination = page.get_by_test_id("library-pagination").inner_html()
    filters = page.get_by_test_id("library-filters").evaluate("n => [...new FormData(n)]")
    domain, url = _domain_snapshot(live_server), page.url
    page.locator("[data-cover-size-input]").press("Home")
    for font_size in ("16px", "32px"):
        page.evaluate("size => document.documentElement.style.fontSize = size", font_size)
        assert members.all_text_contents() == text
        assert members.evaluate_all("nodes => nodes.map(n => n.dataset.libraryMember)") == ids
        assert page.get_by_test_id("library-counts").inner_text() == counts
        assert page.get_by_test_id("library-pagination").inner_html() == pagination
        assert page.get_by_test_id("library-filters").evaluate("n => [...new FormData(n)]") == filters
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.evaluate(_COLLAPSE) == []
        for member in members.all():
            _painted(member)
            link = member.locator(".media-card__detail")
            link.focus()
            expect(link).to_be_focused()
            assert member.get_by_role("heading").inner_text()
        expect(results.get_by_text(location_text, exact=False).first).to_be_visible()
        source_link = results.locator('a[target="_blank"]').first
        if view == "groups":
            expect(source_link).to_be_visible()
            source_link.focus()
            expect(source_link).to_be_focused()
    assert page.url == url and _domain_snapshot(live_server) == domain
    assert_page_clean(page)


@pytest.mark.parametrize("producer", ("home", "items"))
@pytest.mark.parametrize("assets", ("cached-bundle-with-direct", "current-bundle-only"))
def test_real_density_css_works_across_the_cached_bundle_upgrade_seam(authed_page, live_server, producer, assets):
    page = authed_page
    if assets == "cached-bundle-with-direct":
        cached = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
        page.route("**/static/css/app.css", lambda route: route.fulfill(
            status=200, content_type="text/css", body=cached))
    else:
        page.route("**/static/css/item-density.source.css", lambda route: route.fulfill(
            status=200, content_type="text/css", body="/* test the real current bundle alone */"))
    card = _open_producer(page, live_server, producer, "density-upgrade-" + producer + assets)
    before = _footprint(card)
    page.locator("[data-cover-size-input]").press("Home")
    _assert_density(before, _footprint(card), producer)
    assert_page_clean(page)


@pytest.mark.parametrize("producer", ("home", "items", "music"))
@pytest.mark.parametrize("missing", ("parent", "container"))
def test_density_requires_both_exact_markers_and_unmarked_items_keep_the_legacy_curve(
    authed_page, live_server, producer, missing,
):
    page = authed_page
    card = _open_producer(page, live_server, producer, "density-markers-" + producer + missing)
    before = _footprint(card)
    selector, attr, value = (
        ('[data-item-size="density-v1"]', "data-item-size", "density-v1") if missing == "parent" else
        ('[data-item-shelf="density-v1"]', "data-item-shelf", "density-v1") if producer == "home" else
        (f'[data-item-grid="{"music" if producer == "music" else "library"}"]', "data-item-grid",
         "music" if producer == "music" else "library")
    )
    target = page.locator(selector)
    # Preserve a handle because the selector intentionally ceases to match.
    node = target.element_handle()
    node.evaluate("(n, attr) => n.setAttribute(attr, 'unrecognized-mode')", attr)
    page.locator("[data-cover-size-input]").press("Home")
    unmarked = _footprint(card)
    assert unmarked["width"] == before["width"] and unmarked["columns"] == before["columns"]
    assert unmarked["cardWidth"] == pytest.approx(max(80, before["cardWidth"] * .5), abs=1)
    node.evaluate("(n, pair) => n.setAttribute(...pair)", [attr, value])
    _assert_density(before, _footprint(card), producer)
    node.dispose()
    assert_page_clean(page)
