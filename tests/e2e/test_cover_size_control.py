"""Rendered-component/shell browser checks, NOT actual shared-wiring E2E.

This lane cannot edit shared templates. A test-only Jinja shell includes the real
base/card/shelf/Library partials and adds the candidate assets after app.js. All
requests (including real vendored HTMX/OOB swaps) are fulfilled from local files
or synthetic records. No HTTP/auth/database/account integration is claimed here;
the parent must test those after independently reviewed shared wiring.
"""

import base64
from contextlib import contextmanager
import json
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://shelf.test"
INPUT = "[data-cover-size-input]"
CONTROL = "[data-cover-size-control]"
_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _environment():
    from app.config import creator_label
    from app.services.media_cards import build_media_card

    def gettext(text, **kwargs):
        return text % kwargs if kwargs else text

    def ngettext(one, many, number, **kwargs):
        return gettext(one if number == 1 else many, **kwargs)

    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    env.globals.update(
        _=gettext, ngettext=ngettext, creator_label=creator_label, build_media_card=build_media_card,
        current_locale=lambda: "en", js_catalog=lambda: {}, feature_on=lambda key: False,
        i18n_enabled=lambda: False,
    )
    return env


def _records():
    from app.config import MEDIA_TYPES

    rows = []
    for media_type in (*MEDIA_TYPES, "legacy_unknown"):
        for covered in (False, True):
            item_id = len(rows) + 1
            rows.append(dict(
                id=item_id, media_type=media_type,
                title=f"{media_type} — A very long title with readable words and " + "unbroken" * 14,
                authors="A recorded creator", subtitle="A recorded subtitle", matches=True,
                cover_path=f"covers/cover-size-{item_id}.gif" if covered else None,
                series_key="Local series", series_name="Local series", series_position=1,
                tags=("Recorded tag",), owned=True, wishlisted=False, copy_count=1,
                holdings=(dict(source_key="physical", state="available", kind="physical",
                               source_name=None, location="Recorded shelf", url=None,
                               last_seen=None, detail="Recorded position, not a fresh observation"),),
            ))
    return rows


def _render():
    from app.services.media_cards import MediaShelf, build_media_card

    rows = _records()
    cards = tuple(build_media_card(row, edit_href=f"/item/{row['id']}/edit", badges=("Recorded badge",))
                  for row in rows)
    # All finite shapes, both images and placeholders, in each declared host.
    shelf = MediaShelf("cover-shell", "Component shelf", cards, (), True, "/library?view=items")
    html = _environment().from_string("""
        {% extends 'base.html' %}
        {% block head %}
        <link rel="stylesheet" href="/static/css/media-cards.source.css">
        <link rel="stylesheet" href="/static/css/cover-size.source.css">
        {% endblock %}
        {% block content %}
        <div class="page-layout space-y-6" data-component-shell>
            <h1>Cover sizing component shell — not shared-page wiring</h1>
            <p id="cover-size-input-1">Existing ID collision sentinel</p>
            <div id="cover-controls-primary">{% include 'fragments/cover_size_control.html' %}</div>
            <form data-shell-filters method="get" action="/library">
                <label class="block">Catalogue query <input class="block w-full min-w-0 max-w-full" name="q" value="unchanged needle"></label>
                <input type="hidden" name="types" value="book,cd">
                <input type="hidden" name="sort" value="recent">
            </form>
            <div data-host="shelf">{% include 'fragments/media_shelf.html' %}</div>
            <section data-host="music">
                <h2>Music-like shared-card grid</h2>
                <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6" data-shell-grid>
                {% for card in cards %}<div class="min-w-0">{% include 'components/media_card.html' %}</div>{% endfor %}
                </div>
            </section>
            <section data-host="library">{% include 'fragments/library_results.html' %}</section>
            <div id="swapped-card-host" data-host="swapped">{% set card = cards[0] %}{% include 'components/media_card.html' %}</div>
            <div id="cover-controls-oob">{% include 'fragments/cover_size_control.html' %}</div>
            <button type="button" data-shell-swap hx-get="/component/swap" hx-target="#cover-controls-primary"
                    hx-swap="outerHTML" hx-push-url="false">Replace component fixtures</button>
        </div>
        {% endblock %}
    """).render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")), user=None, nav_tabs=[], media_navigation={},
        cards=cards, shelf=shelf, items=rows, result_kind="items", filters={"series": "", "view": "items"},
        view_links=[], matching_count=len(rows), group_count=len(rows), pages=1,
    )
    return html.replace("</body>", '<script src="/static/js/cover-size.js"></script></body>')


