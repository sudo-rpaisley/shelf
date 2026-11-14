"""Regression coverage for the sectioned Settings workspace and user menu."""

from pathlib import Path


def test_settings_page_has_five_section_controls(admin_client):
    html = admin_client.get("/settings").text
    assert "Administration" in html
    assert 'data-testid="settings-section-nav"' in html
    assert 'aria-label="Settings sections"' in html
    assert 'data-testid="settings-section-content"' in html
    for key in ("library", "integrations", "data", "users", "features"):
        assert html.count(f'data-testid="tab-{key}"') == 1


def test_settings_page_keeps_existing_setting_surfaces(admin_client, db):
    """The layout is a shell change: representative existing forms survive."""
    # The tag manager's forms are per-row, so a tag must exist to render one.
    db.execute("INSERT INTO tags (name) VALUES ('layout-check')")
    db.execute("COMMIT")
    html = admin_client.get("/settings").text
    assert 'action="/api/settings/display"' in html
    assert 'action="/api/settings/nav"' in html
    assert 'action="/api/settings"' in html
    assert 'action="/api/tags/' in html
    assert "Audiobookshelf" in html
    assert "Portable archive" in html
    assert "Users" in html


def test_admin_settings_entry_lives_in_account_menu(admin_client):
    html = admin_client.get("/browse").text
    assert 'data-testid="account-menu-button"' in html
    assert 'data-testid="account-menu-panel"' in html
    assert 'data-testid="account-menu-settings"' in html
    assert 'data-nav-tab="settings"' in html


def test_non_admin_account_menus_do_not_offer_admin_settings(editor_client, viewer_client):
    for client in (editor_client, viewer_client):
        html = client.get("/browse").text
        assert 'data-testid="account-menu-button"' in html
        assert 'data-testid="account-menu-settings"' not in html
        # This mirrors the existing server contract: /settings itself has
        # always required the admin role, so moving the link does not tighten
        # editor/viewer access as a side effect of the layout change.
        response = client.get("/settings", follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403)


def test_settings_is_not_rendered_in_primary_nav(admin_client):
    from tests.test_connected_layout import _Markup

    markup = _Markup(admin_client.get("/browse").text)
    roots = [node for node in markup.find("nav") if "data-media-navigation" in node.attrs]
    assert len(roots) == 1
    primary = markup.one("a", within=roots[0], **{"data-nav-primary": "browse"})
    tree = markup.one(**{"data-testid": "nav-menu-panel"})
    # Desktop and mobile now use the same native disclosure tree, not duplicate
    # rows. Browse remains a separate ordinary anchor; Settings stays outside both.
    assert primary not in markup.find("a", within=tree)
    for link in (primary, *markup.find("a", within=tree)):
        assert link.attrs.get("data-nav-tab") != "settings"
        assert link.attrs.get("data-nav-menu-tab") != "settings"
        assert link.attrs.get("href") != "/settings"
    panel = markup.one(**{"data-testid": "account-menu-panel"})
    assert markup.one("a", within=panel, **{"data-testid": "account-menu-settings"}).attrs["href"] == "/settings"


def test_account_menu_controller_is_registered():
    js = Path(__file__).resolve().parent.parent.joinpath("static/js/components.js").read_text()
    assert "Alpine.data('accountMenu'" in js
    assert "openAccount()" in js
    assert "closeAll()" in js
