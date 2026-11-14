"""Shared locator assets and compiled locales on real registered HTTP pages."""

from pathlib import Path

import pytest

from tests.test_connected_layout import _Markup
from tests.test_copy_locator_routes import _seed, _url


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
@pytest.mark.parametrize("surface", ("item", "locator"))
def test_actual_item_and_locator_shells_have_one_direct_sheet_and_post_app_script(
    request, db, client_name, surface,
):
    client = request.getfixturevalue(client_name)
    seed = _seed(db)
    path = f'/item/{seed["item"]}' if surface == "item" else _url(seed)
    before = tuple(db.iterdump())
    response = client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    markup.one("link", rel="stylesheet", href="/static/css/copy-locator.source.css")
    script = markup.one("script", src="/static/js/copy-locator.js")
    assert markup.one("script", src="/static/js/app.js").position < script.position
    assert "async" not in script.attrs and "defer" not in script.attrs
    if surface == "locator":
        target = markup.one("li", id=f'copy-{seed["copy"]}')
        assert "data-copy-locator-target" in target.attrs
    else:
        markup.one("a", id=f'copy-locator-open-{seed["copy"]}')
    assert tuple(db.iterdump()) == before


def test_locator_assets_are_cache_revalidated_and_not_new_store_precache_entries(viewer_client):
    for path in ("/static/css/copy-locator.source.css", "/static/js/copy-locator.js"):
        response = viewer_client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    assert (ROOT / "static/css/input.css").read_text().count('@import "./copy-locator.source.css";') == 1
    worker = (ROOT / "static/sw.js").read_text()
    assert "/static/css/copy-locator.source.css" not in worker
    assert "/static/js/copy-locator.js" not in worker


@pytest.mark.parametrize("locale,find,selected", (
    ("it", "Trova questa copia", "Copia selezionata"),
    ("zh_Hans", "查找此副本", "选中的副本"),
))
def test_locator_uses_the_actual_compiled_account_locale_without_translating_metadata(
    viewer_client, viewer_user, db, monkeypatch, locale, find, selected,
):
    monkeypatch.setenv("SHELF_I18N", "on")
    seed = _seed(db)
    db.execute("UPDATE users SET language = ? WHERE id = ?", (locale, viewer_user["id"]))
    db.commit()
    response = viewer_client.get(_url(seed))
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.one("h1").text.strip() == find
    target = markup.one("li", id=f'copy-{seed["copy"]}')
    assert markup.one("strong", id=f'copy-{seed["copy"]}-selection').text.strip() == selected
    assert "Exact secondary title" in target.text
