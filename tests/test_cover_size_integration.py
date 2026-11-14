"""Actual registered producers, not preferences or standalone component HTML."""

from pathlib import Path

import pytest

from tests.test_connected_layout import _Markup


ROOT = Path(__file__).resolve().parents[1]


def _marked(markup, name, *, tag=None, within=None):
    nodes = [node for node in markup.find(tag, within=within) if name in node.attrs]
    assert len(nodes) == 1
    return nodes[0]


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
@pytest.mark.parametrize("path", ("/", "/library?reset=1&view=items", "/library?reset=1&view=groups", "/music"))
def test_registered_card_producers_have_one_explained_native_control_inside_existing_root(
    request, client_name, path,
):
    response = request.getfixturevalue(client_name).get(path)
    assert response.status_code == 200
    markup = _Markup(response.text)
    main = markup.one("main")
    panel = _marked(markup, "data-cover-size-panel")
    control = _marked(markup, "data-cover-size-control", within=panel)
    assert main in panel.ancestors
    page_root = panel.ancestors[panel.ancestors.index(main) + 1]
    assert page_root is not panel and page_root.tag == "div"
    assert not any(node.tag in {"form", "article", "ul"} for node in control.ancestors)
    assert panel.attrs["role"] == "group" and panel.attrs["aria-label"] == "Cover size"
    explanation = markup.one("p", id=panel.attrs["aria-describedby"])
    assert panel in explanation.ancestors
    assert "not more cards per row" in explanation.text and "stop shrinking" in explanation.text
    slider = _marked(markup, "data-cover-size-input", tag="input", within=control)
    assert slider.ancestors[-1].tag == "label", "Native implicit label works before enhancement"
    assert (slider.attrs["type"], slider.attrs["min"], slider.attrs["max"],
            slider.attrs["step"], slider.attrs["value"]) == ("range", "50", "100", "10", "100")
    assert "disabled" in slider.attrs and "name" not in slider.attrs
    assert "This page only; not saved." in control.text
    assert len(markup.find("button", within=control)) == 3


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
def test_series_view_does_not_advertise_a_control_for_its_direct_noncard_thumbnails(request, client_name):
    response = request.getfixturevalue(client_name).get("/library?reset=1&view=series")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not any("data-cover-size-control" in node.attrs for node in markup.elements)
    assert not any("data-cover-size-panel" in node.attrs for node in markup.elements)


@pytest.mark.parametrize("path", ("/", "/library?reset=1&view=items", "/music"))
def test_unscoped_query_parameter_cannot_load_a_cover_size_preference(viewer_client, path):
    response = viewer_client.get(path + ("&" if "?" in path else "?") + "cover_size=50")
    assert response.status_code == 200
    slider = _marked(_Markup(response.text), "data-cover-size-input", tag="input")
    assert slider.attrs["value"] == "100"


def test_real_assets_are_singleton_post_app_direct_styles_without_new_store_precache(viewer_client):
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    markup.one("link", rel="stylesheet", href="/static/css/cover-size.source.css")
    style = viewer_client.get("/static/css/cover-size.source.css")
    assert style.status_code == 200 and style.headers["cache-control"] == "no-cache"
    script = markup.one("script", src="/static/js/cover-size.js")
    assert "async" not in script.attrs and "defer" not in script.attrs
    assert markup.one("script", src="/static/js/app.js").position < script.position
    assert viewer_client.get(script.attrs["src"]).status_code == 200
    source = (ROOT / "static/css/input.css").read_text()
    assert source.count('@import "./cover-size.source.css";') == 1
    assert source.index('@import "./cover-size.source.css";') > source.index('@import "./media-cards.source.css";')
    worker = (ROOT / "static/sw.js").read_text()
    assert "/static/js/cover-size.js" not in worker and "/static/css/cover-size.source.css" not in worker


def test_card_controls_gets_do_not_change_catalogue_or_private_state(viewer_client, db):
    tables = ("items", "item_copies", "locations", "settings", "user_item_state", "user_item_events")

    def snapshot():
        return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in tables}  # Fixed fixture tables, never request input.

    before = snapshot()
    for path in ("/", "/library?reset=1&view=items", "/library?reset=1&view=groups", "/music"):
        assert viewer_client.get(path).status_code == 200
    assert snapshot() == before


@pytest.mark.parametrize("locale,label,explanation", (
    ("it", "Dimensione delle copertine", "mantenere il testo leggibile"),
    ("zh_Hans", "封面大小", "保证文字可读"),
))
def test_control_and_geometry_explanation_use_the_actual_compiled_account_locale(
    viewer_client, viewer_user, db, monkeypatch, locale, label, explanation,
):
    monkeypatch.setenv("SHELF_I18N", "on")
    db.execute("UPDATE users SET language = ? WHERE id = ?", (locale, viewer_user["id"]))
    db.commit()
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    panel = _marked(markup, "data-cover-size-panel")
    assert panel.attrs["aria-label"] == label
    assert explanation in markup.one(id=panel.attrs["aria-describedby"]).text
