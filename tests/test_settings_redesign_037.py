"""Regression coverage for the rebuilt Settings workspace shell."""

from pathlib import Path


def test_settings_page_has_responsive_section_sidebar(admin_client):
    html = admin_client.get("/settings").text
    assert "Administration" in html
    assert 'data-testid="settings-section-nav"' in html
    assert 'data-testid="settings-section-content"' in html
    for key in ("library", "integrations", "data", "users"):
        assert html.count(f'data-testid="tab-{key}"') == 1
    assert 'aria-label="Settings sections"' in html


def test_settings_shell_keeps_current_feature_fragments(admin_client):
    """The redesign is presentational: current 0.37 controls remain rendered."""
    html = admin_client.get("/settings").text
    for label in (
        "Audiobookshelf",
        "RomM",
        "Komga",
        "OpenID Connect",
        "Portable archive",
        "Users",
    ):
        assert label in html
    assert 'id="romm-panel"' in html
    assert 'id="komga-panel"' in html


def test_connected_service_panels_remain_on_integrations_tab(admin_client):
    html = admin_client.get("/settings").text
    for panel_id in ("romm-panel", "komga-panel"):
        start = html.index(f'id="{panel_id}"')
        opening = html[max(0, start - 120):start + 120]
        assert "tab === 'integrations'" in opening
    # OIDC is also an integration surface even though it has a larger policy UI.
    oidc_pos = html.index("OpenID Connect")
    assert "tab === 'integrations'" in html[max(0, oidc_pos - 500):oidc_pos]


def test_location_error_banner_contract_survives_layout_change(admin_client):
    html = admin_client.get("/settings?location_error=blank").text
    assert 'data-testid="location-error-banner"' in html
    assert "Location name is required." in html
    hostile = admin_client.get("/settings?location_error=%3Cscript%3Ealert(1)%3C/script%3E").text
    assert 'data-testid="location-error-banner"' not in hostile


def test_settings_tab_controller_still_persists_selection():
    js = Path("static/js/components-settings.js").read_text(encoding="utf-8")
    assert "Alpine.data('settingsTabs'" in js
    assert "shelf_settings_tab" in js
    assert "setTab(name)" in js


def test_settings_access_remains_admin_only(editor_client, viewer_client):
    for client in (editor_client, viewer_client):
        response = client.get("/settings", follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403)
