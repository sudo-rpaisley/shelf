"""Finite real-page phone-wrap-v1 matrix; existing journeys remain separate gates.

Stress labels exercise wrapping, not a qualified locale or assistive-technology
engine. Cached-style cases use the retained actual app.css asset, not a stub of
CSS.supports. Default fragments below are real HTTP responses, not old renderers
that supposedly understand the new hosted-explanations flag.
"""

import json
from pathlib import Path
import re
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_connected_library import (
    connected_library, _browser_page, _cached_source, _copy, _counts, _db, _holding, _item, _link,
)
from tests.e2e.test_cover_size_integration import _domain_snapshot, _GIF
from tests.e2e.test_library_artwork_sources import RECTANGLE
from tests.test_library_phone_presentation import AVAILABILITY, GEOMETRY, GROUPING, INTRO, MODE, STYLE


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
CANONICAL = "/library?reset=1&view=items&sort=recent"
WIDTHS = (320, 375, 640, 768, 1024, 1280, 1366, 1920)
# Both sides of the existing 40rem gutter and 48rem card seams, plus two
# intermediate phone/control widths. This is finite, not a synthetic cross product.
FIT_WIDTHS = (360, 430, 639, 767)
BOOK = "An Atlas of Quiet Places and Long Autumn Journeys"
VIDEO = "Evening Journeys Through the Northern Islands"
LOCATION = ("Home / Upstairs landing / Reading room window shelf / Tall archive cabinet / "
            "Upper compartment / Box of illustrated journeys")