def _swap(serial):
    env = _environment()
    control = env.get_template("fragments/cover_size_control.html").render()
    card = env.get_template("components/media_card.html").render(
        card=env.globals["build_media_card"](_records()[0]),
    )
    return (
        f'<div id="cover-controls-primary" data-swap-serial="{serial}">{control}</div>'
        f'<div id="cover-controls-oob" hx-swap-oob="outerHTML" data-swap-serial="{serial}">{control}</div>'
        f'<div id="swapped-card-host" hx-swap-oob="innerHTML">{card}</div>'
    )


_COUNT_LISTENERS = """(() => {
    window.__coverListeners = [];
    const original = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(name, callback, options) {
        if (document.currentScript?.src.endsWith('/static/js/cover-size.js')) {
            window.__coverListeners.push({name, document: this === document});
        }
        return original.call(this, name, callback, options);
    };
})()"""


@contextmanager
def _shell(browser, *, width=1024, height=900, javascript=True, touch=False):
    requests, unexpected = [], []
    serial = 0
    html = _render()  # Fail a broken fixture immediately, not in an async route callback.
    context = browser.new_context(
        viewport={"width": width, "height": height}, java_script_enabled=javascript,
        reduced_motion="reduce", has_touch=touch, service_workers="block",
    )
    context.add_init_script(_COUNT_LISTENERS)

    def serve(route):
        nonlocal serial
        parts = urlsplit(route.request.url)
        requests.append((route.request.method, parts.path))
        if parts.netloc != "shelf.test" or route.request.method != "GET":
            unexpected.append(route.request.url)
            return route.abort()
        if parts.path == "/":
            return route.fulfill(status=200, content_type="text/html", body=html, headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
            })
        if parts.path == "/component/swap":
            serial += 1
            return route.fulfill(status=200, content_type="text/html", body=_swap(serial))
        if parts.path.startswith("/covers/cover-size-") and parts.path.endswith(".gif"):
            return route.fulfill(status=200, content_type="image/gif", body=_GIF)
        if parts.path.startswith("/static/"):
            path = (ROOT / parts.path.lstrip("/")).resolve()
            if path.is_relative_to(ROOT / "static") and path.is_file():
                return route.fulfill(status=200, content_type=mimetypes.guess_type(path)[0], body=path.read_bytes())
        unexpected.append(route.request.url)
        return route.abort()

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(ORIGIN + "/?q=original", wait_until="load")
        if javascript:
            expect(page.locator(CONTROL).first).to_have_attribute("data-cover-size-enhanced", "true")
        yield page, requests
        assert not unexpected, unexpected
        assert_page_clean(page)
    finally:
        context.close()


def _action(page, action, *, instance=0):
    return page.locator(CONTROL).nth(instance).locator(f'[data-cover-size-action="{action}"]')


def _state(page, percentage):
    for slider in page.locator(INPUT).all():
        expect(slider).to_have_value(str(percentage))
    for output in page.locator("[data-cover-size-output]").all():
        expect(output).to_have_text(f"{percentage}%")
    scale = page.evaluate("() => document.documentElement.style.getPropertyValue('--media-cover-scale')")
    assert float(scale) == percentage / 100
    assert 0.5 <= float(scale) <= 1


