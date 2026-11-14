"""Presentation repair controls over the unchanged, authorized navigation model."""

from collections import Counter

import pytest

from app import nav
from app.media_presentation import family_nodes, filter_intent
from tests.test_media_navigation import (
    ADMIN, EDITOR, VIEWER, _ancestors, _feature, _render, _setting,
)


def _fragment(user=ADMIN, *, tabs=None):
    if tabs is None:
        tabs = nav.visible_tabs(user)
    model = nav.media_navigation(tabs, family_nodes())
    _, dom = _render(user, family_nodes(), filter_intent, model=model, nav_tabs=tabs)
    return model, dom


def test_store_is_one_quick_link_beside_not_inside_the_add_disclosure(db):
    _, dom = _fragment()
    store, = dom.find("a", **{"data-nav-tab": "store"})
    add, = dom.find("details", **{"data-nav-group": "add"})
    assert store.attrs["href"] == "/store"
    assert add not in _ancestors(store)
    assert store in add.parent.parent.find("a")
    assert {link.attrs["data-nav-tab"] for link in add.find("a")} == {"scan", "shelf-fill"}


def test_explore_is_initially_expanded_native_quick_navigation_with_real_contents(db):
    _setting(db, "hardcover_token", "fixture-only-not-a-provider-account")
    _, dom = _fragment()
    explore, = dom.find("details", **{"data-nav-group": "explore"})
    assert "open" in explore.attrs
    assert explore.children[0].tag == "summary"
    assert explore.children[0].text.strip() == "Explore"
    assert {link.attrs["data-nav-tab"] for link in explore.find("a")} == {"series", "discover", "stats"}
    assert {link.attrs["href"] for link in explore.find("a")} == {"/series", "/discover", "/stats"}


def test_store_only_add_group_does_not_leave_an_empty_disclosure_for_viewers(db):
    tabs = [tab for tab in nav.visible_tabs(VIEWER) if tab["key"] in {"browse", "store"}]
    model, dom = _fragment(VIEWER, tabs=tabs)
    assert [tab["key"] for tab in model["groups"][0]["tabs"]] == ["store"]
    assert not dom.find("details", **{"data-nav-group": "add"})
    assert not dom.find("details", **{"data-nav-group": "explore"})
    store, = dom.find("a", **{"data-nav-tab": "store"})
    assert store.attrs["href"] == "/store"


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None], ids=["admin", "editor", "viewer", "anonymous"])
@pytest.mark.parametrize("state", ["unconfigured", "configured", "manual-hidden", "feature-off", "all-hidden"])
def test_quick_destinations_never_duplicate_or_bypass_existing_visibility(db, user, state):
    import json

    if state != "unconfigured":
        _setting(db, "hardcover_token", "fixture-only-not-a-provider-account")
        _setting(db, "vision_provider", "ollama")
    if state == "manual-hidden":
        _setting(db, "nav_hidden_tabs", json.dumps(["store", "series", "discover", "stats", "music", "periodicals", "library"]))
    elif state == "feature-off":
        for key in ("store", "series", "hardcover", "stats", "music", "periodicals", "connected_library"):
            _feature(db, key, False)
    elif state == "all-hidden":
        _setting(db, "nav_hidden_tabs", json.dumps(sorted(nav.HIDEABLE_KEYS)))

    tabs = nav.visible_tabs(user)
    before = [dict(tab) for tab in tabs]
    model, dom = _fragment(user, tabs=tabs)
    assert tabs == before
    links = [link for link in dom.find("a") if "data-nav-tab" in link.attrs]
    assert Counter(link.attrs["data-nav-tab"] for link in links) == Counter(tab["key"] for tab in tabs)
    assert Counter(link.attrs["data-nav-menu-tab"] for link in links) == Counter(tab["key"] for tab in tabs)
    visible = {tab["key"] for tab in tabs}
    for key in ("store", "series", "discover", "stats"):
        assert bool(dom.find("a", **{"data-nav-tab": key})) == (key in visible)
    add = dom.find("details", **{"data-nav-group": "add"})
    expected_add = [tab for group in model["groups"] if group["key"] == "add"
                    for tab in group["tabs"] if tab["key"] != "store"]
    assert bool(add) == bool(expected_add)
    for details in dom.find("details"):
        assert details.children[0].tag == "summary"
        assert details.children[0].text.strip()
        assert not details.children[0].find("a")
        assert not details.children[0].find("button")
    manager, = dom.find("a", **{"data-nav-management": "catalogue"})
    assert manager.attrs["href"] == "/browse", "Manage is not incidentally admin-only"
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == ("/library?reset=1&view=items&sort=recent" if "library" in visible else "/browse?sort=newest")
    assert bool(dom.find("a", **{"data-nav-tab": "settings"})) == (user == ADMIN)


def test_quicklink_render_uses_only_the_explicit_authorized_payload_without_queries(db, monkeypatch):
    import app.main  # prepare the actual environment before forbidding policy reads
    from app import database

    tabs = nav.visible_tabs(ADMIN)
    model = nav.media_navigation(tabs, family_nodes())

    def forbidden(*args, **kwargs):
        raise AssertionError("presentation must not re-query or re-authorize navigation")

    monkeypatch.setattr(database, "get_db", forbidden)
    monkeypatch.setattr(nav, "visible_tabs", forbidden)
    monkeypatch.setattr(nav, "_nav_settings", forbidden)
    monkeypatch.setattr(nav, "feature_enabled", forbidden)
    _, dom = _render(ADMIN, family_nodes(), filter_intent, model=model, nav_tabs=tabs)
    assert len(dom.find("a", **{"data-nav-tab": "store"})) == 1
    empty = nav.media_navigation([], family_nodes())
    _, dom = _render(None, family_nodes(), filter_intent, model=empty, nav_tabs=[])
    assert not any("data-nav-tab" in node.attrs for node in dom.walk())
    assert [link.attrs["href"] for link in dom.find("a")] == ["/"]
    shell, = dom.find("details", **{"data-nav-shell": None})
    assert "No navigation available." in shell.text
