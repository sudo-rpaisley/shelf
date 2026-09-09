"""Regression coverage for the richer top-right account menu."""


def _panel(html: str) -> str:
    return html.split('data-testid="account-menu-panel"', 1)[1].split("<!-- Account Modal -->", 1)[0]


def test_admin_account_menu_groups_account_admin_shortcuts_and_sign_out(admin_client):
    html = admin_client.get("/browse").text
    panel = _panel(html)

    assert 'data-testid="account-menu-button"' in html
    assert 'data-testid="account-menu-profile"' in panel
    assert 'data-testid="account-menu-settings"' in panel
    assert 'data-testid="account-menu-logs"' in panel
    assert 'data-testid="account-menu-shortcuts"' in panel
    assert 'data-testid="account-menu-logout"' in panel
    assert "Profile and password" in panel
    assert "Administration" in panel
    assert "Sign out" in panel


def test_viewer_account_menu_omits_admin_destinations(viewer_client):
    panel = _panel(viewer_client.get("/browse").text)

    assert 'data-testid="account-menu-profile"' in panel
    assert 'data-testid="account-menu-shortcuts"' in panel
    assert 'data-testid="account-menu-logout"' in panel
    assert 'data-testid="account-menu-settings"' not in panel
    assert 'data-testid="account-menu-logs"' not in panel


def test_account_menu_keeps_existing_profile_modal(admin_client):
    html = admin_client.get("/browse").text

    assert 'x-data="accountMenu"' in html
    assert "Manage your profile and sign-in details" in html
    assert "Display Name" in html
    assert "Change Password" in html


def test_account_menu_shortcut_action_is_registered_in_csp_component():
    source = open("static/js/components.js", encoding="utf-8").read()

    assert "Alpine.data('accountMenu'" in source
    assert "openShortcuts()" in source
    assert "document.getElementById('shortcut-modal')" in source


def test_oidc_account_modal_is_read_only(client, admin_user, db):
    from app.auth import create_token

    db.execute(
        "INSERT INTO user_identities (user_id, provider, issuer, subject, email) "
        "VALUES (?, 'oidc', ?, ?, ?)",
        (admin_user["id"], "https://idp.example.test", "subject-123", "admin@example.test"),
    )
    token = create_token(
        admin_user["id"], admin_user["username"], admin_user["role"],
        admin_user["display_name"], auth_method="oidc",
    )
    client.cookies.set("access_token", token)

    html = client.get("/browse").text
    assert 'data-testid="oidc-account-managed"' in html
    assert "Managed by your identity provider" in html
    assert "OIDC" in html
    assert 'x-model="current"' not in html
    assert 'x-model="newPw"' not in html
    assert '@click="saveName()"' not in html