def _metrics(page):
    expect(page.locator(".media-card").first).to_be_visible()
    return page.locator(".media-card").evaluate_all("""cards => cards.map(card => {
        const artwork = card.querySelector('.media-artwork');
        const image = artwork.querySelector('img');
        const placeholder = artwork.querySelector('.media-artwork__placeholder');
        const box = artwork.getBoundingClientRect();
        const text = card.querySelector('.media-card__title');
        return {host: card.closest('[data-host]').dataset.host, type: card.dataset.mediaType,
            width: card.getBoundingClientRect().width, parentWidth: card.parentElement.getBoundingClientRect().width,
            artworkWidth: box.width, artworkHeight: box.height, shape: artwork.dataset.coverShape,
            font: getComputedStyle(text).fontSize, lineHeight: getComputedStyle(text).lineHeight,
            titleFits: text.scrollWidth <= text.clientWidth && text.scrollHeight <= text.clientHeight,
            transform: getComputedStyle(card).transform, text: card.textContent,
            image: image ? {src: image.getAttribute('src'), fit: getComputedStyle(image).objectFit,
                width: image.getBoundingClientRect().width, height: image.getBoundingClientRect().height} : null,
            placeholder: placeholder ? {width: placeholder.getBoundingClientRect().width,
                height: placeholder.getBoundingClientRect().height} : null};
    })""")


def _layout(page):
    return page.evaluate("""() => ({
        clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth,
        columns: [...document.querySelectorAll('[data-shell-grid], [data-host="library"] .grid')].map(
            node => ({columns: getComputedStyle(node).gridTemplateColumns, gap: getComputedStyle(node).gap})),
        slots: [...document.querySelectorAll('.media-shelf__slot')].map(node => node.getBoundingClientRect().width),
        shelfGap: getComputedStyle(document.querySelector('.media-shelf__items')).gap,
        filters: [...new FormData(document.querySelector('[data-shell-filters]')).entries()],
        libraryText: document.querySelector('[data-host="library"]').textContent,
    })""")


def _assert_artwork(metrics):
    for card in metrics:
        assert card["width"] > 0 and card["artworkHeight"] > 0, card
        assert card["artworkWidth"] == pytest.approx(card["width"], abs=1), card
        expected = (1 if card["type"] in {"cd", "vinyl", "digital_music", "audiobook"}
                    else 8 / 5 if card["type"] == "cassette"
                    else 4 / 3 if card["type"] == "video_game" else 3 / 2)
        assert card["artworkHeight"] / card["artworkWidth"] == pytest.approx(expected, abs=0.025), card
        assert card["transform"] == "none" and card["titleFits"], card
        child = card["image"] or card["placeholder"]
        assert child and child["width"] == pytest.approx(card["artworkWidth"] - 2, abs=1), card
        assert child["height"] == pytest.approx(card["artworkHeight"] - 2, abs=1), card
        if card["image"]:
            assert card["image"]["fit"] == "contain", card


def _assert_page_fits(page):
    layout = _layout(page)
    assert layout["scrollWidth"] <= layout["clientWidth"], page.evaluate("""() => ({
        wide: [...document.querySelectorAll('main *')].filter(node =>
            node.getBoundingClientRect().width > document.documentElement.clientWidth
            && !node.closest('.media-shelf__items')).slice(0, 15).map(node => ({
                tag: node.tagName, class: node.className, width: node.getBoundingClientRect().width,
                display: getComputedStyle(node).display, html: node.outerHTML.slice(0, 180),
            })),
        shelfLabels: [...document.querySelectorAll('.media-shelf .media-card__sr-only')].slice(-3).map(node => ({
            x: node.getBoundingClientRect().x, width: node.getBoundingClientRect().width,
            parent: node.offsetParent?.tagName, class: node.offsetParent?.className,
        })),
    })""")


