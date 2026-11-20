"""Additive icon-control regressions; authored, not executed at this checkpoint.

Template/source contracts are not native layout, form submission or AT evidence.
Existing tests and their historical results are neither changed nor transferred.
"""

from pathlib import Path
import re

from markupsafe import Markup
import pytest

from app import media_presentation
from app.services import library_catalog
from tests.test_connected_layout import _Markup, _form_values
from tests.test_media_cards import _environment
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _gnu_catalogue, _render, _setting
from tests.test_navigation_search import _filter_values, _model
from tests.test_recent_shelf import _marked, _seed


ROOT = Path(__file__).resolve().parents[1]


def _sizing(*, density=True, messages=None, newstyle=True, copies=1):
    env, calls = _environment(messages, newstyle=newstyle)
    html = env.from_string(
        '{% for unused in range(copies) %}{% include "fragments/cover_size_control.html" %}{% endfor %}'
    ).render(item_size_mode="density-v1" if density else "", copies=copies)
    return _Markup(html), calls


@pytest.mark.parametrize("density", [False, True])
def test_cover_icons_flank_the_single_labelled_native_range_without_becoming_buttons(density):
    markup, _ = _sizing(density=density)
    control = _marked(markup, "data-cover-size-control")
    slider = _marked(markup, "data-cover-size-input", within=control)
    label = markup.one("label", within=control)
    assert slider.tag == "input" and slider.ancestors[-1] is label
    assert slider.attrs == {"type": "range", "min": "0", "max": "10", "step": "1", "value": "5",
                            "aria-valuetext": "100%", "disabled": None, "data-cover-size-input": None}
    caption = markup.one("span", within=label)
    assert caption.text == ("Item size" if density else "Cover size")
    assert "cover-size-control__text" in caption.attrs["class"].split()
    wrapper = label.ancestors[-1]
    assert wrapper.tag == "div" and wrapper.attrs["class"] == "cover-size-control__range"
    smaller = _marked(markup, "data-cover-size-icon", "smaller", within=wrapper)
    larger = _marked(markup, "data-cover-size-icon", "larger", within=wrapper)
    assert smaller.position < label.position < larger.position
    for icon, title in ((smaller, "Smaller covers"), (larger, "Larger covers")):
        assert icon.tag == "span" and icon.ancestors[-1] is wrapper and label not in icon.ancestors
        assert icon.attrs["aria-hidden"] == "true" and icon.attrs["title"] == title
        assert not {"role", "tabindex", "name", "form"} & icon.attrs.keys()
        svg = markup.one("svg", within=icon)
        assert svg.attrs["aria-hidden"] == "true" and svg.attrs["focusable"] == "false"
        assert not icon.text.strip() and not markup.find("button", within=icon)
    small_rect, large_rect = markup.one("rect", within=smaller), markup.one("rect", within=larger)
    assert float(small_rect.attrs["width"]) < float(large_rect.attrs["width"])
    assert float(small_rect.attrs["height"]) < float(large_rect.attrs["height"])
    output = _marked(markup, "data-cover-size-output", within=control)
    assert output.tag == "output" and output.text == "100%" and output.attrs["aria-live"] == "off"
    assert "cover-size-control__text" in output.attrs["class"].split()
    for node in (label, caption, slider, output):
        assert "hidden" not in node.attrs and node.attrs.get("aria-hidden") != "true"
    status = _marked(markup, "data-cover-size-status", within=control)
    retry = _marked(markup, "data-cover-size-retry", within=control)
    assert status.attrs["role"] == "status" and "hidden" in status.attrs
    assert retry.tag == "button" and retry.attrs["type"] == "button" and retry.text == "Retry saving"
    assert "hidden" in retry.attrs and "disabled" in retry.attrs
    assert markup.find("button", within=control) == [retry]
    assert all("cover-size-control__text" not in node.attrs.get("class", "").split() for node in (status, retry))
    assert "Enable JavaScript to change " + ("item" if density else "cover") + " size." in markup.one("noscript").text
    assert not any(markup.find(tag) for tag in ("form", "a", "script", "style"))
    for node in markup.elements:
        assert not any(name.startswith(("on", "@", "x-", "hx-")) for name in node.attrs)


def test_repeated_icon_partials_keep_implicit_labels_without_new_ids_or_preference_controls():
    markup, _ = _sizing(copies=3)
    assert len(markup.find("label")) == len(markup.find("input", type="range")) == 3
    assert len({id(node.ancestors[-1]) for node in markup.find("input")}) == 3
    assert len([node for node in markup.elements if "data-cover-size-icon" in node.attrs]) == 6
    assert not any({"id", "name", "form"} & node.attrs.keys() for node in markup.elements)


