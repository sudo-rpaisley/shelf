"""Authored compact Commands presentation contracts; execution remains gated."""

import pytest

from tests.test_connected_layout import _Markup


def _marked(markup, attribute, tag=None, *, within=None):
    matches = [node for node in markup.find(tag, within=within) if attribute in node.attrs]
    assert len(matches) == 1
    return matches[0]


def test_command_icon_keeps_the_named_native_launcher_and_unchanged_dialog_fallback_contracts():
    from app.main import templates

    markup = _Markup(templates.env.get_template("fragments/command_palette.html").render())
    launcher = _marked(markup, "data-command-palette-launcher", "button")
    assert launcher.attrs["type"] == "button" and "hidden" in launcher.attrs
    assert launcher.attrs["aria-label"] == launcher.attrs["title"] == "Commands"
    assert launcher.attrs["aria-keyshortcuts"] == "Control+K Meta+K"
    assert launcher.attrs["aria-haspopup"] == "dialog" and launcher.attrs["aria-expanded"] == "false"
    dialog = _marked(markup, "data-command-palette-dialog", "dialog")
    assert launcher.attrs["aria-controls"] == dialog.attrs["id"] and "open" not in dialog.attrs
    assert launcher.text.strip() == "", "The full text button must not remain next to the icon"
    icon = markup.one("span", within=launcher, **{"class": "command-palette__launch-icon"})
    assert icon.attrs["aria-hidden"] == "true"
    assert markup.one("svg", within=icon).attrs["focusable"] == "false"
    fallback = _marked(markup, "data-command-palette-fallback", "details")
    assert markup.one("summary", within=fallback).text.strip() == "Commands"
    assert "Use the primary navigation above" in fallback.text
    assert _marked(markup, "data-command-palette-close", "button").text.strip() == "Close"
    assert _marked(markup, "data-command-palette-query", "input").attrs["maxlength"] == "200"
    assert not markup.find("script") and not markup.find("a") and not markup.find("form")
    assert not any(name.startswith(("on", "x-", "hx-")) for node in markup.find() for name in node.attrs)


def test_icon_accessible_name_and_tooltip_forceescape_hostile_translated_markup(monkeypatch):
    from app.main import templates
    from markupsafe import Markup

    hostile = '<launcher-probe data-owned="no">Commands</launcher-probe> & "quoted"'
    monkeypatch.setitem(templates.env.globals, "_", lambda *args, **kwargs: Markup(hostile))
    html = templates.env.get_template("fragments/command_palette.html").render()
    markup = _Markup(html)
    launcher = _marked(markup, "data-command-palette-launcher", "button")
    assert launcher.attrs["aria-label"] == launcher.attrs["title"] == hostile
    assert not markup.find("launcher-probe") and "&lt;launcher-probe" in html
    assert not any("data-owned" in node.attrs for node in markup.find())


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
def test_real_shell_keeps_one_command_owner_and_the_existing_independent_help_button(fixture, request):
    response = request.getfixturevalue(fixture).get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    root = _marked(markup, "data-command-palette", "div")
    launcher = _marked(markup, "data-command-palette-launcher", "button", within=root)
    assert launcher.attrs["aria-label"] == "Commands"
    assert not any(node.tag in {"nav", "main", "form"} for node in root.ancestors)
    help_button = _marked(markup, "data-shortcut-help", "button")
    assert root not in help_button.ancestors and help_button.text.strip() == "?"
    assert help_button.attrs["aria-controls"] == "shortcut-modal"
    assert {"fixed", "bottom-4", "left-4", "w-8", "h-8", "rounded-full"} <= set(help_button.attrs["class"].split())