@pytest.mark.parametrize("width", [320, 375, 640, 768, 1024, 1280, 1366, 1920])
def test_component_shell_computed_sizes_reset_and_all_shapes_in_each_host(browser, width, tmp_path):
    with _shell(browser, width=width) as (page, _):
        before, layout = _metrics(page), _layout(page)
        assert all(slot == pytest.approx(160) for slot in layout["slots"]), "Original 10rem default"
        _assert_page_fits(page)
        for _ in range(5):
            _action(page, "smaller").click()
        _state(page, 50)
        after, smaller_layout = _metrics(page), _layout(page)
        assert smaller_layout == layout, "No column, slot, gutter, filter, metadata or count changes"
        assert len(before) == len(after)
        assert {card["shape"] for card in after} == {"square", "portrait", "cassette", "game"}
        for original, smaller in zip(before, after, strict=True):
            assert smaller["width"] == pytest.approx(max(80, original["width"] / 2), abs=1), smaller
            assert smaller["width"] < original["width"]
            assert (smaller["font"], smaller["lineHeight"], smaller["text"]) == (
                original["font"], original["lineHeight"], original["text"],
            )
            if original["image"]:
                assert smaller["image"]["src"] == original["image"]["src"]
        _assert_artwork(before)
        _assert_artwork(after)
        assert all(card["width"] == pytest.approx(80) for card in after if card["host"] == "shelf")
        _action(page, "reset", instance=1).click()
        _state(page, 100)
        assert _metrics(page) == before
        (tmp_path / f"component-shell-geometry-{width}.json").write_text(json.dumps({
            "scope": "Rendered component/shell, not actual app wiring or WCAG conformance",
            "width": width, "before": before, "smaller": after, "layout": layout,
        }, indent=2) + "\n")


def test_component_shell_multiple_controls_have_collision_free_labels_descriptions_and_outputs(browser):
    with _shell(browser) as (page, _):
        sliders = page.get_by_role("slider", name="Cover size", exact=True)
        expect(sliders).to_have_count(2)
        associations = sliders.evaluate_all("""inputs => inputs.map(input => ({
            id: input.id, labels: [...input.labels].map(label => label.htmlFor),
            note: document.getElementById(input.getAttribute('aria-describedby')).textContent,
            outputFor: input.closest('[data-cover-size-control]').querySelector('output').getAttribute('for'),
        }))""")
        assert len({row["id"] for row in associations}) == 2
        assert "cover-size-input-1" not in {row["id"] for row in associations}
        for row in associations:
            assert row["labels"] == [row["id"]] and row["outputFor"] == row["id"]
            assert row["note"] == "This page only; not saved."
        _action(page, "smaller", instance=1).click()
        _state(page, 90)
        _action(page, "smaller").click()
        _state(page, 80)


def test_component_shell_shelf_containing_block_prevents_existing_offscreen_label_document_overflow(browser):
    with _shell(browser, width=320) as (page, _):
        _assert_page_fits(page)
        page.locator(".media-shelf__slot").evaluate_all("nodes => nodes.forEach(node => node.style.position = 'static')")
        assert _layout(page)["scrollWidth"] > _layout(page)["clientWidth"], "Negative control recreates original overflow"
        page.locator(".media-shelf__slot").evaluate_all("nodes => nodes.forEach(node => node.style.removeProperty('position'))")
        _assert_page_fits(page)
        assert all(slot == pytest.approx(160) for slot in _layout(page)["slots"])


def test_component_shell_native_keys_home_end_and_non_drag_buttons(browser):
    with _shell(browser) as (page, _):
        slider = page.get_by_role("slider", name="Cover size", exact=True).first
        slider.focus()
        for key, value in [("Home", 50), ("ArrowRight", 60), ("ArrowUp", 70), ("ArrowLeft", 60),
                           ("ArrowDown", 50), ("End", 100), ("ArrowRight", 100)]:
            page.keyboard.press(key)
            _state(page, value)
            expect(slider).to_be_focused()
        expect(_action(page, "larger")).to_be_disabled()
        for percentage in [90, 80, 70, 60, 50]:
            _action(page, "smaller").click()
            _state(page, percentage)
        expect(_action(page, "smaller")).to_be_disabled()
        for percentage in [60, 70, 80, 90, 100]:
            _action(page, "larger", instance=1).click()
            _state(page, percentage)
        _action(page, "smaller").focus()
        page.keyboard.press("Space")
        _state(page, 90)
        page.keyboard.press("Tab")
        expect(_action(page, "larger")).to_be_focused()
        page.keyboard.press("Tab")
        expect(_action(page, "reset")).to_be_focused()
        page.keyboard.press("Enter")
        _state(page, 100)


