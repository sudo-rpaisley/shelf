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


# Phone follow-up: append-only controls; the 24 preceding cases are frozen390.
@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER], ids=["admin", "editor", "viewer"])
def test_phone_library_is_the_same_authorized_nested_anchor_with_an_external_description(db, user):
    model, dom = _fragment(user)
    assert model["all_media"]["key"] == "library"
    assert model["all_media"]["label"] == "All media", "presentation must not mutate the model"
    library, = dom.find("a", **{"data-nav-tab": "library"})
    assert library.text.strip() == "Library"
    assert library.attrs["href"] == model["all_media"]["path"] == "/library?reset=1&view=items&sort=recent"
    assert library.attrs["data-nav-menu-tab"] == "library"
    assert any("data-nav-browse" in parent.attrs for parent in _ancestors(library))
    assert not library.find("a") and not library.find("button")
    description, = library.parent.find("span", **{"class": "media-navigation__description"})
    assert description.text.strip() == "All media"
    assert description not in library.find()
    assert not description.find("a") and not description.find("button")
    assert [link.text.strip() for link in dom.find("a")].count("Library") == 1
    assert not any(link.text.strip() == "All media" for link in dom.find("a"))


@pytest.mark.parametrize("reason", ["hidden", "disabled", "anonymous", "empty"])
def test_phone_library_wording_cannot_reintroduce_an_unauthorized_library(db, reason):
    import json

    if reason == "hidden":
        _setting(db, "nav_hidden_tabs", json.dumps(["library"]))
    if reason == "disabled":
        _feature(db, "connected_library", False)
    _, dom = _fragment(None if reason in {"anonymous", "empty"} else ADMIN,
                       tabs=[] if reason == "empty" else None)
    assert not dom.find("a", **{"data-nav-tab": "library"})
    assert not dom.find("span", **{"class": "media-navigation__description"})
    assert not any(link.text.strip() in {"Library", "All media"} for link in dom.find("a"))


def test_phone_server_fallback_preserves_the_real_first_summary_and_browse_shell_adjacency(db):
    _, dom = _fragment()
    primary, = dom.find("a", **{"data-nav-primary": "browse"})
    shell, = dom.find("details", **{"data-nav-shell": None})
    assert primary.parent.children[primary.parent.children.index(primary) + 1] is shell
    assert shell.children[0].tag == "summary"
    assert shell.children[0].attrs["data-testid"] == "nav-menu-button"
    assert "role" not in shell.children[0].attrs
    assert not shell.children[0].find("button")
    assert not shell.find("button"), "replacement must only be committed after actual browser verification"
    assert "data-nav-shell-button" not in shell.attrs
    tree, = shell.find("div", **{"data-testid": "nav-menu-panel"})
    assert tree.parent is shell


def test_phone_library_render_remains_pure_for_explicit_authorized_and_empty_payloads(db, monkeypatch):
    from copy import deepcopy
    import app.main
    from app import database

    tabs = nav.visible_tabs(ADMIN)
    model = nav.media_navigation(tabs, family_nodes())
    before = deepcopy(model)

    def forbidden(*args, **kwargs):
        raise AssertionError("phone presentation must not query or change authorization")

    monkeypatch.setattr(database, "get_db", forbidden)
    monkeypatch.setattr(nav, "visible_tabs", forbidden)
    monkeypatch.setattr(nav, "_nav_settings", forbidden)
    monkeypatch.setattr(nav, "feature_enabled", forbidden)
    _, dom = _render(ADMIN, family_nodes(), filter_intent, model=model, nav_tabs=tabs)
    library, = dom.find("a", **{"data-nav-tab": "library"})
    assert library.text.strip() == "Library"
    assert model == before
    empty = nav.media_navigation([], family_nodes())
    _, dom = _render(None, family_nodes(), filter_intent, model=empty, nav_tabs=[])
    assert [link.attrs["href"] for link in dom.find("a")] == ["/"]
    assert not dom.find("a", **{"data-nav-tab": "library"})


def test_phone_old_node_double_legitimately_selects_native_fallback_not_the_real_button_branch(db):
    from tests.test_media_navigation import NODE_DOM_DOUBLE, _run_node_dom_double

    _, dom = _fragment()
    program = NODE_DOM_DOUBLE + r"""
desktop = false;
vm.runInNewContext(fs.readFileSync('static/js/media-navigation.js', 'utf8'), {document, window});
flush();
const root = document.querySelector('[data-media-navigation]');
const shell = root.querySelector('details[data-nav-shell]');
assert.equal(typeof document.createElement, 'undefined', 'honest missing DOM creation capability');
assert.equal(root.getAttribute('data-nav-enhanced'), 'true', 'native enhancement still completes');
assert.equal(shell.getAttribute('data-nav-shell-button'), null);
assert.equal(shell.querySelector('button'), null);
assert.equal(summary(shell).getAttribute('data-testid'), 'nav-menu-button');
activate(shell, 'Enter');
assert.ok(shell.open);
assert.ok(root.querySelector('details[data-nav-browse]').open);
console.log('Genuine native capability fallback; not browser/visual evidence.');
"""
    _run_node_dom_double(program, dom)
