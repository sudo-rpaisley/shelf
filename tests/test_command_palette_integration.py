"""Registered HTTP/shell glue for the independently reviewed palette component."""

from pathlib import Path

import pytest

from tests.test_connected_layout import _Markup


ROOT = Path(__file__).resolve().parents[1]


def _marked(markup, attribute, *, tag=None):
    result = [node for node in markup.find(tag) if attribute in node.attrs]
    assert len(result) == 1
    return result[0]


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
@pytest.mark.parametrize("path", ("/", "/browse", "/library"))
def test_registered_pages_have_one_palette_outside_main_and_protected_navigation(request, client_name, path):
    response = request.getfixturevalue(client_name).get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    main = markup.one("main")
    navigation = _marked(markup, "data-media-navigation", tag="nav")
    palette = _marked(markup, "data-command-palette")
    assert main not in palette.ancestors and navigation not in palette.ancestors
    assert navigation.position < palette.position < main.position
    dialog = _marked(markup, "data-command-palette-dialog", tag="dialog")
    assert "open" not in dialog.attrs
    assert not markup.find("a", within=palette), "The fragment must not carry a second destination registry"
    launcher = _marked(markup, "data-command-palette-launcher", tag="button")
    assert launcher.attrs["aria-controls"] == dialog.attrs["id"]
    assert "hidden" in launcher.attrs, "Native fallback is visible until enhancement succeeds"
    assert _marked(markup, "data-command-palette-fallback").tag == "details"


def test_real_shell_load_order_keeps_existing_alpine_and_app_registration(viewer_client):
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    sources = [node.attrs["src"] for node in markup.find("script") if "src" in node.attrs]
    script = markup.one("script", src="/static/js/command-palette.js")
    assert sources.index(script.attrs["src"]) > sources.index("/static/js/app.js")
    assert "async" not in script.attrs
    assert sources.index("/static/js/component-load-guard.js") < sources.index("/static/js/components.js")
    assert sources.index("/static/js/components-settings.js") < sources.index("/static/vendor/alpinejs-csp-3.15.9.min.js")
    alpine = markup.one("script", src="/static/vendor/alpinejs-csp-3.15.9.min.js")
    assert "defer" in alpine.attrs and "async" not in alpine.attrs
    assert "Open command palette" in markup.one(id="shortcut-modal").text
    help_button = _marked(markup, "data-shortcut-help", tag="button")
    assert help_button.attrs["aria-controls"] == "shortcut-modal"
    assert "onclick" not in help_button.attrs
    assert viewer_client.get(script.attrs["src"]).status_code == 200


def test_direct_no_cache_styles_do_not_expand_store_precaching(viewer_client):
    markup = _Markup(viewer_client.get("/").text)
    path = "/static/css/command-palette.source.css"
    markup.one("link", rel="stylesheet", href=path)
    response = viewer_client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    source = (ROOT / "static/css/input.css").read_text()
    assert source.count('@import "./command-palette.source.css";') == 1
    assert source.index('@import "./command-palette.source.css";') > source.index('@import "./media-cards.source.css";')
    worker = (ROOT / "static/sw.js").read_text()
    assert path not in worker and "/static/js/command-palette.js" not in worker


def test_palette_shell_gets_do_not_write_catalogue_copies_locations_or_personal_activity(viewer_client, db):
    tables = ("items", "item_copies", "locations", "user_item_state", "user_item_events")

    def snapshot():
        return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in tables}  # Fixed test-owned table names, not request input.

    before = snapshot()
    for path in ("/", "/browse", "/library"):
        assert viewer_client.get(path).status_code == 200
    assert snapshot() == before


@pytest.mark.parametrize("locale,label,search_label", (
    ("it", "Comandi", "Cerca «{query}» nel catalogo"),
    ("zh_Hans", "命令", "在目录中搜索“{query}”"),
))
def test_palette_uses_the_real_compiled_account_locale_catalogue(
    viewer_client, viewer_user, db, monkeypatch, locale, label, search_label,
):
    monkeypatch.setenv("SHELF_I18N", "on")
    db.execute("UPDATE users SET language = ? WHERE id = ?", (locale, viewer_user["id"]))
    db.commit()
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert _marked(markup, "data-command-palette-launcher").text == label
    assert _marked(markup, "data-command-palette-dialog").attrs["data-search-label"] == search_label