def test_component_shell_range_updates_during_pointer_drag_before_change(browser):
    with _shell(browser) as (page, _):
        slider = page.locator(INPUT).first
        page.evaluate("""() => {
            window.__rangeEvents = [];
            for (const name of ['input', 'change']) document.addEventListener(name, event => {
                if (event.target.matches('[data-cover-size-input]')) window.__rangeEvents.push(name);
            });
        }""")
        box = slider.bounding_box()
        y = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + box["width"] - 8, y)
        page.mouse.down()
        try:
            page.mouse.move(box["x"] + 8 + (box["width"] - 16) * 0.4, y, steps=3)
            _state(page, 70)
            assert "input" in page.evaluate("() => window.__rangeEvents")
            assert "change" not in page.evaluate("() => window.__rangeEvents")
        finally:
            page.mouse.up()
        assert "change" in page.evaluate("() => window.__rangeEvents")


def test_component_shell_real_htmx_and_oob_replacements_inherit_state_without_retained_handlers(browser):
    with _shell(browser) as (page, requests):
        _action(page, "smaller").click()
        percentage = 90
        old_ids = set()
        for serial in range(1, 5):
            old = page.locator(INPUT).first.element_handle()
            old_ids.update(page.locator(INPUT).evaluate_all("inputs => inputs.map(input => input.id)"))
            with page.expect_response(lambda response: urlsplit(response.url).path == "/component/swap"):
                page.locator("[data-shell-swap]").click()
            for host in ["#cover-controls-primary", "#cover-controls-oob"]:
                expect(page.locator(host)).to_have_attribute("data-swap-serial", str(serial))
            expect(page.locator("[data-shell-swap]")).to_be_enabled()
            _state(page, percentage)
            fresh_ids = page.locator(INPUT).evaluate_all("inputs => inputs.map(input => input.id)")
            assert not old_ids.intersection(fresh_ids)
            assert len(set(fresh_ids)) == 2
            assert old.evaluate("node => node.isConnected") is False
            old.evaluate("""node => {node.value = '50'; node.dispatchEvent(new Event('input', {bubbles:true}));}""")
            _state(page, percentage)
            swapped_width = page.locator("#swapped-card-host .media-card").evaluate("node => node.getBoundingClientRect().width")
            parent_width = page.locator("#swapped-card-host").evaluate("node => node.getBoundingClientRect().width")
            assert swapped_width == pytest.approx(max(80, parent_width * percentage / 100), abs=1)
            _action(page, "smaller", instance=1).click()
            percentage -= 10
            _state(page, percentage)
            old.dispose()
        assert requests.count(("GET", "/component/swap")) == 4
        listeners = page.evaluate("() => window.__coverListeners")
        assert all(listener["document"] for listener in listeners)
        assert len(listeners) == 6, listeners  # includes the one-shot DOMContentLoaded handler


def test_component_shell_reexecuted_script_does_not_reset_state_or_duplicate_listeners(browser):
    with _shell(browser) as (page, _):
        _action(page, "smaller").click()
        before = page.evaluate("() => window.__coverListeners")
        page.evaluate("""() => new Promise((resolve, reject) => {
            const script = document.createElement('script'); script.src = '/static/js/cover-size.js';
            script.onload = resolve; script.onerror = reject; document.body.append(script);
        })""")
        _state(page, 90)
        assert page.evaluate("() => window.__coverListeners") == before
        _action(page, "smaller").click()
        _state(page, 80)


