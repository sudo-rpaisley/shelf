"""The API tokens tab in the account modal: rendered for every role with
`api` on, absent with it off, and the dropdown panel unchanged."""

import re

import pytest

from tests.test_account_menu import _DOM, _section
from tests.test_features_gate import off

MARKER = "<!-- Account Modal -->"
CLIENTS = ["viewer_client", "editor_client", "admin_client"]


def _modal(html: str) -> str:
    dom = _DOM(html)
    matches = [node for node in dom.nodes if "data-account-dialog" in node["attrs"]]
    assert len(matches) == 1
    node = matches[0]
    return html[node["start"]:node["end"]]


def _panel(html: str) -> str:
    return _section(html, "account-menu-panel", "account-menu-panel")


def _api_token_controls(html):
    """Actual API-token hooks/routes, not arbitrary text such as csrf_token."""
    controls = []
    for node in _DOM(html).nodes:
        attrs = node["attrs"]
        testid = attrs.get("data-testid", "")
        urls = [attrs.get(name, "") for name in ("href", "action", "hx-get", "hx-post", "hx-delete")]
        if (testid.startswith(("api-token-", "api-tokens-", "admin-token-"))
                or testid == "account-tab-tokens"
                or attrs.get("x-data") in {"apiTokensPanel", "adminTokensPanel"}
                or attrs.get("@click") == "tab = 'tokens'"
                or any(re.fullmatch(r"/api/(?:tokens|users/\d+/tokens)(?:/\d+)?(?:\?.*)?", url)
                       for url in urls)):
            controls.append((node["tag"], attrs))
    return controls


@pytest.mark.parametrize("fixture", CLIENTS)
def test_every_role_gets_the_tokens_tab(request, fixture):
    html = request.getfixturevalue(fixture).get("/browse").text
    modal = _modal(html)
    assert 'data-testid="account-tab-tokens"' in modal
    assert 'x-data="apiTokensPanel"' in modal
    assert 'data-testid="api-tokens-pane"' in modal
    assert "This is the only time the token is shown." in modal
    assert "No tokens yet." in modal
    assert _api_token_controls(modal)  # positive census cannot pass vacuously


@pytest.mark.parametrize("fixture", CLIENTS)
def test_the_tab_is_absent_with_api_off(request, fixture):
    client = request.getfixturevalue(fixture)
    off("api")
    html = client.get("/browse").text
    assert MARKER in html  # the modal itself still renders
    assert 'data-testid="account-tab-tokens"' not in html
    assert 'data-testid="api-tokens-pane"' not in html
    assert "apiTokensPanel" not in html


@pytest.mark.parametrize("fixture", CLIENTS)
def test_the_dropdown_panel_is_unchanged(request, fixture):
    client = request.getfixturevalue(fixture)
    on_html = client.get("/browse").text
    on_panel = _panel(on_html)
    off("api")
    assert _panel(client.get("/browse").text) == on_panel
    dropdown = _section(on_html, "account-menu-panel", "account-menu-panel")
    assert _api_token_controls(dropdown) == []
    logout = [node for node in _DOM(dropdown).nodes
              if node["tag"] == "form" and node["attrs"].get("action") == "/logout"]
    assert len(logout) == 1
    csrf = [node["attrs"] for node in _DOM(dropdown).nodes
            if node["tag"] == "input" and node["attrs"].get("name") == "_csrf"]
    assert len(csrf) == 1
    assert csrf[0]["type"] == "hidden" and csrf[0]["value"]


def test_dropdown_census_distinguishes_api_management_from_logout_csrf():
    assert _api_token_controls(
        '<form action="/logout"><input type="hidden" name="_csrf" value="csrf-token-value">'
        '<button type="submit">Sign out</button></form>'
    ) == []
    controls = _api_token_controls(
        '<button data-testid="account-tab-tokens">API tokens</button>'
        '<input data-testid="api-token-name-input"><button data-testid="api-token-create">Create</button>'
        '<button data-testid="api-token-revoke">Revoke</button>'
        '<a href="/api/tokens/12">Own token</a><form action="/api/users/7/tokens/12"></form>'
    )
    assert len(controls) == 6


def test_the_tokens_tab_sits_after_the_existing_tabs(admin_client):
    modal = _modal(admin_client.get("/browse").text)
    assert modal.index(">Profile</button>") < modal.index(">Password</button>") \
        < modal.index('data-testid="account-tab-tokens"')


def test_the_component_is_registered_in_the_load_guard():
    from pathlib import Path
    guard = Path("static/js/component-load-guard.js").read_text()
    assert "apiTokensPanel: 'components-settings.js'" in guard
    assert "Alpine.data('apiTokensPanel'" in Path("static/js/components-settings.js").read_text()