@pytest.mark.parametrize("density", [False, True])
@pytest.mark.parametrize("newstyle", [False, True])
def test_icon_tooltips_and_clipped_label_escape_real_gnu_translations(density, newstyle):
    caption = "Item size" if density else "Cover size"
    labels = (caption, "Smaller covers", "Larger covers", "Retry saving")
    percent = "100%%" if newstyle else "100%d"
    messages = {label: f'" data-probe="yes"><icon-probe>{label}</icon-probe> & {percent}' for label in labels}
    markup, calls = _sizing(density=density, messages=messages, newstyle=newstyle)
    def expected(label):
        return f'" data-probe="yes"><icon-probe>{label}</icon-probe> & {"100%" if newstyle else "100%d"}'
    assert markup.one("label").text.strip() == expected(caption)
    for value, title in (("smaller", "Smaller covers"), ("larger", "Larger covers")):
        assert _marked(markup, "data-cover-size-icon", value).attrs["title"] == expected(title)
    assert _marked(markup, "data-cover-size-retry").text == expected("Retry saving")
    assert all(label in calls for label in labels)
    assert not markup.find("icon-probe") and not any("data-probe" in node.attrs for node in markup.elements)


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER])
@pytest.mark.parametrize("state", ["visible", "hidden", "disabled"])
def test_standalone_magnifier_keeps_the_authorized_native_get_form_and_single_query(db, user, state):
    _feature(db, "connected_library", state != "disabled")
    _setting(db, "nav_hidden_tabs", '["library"]' if state == "hidden" else '[]')
    html, _ = _render(user, media_presentation.family_nodes(), media_presentation.filter_intent)
    markup = _Markup(html)
    search = _marked(markup, "data-navigation-search")
    model = _model(user)
    assert search.tag == "form" and search.attrs["method"] == "get" and search.attrs["action"] == model["search"]["path"]
    field = markup.one("span", within=search, **{"class": "media-navigation__search-field"})
    query = markup.one("input", within=field, type="search")
    button = markup.one("button", within=field)
    assert query.attrs["name"] == "q" and query.attrs["id"] == "library-q"
    assert int(query.attrs["maxlength"]) == model["search"]["max_length"]
    assert "form" not in query.attrs and "form" not in button.attrs
    _assert_magnifier(markup, button)
    assert markup.one("label", within=search).attrs["for"] == query.attrs["id"]
    assert len(markup.find("input", name="q")) == 1 and query.position < button.position
    assert not any(node.tag in {"details", "form"} for node in search.ancestors)
    assert _form_values(markup, search) == {**dict(model["search"]["fields"]), "q": ""}


def _assert_magnifier(markup, button):
    assert button.attrs["type"] == "submit" and button.attrs["aria-label"] == button.attrs["title"] == "Search"
    assert not button.text.strip() and "name" not in button.attrs
    svg = markup.one("svg", within=button)
    assert svg.attrs["aria-hidden"] == "true" and svg.attrs["focusable"] == "false"
    assert markup.one("circle", within=svg) and markup.one("path", within=svg)
    assert not any(name.startswith(("on", "@", "x-", "hx-")) for name in button.attrs)


@pytest.mark.parametrize("query", ['Icon 星 & "query"', ""])
def test_library_magnifier_and_input_keep_the_external_filter_owner_and_saved_view(viewer_client, viewer_user, db, query):
    saved = {"q": "Saved search", "types": "book,ebook", "family": "book", "view": "items", "sort": "recent", "page_size": "48"}
    created = viewer_client.post("/api/library/views", data={"name": "Icon form ownership", **saved}, follow_redirects=False)
    assert created.status_code == 303
    view_id = db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0]
    before = tuple(db.iterdump())
    response = viewer_client.get("/library", params={"saved_view": view_id, "q": query, "page": 2})
    assert response.status_code == 200
    markup = _Markup(response.text)
    search = _marked(markup, "data-navigation-search")
    form = markup.testid("library-filters")
    field = markup.one("span", within=search, **{"class": "media-navigation__search-field"})
    input_node = markup.one("input", within=field, name="q")
    button = markup.one("button", within=field)
    _assert_magnifier(markup, button)
    assert search.tag == "div" and input_node.attrs["value"] == query
    assert input_node.attrs["form"] == button.attrs["form"] == form.attrs["id"] == "library-filters"
    assert form not in input_node.ancestors and form not in button.ancestors
    assert not markup.find("input", within=form, name="q") and len(markup.find("input", name="q")) == 1
    expected = {**library_catalog.normalize_filters({**saved, "q": query}), "saved_view": str(view_id)}
    assert _filter_values(markup, form) == expected and "page" not in expected
    submitted = viewer_client.get("/library", params=expected)
    assert submitted.status_code == 200
    assert tuple(db.iterdump()) == before


