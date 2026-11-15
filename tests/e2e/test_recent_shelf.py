"""Real Home screenshots and interaction: not a mocked streaming-service frontend."""

from pathlib import Path
import sqlite3

from PIL import Image, ImageDraw, ImageFont
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard, insert_item
from tests.e2e.test_connected_library import connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_responsive import _COLLAPSE


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
LONG_CREATOR = ("Lara Elena Donnelly, French Carlomagno, Pius Bak, Samantha Dodge, Fabiana Mascolo, "
                "Francesco Segala, Jim Campbell, Evan Cagle, Jenny Frison, Allyson Gronowitz, Sierra Hahn")


def _seed(server, suffix, request, count=36):
    types = ("book", "cd", "cassette", "video_game", "vinyl", "dvd")
    colors = ((67, 100, 111), (134, 67, 77), (102, 84, 128), (131, 102, 49), (64, 111, 99), (71, 86, 126))
    titles = ("Dungeons & Dragons Monster Manual (5.5e)", "Songs for a Long Autumn Evening",
              "The Storyteller — Fairies and Forgotten Journeys", "Labyrinth — Masquerade",
              "The Mermaid and the Fisherman", "2000 AD — Adventures Beyond the Horizon")
    ids = []
    for index in range(count):
        name = f"recent-style-{suffix}-{index}.png"
        image = Image.new("RGB", (480, 720), colors[index % len(colors)])
        draw = ImageDraw.Draw(image)
        draw.rectangle((28, 28, 452, 692), outline=(225, 213, 173), width=5)
        draw.ellipse((100, 230, 380, 510), outline=(225, 213, 173), width=9)
        draw.text((50, 70), f"SHELF\nVOLUME {index + 1:02d}", font=ImageFont.load_default(size=38), fill="white")
        draw.text((50, 600), "A NEW DISCOVERY", font=ImageFont.load_default(size=28), fill="white")
        image.save(server["data_dir"] / "covers" / name)
        ids.append(insert_item(server["data_dir"], media_type=types[index % len(types)],
                               title=f"{titles[index % len(titles)]} — {suffix} {index:02d}",
                               authors=LONG_CREATOR,
                               cover_path="covers/" + name))
    with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
        original_dates = list(db.execute(
            "SELECT created_at, id FROM items WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
        )) if ids else []
        db.executemany("UPDATE items SET created_at = '9999-01-01 12:00:00' WHERE id = ?", ((item,) for item in ids))
    def restore_dates():
        # These are this test's own synthetic rows. Do not let future-dated
        # screenshot fixtures displace later tests' genuinely newer additions.
        with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
            db.executemany("UPDATE items SET created_at = ? WHERE id = ?", original_dates)
    request.addfinalizer(restore_dates)
    return list(reversed(ids))


def _open(page, server, width=1366):
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(server["url"] + "/", wait_until="load")
    return page.get_by_test_id("home-recent-items")


def test_recently_added_is_a_real_24_item_row_not_a_short_eight_item_strip(authed_page, live_server, request):
    expected = _seed(live_server, "inventory", request)
    page = authed_page
    recent = _open(page, live_server)
    cards = recent.locator(".media-card")
    expect(cards).to_have_count(24)
    assert cards.evaluate_all("nodes => nodes.map(n => Number(n.dataset.itemId))") == expected[:24]
    assert recent.locator("template[data-recent-overflow]").count() == 0
    assert recent.locator(".media-shelf__slot").count() == 25
    assert recent.locator(".recent-shelf__all").get_attribute("href") == "/library?reset=1&view=items&sort=recent"
    assert_page_clean(page)


