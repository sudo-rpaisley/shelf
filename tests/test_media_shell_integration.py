"""Real shell producers consume the common registry and strict route filters.

Server DOM and source-wiring checks only: these do not execute the browser's
disclosure enhancement, HTMX lifecycle, CSS layout or account preference storage.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import config
from app.media_presentation import expand_selectors, family_nodes
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_features_gate import off
from tests.test_nav import _set


ROOT = Path(__file__).resolve().parents[1]


def _shell(client, path="/browse"):
    response = client.get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    roots = [node for node in markup.find("nav") if "data-media-navigation" in node.attrs]
    assert len(roots) == 1
    return markup, roots[0]


def _members(response):
    assert response.status_code == 200
    markup = _Markup(response.text)
    return {int(node.attrs["data-library-member"]) for node in markup.elements
            if "data-library-member" in node.attrs}


def _mixed(db):
    result = {}
    for index, media_type in enumerate((*config.MEDIA_TYPES, "legacy_shell_format")):
        result[media_type] = _insert_item(
            db, title=f"Shell {media_type}", isbn=None, media_type=media_type,
            created_at=f"2026-01-01 00:{index:02d}:00",
        )
    _insert_item(db, title="Shell trashed", isbn=None, deleted_at="2026-01-02 00:00:00")
    db.commit()
    return result


@pytest.mark.parametrize("client_name", ["admin_client", "editor_client", "viewer_client"])
def test_primary_browse_follows_to_the_entire_live_mixed_catalogue(db, request, client_name):
    expected = _mixed(db)
    client = request.getfixturevalue(client_name)
    markup, root = _shell(client)
    primary = markup.one("a", within=root, **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/library?reset=1&view=items&sort=recent"
    assert "types" not in parse_qs(urlsplit(primary.attrs["href"]).query)
    response = client.get(primary.attrs["href"])
    assert _members(response) == set(expected.values())
    rendered = _Markup(response.text)
    assert rendered.one("select", name="view").attrs["name"] == "view"
    selected = rendered.one("option", within=rendered.one("select", name="view"), value="items")
    assert "selected" in selected.attrs
    assert markup.one("a", within=root, **{"data-nav-management": "catalogue"}).attrs["href"] == "/browse"


def test_primary_browse_does_not_carry_the_current_saved_view(viewer_client, viewer_user, db):
    expected = _mixed(db)
    saved = viewer_client.post("/api/library/views", data={"name": "Only DVD", "media_type": "dvd"},
                               follow_redirects=False)
    assert saved.status_code == 303
    view_id = db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0]
    markup, root = _shell(viewer_client, f"/library?saved_view={view_id}")
    primary = markup.one("a", within=root, **{"data-nav-primary": "browse"})
    assert "saved_view" not in primary.attrs["href"]
    assert _members(viewer_client.get(primary.attrs["href"])) == set(expected.values())


@pytest.mark.parametrize("reason", ["disabled", "hidden"])
def test_mixed_browse_fallback_uses_the_real_core_route(viewer_client, db, reason):
    expected = _mixed(db)
    if reason == "disabled":
        off("connected_library")
    else:
        _set(db, "nav_hidden_tabs", json.dumps(["library"]))
    markup, root = _shell(viewer_client)
    primary = markup.one("a", within=root, **{"data-nav-primary": "browse"})
    assert primary.attrs["href"] == "/browse?sort=newest"
    destination = viewer_client.get(primary.attrs["href"])
    assert destination.status_code == 200
    parsed = _Markup(destination.text)
    assert {int(node.attrs["data-item-id"]) for node in parsed.elements
            if "data-item-id" in node.attrs} == set(expected.values())
    assert not any(link.attrs.get("href", "").startswith("/library") for link in markup.find("a", within=root))


@pytest.mark.parametrize("family_key", ["books", "comics", "music", "films", "tv", "games", "periodicals"])
def test_rendered_family_links_reach_their_real_filtered_items(viewer_client, db, family_key):
    items = _mixed(db)
    family = next(node for node in family_nodes() if node.key == family_key)
    markup, root = _shell(viewer_client)
    link = markup.one("a", within=root, **{"data-nav-family-link": family_key})
    expected_types = expand_selectors((family.selector,))
    query = parse_qs(urlsplit(link.attrs["href"]).query)
    assert query == {"reset": ["1"], "view": ["items"], "sort": ["recent"],
                     "types": [",".join(expected_types)]}
    assert _members(viewer_client.get(link.attrs["href"])) == {items[key] for key in expected_types}


def test_every_rendered_format_link_is_consumed_without_broadening(viewer_client, db):
    items = _mixed(db)
    markup, root = _shell(viewer_client)
    links = [link for link in markup.find("a", within=root) if "data-nav-format" in link.attrs]
    assert {link.attrs["data-nav-format"] for link in links} == set(config.MEDIA_TYPES)
    assert len(links) == len(config.MEDIA_TYPES)
    for link in links:
        media_type = link.attrs["data-nav-format"]
        assert parse_qs(urlsplit(link.attrs["href"]).query)["types"] == [media_type]
        assert _members(viewer_client.get(link.attrs["href"])) == {items[media_type]}


@pytest.mark.parametrize("origin", ["music", "periodicals"])
@pytest.mark.parametrize("reason", ["disabled", "hidden"])
def test_hidden_origins_remove_the_whole_nested_branch(viewer_client, db, origin, reason):
    if reason == "disabled":
        off(origin)
    else:
        _set(db, "nav_hidden_tabs", json.dumps([origin]))
    markup, root = _shell(viewer_client)
    assert not markup.find("li", within=root, **{"data-nav-family": origin})
    assert not any(link.attrs.get("href", "").startswith("/" + origin)
                   for link in markup.find("a", within=root))
    assert markup.find("a", within=root, **{"data-nav-primary": "browse"})


@pytest.mark.parametrize("client_name,role", [("admin_client", "admin"), ("editor_client", "editor"),
                                               ("viewer_client", "viewer")])
def test_shared_shell_preserves_account_destinations_and_role_gates(db, request, client_name, role):
    markup, root = _shell(request.getfixturevalue(client_name))
    panel = markup.one(**{"data-testid": "account-menu-panel"})
    account = {link.attrs.get("data-nav-tab") for link in markup.find("a", within=panel)}
    assert {"reading-life", "source-progress", "activity", "sources"} <= account
    assert ("source-migrations" in account) is (role == "admin")
    assert ("settings" in account) is (role == "admin")
    assert ("logs" in account) is (role == "admin")
    tree = markup.one(**{"data-testid": "nav-menu-panel"})
    primary_keys = {link.attrs.get("data-nav-tab") for link in markup.find("a", within=tree)}
    assert not primary_keys & {"settings", "logs", "source-migrations", "activity"}
    assert markup.one("a", within=root, **{"data-nav-tab": "music"}) in markup.find("a", within=tree)
    if role == "viewer":
        assert not markup.find("a", within=root, **{"data-nav-tab": "scan"})


@pytest.mark.parametrize("keyword", [False, True])
@pytest.mark.parametrize("explicit_model", [False, True])
def test_wrapper_derives_only_from_the_preserved_explicit_empty_context(monkeypatch, db, keyword, explicit_model):
    import app.main as main

    tabs = []
    context = {"nav_tabs": tabs}
    sentinel = {"primary": None, "families": [], "groups": [], "management": None}
    if explicit_model:
        context["media_navigation"] = sentinel
    monkeypatch.setattr(main, "_original_template_response", lambda *args, **kwargs: None)
    request = SimpleNamespace(state=SimpleNamespace(user={"role": "viewer"}))
    if keyword:
        main._template_response_with_user(request, "base.html", context=context)
    else:
        main._template_response_with_user(request, "base.html", context)
    assert context["nav_tabs"] is tabs
    assert context["media_navigation"]["primary"] is None
    assert context["media_navigation"]["families"] == []
    assert context["media_navigation"]["groups"] == []
    if explicit_model:
        assert context["media_navigation"] is sentinel


def test_real_base_loads_the_enhancement_once_and_css_sources_are_imported(viewer_client):
    markup, _ = _shell(viewer_client)
    scripts = markup.find("script", src="/static/js/media-navigation.js")
    assert len(scripts) == 1
    assert "async" not in scripts[0].attrs
    assert viewer_client.get(scripts[0].attrs["src"]).status_code == 200
    css = (ROOT / "static/css/input.css").read_text()
    for name in ("media-navigation.source.css", "media-cards.source.css"):
        assert css.count(f'@import "./{name}";') == 1
    assert "--app-gutter" in css and "80px" in css
