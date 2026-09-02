"""Regression coverage for the top-right account menu."""


def _section(html: str, start: str, end: str) -> str:
    """Return a stable template section bounded by rendered HTML comments."""
    return html.split(start, 1)[1].split(end, 1)[0]


def test_admin_account_menu_contains_settings_logs_and_sign_out(admin_client, db):
    html = admin_client.get("/browse").text

    assert 'data-testid="account-menu-button"' in html
    assert 'data-testid="account-menu-panel"' in html
    assert 'data-testid="account-profile-action"' in html

    account = _section(html, 'data-testid="account-menu-panel"', '<!-- Account Modal -->')
    assert 'href="/settings"' in account
    assert 'data-nav-tab="settings"' in account
    assert 'href="/logs"' in account
    assert 'data-nav-tab="logs"' in account
    assert 'action="/logout"' in account
    assert "Sign out" in account


def test_account_admin_actions_are_not_in_mobile_library_menu(admin_client, db):
    html = admin_client.get("/browse").text
    mobile = _section(html, 'data-testid="nav-menu-panel"', '<!-- Desktop navigation')

    assert 'href="/browse"' in mobile
    assert 'href="/settings"' not in mobile
    assert 'href="/logs"' not in mobile


def test_viewer_account_menu_omits_admin_destinations(viewer_client, db):
    html = viewer_client.get("/browse").text
    account = _section(html, 'data-testid="account-menu-panel"', '<!-- Account Modal -->')

    assert "Account" in account
    assert "Keyboard shortcuts" in account
    assert "Sign out" in account
    assert 'href="/settings"' not in account
    assert 'href="/logs"' not in account


def test_account_menu_uses_csp_registered_component(admin_client, db):
    html = admin_client.get("/browse").text
    assert 'x-data="accountMenu"' in html
    assert "Alpine.data('accountMenu'" in open("static/js/components.js", encoding="utf-8").read()

    desktop = _section(html, '<!-- Desktop navigation', '<!-- Account actions')
    assert 'data-testid="nav-add-button"' in desktop
    assert 'data-testid="nav-add-panel"' in desktop
    assert 'data-testid="nav-more-button"' in desktop
    assert 'data-testid="nav-more-panel"' in desktop
    assert '<details' not in desktop
    assert desktop.count('x-data="navMenu"') == 2

    account = _section(html, 'data-testid="account-menu-panel"', '<!-- Account Modal -->')
    assert 'data-testid="account-shortcuts-action"' in account
    assert 'title="Keyboard shortcuts (?)"' not in html
    assert 'id="shortcut-modal" role="dialog"' in html
    assert 'hidden flex items-center justify-center' in html