@pytest.mark.parametrize("width", (320, 375, 768, 1366, 1920))
def test_artwork_led_previews_keep_credits_compact_and_full_details_accessible(
    authed_page, live_server, width, tmp_path, request,
):
    _seed(live_server, f"visual-{width}", request)
    page = authed_page
    recent = _open(page, live_server, width)
    recent.locator("img").evaluate_all("images => Promise.all(images.map(image => { image.loading='eager'; return image.decode(); }))")
    if width == 1366:
        recent.screenshot(path=str(tmp_path / "recent-observed-before-assertions.png"))
    card = recent.locator(".media-card").first
    title = card.locator("h3")
    creator = card.locator(".media-card__creator")
    measure = "n => ({height:n.clientHeight,scroll:n.scrollHeight,width:n.clientWidth,scrollWidth:n.scrollWidth,line:parseFloat(getComputedStyle(n).lineHeight)})"
    title_box, creator_box = title.evaluate(measure), creator.evaluate(measure)
    assert title_box["height"] <= title_box["line"] * 2 + 1, title_box
    assert creator_box["height"] <= creator_box["line"] + 1, creator_box
    assert creator_box["scrollWidth"] > creator_box["width"], "Real long credits must exercise the one-line preview boundary"
    stage = card.locator(".media-card__art-stage")
    expect(stage).to_be_visible()
    expect(card.locator(".media-card__detail")).to_have_accessible_name(title.text_content())
    for sample in recent.locator(".media-card").all():
        geometry = sample.evaluate("""n => {
            const s=n.querySelector('.media-card__art-stage').getBoundingClientRect();
            const a=n.querySelector('.media-artwork'),r=a.getBoundingClientRect();
            return {sx:s.x,sy:s.y,sw:s.width,sh:s.height,x:r.x,y:r.y,w:r.width,h:r.height,
                shape:a.dataset.coverShape,fit:a.querySelector('img') ? getComputedStyle(a.querySelector('img')).objectFit : null};
        }""")
        assert abs(geometry["x"] + geometry["w"] / 2 - geometry["sx"] - geometry["sw"] / 2) <= 2
        assert abs(geometry["y"] + geometry["h"] / 2 - geometry["sy"] - geometry["sh"] / 2) <= 2, geometry
        assert geometry["w"] <= geometry["sw"] and geometry["h"] <= geometry["sh"]
        ratios = {"portrait": 2 / 3, "square": 1, "cassette": 5 / 8, "game": 3 / 4}
        assert geometry["w"] / geometry["h"] == pytest.approx(ratios[geometry["shape"]], abs=.01)
        assert geometry["fit"] in (None, "contain")
    terminal = recent.locator(".media-shelf__view-more")
    assert terminal.bounding_box()["height"] <= stage.bounding_box()["height"] + 2
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate(_COLLAPSE) == []
    if width in (375, 1366, 1920):
        recent.screenshot(path=str(tmp_path / f"recent-shelf-{width}.png"))
    details = card.locator("details")
    summary = details.locator("summary")
    summary.focus()
    summary.press("Enter")
    expect(details).to_have_attribute("open", "")
    expect(details.locator("[data-card-full-title]")).to_have_text(title.text_content())
    expect(details.locator("[data-card-full-creator]")).to_have_text(creator.text_content())
    expect(details.locator("[data-card-full-creator]")).to_be_visible()
    summary.press("Enter")
    expect(details).not_to_have_attribute("open", "")
    assert_page_clean(page)


def test_row_navigation_pages_natively_and_keeps_the_button_focus(authed_page, live_server, request):
    _seed(live_server, "navigation", request)
    page = authed_page
    recent = _open(page, live_server, 1366)
    track = recent.locator(".media-shelf__items")
    previous = recent.get_by_role("button", name="Previous recently added items", exact=True)
    following = recent.get_by_role("button", name="Next recently added items", exact=True)
    expect(previous).to_be_disabled()
    expect(following).to_be_enabled()
    following.focus()
    following.press("Space")
    expect(track).not_to_have_js_property("scrollLeft", 0)
    expect(following).to_be_focused()
    expect(previous).to_be_enabled()
    for _ in range(30):
        if not following.is_enabled():
            break
        following.click()
    expect(following).to_be_disabled()
    expect(following).to_be_focused()
    assert track.evaluate("n => Math.abs(n.scrollLeft) >= n.scrollWidth - n.clientWidth - 2")
    for _ in range(30):
        if not previous.is_enabled():
            break
        previous.click()
    expect(previous).to_be_disabled()
    expect(track).to_have_js_property("scrollLeft", 0)
    assert_page_clean(page)


