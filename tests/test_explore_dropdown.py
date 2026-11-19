"""Authored Explore disclosure contracts; no rendering/test result is implied."""

import json

import pytest

from app import media_presentation, nav
from tests.test_browse_button_removal import _navigation
from tests.test_connected_layout import _Markup
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _render, _setting


@pytest.mark.parametrize("user", (ADMIN, EDITOR, VIEWER, None))
@pytest.mark.parametrize("state", ("default", "configured", "hidden", "features-off"))
def test_explore_is_initially_closed_with_the_exact_authorized_vertical_menu_contents(db, user, state):
    if state == "configured":
        _setting(db, "hardcover_token", "synthetic-explore-fixture-only")
    elif state == "hidden":
        _setting(db, "nav_hidden_tabs", json.dumps(["series", "discover", "stats"]))
    elif state == "features-off":
        for key in ("series", "hardcover", "stats"):
            _feature(db, key, False)
    markup, model = _navigation(user)
    groups = [group for group in model["groups"] if group["key"] == "explore"]
    rendered = markup.find("details", **{"data-nav-group": "explore"})
    assert len(rendered) == len(groups)
    if not groups:
        return
    (explore,) = rendered
    assert "open" not in explore.attrs and "data-nav-disclosure" in explore.attrs
    wrapper = explore.ancestors[-1]
    assert wrapper.attrs["class"] == "media-navigation__top" and "data-nav-hover" in wrapper.attrs
    summary = markup.one("summary", within=explore)
    assert summary.text.strip() == "Explore" and "role" not in summary.attrs
    panel = markup.one("ul", within=explore, **{"class": "media-navigation__panel"})
    links = markup.find("a", within=explore)
    expected = groups[0]["tabs"]
    assert [link.attrs["data-nav-tab"] for link in links] == [tab["key"] for tab in expected]
    assert [link.attrs["href"] for link in links] == [tab["path"] for tab in expected]
    assert summary.position < panel.position < links[0].position
    assert all(panel in link.ancestors for link in links)
    assert not markup.find("button", within=explore) and not markup.find("input", within=explore)
    assert not any("media-navigation__quick" in node.attrs.get("class", "").split() for node in markup.find())
    if state in ("hidden", "features-off"):
        assert not {"series", "discover", "stats"} & {link.attrs["data-nav-tab"] for link in links}
    store = markup.find("a", **{"data-nav-tab": "store"})
    assert all(explore not in link.ancestors and link.attrs["href"] == "/store" for link in store)


def test_an_absent_explore_payload_does_not_add_an_empty_menu_or_new_destinations(db):
    tabs = [tab for tab in nav.visible_tabs(VIEWER) if tab["key"] in {"browse", "store"}]
    model = nav.media_navigation(tabs, media_presentation.family_nodes(), intent_for=media_presentation.filter_intent)
    html, _ = _render(VIEWER, media_presentation.family_nodes(), media_presentation.filter_intent,
                      model=model, nav_tabs=tabs)
    markup = _Markup(html)
    assert not markup.find("details", **{"data-nav-group": "explore"})
    assert len(markup.find("a", **{"data-nav-tab": "store"})) == 1
    assert {node.attrs["data-nav-tab"] for node in markup.find("a") if "data-nav-tab" in node.attrs} == {tab["key"] for tab in tabs}


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
def test_real_shell_explore_is_a_native_closed_dropdown_with_series_and_stats(fixture, request):
    response = request.getfixturevalue(fixture).get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    explore = markup.one("details", **{"data-nav-group": "explore"})
    assert "open" not in explore.attrs
    assert markup.one("a", within=explore, **{"data-nav-tab": "series"}).attrs["href"] == "/series"
    assert markup.one("a", within=explore, **{"data-nav-tab": "stats"}).attrs["href"] == "/stats"
