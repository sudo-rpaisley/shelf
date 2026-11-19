"""Authored duplicate-button removal contracts, not executed qualification."""

from copy import deepcopy
import json

import pytest

from app import media_presentation, nav
from tests.test_connected_layout import _Markup
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _render, _setting


def _marked(markup, name, *, within=None):
    nodes = [node for node in markup.find(within=within) if name in node.attrs]
    assert len(nodes) == 1
    return nodes[0]


def _navigation(user):
    tabs = nav.visible_tabs(user)
    before = deepcopy(tabs)
    model = nav.media_navigation(tabs, media_presentation.family_nodes(), intent_for=media_presentation.filter_intent)
    model_before = deepcopy(model)
    html, _ = _render(user, media_presentation.family_nodes(), media_presentation.filter_intent,
                      model=model, nav_tabs=tabs, add_media_entry_url="https://testserver/add")
    assert tabs == before and model == model_before
    markup = _Markup(html)
    keys = [node.attrs["data-nav-tab"] for node in markup.find("a") if "data-nav-tab" in node.attrs]
    expected = {tab["key"] for tab in tabs}
    if model["primary"] and model["all_media"]:
        expected.remove("browse")  # Only the redundant Browse destination is omitted.
    assert len(keys) == len(set(keys)) and set(keys) == expected
    assert [node.attrs["data-nav-menu-tab"] for node in markup.find("a") if "data-nav-menu-tab" in node.attrs] == keys
    assert not markup.find("details", **{"data-nav-group": "manage"})
    assert not any("data-nav-management" in node.attrs for node in markup.find())
    assert not markup.find("a", href="/browse")
    return markup, model


@pytest.mark.parametrize("user", (ADMIN, EDITOR, VIEWER))
def test_library_is_the_one_native_primary_menu_link_without_a_duplicate_browse_button(db, user):
    markup, model = _navigation(user)
    primary = markup.one("a", **{"data-nav-primary": "browse"})
    assert primary is markup.one("a", **{"data-nav-tab": "library"})
    assert primary.attrs["href"] == model["primary"]["path"] == "/library?reset=1&view=items&sort=recent"
    assert primary.text.strip() == "Library"
    shell = _marked(markup, "data-nav-shell")
    media = _marked(markup, "data-nav-browse")
    assert shell in primary.ancestors and media in primary.ancestors
    assert "media-navigation__primary" not in primary.attrs["class"].split()
    assert not any("media-navigation__primary" in node.attrs.get("class", "").split() for node in markup.find("a"))
    assert not any(node.text.strip() == "Browse" for node in markup.find("a"))
    main = markup.one("div", **{"class": "media-navigation__main"})
    assert main.attrs["data-nav-layout"] == "library-primary-v1"
    assert model["management"]["path"] == "/browse", "Removing the shortcut does not change the backing destination"
    assert not markup.find("a", **{"data-nav-tab": "browse"})
    assert not any(key.startswith(("on", "@", "x-", "hx-")) for key in primary.attrs)
    assert bool(markup.find(**{"data-testid": "add-media-entry"})) == (user["role"] in ("admin", "editor"))


@pytest.mark.parametrize("cause", ("feature", "manual"))
@pytest.mark.parametrize("user", (ADMIN, EDITOR, VIEWER))
def test_unavailable_library_keeps_the_authorized_standalone_core_browse_fallback(db, cause, user):
    if cause == "feature":
        _feature(db, "connected_library", False)
    else:
        _setting(db, "nav_hidden_tabs", json.dumps(["library"]))
    markup, model = _navigation(user)
    assert model["all_media"] is None
    primary = markup.one("a", **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/browse?sort=newest" and primary.text.strip() == "Browse"
    assert "media-navigation__primary" in primary.attrs["class"].split()
    assert _marked(markup, "data-nav-shell") not in primary.ancestors
    assert not markup.find("a", **{"data-nav-tab": "library"})
    assert "data-nav-layout" not in markup.one("div", **{"class": "media-navigation__main"}).attrs
    assert primary is markup.one("a", **{"data-nav-tab": "browse"})


def test_anonymous_navigation_keeps_the_same_core_fallback_without_gaining_library(db):
    markup, _ = _navigation(None)
    assert markup.one("a", **{"data-nav-primary": "browse"}).attrs["href"] == "/browse?sort=newest"
    assert not markup.find("a", **{"data-nav-tab": "library"})
    assert not markup.find(**{"data-testid": "add-media-entry"})


@pytest.mark.parametrize("legacy_management", (False, True))
def test_explicit_empty_navigation_does_not_invent_a_library_or_browse_entry(db, legacy_management):
    model = {"primary": None, "all_media": None, "families": [], "groups": [],
             "management": {"path": "/browse", "label": "Catalogue management"} if legacy_management else None}
    before = deepcopy(model)
    html, _ = _render(None, media_presentation.family_nodes(), media_presentation.filter_intent,
                      model=model, nav_tabs=[])
    markup = _Markup(html)
    assert model == before
    assert not markup.find("a", **{"data-nav-primary": "browse"})
    assert [node.attrs["href"] for node in markup.find("a")] == ["/"]
    assert markup.one("p", **{"class": "media-navigation__empty"}).text.strip() == "No navigation available."


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
def test_real_library_shell_marks_the_existing_library_link_current_not_a_duplicate(fixture, request):
    response = request.getfixturevalue(fixture).get("/library")
    assert response.status_code == 200
    markup = _Markup(response.text)
    link = markup.one("a", **{"data-nav-primary": "browse"})
    assert link.attrs["data-nav-tab"] == "library" and link.attrs["aria-current"] == "page"
    assert _marked(markup, "data-nav-browse") in link.ancestors


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
def test_removing_manage_leaves_the_existing_browse_page_available_without_a_duplicate_nav_link(fixture, request):
    response = request.getfixturevalue(fixture).get("/browse")
    assert response.status_code == 200
    markup = _Markup(response.text)
    navigation = _marked(markup, "data-media-navigation")
    assert not markup.find("details", within=navigation, **{"data-nav-group": "manage"})
    assert not any("data-nav-management" in node.attrs for node in markup.find(within=navigation))
    assert not markup.find("a", within=navigation, href="/browse")
    assert not markup.find("a", within=navigation, **{"data-nav-tab": "browse"})
    primary = markup.one("a", within=navigation, **{"data-nav-primary": "browse"})
    assert primary.attrs["data-nav-tab"] == "library"
    assert primary.attrs["href"] == "/library?reset=1&view=items&sort=recent"