def test_real_no_js_preview_keeps_eight_native_items_and_expandable_complete_credits(authed_page, live_server, browser, request):
    expected = _seed(live_server, "no-js", request)
    context = browser.new_context(java_script_enabled=False, service_workers="block",
                                  viewport={"width": 375, "height": 900})
    context.add_cookies(authed_page.context.cookies())
    page = attach_page_guard(context.new_page())
    try:
        recent = _open(page, live_server, 375)
        cards = recent.locator(".media-card")
        expect(cards).to_have_count(8)
        assert cards.evaluate_all("nodes => nodes.map(n => Number(n.dataset.itemId))") == expected[:8]
        for button in recent.locator("[data-recent-action]").all():
            expect(button).to_be_disabled()
        card = cards.first
        card.locator("summary").click()
        expect(card.locator("[data-card-full-creator]")).to_be_visible()
        expect(recent.locator(".recent-shelf__all")).to_have_attribute("href", "/library?reset=1&view=items&sort=recent")
        assert_page_clean(page)
    finally:
        context.close()


@pytest.mark.parametrize("count", (0, 1, 8, 9, 24, 25))
def test_enhanced_counts_and_overflow_hints_follow_actual_materialized_inventory(connected_library, count, request):
    server, page = connected_library, connected_library["page"]
    expected = _seed(server, f"count-{count}", request, count=count)
    recent = _open(page, server)
    cards = recent.locator(".media-card")
    expect(cards).to_have_count(min(count, 24))
    assert cards.evaluate_all("nodes => nodes.map(n => Number(n.dataset.itemId))") == expected[:24]
    expect(recent.locator("[data-recent-count]")).to_have_text(f"{min(count, 24)} recent {'item' if count == 1 else 'items'}")
    if count:
        hint = recent.locator(".media-shelf__hint")
        if count > 24:
            expect(hint).to_be_visible()
        else:
            expect(hint).to_be_hidden()
    assert_page_clean(page)


def test_density_buttons_range_and_enlarged_text_keep_navigation_edges_current(authed_page, live_server, request, tmp_path):
    _seed(live_server, "resize", request)
    page = authed_page
    recent = _open(page, live_server, 375)
    track = recent.locator(".media-shelf__items")
    following = recent.locator('[data-recent-action="next"]')
    previous = recent.locator('[data-recent-action="previous"]')
    for _ in range(30):
        if not following.is_enabled():
            break
        following.click()
    expect(following).to_be_disabled()
    page.locator('[data-cover-size-action="smaller"]').click()
    expect(page.locator("[data-cover-size-input]")).to_have_value("90")
    expect(following).to_be_disabled()
    page.locator('[data-cover-size-action="larger"]').click()
    expect(following).to_be_enabled()
    page.locator("[data-cover-size-input]").press("Home")
    expect(following).to_be_disabled()
    page.evaluate("document.documentElement.style.fontSize = '32px'")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate(_COLLAPSE) == []
    expect(previous).to_be_enabled()
    assert track.evaluate("n => n.scrollWidth > n.clientWidth")
    recent.screenshot(path=str(tmp_path / "recent-shelf-375-dense-200.png"))
    assert_page_clean(page)


def test_logical_rtl_paging_and_edge_focus_do_not_reverse_item_order(authed_page, live_server, request):
    expected = _seed(live_server, "rtl", request)
    page = authed_page
    recent = _open(page, live_server)
    recent.locator("[data-recent-shelf]").evaluate("n => n.dir = 'rtl'")
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    following = recent.locator('[data-recent-action="next"]')
    expect(following).to_be_enabled()
    following.click()
    expect(following).to_be_focused()
    assert recent.locator(".media-shelf__items").evaluate("n => n.scrollLeft < 0")
    assert recent.locator(".media-card").evaluate_all("nodes => nodes.map(n => Number(n.dataset.itemId))") == expected[:24]
    assert_page_clean(page)


