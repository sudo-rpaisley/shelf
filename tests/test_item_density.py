"""Opt-in whole-item mode, legacy component and data/endpoint boundaries."""

from pathlib import Path

import pytest

from app import config
from app.services.media_cards import build_media_card
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_cover_size_integration import _marked
from tests.test_media_cards import _environment


ROOT = Path(__file__).resolve().parents[1]
MODE = "density-v1"
HELP = "Smaller items fit more per row when space allows. Text stays readable; results per page are unchanged."
ABSENT = object()


@pytest.mark.parametrize("mode", (ABSENT, None, "", "density-v1 ", "future", MODE),
                         ids=("undefined", "none", "empty", "nonexact", "future", "exact"))
def test_only_the_exact_item_mode_changes_the_native_control_name(mode):
    env, _ = _environment()
    context = {} if mode is ABSENT else {"item_size_mode": mode}
    markup = _Markup(env.get_template("fragments/cover_size_control.html").render(context))
    label = markup.one("label")
    assert label.text.strip() == ("Item size" if mode == MODE else "Cover size")
    slider = markup.one("input")
    assert {key: slider.attrs[key] for key in ("min", "max", "step", "value")} == {
        "min": "50", "max": "100", "step": "10", "value": "100",
    }
    assert "disabled" in slider.attrs and "name" not in slider.attrs
    assert "This page only; not saved." in _marked(markup, "data-cover-size-control").text


@pytest.mark.parametrize("mode", (ABSENT, None, "", "density-v1 ", "future", MODE),
                         ids=("undefined", "none", "empty", "nonexact", "future", "exact"))
def test_library_fragment_requires_explicit_exact_mode_and_keeps_legacy_content(viewer_client, db, mode):
    item = _insert_item(db, title="Density marker boundary", isbn=None)
    db.commit()
    response = viewer_client.get("/api/library/search", params={"view": "items"})
    assert response.status_code == 200
    context = dict(response.context)
    if mode is not ABSENT:
        context["item_size_mode"] = mode
    env, _ = _environment()
    env.globals.update(build_media_card=build_media_card, creator_label=config.creator_label)
    markup = _Markup(env.get_template("fragments/library_results.html").render(context))
    member = markup.one(**{"data-library-member": str(item)})
    article = next(node for node in member.ancestors if node.tag == "article")
    grid = article.ancestors[-1]
    assert grid.attrs.get("data-item-grid") == ("library" if mode == MODE else None)
    assert "No recorded holdings" in member.text
    assert markup.one("h3", within=member).text.strip() == "Density marker boundary"


@pytest.mark.parametrize("path", ("/", "/library?reset=1&view=items", "/library?reset=1&view=groups", "/music"))
def test_real_producers_opt_in_once_without_exposing_a_saved_preference(viewer_client, path):
    response = viewer_client.get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    roots = [node for node in markup.elements if node.attrs.get("data-item-size") == MODE]
    assert len(roots) == 1
    panel = _marked(markup, "data-cover-size-panel")
    assert panel.attrs["aria-label"] == "Item size" and roots[0] in panel.ancestors
    assert HELP in markup.one("p", id=panel.attrs["aria-describedby"]).text
    assert not any(node.attrs.get("name") in {"item_size", "cover_size"} for node in markup.elements)


@pytest.mark.parametrize("path", ("/browse", "/library?reset=1&view=series", "/api/library/search?item_size_mode=density-v1"))
def test_query_parameters_cannot_opt_other_surfaces_into_density(viewer_client, path):
    response = viewer_client.get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not any({"data-item-size", "data-item-grid", "data-item-shelf"} & node.attrs.keys()
                   for node in markup.elements)


def test_density_style_is_singleton_real_post_legacy_and_not_precached(viewer_client):
    markup = _Markup(viewer_client.get("/").text)
    style = markup.one("link", rel="stylesheet", href="/static/css/item-density.source.css")
    assert style.position > markup.one("link", href="/static/css/cover-size.source.css").position
    response = viewer_client.get(style.attrs["href"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    css = response.text
    assert '[data-item-size="density-v1"]' in css and '[data-item-grid="library"]' in css
    assert "transform:" not in css and "zoom:" not in css and "font-size:" not in css
    source = (ROOT / "static/css/input.css").read_text()
    assert source.count('@import "./item-density.source.css";') == 1
    assert source.index('@import "./item-density.source.css";') > source.index('@import "./cover-size.source.css";')
    assert "/static/css/item-density.source.css" not in (ROOT / "static/sw.js").read_text()


@pytest.mark.parametrize("newstyle", (False, True), ids=("raw-gnu", "newstyle-gnu"))
def test_item_labels_and_density_help_escape_translations_once(newstyle):
    messages = {label: f'<img src=x onerror="bad()"> {label}' for label in (
        "Item size", "Enable JavaScript to change item size.", HELP,
    )}
    env, calls = _environment(messages, newstyle=newstyle)
    markup = _Markup(env.get_template("fragments/cover_size_panel.html").render(
        item_size_mode=MODE, cover_size_description_id="density-description",
    ))
    assert not markup.find("img")
    assert _marked(markup, "data-cover-size-panel").attrs["aria-label"] == messages["Item size"]
    assert messages[HELP] in markup.one("p", id="density-description").text
    for label in messages:
        assert label in calls