@pytest.fixture
def phone_library(connected_library):
    server = connected_library
    with _db(server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES (?)", (LOCATION,)).lastrowid
        book = _item(db, BOOK, location_id=location)
        _copy(db, book, location)
        video = _item(db, VIDEO, "digital_video", authors="Robin Clearwater", duration_mins=142)
    try:
        yield {**server, "book": book, "video": video}
    finally:
        assert_page_clean(server["page"])


def _open(server, width=375, *, path=CANONICAL, page=None):
    page = page or server["page"]
    page.set_viewport_size({"width": width, "height": 900})
    response = page.goto(server["url"] + path, wait_until="load")
    assert response.status == 200
    root = page.get_by_test_id("library-page")
    expect(root).to_be_visible()
    assert root.get_attribute("data-library-presentation") == MODE
    return page


def _values(page):
    return page.get_by_test_id("library-filters").evaluate("node => Object.fromEntries(new FormData(node))")


def _help(page):
    node = page.locator(f'details[data-library-help="{MODE}"]')
    expect(node).to_have_count(1)
    expect(node.locator(":scope > summary")).to_be_visible()
    assert node.locator(":scope > summary").inner_text().strip()
    return node


def _box(locator):
    box = locator.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0
    return box


def _controls(page, *, no_js=False):
    """Visible targets remain outside Help and do not collide when they wrap."""
    help_node = _help(page)
    control = page.locator("[data-cover-size-control]")
    expect(control).to_have_count(1)
    slider = control.get_by_role("slider")
    expect(slider).to_have_count(1)
    assert slider.evaluate("node => node.labels.length === 1 && !!node.labels[0].textContent.trim()")
    if no_js:
        expect(slider).to_be_disabled()
    else:
        expect(slider).to_be_enabled()
    expect(control.locator("[data-cover-size-output]")).to_have_text("100%")
    controls = [slider, control.locator("[data-cover-size-output]"),
                control.get_by_text("This page only; not saved.", exact=True)]
    for action in ("smaller", "larger", "reset"):
        button = control.locator(f'[data-cover-size-action="{action}"]')
        expect(button).to_be_visible()
        assert _box(button)["width"] >= 80 and _box(button)["height"] >= 40
        controls.append(button)
    presets = page.get_by_role("navigation", name="Library presets", exact=True).get_by_role("link")
    expect(presets).to_have_count(6)
    controls.extend(presets.all())
    form = page.get_by_test_id("library-filters")
    controls.extend([form.locator('label[for="library-q"]'), form.locator('input[name="q"]'),
                     form.get_by_role("button", name="Search", exact=True),
                     page.get_by_test_id("library-filter-options").locator(":scope > summary"),
                     page.get_by_test_id("library-saved-views").locator(":scope > summary"),
                     help_node.locator(":scope > summary")])
    controls.extend(page.get_by_role("navigation", name="Connected library", exact=True).get_by_role("link").all())
    boxes = []
    width = page.viewport_size["width"]
    for node in controls:
        expect(node).to_be_visible()
        box = _box(node)
        assert box["x"] >= -.5 and box["x"] + box["width"] <= width + .5, box
        assert node.evaluate("node => !node.closest('details[data-library-help]') || node.tagName === 'SUMMARY'")
        assert node.evaluate("node => parseFloat(getComputedStyle(node).fontSize)") >= 14
        boxes.append(box)
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            overlap_x = min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"])
            overlap_y = min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"])
            assert overlap_x <= 1 or overlap_y <= 1, (first, second)
    assert _box(form.locator('input[name="q"]'))["width"] >= 80
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    return help_node


_PAINTED = """member => {
    const box = node => {const r=node.getBoundingClientRect();
        return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
    const outer=box(member), art=box(member.querySelector('.media-artwork'));
    const walker=document.createTreeWalker(member, NodeFilter.SHOW_TEXT), lines=[], unpainted=[];
    for (let node=walker.nextNode(); node; node=walker.nextNode()) {
        if (!node.textContent.trim() || node.parentElement.closest('.sr-only,.media-card__sr-only,script,style,noscript')) continue;
        const range=document.createRange(); range.selectNodeContents(node);
        const rects=Array.from(range.getClientRects()).filter(r=>r.width>.1 && r.height>.1);
        if (!rects.length || getComputedStyle(node.parentElement).visibility !== 'visible') unpainted.push(node.textContent);
        for (const r of rects) lines.push({text:node.textContent.trim(),left:r.left,top:r.top,right:r.right,bottom:r.bottom});
    }
    const overlap=(a,b)=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 &&
                          Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1;
    const outside=lines.filter(r=>r.left<outer.left-1 || r.right>outer.right+1 ||
        r.top<outer.top-1 || r.bottom>outer.bottom+1 || r.left<-.5 || r.right>document.documentElement.clientWidth+.5);
    const collisions=[];
    lines.forEach((line,i)=>{if(overlap(line,art))collisions.push({text:line.text,with:'artwork'});
        lines.slice(i+1).forEach(other=>{if(overlap(line,other))collisions.push({text:line.text,with:other.text});});});
    const title=member.querySelector('h3'), card=member.querySelector('.media-card');
    return {outer,art,lines,unpainted,outside,collisions,display:getComputedStyle(card).display,
        memberOverflow:[getComputedStyle(member).overflowX,getComputedStyle(member).overflowY],
        transforms:[member,card,title].map(n=>getComputedStyle(n).transform),
        titlePosition:getComputedStyle(title).position,titleOverflow:getComputedStyle(title).textOverflow,
        titleHeight:title.clientHeight,titleScroll:title.scrollHeight};
}"""


def _painted(member):
    """Float/block bounding boxes may overlap; painted text lines must not."""
    expect(member.locator("h3")).to_be_visible()
    measured = member.evaluate(_PAINTED)
    assert measured["lines"] and not measured["unpainted"], measured
    assert not measured["outside"] and not measured["collisions"], measured
    outer, art = measured["outer"], measured["art"]
    assert art["left"] >= outer["left"] - 1 and art["right"] <= outer["right"] + 1, measured
    assert art["top"] >= outer["top"] - 1 and art["bottom"] <= outer["bottom"] + 1, measured
    assert measured["display"] != "contents" and outer["width"] > 0
    assert not {"hidden", "clip"} & set(measured["memberOverflow"])
    assert measured["transforms"] == ["none"] * 3
    assert measured["titlePosition"] not in {"absolute", "fixed"}
    assert measured["titleOverflow"] != "ellipsis" and measured["titleScroll"] <= measured["titleHeight"] + 1
    assert member.locator("h3").evaluate("node => parseFloat(getComputedStyle(node).fontSize)") >= 14
    return measured


def _caption_tab(page):
    summary = page.get_by_test_id("library-filter-options").locator(":scope > summary")
    summary.focus()
    summary.press("Tab")
    expect(page.get_by_test_id("library-saved-views").locator(":scope > summary")).to_be_focused()


def test_exact_375_fixture_keeps_recent_two_before_one_counts_links_and_real_slack(phone_library, tmp_path):
    """This front-screen check does NOT replace the retained journey's later leg."""
    server, page = phone_library, phone_library["page"]
    assert (server["book"], server["video"]) == (1, 2), "Fresh fixture must not inherit a session catalogue"
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(server["url"] + CANONICAL, wait_until="load")
    expect(page).to_have_url(server["url"] + CANONICAL)
    _counts(page, 2, 2)
    expect(page.get_by_test_id("library-counts")).to_have_text("2 matching items in 2 groups")
    members = page.locator("[data-library-member]")
    assert members.evaluate_all("nodes => nodes.map(n=>n.dataset.libraryMember)") == ["2", "1"]
    assert _values(page)["sort"] == "recent" and _values(page)["view"] == "items"
    for item_id, title in ((2, VIDEO), (1, BOOK)):
        member = page.locator(f'[data-library-member="{item_id}"]')
        expect(member.locator("h3")).to_have_count(1)
        expect(member.locator("h3")).to_have_text(title)
        assert member.inner_text().count(title) == 1
        expect(member.locator(".media-card__detail")).to_have_attribute("href", f"/item/{item_id}?from=library")
    page.evaluate("window.scrollTo(0,0)")
    metrics = {"results": _box(page.get_by_test_id("library-results")),
               "members": [{"id": node.get_attribute("data-library-member"), "member": _box(node),
                            "heading": _box(node.locator("h3")), "artwork": _box(node.locator(".media-artwork"))}
                           for node in members.all()],
               "search": _box(page.locator("#library-q"))}
    metrics["book_slack_pixels"] = 900 - metrics["members"][1]["heading"]["y"]
    metrics["book_slack_title_lines"] = metrics["book_slack_pixels"] / page.locator(
        '[data-library-member="1"] h3').evaluate("node => parseFloat(getComputedStyle(node).lineHeight)")
    print(json.dumps(metrics, indent=2))
    (tmp_path / "phone-first-screen.json").write_text(json.dumps(metrics, indent=2))
    assert metrics["results"]["y"] < 900 and metrics["search"]["width"] >= 120, metrics
    assert metrics["members"][0]["heading"]["y"] < metrics["members"][1]["heading"]["y"] < 900, metrics
    # Keep the original threshold AND require more than a single-digit margin;
    # record the measured slack rather than infer it from a layout budget.
    assert metrics["book_slack_pixels"] >= 10, metrics
    assert page.get_by_test_id("library-page").get_attribute("data-library-presentation") == MODE
    _controls(page)
    _caption_tab(page)
    for member in members.all():
        _painted(member)


@pytest.mark.parametrize("width", WIDTHS + FIT_WIDTHS)
def test_real_controls_and_wrapping_members_fit_established_widths_and_seams(phone_library, width):
    page = _open(phone_library, width)
    help_node = _controls(page)
    expect(help_node).not_to_have_attribute("open", "")
    _caption_tab(page)
    for member in page.locator("[data-library-member]").all():
        assert member.get_attribute("data-library-member-presentation") == MODE
        measured = _painted(member)
        card = _box(member.locator(".media-card"))
        assert abs(card["width"] - measured["outer"]["width"]) <= 1
        columns = member.evaluate("node => getComputedStyle(node.parentElement.parentElement).gridTemplateColumns.split(' ').length")
        assert columns == (3 if width >= 1280 else 2 if width >= 768 else 1)
        art = measured["art"]
        assert abs(art["width"] / art["height"] - 2 / 3) < .01
        if width < 768:
            expected = min(card["width"] * .3, 96)
            assert abs(art["width"] - expected) <= max(2, expected * .08)
        else:
            assert abs(art["width"] - card["width"]) <= 1


@pytest.mark.parametrize("width", (320, 375, 767, 1024))
def test_200_percent_long_control_labels_wrap_without_hiding_or_shrinking_targets(phone_library, width):
    page = _open(phone_library, width)
    before = _values(page)
    member_text = page.locator("[data-library-member]").all_text_contents()
    page.evaluate("""() => {
        document.documentElement.style.fontSize='200%';
        const labels = {
            '.cover-size-control__range > span':'Dimensione delle copertine della raccolta',
            '[data-cover-size-action="smaller"]':'Copertine più piccole',
            '[data-cover-size-action="larger"]':'Copertine più grandi',
            '[data-cover-size-action="reset"]':'Ripristina dimensione',
            'label[for="library-q"]':'Cerca nella raccolta personale e nelle biblioteche collegate',
            '[data-library-help] > summary':'Informazioni e aiuto per la raccolta personale',
            '[data-testid="library-filter-options"] > summary':'Filtri e opzioni di visualizzazione e ordinamento',
            '[data-testid="library-saved-views"] > summary':'Visualizzazioni personali salvate della raccolta'
        };
        Object.entries(labels).forEach(([selector,text])=>document.querySelector(selector).textContent=text);
        document.querySelectorAll('nav[aria-label="Library presets"] a').forEach((n,i)=>
            n.textContent='Vista della raccolta personale numero '+(i+1));
    }""")
    _controls(page)
    _caption_tab(page)
    assert _values(page) == before
    assert page.locator("[data-library-member]").all_text_contents() == member_text
    for member in page.locator("[data-library-member]").all():
        _painted(member)


_CURVE = """nodes => nodes.map(card => {
    const member=card.closest('[data-library-member]'), art=card.querySelector('.media-artwork');
    const r=card.getBoundingClientRect(), h=card.parentElement.getBoundingClientRect(), a=art.getBoundingClientRect();
    return {id:card.dataset.itemId,card:r.width,cardDisplay:getComputedStyle(card).display,host:h.width,
        grid:getComputedStyle(member.parentElement.parentElement).gridTemplateColumns,
        gridWidth:member.parentElement.parentElement.getBoundingClientRect().width,
        text:member.textContent,fonts:Array.from(member.querySelectorAll('h3,p,span,strong,a')).map(n=>getComputedStyle(n).fontSize),
        artWidth:a.width,artHeight:a.height,shape:art.dataset.coverShape,
        image:card.querySelector('img')?.getAttribute('src') ?? null,
        fit:card.querySelector('img') ? getComputedStyle(card.querySelector('img')).objectFit : null,
        placeholders:card.querySelectorAll('.media-artwork__placeholder').length};
})"""


@pytest.mark.parametrize("view", ("items", "groups"))
@pytest.mark.parametrize("width", (375, 1280))
def test_all_native_formats_images_unknowns_and_placeholders_keep_one_outer_curve_before_reset(
    phone_library, width, view, tmp_path,
):
    # The genuine SVG byte/natural-dimension contract remains in the existing
    # test_library_artwork_sources suite. Here it is one real input to resizing,
    # beside raster/placeholder branches, not a replacement qualification claim.
    from app.config import MEDIA_TYPES
    from app.media_presentation import artwork_spec

    server = phone_library
    expected = {}
    with _db(server) as db:
        for index, media_type in enumerate((*MEDIA_TYPES, "legacy_unknown")):
            for covered in (False, True):
                name = f"phone-{index}.svg" if media_type in {"vinyl", "cassette"} else f"phone-{index}.gif"
                if covered:
                    (server["data_dir"] / "covers" / name).write_bytes(RECTANGLE if name.endswith("svg") else _GIF)
                item_id = _item(db, f"Phone native {index:02d} {media_type} {'image' if covered else 'placeholder'}", media_type,
                                cover_path="covers/" + name if covered else None)
                expected[str(item_id)] = (artwork_spec(media_type).shape, covered)
    path = "/library?" + urlencode({"reset": "1", "view": view, "sort": "title", "q": "Phone native", "page_size": "96"})
    page = _open(server, width, path=path)
    cards = page.locator("[data-library-member] .media-card")
    expect(cards).to_have_count(len(expected))
    # Make all lazy images genuine decoded inputs before measuring the curve.
    for image in cards.locator("img").all():
        image.scroll_into_view_if_needed()
        expect(image).to_have_js_property("complete", True)
        assert image.evaluate("node => node.naturalWidth > 0 && node.naturalHeight > 0")
    baseline = cards.evaluate_all(_CURVE)
    assert all(len(measured["grid"].split()) == (3 if width >= 1280 else 1) for measured in baseline)
    domain, url = _domain_snapshot(server), page.url
    storage = page.evaluate("() => [JSON.stringify(localStorage),JSON.stringify(sessionStorage),document.cookie]")
    requests = []

    def document_request(request):
        if not request.url.startswith((server["url"] + "/static/", server["url"] + "/covers/")):
            requests.append((request.method, request.url))

    page.on("request", document_request)
    samples = []
    try:
        slider = page.locator("[data-cover-size-input]")
        expect(slider).to_have_value("100")
        for percentage in (100, 90, 80, 70, 60, 50):
            if percentage != 100:
                page.locator('[data-cover-size-action="smaller"]').click()
            expect(slider).to_have_value(str(percentage))
            expect(page.locator("[data-cover-size-output]")).to_have_text(f"{percentage}%")
            current = cards.evaluate_all(_CURVE)
            for original, measured in zip(baseline, current, strict=True):
                host = original["host"]
                assert measured["card"] == pytest.approx(min(host, max(80, host * percentage / 100)), abs=1)
                assert measured["cardDisplay"] != "contents"
                for key in ("id", "host", "grid", "gridWidth", "text", "fonts", "image"):
                    assert measured[key] == original[key], (percentage, key, measured, original)
                shape, covered = expected[measured["id"]]
                assert measured["shape"] == shape
                ratio = {"portrait": 2 / 3, "square": 1, "cassette": 5 / 8, "game": 3 / 4}[shape]
                assert measured["artWidth"] / measured["artHeight"] == pytest.approx(ratio, abs=.01)
                if covered:
                    assert measured["fit"] == "contain" and measured["placeholders"] == 0
                else:
                    assert measured["image"] is None and measured["placeholders"] == 1
                expected_art = min(measured["card"] * .3, 96) if width < 768 else measured["card"]
                assert measured["artWidth"] == pytest.approx(expected_art, abs=max(2, expected_art * .08))
            samples.append({"percentage": percentage, "cards": current})
        # Fifty percent was measured above, while still selected, BEFORE Reset.
        for member in page.locator("[data-library-member]").all():
            _painted(member)
        expect(page.locator('[data-cover-size-action="smaller"]')).to_be_disabled()
        page.locator('[data-cover-size-action="larger"]').click()
        expect(slider).to_have_value("60")
        page.locator('[data-cover-size-action="reset"]').click()
        expect(slider).to_have_value("100")
        assert cards.evaluate_all(_CURVE) == baseline
        assert page.url == url and requests == [] and _domain_snapshot(server) == domain
        assert page.evaluate("() => [JSON.stringify(localStorage),JSON.stringify(sessionStorage),document.cookie]") == storage
    finally:
        page.remove_listener("request", document_request)
        (tmp_path / "phone-native-curve.json").write_text(json.dumps(samples, indent=2))
    page.reload(wait_until="load")
    expect(page.locator("[data-cover-size-input]")).to_have_value("100")


def _focus_decorations(member):
    decorations = """node => [node,node.querySelector('.media-card'),node.querySelector('.media-card__detail'),node.parentElement]
        .map(n=>{const s=getComputedStyle(n),r=n.getBoundingClientRect();return {outline:s.outlineStyle,width:parseFloat(s.outlineWidth),
            color:s.outlineColor,shadow:s.boxShadow,border:s.borderTopStyle,borderWidth:parseFloat(s.borderTopWidth),
            borderColor:s.borderTopColor,left:r.left,top:r.top,right:r.right,bottom:r.bottom};})"""
    return member.evaluate(decorations)


def _enclosing_focus(before, after, member):
    enclosing = []
    for old, new in zip(before, after, strict=True):
        painted = ((new["outline"] not in {"none", "hidden"} and new["width"] > 0 and new["color"] != "rgba(0, 0, 0, 0)")
                   or new["shadow"] != "none" or (new["border"] not in {"none", "hidden"} and new["borderWidth"] > 0))
        changed = any(new[key] != old[key] for key in ("outline", "width", "color", "shadow", "border", "borderWidth", "borderColor"))
        if painted and changed and new["left"] <= member["left"] + 1 and new["right"] >= member["right"] - 1 and new["top"] <= member["top"] + 1 and new["bottom"] >= member["bottom"] - 1:
            enclosing.append(new)
    return enclosing


@pytest.mark.parametrize("target", ("artwork", "title"))
def test_actual_title_and_artwork_hit_the_one_native_link_with_enclosing_keyboard_focus(phone_library, target):
    page = _open(phone_library)
    video = page.locator(f'[data-library-member="{phone_library["video"]}"]')
    book = page.locator(f'[data-library-member="{phone_library["book"]}"]')
    link = video.locator(".media-card__detail")
    expect(link).to_have_count(1)
    expect(video.locator("h3")).to_have_count(1)
    assert video.locator('[tabindex]:not([tabindex="-1"])').count() == 0
    before = _focus_decorations(video)
    page.keyboard.press("Tab")  # Establish actual keyboard modality before focus.
    link.focus()
    expect(link).to_be_focused()
    after = _focus_decorations(video)
    member = _painted(video)["outer"]
    enclosing = _enclosing_focus(before, after, member)
    assert enclosing, {"before": before, "after": after, "member": member}
    link.press("Tab")
    expect(book.locator(".media-card__detail")).to_be_focused()
    node = video.locator(".media-artwork" if target == "artwork" else "h3")
    node.scroll_into_view_if_needed()
    hit = node.evaluate("""node => {const range=document.createRange();range.selectNodeContents(node);
        const r=node.matches('h3') ? range.getClientRects()[0] : node.getBoundingClientRect();
        const b=node.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
        return {native:document.elementFromPoint(x,y)?.closest('a') === node.closest('a'),x:x-b.left,y:y-b.top};}""")
    assert hit["native"], hit
    with page.expect_navigation(wait_until="load") as navigation:
        node.click(position={"x": hit["x"], "y": hit["y"]})
    assert navigation.value.status == 200
    expect(page).to_have_url(phone_library["url"] + f'/item/{phone_library["video"]}?from=library')
    expect(page.get_by_role("heading", level=1)).to_have_text(VIDEO)


@pytest.mark.parametrize("decoration", (
    "transparent-outset-shadow", "transparent-outline", "transparent-border", "top-border-only",
    "zero-outset-shadow", "inset-shadow", "offset-outset-shadow", "non-enclosing-outline",
    "unchanged-outline-with-transparent-shadow",
))
def test_focus_oracle_rejects_nonpainting_or_non_enclosing_fixture(phone_library, decoration, tmp_path):
    """Isolated browser STYLE MUTATIONS test the oracle, not production focus CSS."""
    page = _open(phone_library)
    video = page.locator(f'[data-library-member="{phone_library["video"]}"]')
    link = video.locator(".media-card__detail")
    # Suppress ALL real anchor/member/ancestor cues, including pseudo-elements;
    # an otherwise valid source outline must not rescue a bad fixture shadow.
    page.add_style_tag(content="""*, *::before, *::after {
        outline: none !important; box-shadow: none !important; border-color: transparent !important;
    }""")
    residual = video.evaluate("""node => {
        const nodes=new Set([node,...node.querySelectorAll('*')]);
        for(let n=node.parentElement;n;n=n.parentElement)nodes.add(n);
        return [...nodes].flatMap(n=>[null,'::before','::after'].map(p=>getComputedStyle(n,p)))
            .filter(s=>s.outlineStyle!=='none' || s.boxShadow!=='none' ||
                ['Top','Right','Bottom','Left'].some(side=>s['border'+side+'Color']!=='rgba(0, 0, 0, 0)'))
            .map(s=>({outline:s.outline,shadow:s.boxShadow,border:s.borderColor}));
    }""")
    assert residual == [], residual
    selector = f'[data-library-member="{phone_library["video"]}"]'
    declarations = {
        "transparent-outset-shadow": "box-shadow: 0 0 0 2px transparent !important;",
        "transparent-outline": "outline: 2px solid rgba(255, 0, 0, 0) !important;",
        "transparent-border": "border: 2px solid rgba(255, 0, 0, 0) !important;",
        "top-border-only": "border: 2px solid transparent !important; border-top-color: yellow !important;",
        "zero-outset-shadow": "box-shadow: 0 0 0 0 yellow !important;",
        "inset-shadow": "box-shadow: inset 0 0 0 2px yellow !important;",
        "offset-outset-shadow": "box-shadow: 8px 0 0 0 yellow !important;",
        "non-enclosing-outline": "outline: 2px solid yellow !important;",
        "unchanged-outline-with-transparent-shadow": "box-shadow: 0 0 0 2px transparent !important;",
    }
    static = selector + " {outline: 2px solid yellow !important;}" if decoration == "unchanged-outline-with-transparent-shadow" else ""
    target = selector + (" .media-card" if decoration == "non-enclosing-outline" else "")
    page.add_style_tag(content=static + target + ":focus-within {" + declarations[decoration] + "}")
    before = _focus_decorations(video)
    page.keyboard.press("Tab")
    link.focus()
    expect(link).to_be_focused()
    assert video.evaluate("node => node.matches(':focus-within')")
    after = _focus_decorations(video)
    member = video.evaluate("node => {const r=node.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};}")
    enclosing = _enclosing_focus(before, after, member)
    evidence = {"fixture": decoration, "before": before, "after": after, "member": member, "accepted": enclosing}
    print(json.dumps(evidence, indent=2))
    (tmp_path / "focus-oracle-fixture.json").write_text(json.dumps(evidence, indent=2))
    assert enclosing == [], evidence
    assert_page_clean(page)


@pytest.mark.parametrize("width", (375, 1280))
def test_help_is_single_copy_native_expanded_and_print_discoverable_without_hiding_actions(phone_library, width):
    page = _open(phone_library, width)
    help_node = _controls(page)
    values = _values(page)
    expect(help_node).not_to_have_attribute("open", "")
    for text in (INTRO, GEOMETRY, AVAILABILITY):
        expect(help_node.get_by_text(text, exact=True)).to_be_hidden()
        expect(page.get_by_text(text, exact=True)).to_have_count(1)
    page.emulate_media(media="print")
    expect(help_node.locator(":scope > summary")).to_be_visible()
    help_node.locator(":scope > summary").press("Enter")
    expect(help_node).to_have_attribute("open", "")
    for text in (INTRO, GEOMETRY, AVAILABILITY):
        expect(help_node.get_by_text(text, exact=True)).to_be_visible()
    expect(page.get_by_test_id("library-counts")).to_be_visible()
    expect(page.get_by_text(LOCATION, exact=False)).to_be_visible()
    for member in page.locator("[data-library-member]").all():
        _painted(member)
    assert _values(page) == values
    page.emulate_media(media="screen")
    help_node.locator(":scope > summary").press("Space")
    expect(help_node).not_to_have_attribute("open", "")
    _controls(page)
    with page.expect_navigation(wait_until="load") as navigation:
        page.get_by_test_id("library-filters").get_by_role("button", name="Search", exact=True).click()
    assert navigation.value.status == 200 and _values(page) == values
    _controls(page)


def test_no_js_help_native_submission_and_disabled_cover_controls_keep_the_full_page(phone_library, browser):
    server = phone_library
    with _browser_page(browser, java_script_enabled=False, service_workers="block",
                       storage_state=server["page"].context.storage_state(), viewport={"width": 375, "height": 900}) as page:
        try:
            _open(server, page=page)
            help_node = _controls(page, no_js=True)
            expect(page.get_by_text("Enable JavaScript to change cover size.", exact=True)).to_be_visible()
            before = _values(page)
            help_node.locator(":scope > summary").press("Enter")
            expect(help_node.get_by_text(GEOMETRY, exact=True)).to_be_visible()
            help_node.locator(":scope > summary").press("Space")
            _caption_tab(page)
            for member in page.locator("[data-library-member]").all():
                _painted(member)
            with page.expect_navigation(wait_until="load") as navigation:
                page.get_by_test_id("library-filters").get_by_role("button", name="Search", exact=True).click()
            assert navigation.value.status == 200 and _values(page) == before
            _controls(page, no_js=True)
        finally:
            assert_page_clean(page)


@pytest.mark.parametrize("cached_direct", (False, True), ids=("new-html-old-css", "old-app-css-new-direct-css"))
def test_actual_cached_app_asset_with_new_html_preserves_native_help_and_live_controls(phone_library, cached_direct):
    page = phone_library["page"]
    cached = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
    served = []

    def old_bundle(route):
        served.append(route.request.url)
        route.fulfill(status=200, content_type="text/css", body=cached)

    page.route("**/static/css/app.css", old_bundle)
    if not cached_direct:
        page.route("**" + STYLE, lambda route: route.fulfill(status=200, content_type="text/css", body="/* presentation stylesheet unavailable */"))
    _open(phone_library)
    assert len(served) == 1
    expect(page.locator(f'head link[href="{STYLE}"]')).to_have_count(1)
    help_node = _controls(page)
    before = _values(page)
    help_node.locator(":scope > summary").press("Enter")
    expect(help_node.get_by_text(GEOMETRY, exact=True)).to_be_visible()
    help_node.locator(":scope > summary").press("Space")
    expect(help_node.get_by_text(GEOMETRY, exact=True)).to_be_hidden()
    member = page.locator(f'[data-library-member="{phone_library["book"]}"]')
    measured = _painted(member)
    card = _box(member.locator(".media-card"))
    if cached_direct:
        assert measured["art"]["width"] == pytest.approx(min(.3 * card["width"], 96), abs=3)
    else:
        # Natural native blocks/actions are safe. There is NO 900px claim for
        # new HTML without its enhancement CSS.
        assert measured["art"]["width"] == pytest.approx(card["width"], abs=1)
    page.locator('[data-cover-size-action="smaller"]').click()
    expect(page.locator("[data-cover-size-input]")).to_have_value("90")
    assert _box(member.locator(".media-card"))["width"] == pytest.approx(measured["outer"]["width"] * .9, abs=1)
    assert _values(page) == before


@pytest.mark.parametrize("markers", ("member-only", "page-only", "neither", "unknown-page"))
def test_new_direct_css_requires_both_markers_and_default_http_fragment_keeps_full_width_art(phone_library, markers):
    server, page = phone_library, phone_library["page"]
    response = page.request.get(server["url"] + CANONICAL)
    assert response.status == 200
    html = response.text()
    assert f'data-library-presentation="{MODE}"' in html
    assert f'data-library-member-presentation="{MODE}"' in html
    if markers in {"page-only", "neither"}:
        fragment = page.request.get(server["url"] + "/api/library/search?view=items&sort=recent")
        assert fragment.status == 200 and "data-library-member-presentation" not in fragment.text()
        assert AVAILABILITY in fragment.text()
        html, count = re.subn(r'<section\b[^>]*id="library-results".*?</section>', lambda match: fragment.text(), html, count=1, flags=re.S)
        assert count == 1
    if markers in {"member-only", "neither", "unknown-page"}:
        value = 'data-library-presentation="unrecognized-mode"' if markers == "unknown-page" else ""
        html = html.replace(f'data-library-presentation="{MODE}"', value, 1)
    # Real cached app.css + current direct stylesheet isolates the upgrade seam;
    # no claim is made that the entire old renderer honored a future host flag.
    page.route("**/static/css/app.css", lambda route: route.fulfill(
        status=200, content_type="text/css", body=(ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()))
    fixture_path = "/library?phone-upgrade-fixture=" + markers
    page.route("**" + fixture_path, lambda route: route.fulfill(
        status=200, content_type="text/html", body=html,
        headers={"Content-Security-Policy": response.headers["content-security-policy"]}))
    page.set_viewport_size({"width": 375, "height": 900})
    page.goto(server["url"] + fixture_path, wait_until="load")
    expect(page.locator(f'head link[href="{STYLE}"]')).to_have_count(1)
    # Compare the whole finite formatting signature with the enhancement really
    # disabled, not just artwork width: an unguarded display/margin/font rule
    # would otherwise escape a full-width-art assertion.
    signature = """nodes => nodes.map(member => [member,...member.querySelectorAll('*')].map(node => {
        const s=getComputedStyle(node), keys=['display','position','float','clear','width','minWidth','maxWidth',
            'height','minHeight','maxHeight','marginTop','marginRight','marginBottom','marginLeft',
            'paddingTop','paddingRight','paddingBottom','paddingLeft','gap','fontSize','lineHeight',
            'overflowX','overflowY','overflowWrap','textOverflow','transform','contain'];
        return Object.fromEntries(keys.map(key=>[key,s[key]]));
    }))"""
    style = page.locator(f'head link[href="{STYLE}"]')
    style.evaluate("node => node.sheet.disabled = true")
    without_enhancement = page.locator("[data-library-member]").evaluate_all(signature)
    style.evaluate("node => node.sheet.disabled = false")
    assert page.locator("[data-library-member]").evaluate_all(signature) == without_enhancement
    for member in page.locator("[data-library-member]").all():
        measured = _painted(member)
        card = _box(member.locator(".media-card"))
        assert measured["art"]["width"] == pytest.approx(card["width"], abs=1)
        assert _box(member.locator("h3"))["y"] >= measured["art"]["bottom"] - 1


def test_long_metadata_holdings_and_existing_other_format_disclosure_keep_order_and_all_actions(phone_library):
    server = phone_library
    title = "A substantially longer local title: " + "遠い島の記録 and the winter archive " * 5
    with _db(server) as db:
        source = _cached_source(db, server["url"])
        book = _item(db, title, subtitle="The full subtitle remains ordinary visible text " * 3,
                     series_name="Archive Saga #1/2", series_position=0.5, authors="A creator with a substantial credit")
        location = db.execute("INSERT INTO locations (name) VALUES (?)", (LOCATION + " / " + "Archive " * 20,)).lastrowid
        _copy(db, book, location)
        _holding(db, source, book, server["url"] + "/local-open/book")
        digital = _item(db, "The other complete edition", "ebook")
        _holding(db, source, digital, server["url"] + "/local-open/ebook")
        _link(db, book, digital)
    page = _open(server, path="/library?reset=1&view=groups&availability=physical")
    _counts(page, 2, 2)  # The original physical book plus this new group.
    other = page.locator(f'[data-library-group="{book}"] > details')
    expect(other).to_have_count(1)
    other.locator(":scope > summary").press("Enter")
    for item_id, matches in ((book, "true"), (digital, "false")):
        member = page.locator(f'[data-library-member="{item_id}"]')
        expect(member).to_have_attribute("data-matches", matches)
        expect(member.locator("details")).to_have_count(0)
        expect(member.locator("h3")).to_have_count(1)
        assert not member.locator('a[href$="/edit"]').count()
        _painted(member)
        for action in member.locator('[data-holding-source] a').all():
            expect(action).to_be_visible()
            assert action.get_attribute("target") == "_blank"
            assert action.get_attribute("rel") == "noopener noreferrer"
            action.scroll_into_view_if_needed()
            assert action.evaluate("node => {const r=node.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('a')===node;}")
    member = page.locator(f'[data-library-member="{book}"]')
    expect(member.locator("h3")).to_have_text(title)
    text = member.inner_text()
    assert text.index(f"#{book}") < text.index("Archive Saga #1/2 · 0.5") < text.index("Matches filters")
    lines = _painted(member)["lines"]
    identity = next(line for line in lines if line["text"] == f"#{book}")
    series = next(line for line in lines if "Archive Saga #1/2" in line["text"])
    status = next(line for line in lines if line["text"] == "Matches filters")
    for first, second in ((identity, series), (series, status)):
        assert second["top"] >= first["top"] - 1
        if abs(second["top"] - first["top"]) <= 1:
            assert second["left"] >= first["left"]
    assert "Archive " * 20 in text and "The full subtitle remains ordinary visible text " * 2 in text
    expect(member.locator('[data-holding-source] a')).to_have_count(1)
    expect(page.locator(f'[data-library-member="{digital}"] [data-holding-source] a')).to_have_count(1)
    expect(_help(page).get_by_text(GROUPING, exact=True)).to_have_count(1)


@pytest.mark.parametrize("drilldown", (False, True), ids=("series-index", "series-card-drilldown"))
def test_series_stays_control_free_with_help_and_natural_numeric_order(phone_library, drilldown):
    server = phone_library
    with _db(server) as db:
        later = _item(db, "Series later", series_name="星 #1/2", series_position=2)
        earlier = _item(db, "Series earlier", series_name="星 #1/2", series_position=.5)
    path = "/library?" + urlencode({"reset": "1", "view": "series", **({"series": "星 #1/2"} if drilldown else {})})
    page = _open(server, path=path)
    expect(page.locator("[data-cover-size-panel],[data-cover-size-control]")).to_have_count(0)
    expect(page.locator("#library-cover-size-description")).to_have_count(0)
    help_node = _help(page)
    assert GEOMETRY not in help_node.text_content()
    help_node.locator(":scope > summary").press("Enter")
    expect(help_node.get_by_text(INTRO, exact=True)).to_be_visible()
    expect(help_node.get_by_text(AVAILABILITY, exact=True)).to_be_visible()
    if drilldown:
        assert page.locator("[data-library-member]").evaluate_all("nodes=>nodes.map(n=>n.dataset.libraryMember)") == [str(earlier), str(later)]
        for member in page.locator("[data-library-member]").all():
            _painted(member)
    else:
        expect(page.locator('[data-library-series="星 #1/2"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    _caption_tab(page)