def test_reexecution_and_real_htmx_replacement_enhance_once_without_new_catalogue_requests(authed_page, live_server, request):
    _seed(live_server, "lifecycle", request)
    page = authed_page
    recent = _open(page, live_server)
    before, url = _domain_snapshot(live_server), page.url
    storage = page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]")
    requests = []
    def record(request):
        if not request.url.startswith((live_server["url"] + "/static/", live_server["url"] + "/covers/")):
            requests.append((request.method, request.url))
    page.on("request", record)
    try:
        page.evaluate("""() => new Promise((resolve,reject) => {
            const script=document.createElement('script');script.src='/static/js/recent-shelf.js';
            script.onload=resolve;script.onerror=reject;document.body.append(script);
        })""")
        track = recent.locator(".media-shelf__items")
        width = track.evaluate("n => n.clientWidth")
        recent.locator('[data-recent-action="next"]').click()
        assert track.evaluate("n => n.scrollLeft") == pytest.approx(width * .9, abs=2)
        assert requests == []
        old = recent.locator('[data-recent-shelf="streaming-v1"]').element_handle()
        page.evaluate("""() => {
            const button=document.createElement('button');button.type='button';button.textContent='Replace shelf fixture';
            button.dataset.testid='recent-swap';button.setAttribute('hx-get','/');
            button.setAttribute('hx-select','[data-recent-shelf="streaming-v1"]');
            button.setAttribute('hx-target','[data-recent-shelf="streaming-v1"]');
            button.setAttribute('hx-swap','outerHTML');document.querySelector('main').append(button);htmx.process(button);
        }""")
        with page.expect_response(lambda response: response.url == live_server["url"] + "/"):
            page.get_by_test_id("recent-swap").click()
        expect(recent.locator(".media-card")).to_have_count(24)
        assert old.evaluate("n => n.isConnected") is False
        assert page.locator("#home-recent-row").count() == 1 and page.locator("#home-recent-heading").count() == 1
        expect(recent.locator('[data-recent-action="previous"]')).to_be_disabled()
        assert len(requests) == 1 and requests[0][0] == "GET"
        old.dispose()
        assert page.url == url and _domain_snapshot(live_server) == before
        assert page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]") == storage
        assert_page_clean(page)
    finally:
        page.remove_listener("request", record)


