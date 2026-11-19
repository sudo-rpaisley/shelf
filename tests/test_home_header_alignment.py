"""Authored Home heading/control contracts; runtime qualification remains gated."""

import pytest

from tests.test_command_launcher import _marked
from tests.test_connected_layout import _Markup
from tests.test_features_gate import off


def _home_contract(response):
    assert response.status_code == 200
    markup = _Markup(response.text)
    header = markup.testid("home-header")
    row = _marked(markup, "data-home-header-row", "div", within=header)
    tools = _marked(markup, "data-home-header-tools", "div", within=row)
    title = markup.one("h1", within=row)
    assert title.text.strip() == "Home" and title.attrs["class"] == "text-2xl font-bold"
    control = _marked(markup, "data-cover-size-control", "div")
    panel = _marked(markup, "data-cover-size-panel", "div")
    assert row in control.ancestors and tools in panel.ancestors and panel in control.ancestors
    assert markup.one("main") in control.ancestors
    assert not any(node.tag in {"nav", "form"} for node in control.ancestors)
    toolbar = _marked(markup, "data-media-display-tools", "div")
    assert toolbar not in control.ancestors
    assert not any("data-cover-size-control" in node.attrs for node in markup.find(within=toolbar))
    assert _marked(markup, "data-command-palette", "div", within=toolbar)
    assert not markup.find("button", within=header) and not markup.find("a", within=header)
    slider = markup.one("input", within=control)
    assert slider.attrs["type"] == "range" and "disabled" in slider.attrs
    assert (slider.attrs["min"], slider.attrs["max"], slider.attrs["step"], slider.attrs["value"]) == ("0", "10", "1", "5")
    assert slider.attrs["aria-valuetext"] == "100%"
    assert markup.one("output", within=control).text.strip() == "100%"
    assert markup.one("span", within=markup.one("label", within=control)).text.strip() == "Item size"
    assert panel.attrs["aria-describedby"] == "global-cover-size-description"
    assert len(markup.find(id="global-cover-size-description")) == 1
    shell = markup.one("html")
    assert shell.attrs["data-cover-size-account"].isdecimal() and shell.attrs["data-cover-size-mount"] == ""
    description, = [node for node in markup.find("p", within=header) if node.ancestors[-1] is header]
    assert description.text.startswith("A quick view of your collection.")
    assert row.position < description.position
    assert markup.testid("home-summary") and markup.testid("home-recent-items")
    return markup, description


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
def test_home_has_one_shell_owned_size_control_in_the_heading_row_not_a_second_toolbar_copy(fixture, request):
    _home_contract(request.getfixturevalue(fixture).get("/"))


def test_core_only_home_keeps_the_same_control_identity_and_its_original_description(viewer_client):
    off("connected_library")
    _, description = _home_contract(viewer_client.get("/"))
    assert "Use Browse when you want to search, filter or organise it." in description.text


@pytest.mark.parametrize("path", ("/browse", "/library", "/music", "/activity", "/series"))
def test_other_catalogue_pages_keep_the_single_default_shell_toolbar_placement(viewer_client, path):
    response = viewer_client.get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    control = _marked(markup, "data-cover-size-control", "div")
    toolbar = _marked(markup, "data-media-display-tools", "div")
    assert toolbar in control.ancestors
    assert not any(node.tag in {"nav", "main", "form"} for node in control.ancestors)
    assert not any("data-home-header-tools" in node.attrs for node in markup.find())
    assert markup.one("input", within=control).attrs["value"] == "5"


def test_anonymous_login_still_has_no_sizing_control_or_account_storage_identity(client):
    response = client.get("/login")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not any("data-cover-size-control" in node.attrs for node in markup.find())
    assert "data-cover-size-account" not in markup.one("html").attrs
