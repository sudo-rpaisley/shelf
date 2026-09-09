"""Regression coverage for grouped Shelf navigation."""

from app.nav import NAV_TABS, visible_tabs


ADMIN = {"id": 1, "username": "admin", "role": "admin"}
VIEWER = {"id": 2, "username": "viewer", "role": "viewer"}


def test_registry_groups_current_destinations_without_changing_account_menu():
    groups = {tab["key"]: tab.get("group", "primary") for tab in NAV_TABS}
    assert [k for k, v in groups.items() if v == "primary"] == ["browse", "my-list", "series", "discover"]
    assert [k for k, v in groups.items() if v == "add"] == ["scan", "intake", "shelf-fill"]
    assert [k for k, v in groups.items() if v == "more"] == ["store", "music", "periodicals", "stats", "attention"]
    assert [k for k, v in groups.items() if v == "account"] == ["settings", "logs"]
    assert [tab["key"] for tab in NAV_TABS if tab.get("menu") == "account"] == ["settings", "logs"]


def test_visible_tabs_projects_group_for_templates(db):
    by_key = {tab["key"]: tab for tab in visible_tabs(ADMIN)}
    assert by_key["browse"]["group"] == "primary"
    assert by_key["scan"]["group"] == "add"
    assert by_key["stats"]["group"] == "more"
    assert by_key["settings"]["group"] == "account"


def test_rendered_admin_navigation_uses_add_more_and_account_surfaces(admin_client):
    html = admin_client.get("/browse").text
    assert 'data-testid="nav-add-button"' in html
    assert 'data-testid="nav-more-button"' in html
    assert 'data-nav-tab="browse"' in html
    assert 'data-nav-tab="series"' in html
    assert 'data-nav-tab="scan"' in html
    assert 'data-nav-tab="stats"' in html
    assert 'data-testid="account-menu-settings"' in html
    mobile = html.split('data-testid="nav-menu-panel"', 1)[1].split('<!-- Desktop navigation', 1)[0]
    assert 'data-nav-menu-tab="settings"' not in mobile
    assert 'data-nav-menu-tab="logs"' not in mobile


def test_viewer_has_no_empty_add_menu(viewer_client):
    html = viewer_client.get("/browse").text
    assert 'data-testid="nav-add-button"' not in html
    assert 'data-testid="nav-more-button"' in html