def test_component_shell_size_changes_have_no_network_storage_cookie_url_or_form_writes_and_reload_resets(browser):
    with _shell(browser) as (page, requests):
        # Fully load fixture artwork first, so native lazy loading cannot be
        # mistaken for a component request after its geometry changes.
        page.locator("img").evaluate_all("""images => Promise.all(images.map(image => {
            image.loading = 'eager'; return image.decode();
        }))""")
        before = list(requests)
        url = page.url
        layout = _layout(page)
        page.evaluate("""() => {
            window.__coverIO = [];
            const forbidden = name => function() { window.__coverIO.push(name); throw Error(name); };
            for (const name of ['localStorage', 'sessionStorage', 'indexedDB']) {
                Object.defineProperty(window, name, {configurable:true, get:forbidden(name)});
            }
            Object.defineProperty(document, 'cookie', {configurable:true,
                get:forbidden('cookie read'), set:forbidden('cookie write')});
            window.fetch = forbidden('fetch');
            window.XMLHttpRequest = forbidden('xhr');
            navigator.sendBeacon = forbidden('beacon');
            history.pushState = forbidden('pushState'); history.replaceState = forbidden('replaceState');
            document.querySelector('[data-shell-filters]').addEventListener('submit', forbidden('form submit'));
        }""")
        for _ in range(5):
            _action(page, "smaller").click()
        _action(page, "larger", instance=1).click()
        _action(page, "reset").click()
        page.locator(INPUT).first.focus()
        page.keyboard.press("Home")
        _state(page, 50)
        assert _layout(page) == layout
        assert page.evaluate("() => window.__coverIO") == []
        assert requests == before and page.url == url
        assert page.context.cookies() == []
        page.reload(wait_until="load")
        _state(page, 100)
        assert page.url == url and _layout(page) == layout


def test_component_shell_unowned_and_prevented_inputs_and_unknown_actions_do_not_change_state(browser):
    with _shell(browser) as (page, _):
        page.evaluate("""() => {
            const outside = document.createElement('input');
            outside.type = 'range'; outside.dataset.coverSizeInput = ''; outside.value = '50';
            document.body.append(outside); outside.dispatchEvent(new Event('input', {bubbles:true})); outside.remove();
            const root = document.querySelector('[data-cover-size-control]');
            const unknown = document.createElement('button');
            unknown.type = 'button'; unknown.dataset.coverSizeAction = 'save'; root.append(unknown); unknown.click();
            unknown.remove();
            const input = root.querySelector('input'); input.value = '50';
            const event = new Event('input', {bubbles:true, cancelable:true}); event.preventDefault();
            input.dispatchEvent(event);
            document.dispatchEvent(new CustomEvent('htmx:afterSwap'));
        }""")
        _state(page, 100)


def test_component_shell_no_javascript_keeps_default_artwork_and_honest_disabled_native_fallback(browser):
    with _shell(browser, width=375, javascript=False) as (page, _):
        expect(page.get_by_role("slider", name="Cover size", exact=True)).to_have_count(2)
        for control in page.locator(CONTROL).all():
            expect(control.locator(INPUT)).to_be_disabled()
            expect(control.locator(INPUT)).to_have_value("100")
            expect(control.get_by_text("Enable JavaScript to change cover size.", exact=True)).to_be_visible()
            for button in control.locator("button").all():
                expect(button).to_be_disabled()
        assert page.evaluate("() => document.documentElement.style.getPropertyValue('--media-cover-scale')") == ""
        assert all(slot == pytest.approx(160) for slot in _layout(page)["slots"])
        _assert_artwork(_metrics(page))
        link = page.locator(".media-card__detail").first
        link.focus()
        expect(link).to_be_focused()
        expect(link).to_have_attribute("href", "/item/1")
        page.keyboard.press("Tab")
        expect(page.locator(".media-card__edit").first).to_be_focused()


def test_component_shell_touch_buttons_reduced_motion_and_text_zoom_reflow(browser):
    with _shell(browser, width=320, height=640, touch=True) as (page, _):
        page.evaluate("() => document.documentElement.style.fontSize = '32px'")
        before = _metrics(page)
        for _ in range(5):
            _action(page, "smaller").tap()
        _state(page, 50)
        after = _metrics(page)
        _assert_page_fits(page)
        assert all(original["font"] == smaller["font"] for original, smaller in zip(before, after, strict=True))
        assert page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert page.locator(CONTROL).first.evaluate("node => getComputedStyle(node).transitionDuration") == "0s"
        assert all(card["width"] >= 80 for card in after)
        _assert_artwork(after)
        _action(page, "reset").tap()
        _state(page, 100)
