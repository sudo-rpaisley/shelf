"""Raw markup, not a post-JS DOM, must supply CSP-safe shortcut controls."""

import re
from pathlib import Path

import pytest

from tests.test_connected_layout import _Markup


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app/templates/base.html"
SURFACES = ("trigger", "backdrop", "close")


def _controls(markup):
    trigger, = [node for node in markup.find("button") if "data-shortcut-help" in node.attrs]
    backdrop = markup.one("div", id="shortcut-modal")
    close = markup.one("button", within=backdrop)
    return {"trigger": trigger, "backdrop": backdrop, "close": close}


def _assert_no_inline_events(node):
    handlers = [name for name in node.attrs if name.lower().startswith("on")]
    assert not handlers, f"Inline event attributes on {node.tag}: {handlers}"


def _with_inline_handler(html, surface):
    """Mutate markup bytes in memory; never edit a template or a served file."""
    if surface == "trigger":
        match = re.search(r"<button\s[^>]*\bdata-shortcut-help(?=[\s=>])", html)
        tag = "button"
    else:
        match = re.search(r'<div\s[^>]*\bid="shortcut-modal"', html)
        tag = "div"
        if surface == "close":
            assert match is not None
            match = re.compile(r"<button\s").search(html, match.end())
            tag = "button"
    assert match is not None, f"Missing actual {surface} markup"
    offset = match.start() + len(tag) + 1
    return html[:offset] + ' onclick="void 0"' + html[offset:]


@pytest.mark.parametrize("surface", SURFACES)
def test_template_source_has_no_inline_shortcut_event_attributes(surface):
    # Runtime removeAttribute() cannot repair a violation emitted while parsing.
    _assert_no_inline_events(_controls(_Markup(BASE.read_text()))[surface])


def test_template_source_declares_the_real_target_and_stable_close_hook():
    controls = _controls(_Markup(BASE.read_text()))
    assert controls["trigger"].attrs["aria-controls"] == controls["backdrop"].attrs["id"]
    assert "data-shortcut-help-close" in controls["close"].attrs
    assert "hidden" in controls["backdrop"].attrs["class"].split()


@pytest.mark.parametrize("surface", SURFACES)
def test_raw_markup_guard_rejects_reintroduced_inline_handlers(surface):
    source = BASE.read_text()
    mutated = _with_inline_handler(source, surface)
    assert mutated != source
    with pytest.raises(AssertionError, match="Inline event attributes"):
        _assert_no_inline_events(_controls(_Markup(mutated))[surface])


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
@pytest.mark.parametrize("locale,title,heading", (
    ("en", "Keyboard shortcuts (?)", "Keyboard Shortcuts"),
    ("it", "Scorciatoie da tastiera (?)", "Scorciatoie da tastiera"),
))
def test_registered_shell_renders_safe_controls_in_the_actual_locale(
    request, monkeypatch, client_name, locale, title, heading,
):
    monkeypatch.setenv("SHELF_I18N", "on")
    client = request.getfixturevalue(client_name)
    response = client.get("/", headers={"Accept-Language": locale})
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.one("html").attrs["lang"] == locale
    controls = _controls(markup)
    for node in controls.values():
        _assert_no_inline_events(node)
    assert controls["trigger"].attrs["title"] == title
    assert controls["trigger"].text.strip() == "?"
    assert controls["trigger"].attrs["aria-controls"] == controls["backdrop"].attrs["id"]
    assert "data-shortcut-help-close" in controls["close"].attrs
    assert controls["close"].attrs["aria-label"]
    assert markup.one("h3", within=controls["backdrop"]).text == heading
    script = markup.one("script", src="/static/js/app.js")
    served = client.get(script.attrs["src"])
    assert served.status_code == 200
    assert served.text == (ROOT / "static/js/app.js").read_text()
    directives = {parts[0]: parts[1:] for directive in response.headers["content-security-policy"].split(";")
                  if (parts := directive.split())}
    assert directives["script-src"] == ["'self'"]
