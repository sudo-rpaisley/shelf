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
    from html.parser import HTMLParser

    class NavLinks(HTMLParser):
        def __init__(self, root_id):
            super().__init__()
            self.root_id, self.depth, self.links = root_id, 0, []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "div" and attrs.get("data-testid") == self.root_id:
                self.depth = 1
            elif self.depth and tag == "div":
                self.depth += 1
            if self.depth and tag == "a":
                self.links.append(attrs)

        def handle_endtag(self, tag):
            if self.depth and tag == "div":
                self.depth -= 1

    html = admin_client.get("/browse").text
    for root_id, key in (("nav-activity-groups", "data-nav-tab"), ("nav-menu-panel", "data-nav-menu-tab")):
        nav = NavLinks(root_id)
        nav.feed(html)
        assert any(link.get(key) == "browse" for link in nav.links)
        assert all(link.get(key) != "settings" and link.get("href") != "/settings" for link in nav.links)


def test_account_menu_controller_is_registered():
    js = Path(__file__).resolve().parent.parent.joinpath("static/js/components.js").read_text()
    assert "Alpine.data('accountMenu'" in js
    assert "openAccount()" in js
    assert "closeAll()" in js
