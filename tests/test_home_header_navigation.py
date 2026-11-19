"""Additive Home header regressions; authored, not runtime-qualified results."""

import pytest

from tests.test_connected_layout import _Markup
from tests.test_features_gate import off


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
@pytest.mark.parametrize("disabled_features", (
    (), ("connected_library",), ("stats",), ("connected_library", "stats"),
))
def test_home_header_omits_shortcuts_without_removing_other_entry_points(fixture, disabled_features, request):
    client = request.getfixturevalue(fixture)
    off(*disabled_features)
    response = client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    header = markup.testid("home-header")
    assert markup.one("h1", within=header).text.strip() == "Home"
    assert "A quick view of your collection." in markup.one("p", within=header).text
    for tag in ("a", "button", "form"):
        assert not markup.find(tag, within=header)
    assert not markup.find(within=header, role="button")

    navigation = markup.one("nav", **{"aria-label": "Primary navigation"})
    assert header not in navigation.ancestors and navigation not in header.ancestors
    connected = "connected_library" not in disabled_features
    stats = "stats" not in disabled_features
    assert bool(markup.find("a", within=navigation, href="/library")) == connected
    assert bool(markup.find("a", within=navigation, href="/activity")) == connected
    assert bool(markup.find("a", within=navigation, href="/stats")) == stats
    assert bool(markup.find("a", within=navigation, href="/scan")) == (fixture != "viewer_client")

    assert not [node for node in markup.find("h2") if node.text.strip() == "Useful places"]
    assert markup.find("a", within=markup.testid("home-summary"), href="/browse")
    assert markup.testid("home-recent-items")
    assert bool(markup.find(**{"data-testid": "home-personal-library"})) == connected
