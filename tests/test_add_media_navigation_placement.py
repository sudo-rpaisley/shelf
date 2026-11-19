"""Authored placement/policy regressions; no rendering/test result is implied."""

from copy import deepcopy
import json

import pytest

from app import media_presentation, nav
from tests.test_connected_layout import _Markup
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _render, _setting


MISSING = object()


def _markup(user, *, url="https://testserver/add"):
    tabs = nav.visible_tabs(user)
    before = deepcopy(tabs)
    extra = {} if url is MISSING else {"add_media_entry_url": url}
    html, _ = _render(user, media_presentation.family_nodes(), media_presentation.filter_intent,
                      nav_tabs=tabs, **extra)
    assert tabs == before
    markup = _Markup(html)
    marked = markup.find("a")
    keys = [node.attrs["data-nav-tab"] for node in marked if "data-nav-tab" in node.attrs]
    assert len(keys) == len(set(keys)) and set(keys) == {tab["key"] for tab in tabs}
    return markup


def _entry(markup, url):
    entry = markup.testid("add-media-entry")
    group = markup.one("details", **{"data-nav-group": "add"})
    assert entry.tag == "a" and entry.attrs["href"] == url
    assert group in entry.ancestors and "data-nav-disclosure" in group.attrs
    assert markup.one("summary", within=group).text.strip() == "Add / Scan"
    assert markup.find("a", within=group)[0] is entry
    assert "data-nav-tab" not in entry.attrs and "data-nav-menu-tab" not in entry.attrs
    assert not any(name.startswith(("on", "@", "hx-", "x-")) for name in entry.attrs)
    brand = markup.one("a", href="/")
    assert brand.ancestors[-1] not in entry.ancestors
    return group


@pytest.mark.parametrize("user", (ADMIN, EDITOR))
@pytest.mark.parametrize("url", ("https://testserver/add", "https://testserver/shelf/add"))
def test_authorized_named_route_entry_is_once_inside_add_scan_not_beside_brand(db, user, url):
    markup = _markup(user, url=url)
    group = _entry(markup, url)
    store = markup.one("a", **{"data-nav-tab": "store"})
    assert group not in store.ancestors
    assert markup.one("a", within=group, **{"data-nav-tab": "scan"})


@pytest.mark.parametrize("user", (VIEWER, None))
def test_viewers_and_visitors_do_not_gain_the_entry_even_with_a_supplied_url(db, user):
    markup = _markup(user)
    assert not markup.find(**{"data-testid": "add-media-entry"})
    assert not markup.find("details", **{"data-nav-group": "add"})


@pytest.mark.parametrize("url", (MISSING, None, ""))
def test_bare_fragments_cannot_invent_an_add_destination(db, url):
    markup = _markup(EDITOR, url=url)
    assert not markup.find(**{"data-testid": "add-media-entry"})
    assert markup.one("a", **{"data-nav-tab": "scan"})


@pytest.mark.parametrize("user", (ADMIN, EDITOR))
@pytest.mark.parametrize("hide_store", (False, True))
def test_hidden_add_tabs_leave_one_action_only_dropdown_without_reintroducing_tabs(db, user, hide_store):
    hidden = ["scan", "intake", "shelf-fill"] + (["store"] if hide_store else [])
    _setting(db, "nav_hidden_tabs", json.dumps(hidden))
    markup = _markup(user)
    group = _entry(markup, "https://testserver/add")
    assert len(markup.find("a", within=group)) == 1
    assert not [node for node in markup.find("a", within=group) if "data-nav-tab" in node.attrs]
    assert bool(markup.find("a", **{"data-nav-tab": "store"})) == (not hide_store)
    assert not any(node.text.strip() == "No navigation available." for node in markup.find("p"))


def test_music_feature_is_not_a_new_requirement_for_the_add_entry(db):
    _feature(db, "music", False)
    markup = _markup(EDITOR)
    _entry(markup, "https://testserver/add")
    assert not markup.find("a", **{"data-nav-tab": "music"})


def test_action_only_payload_does_not_claim_navigation_is_empty(db):
    model = {"primary": None, "all_media": None, "families": [], "groups": [], "management": None}
    html, _ = _render(EDITOR, media_presentation.family_nodes(), media_presentation.filter_intent,
                      model=model, nav_tabs=[], add_media_entry_url="https://testserver/add")
    markup = _Markup(html)
    _entry(markup, "https://testserver/add")
    assert not [node for node in markup.find("a") if "data-nav-tab" in node.attrs]
    assert not any(node.text.strip() == "No navigation available." for node in markup.find("p"))


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
@pytest.mark.parametrize("path", ("/", "/browse", "/stats"))
def test_real_page_shell_moves_the_existing_entry_without_changing_role_or_url(fixture, path, request):
    response = request.getfixturevalue(fixture).get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    if fixture == "viewer_client":
        assert not markup.find(**{"data-testid": "add-media-entry"})
    else:
        group = _entry(markup, "https://testserver/add")
        assert markup.one("nav", **{"aria-label": "Primary navigation"}) in group.ancestors