def test_old_cached_bundle_uses_the_real_direct_style_for_compact_complete_previews(authed_page, live_server, request):
    _seed(live_server, "cached", request)
    page = authed_page
    cached = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
    page.route("**/static/css/app.css", lambda route: route.fulfill(status=200, content_type="text/css", body=cached))
    recent = _open(page, live_server)
    expect(recent.locator(".media-card")).to_have_count(24)
    card = recent.locator(".media-card").first
    assert card.locator(".media-card__creator").evaluate("n => n.clientHeight <= parseFloat(getComputedStyle(n).lineHeight)+1")
    card.locator("summary").click()
    expect(card.locator("[data-card-full-creator]")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


@pytest.mark.parametrize("missing", ("parent", "card"))
def test_preview_css_requires_both_exact_markers_not_just_shared_card_markup(authed_page, live_server, request, missing):
    _seed(live_server, "markers-" + missing, request)
    page = authed_page
    cached = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
    page.route("**/static/css/app.css", lambda route: route.fulfill(status=200, content_type="text/css", body=cached))
    recent = _open(page, live_server)
    card = recent.locator(".media-card").first
    page.evaluate("document.fonts.ready")
    recent.locator("img").evaluate_all("images => Promise.all(images.map(image => { image.loading='eager'; return image.decode(); }))")
    assert card.locator(".media-card__creator").evaluate("n => n.clientHeight <= parseFloat(getComputedStyle(n).lineHeight)+1 && n.scrollWidth > n.clientWidth")
    assert card.locator(".media-card__art-stage").evaluate("n => getComputedStyle(n).display === 'grid' && getComputedStyle(n).aspectRatio === '5 / 8'")
    # This is a card-style oracle, not a comparison of differently aligned rows.
    # Keep the valid parent's cross-axis layout fixed while disabling the sheet;
    # otherwise unshortened siblings can stretch this negative-control article.
    track = recent.locator(".media-shelf__items")
    track.evaluate("n => n.style.setProperty('align-items', 'flex-start', 'important')")
    if missing == "parent":
        recent.locator("[data-recent-shelf]").evaluate("n => n.setAttribute('data-recent-shelf','unknown')")
    else:
        card.evaluate("n => n.setAttribute('data-card-presentation','unknown')")
    signature = """n => [n,...n.querySelectorAll('*')].map(element=>{
        const s=getComputedStyle(element), keys=['display','width','height','minHeight','maxHeight',
            'paddingTop','paddingRight','paddingBottom','paddingLeft','fontSize','lineHeight',
            'overflowX','overflowY','whiteSpace','textOverflow','aspectRatio','alignContent'];
        return Object.fromEntries(keys.map(key=>[key,s[key]]));
    })"""
    widths = (track.bounding_box()["width"], card.locator("..").bounding_box()["width"])
    before = card.evaluate(signature)
    style = page.locator('head link[href="/static/css/recent-shelf.source.css"]')
    style.evaluate("n => n.sheet.disabled = true")
    assert (track.bounding_box()["width"], card.locator("..").bounding_box()["width"]) == widths
    assert card.evaluate(signature) == before
    assert_page_clean(page)


def test_opened_full_details_are_readable_at_200_percent_text_and_50_percent_density(
    authed_page, live_server, request, tmp_path,
):
    _seed(live_server, "enlarged-details", request)
    page = authed_page
    recent = _open(page, live_server, 375)
    page.evaluate("document.documentElement.style.fontSize = '32px'")
    page.locator("[data-cover-size-input]").press("Home")
    expect(page.locator("[data-cover-size-input]")).to_have_value("50")
    card = recent.locator(".media-card").first
    details = card.locator("details")
    details.locator("summary").focus()
    details.locator("summary").press("Enter")
    expect(details).to_have_attribute("open", "")
    expect(details.locator("[data-card-full-title]")).to_have_text(
        "2000 AD — Adventures Beyond the Horizon — enlarged-details 35",
    )
    expect(details.locator("[data-card-full-creator]")).to_have_text(LONG_CREATOR)
    expect(details.locator("[data-card-full-type]")).to_have_text("DVD / Blu-ray")
    geometry = details.evaluate("""n => {
        const card=n.closest('.media-card').getBoundingClientRect();
        const track=n.closest('.media-shelf__items').getBoundingClientRect();
        return [...n.querySelectorAll('.media-card__full-text, .media-card__full-text > p')].map(p=>{
            const r=p.getBoundingClientRect(),s=getComputedStyle(p);
            return {x:r.x,right:r.right,y:r.y,bottom:r.bottom,cardX:card.x,cardRight:card.right,
                trackY:track.y,trackBottom:track.bottom,width:p.clientWidth,scrollWidth:p.scrollWidth,
                height:p.clientHeight,scrollHeight:p.scrollHeight,clamp:s.webkitLineClamp,
                whiteSpace:s.whiteSpace,maxHeight:s.maxHeight};
        });
    }""")
    assert len(geometry) == 4
    for text in geometry:
        assert text["width"] + 1 >= text["scrollWidth"] and text["height"] + 1 >= text["scrollHeight"], text
        assert text["clamp"] in ("none", "0") and text["whiteSpace"] != "nowrap" and text["maxHeight"] == "none"
        assert text["x"] >= text["cardX"] - 1 and text["right"] <= text["cardRight"] + 1, text
        assert text["y"] >= text["trackY"] - 1 and text["bottom"] <= text["trackBottom"] + 1, text
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate(_COLLAPSE) == []
    recent.locator("img").evaluate_all("images => Promise.all(images.map(image => { image.loading='eager'; return image.decode(); }))")
    # Capture from the document top so the sticky navbar does not cover the
    # disclosure in an oversized element screenshot; no application CSS is hidden.
    page.evaluate("window.scrollTo(0, 0)")
    capture = tmp_path / "recent-shelf-opened-375-dense-200-full.png"
    page.screenshot(path=str(capture), full_page=True)
    box = details.bounding_box()
    with Image.open(capture) as image:
        image.crop((int(box["x"]), int(box["y"]), int(box["x"] + box["width"] + 1),
                    int(box["y"] + box["height"] + 1))).save(tmp_path / "recent-shelf-opened-375-dense-200-details.png")
    assert_page_clean(page)


def test_malformed_overflow_cannot_add_a_second_terminal_or_disable_the_native_fallback(
    authed_page, live_server, request,
):
    expected = _seed(live_server, "malformed", request)
    page = authed_page
    source = (ROOT / "static/js/recent-shelf.js").read_text()
    # Fault injection runs immediately before the unchanged enhancement script,
    # after the real Home template has parsed but before its buffer is moved.
    corrupt = """(function () {
        const template=document.querySelector('template[data-recent-overflow]');
        const extra=document.createElement('a');extra.className='media-shelf__view-more';
        extra.href='/library?reset=1&view=items&sort=recent';extra.textContent='Malformed terminal fixture';
        template.content.querySelector('li').append(extra);
    }());\n"""
    page.route("**/static/js/recent-shelf.js", lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=corrupt + source,
    ))
    recent = _open(page, live_server)
    expect(recent.locator(".media-card")).to_have_count(8)
    assert recent.locator(".media-card").evaluate_all("nodes=>nodes.map(n=>Number(n.dataset.itemId))") == expected[:8]
    expect(recent.locator("[data-recent-count]")).to_have_text("8 recent items")
    expect(recent.locator("template[data-recent-overflow]")).to_have_count(1)
    expect(recent.locator(".media-shelf__items .media-shelf__view-more")).to_have_count(1)
    for button in recent.locator("[data-recent-action]").all():
        expect(button).to_be_disabled()
    recent.locator(".media-card summary").first.click()
    expect(recent.locator("[data-card-full-creator]").first).to_be_visible()
    assert_page_clean(page)