def test_magnifier_accessible_name_and_title_escape_gnu_text_without_translating_the_query(db, monkeypatch):
    import app.i18n as i18n

    hostile = '\" autofocus data-probe="yes"><search-probe>Search</search-probe> & "quoted"'
    raw = Markup('\" autofocus data-probe="yes"><query-probe>Raw 100%d 星</query-probe>')
    catalog = _gnu_catalogue({"Search": hostile})
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: catalog)
    html, _ = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent,
                      navigation_search_form="library-filters", filters={"q": raw})
    markup = _Markup(html)
    search = _marked(markup, "data-navigation-search")
    button = markup.one("button", within=search)
    assert button.attrs["aria-label"] == button.attrs["title"] == hostile and not button.text.strip()
    assert markup.one("input", within=search, name="q").attrs["value"] == str(raw) and raw not in catalog.calls
    assert not markup.find("search-probe") and not markup.find("query-probe")
    assert not any({"autofocus", "data-probe"} & node.attrs.keys() for node in markup.elements)


@pytest.mark.parametrize("count", [0, 1, 30])
def test_removed_shelf_hint_keeps_recent_selection_continue_activity_and_nojs_guidance(viewer_client, db, count):
    _feature(db, "connected_library", True)
    expected, trashed = _seed(db, count)
    if count:
        saved = viewer_client.post(f"/api/library/items/{expected[0]}/personal", data={"status": "in_progress", "progress": "17"}, follow_redirects=False)
        assert saved.status_code == 303
    before = tuple(db.iterdump())
    response = viewer_client.get("/")
    assert response.status_code == 200 and "Swipe or scroll to explore." not in response.text
    markup = _Markup(response.text)
    recent = _marked(markup, "data-recent-shelf", "streaming-v1")
    continuing = markup.testid("home-personal-library")
    assert markup.one("h2", within=recent).text == "Recently added"
    assert markup.one("h2", within=continuing).text == "Continue"
    assert markup.one("a", within=continuing, href="/account/source-progress#native-continue-settings").text == "Connect or refresh digital Continue"
    if count:
        live = markup.one("ul", id="home-recent-row")
        assert [int(node.attrs["data-item-id"]) for node in markup.find("article", within=live)] == expected[:8]
        buffer = _marked(markup, "data-recent-overflow", within=recent)
        assert [int(node.attrs["data-item-id"]) for node in markup.find("article", within=buffer)] == expected[8:24]
        assert "Select Recently added to open the complete collection without JavaScript." in markup.one("noscript", within=recent).text
        continue_row = markup.one("ul", id="home-continue-row")
        assert [int(node.attrs["data-item-id"]) for node in markup.find("article", within=continue_row)] == expected[:1]
        for shell, row_id in ((recent, "home-recent-row"), (continuing, "home-continue-row")):
            buttons = [node for node in markup.find("button", within=shell) if "data-recent-action" in node.attrs]
            assert [node.attrs["data-recent-action"] for node in buttons] == ["previous", "next"]
            assert all(node.attrs["aria-controls"] == row_id for node in buttons)
        assert str(trashed) not in [node.attrs["data-item-id"] for node in markup.find("article")]
    else:
        assert not markup.find("ul", id="home-recent-row") and not markup.find("ul", id="home-continue-row")
        assert "Your plans are separate from the shared wishlist." in continuing.text
    assert tuple(db.iterdump()) == before


def test_icon_css_clips_only_owned_text_and_reserves_logical_search_space():
    size_css = (ROOT / "static/css/cover-size.source.css").read_text()
    clipped = re.search(r"\.cover-size-control__text\s*\{([^}]+)\}", size_css)[1]
    for declaration in ("position: absolute", "inline-size: 1px", "block-size: 1px", "min-inline-size: 0", "clip-path: inset(50%)"):
        assert declaration in clipped
    assert "display: none" not in clipped and "visibility: hidden" not in clipped
    assert "grid-template-columns: auto minmax(80px, 10rem) auto" in size_css
    nav_css = (ROOT / "static/css/media-navigation.source.css").read_text()
    field = re.search(r"\.media-navigation__search-field\s*\{([^}]+)\}", nav_css)[1]
    input_rule = re.search(r"\.media-navigation__search-input\s*\{([^}]+)\}", nav_css)[1]
    button_rule = re.search(r"\.media-navigation__search-submit\s*\{([^}]+)\}", nav_css)[1]
    assert "position: relative" in field and "max(44px, 2.75rem)" in field
    assert "padding-inline: calc(var(--nav-search-target) + 0.5rem) 0.75rem" in input_rule
    assert "inset-inline-start: 1px" in button_rule and "position: absolute" in button_rule
    assert "min-block-size: var(--nav-search-target)" in button_rule
    assert "::-webkit-search-cancel-button" not in nav_css and "appearance:" not in input_rule
