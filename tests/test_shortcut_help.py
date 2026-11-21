"""The account action and preserved modal use locale-independent CSP bindings.

The old standalone trigger was deliberately removed: restoring its id would
attach app.js's toggle as well as accountMenu.openShortcuts and close the dialog.
The action is bound by its Alpine method; modal/close retain their app.js IDs.
"""
import re
from pathlib import Path

import pytest


@pytest.mark.parametrize("path", ["/browse", "/"])
def test_no_inline_onclick_anywhere(admin_client, path):
    r = admin_client.get(path)
    assert r.status_code == 200
    assert ' onclick="' not in r.text


def test_shortcut_controls_carry_ids(admin_client):
    from tests.test_ui_phase4_contracts_20261121 import _Markup

    response = admin_client.get("/browse")
    assert response.status_code == 200
    root = _Markup(response.text).root
    assert not root.find_all(id="shortcut-help-trigger")
    panel = root.one(**{"data-testid": "account-menu-panel", "role": "menu"})
    action = panel.one("button", **{"data-testid": "account-shortcuts-action"})
    assert action.attrs["type"] == "button"
    assert action.attrs["role"] == "menuitem"
    assert action.attrs["@click"] == "openShortcuts()"
    modal = root.one(id="shortcut-modal")
    assert modal.attrs["role"] == "dialog"
    assert modal.attrs["aria-modal"] == "true"
    assert modal.attrs["aria-labelledby"] == "shortcut-modal-title"
    assert modal.one("h3", id=modal.attrs["aria-labelledby"])
    close = modal.one("button", id="shortcut-modal-close")
    assert close.attrs["type"] == "button"
    assert close.attrs["aria-label"] == "Close keyboard shortcuts"
    assert "data-shortcut-close" in close.attrs

    components = Path("static/js/components.js").read_text()
    method = re.search(r"openShortcuts\(\) \{(.*?)\n            \}", components, re.S)
    assert method is not None
    assert " ".join(method.group(1).split()) == (
        "this.open = false; const modal = document.getElementById('shortcut-modal'); "
        "if (modal) modal.classList.remove('hidden');"
    )


def test_account_shortcuts_action_keeps_visible_english_guidance(admin_client):
    """Successor of test_trigger_keeps_english_title; no duplicate old trigger."""
    from tests.test_ui_phase4_contracts_20261121 import _Markup

    response = admin_client.get("/browse")
    assert response.status_code == 200
    root = _Markup(response.text).root
    action = root.one("button", **{"data-testid": "account-shortcuts-action"})
    assert action.one("span").text == "Keyboard shortcuts"
    assert action.one("kbd").text == "?"
    modal = root.one(id="shortcut-modal")
    assert modal.one(id="shortcut-modal-title").text == "Keyboard Shortcuts"
    assert "Shortcuts are disabled when typing in inputs" in modal.text
    for label, key in (("Focus search", "/"), ("Go to Scan", "s"), ("Go to Browse", "b"), ("Show shortcuts", "?")):
        labels = [span for span in modal.find_all("span") if span.text == label]
        assert len(labels) == 1
        assert labels[0].parent.one("kbd").text == key