@pytest.mark.parametrize("replacement", ("swap", "oob"))
def test_retained_wrapper_htmx_replacement_moves_each_fresh_buffer_once(
    authed_page, live_server, request, replacement,
):
    expected = _seed(live_server, "retained-" + replacement, request)
    page = authed_page
    recent = _open(page, live_server)
    wrapper = recent.locator('[data-recent-shelf="streaming-v1"]').element_handle()
    expect(recent.locator(".media-card")).to_have_count(24)
    recent.locator(".media-shelf__items").evaluate("n => n.dataset.recentOriginalTrack = 'true'")
    original_track = recent.locator(".media-shelf__items").element_handle()
    response = page.request.get(live_server["url"] + "/")
    assert response.status == 200
    html = response.text()
    fixture_url = live_server["url"] + "/__recent-retained-fixture"
    if replacement == "oob":
        html = page.evaluate("""html => {
            const doc=new DOMParser().parseFromString(html,'text/html');
            const root=doc.querySelector('[data-recent-shelf="streaming-v1"]');
            root.setAttribute('hx-swap-oob','innerHTML:[data-recent-shelf="streaming-v1"]');
            return root.outerHTML;
        }""", html)
    page.route(fixture_url, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.evaluate("""({url,replacement}) => {
        const button=document.createElement('button');button.type='button';button.textContent='Replace retained fixture';
        button.dataset.testid='recent-retained-swap';button.setAttribute('hx-get',url);
        if(replacement==='swap') {
            button.setAttribute('hx-select','[data-recent-shelf="streaming-v1"] > *');
            button.setAttribute('hx-target','[data-recent-shelf="streaming-v1"]');
            button.setAttribute('hx-swap','innerHTML');
        } else {
            const result=document.createElement('div');result.id='recent-fixture-result';document.querySelector('main').append(result);
            button.setAttribute('hx-target','#recent-fixture-result');
        }
        document.querySelector('main').append(button);htmx.process(button);
    }""", {"url": fixture_url, "replacement": replacement})
    page.evaluate("""url => {
        window.__recentCancelSwap=true;
        document.addEventListener('htmx:beforeSwap',event=>{
            if(window.__recentCancelSwap && event.detail.xhr.responseURL===url) {
                event.preventDefault();document.documentElement.dataset.recentFixtureCancelled='true';
            }
        });
    }""", fixture_url)
    # Negative control: a completed request alone must not count as replacement.
    with page.expect_response(lambda response: response.url == fixture_url):
        page.get_by_test_id("recent-retained-swap").click()
    expect(page.locator("html")).to_have_attribute("data-recent-fixture-cancelled", "true")
    expect(page.locator('[data-recent-original-track="true"]')).to_have_count(1)
    assert original_track.evaluate("n => n.isConnected")
    page.evaluate("window.__recentCancelSwap = false")
    with page.expect_response(lambda response: response.url == fixture_url):
        page.get_by_test_id("recent-retained-swap").click()
    expect(page.locator('[data-recent-original-track="true"]')).to_have_count(0)
    assert original_track.evaluate("n => !n.isConnected")
    expect(recent.locator(".media-card")).to_have_count(24)
    assert recent.locator(".media-card").evaluate_all("nodes=>nodes.map(n=>Number(n.dataset.itemId))") == expected[:24]
    assert wrapper.evaluate("n => n.isConnected && n === document.querySelector('[data-recent-shelf=streaming-v1]')")
    expect(recent.locator("template[data-recent-overflow]")).to_have_count(0)
    expect(recent.locator(".media-shelf__view-more")).to_have_count(1)
    expect(recent.locator("[data-recent-count]")).to_have_text("24 recent items")
    expect(recent.locator('[data-recent-action="previous"]')).to_be_disabled()
    page.evaluate("document.dispatchEvent(new Event('htmx:oobAfterSwap'))")
    expect(recent.locator(".media-card")).to_have_count(24)
    original_track.dispose()
    wrapper.dispose()
    assert_page_clean(page)


def test_prevented_unowned_and_detached_actions_do_not_scroll_or_retain_observed_roots(
    authed_page, live_server, request,
):
    _seed(live_server, "action-boundaries", request)
    page = authed_page
    page.add_init_script("""(() => {
        const Native=ResizeObserver;window.__recentObservedRoots=new Set();
        window.ResizeObserver=class extends Native {
            observe(node,options) {if(node.matches('[data-recent-shelf="streaming-v1"]'))window.__recentObservedRoots.add(node);super.observe(node,options);}
            unobserve(node) {window.__recentObservedRoots.delete(node);super.unobserve(node);}
        };
        document.addEventListener('click',event=>{
            const button=event.target.closest('[data-recent-action]');
            if(!button)return;
            if(window.__recentPreventAction)event.preventDefault();
            if(window.__recentDetachAction)button.closest('[data-recent-shelf]').remove();
        },true);
    })();""")
    recent = _open(page, live_server)
    track = recent.locator(".media-shelf__items").element_handle()
    following = recent.locator('[data-recent-action="next"]')
    assert page.evaluate("window.__recentObservedRoots.size") == 1
    page.evaluate("window.__recentPreventAction = true")
    following.click()
    assert track.evaluate("n=>n.scrollLeft") == 0
    page.evaluate("""() => {
        window.__recentPreventAction=false;
        const button=document.createElement('button');button.type='button';button.dataset.recentAction='next';
        button.dataset.testid='recent-unowned-action';button.textContent='Unowned fixture';document.querySelector('main').append(button);
    }""")
    page.get_by_test_id("recent-unowned-action").click()
    assert track.evaluate("n=>n.scrollLeft") == 0
    page.evaluate("window.__recentDetachAction = true")
    following.click()
    assert track.evaluate("n=>n.scrollLeft === 0 && !n.isConnected")
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert page.evaluate("window.__recentObservedRoots.size") == 0
    track.dispose()
    assert_page_clean(page)


def test_missing_native_scroll_capability_keeps_a_usable_eight_item_fallback(authed_page, live_server, request):
    expected = _seed(live_server, "capability", request)
    page = authed_page
    page.add_init_script("Element.prototype.scrollBy = undefined")
    recent = _open(page, live_server)
    expect(recent.locator(".media-card")).to_have_count(8)
    assert recent.locator(".media-card").evaluate_all("nodes=>nodes.map(n=>Number(n.dataset.itemId))") == expected[:8]
    for button in recent.locator("[data-recent-action]").all():
        expect(button).to_be_disabled()
    recent.locator(".media-card summary").first.click()
    expect(recent.locator("[data-card-full-creator]").first).to_be_visible()
    assert_page_clean(page)


def test_touch_and_reduced_motion_keep_native_details_and_nonanimated_paging(authed_page, live_server, browser, request):
    _seed(live_server, "touch", request)
    context = browser.new_context(has_touch=True, reduced_motion="reduce", service_workers="block",
                                  viewport={"width": 375, "height": 900})
    context.add_cookies(authed_page.context.cookies())
    page = attach_page_guard(context.new_page())
    try:
        recent = _open(page, live_server, 375)
        recent.locator('[data-recent-action="next"]').tap()
        expect(recent.locator(".media-shelf__items")).not_to_have_js_property("scrollLeft", 0)
        card = recent.locator(".media-card").nth(3)
        card.locator("summary").tap()
        expect(card.locator("[data-card-full-creator]")).to_be_visible()
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert recent.locator("[data-recent-shelf]").evaluate("n => getComputedStyle(n).scrollBehavior") == "auto"
        assert_page_clean(page)
    finally:
        context.close()
