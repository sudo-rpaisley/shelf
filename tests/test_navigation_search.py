"""Navigation search/subtitle regression source; not executed or qualified here.

Markup form ownership below is a finite projection, not browser FormData proof.
Native submission/keyboard/no-JS behavior has separate authored browser journeys.
"""

from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

from markupsafe import Markup
import pytest

from app import media_presentation, nav
from app.services import library_catalog
from tests.test_connected_layout import _Markup, _form_values
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _gnu_catalogue, _render, _setting
from tests.test_recent_shelf import _marked, _seed


def _model(user):
    tabs = nav.visible_tabs(user)
    before = deepcopy(tabs)
    result = nav.media_navigation(tabs, media_presentation.family_nodes(), intent_for=media_presentation.filter_intent)
    assert tabs == before
    return result


def _filter_values(markup, form):
    """Original descendant helper + the one explicit external q owner only."""
    result = _form_values(markup, form)
    assert "q" not in result
    (query,) = markup.find("input", name="q", form=form.attrs["id"])
    assert query.attrs["type"] == "search" and "disabled" not in query.attrs
    result["q"] = query.attrs["value"]
    return result


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER])
@pytest.mark.parametrize("library_state", ["visible", "hidden", "disabled"])
def test_global_search_model_reuses_the_exact_eligible_primary_url_without_mutating_tabs(db, user, library_state):
    _feature(db, "connected_library", library_state != "disabled")
    _setting(db, "nav_hidden_tabs", '["library"]' if library_state == "hidden" else '[]')
    model = _model(user)
    target = urlsplit(model["primary"]["path"])
    search = model["search"]
    assert search["path"] == target.path
    assert search["fields"] == tuple(parse_qsl(target.query, keep_blank_values=True))
    assert search["max_length"] == (300 if library_state == "visible" else 200)
    assert dict(search["fields"]) == ({"reset": "1", "view": "items", "sort": "recent"}
                                      if library_state == "visible" else {"sort": "newest"})
    assert search["path"] == ("/library" if library_state == "visible" else "/browse")


def test_missing_core_navigation_does_not_invent_a_search_destination():
    assert nav.media_navigation([], (), intent_for=lambda selectors: None)["search"] is None


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
def test_nav_search_is_native_labelled_outside_disclosures_and_absent_for_anonymous_shells(db, user):
    html, _ = _render(user, media_presentation.family_nodes(), media_presentation.filter_intent)
    markup = _Markup(html)
    searches = [node for node in markup.elements if "data-navigation-search" in node.attrs]
    assert len(searches) == (0 if user is None else 1)
    if user is None:
        assert not markup.find("input", id="library-q")
        return
    search = searches[0]
    model = _model(user)
    assert search.tag == "form" and search.attrs["method"] == "get" and search.attrs["action"] == model["search"]["path"]
    assert search.attrs["role"] == "search" and search.attrs["aria-label"] == "Search your library"
    assert not any(node.tag in {"details", "form"} for node in search.ancestors)
    assert any("data-media-navigation" in node.attrs for node in search.ancestors)
    query = markup.one("input", within=search, type="search")
    assert query.attrs["id"] == "library-q" and query.attrs["name"] == "q" and query.attrs["value"] == ""
    assert "form" not in query.attrs and int(query.attrs["maxlength"]) == model["search"]["max_length"]
    assert markup.one("label", within=search, **{"for": query.attrs["id"]}).text == "Search your library"
    assert markup.one("button", within=search).attrs["type"] == "submit"
    assert _form_values(markup, search) == {**dict(model["search"]["fields"]), "q": ""}


@pytest.mark.parametrize("fixture", ["viewer_client", "editor_client", "admin_client"])
def test_library_moves_actual_query_to_nav_and_keeps_one_external_owner_and_all_filters(request, fixture):
    client = request.getfixturevalue(fixture)
    filters = {"q": 'Current 星 & "query"', "types": "book,ebook", "family": "book", "view": "items", "sort": "recent", "page_size": "48"}
    response = client.get("/library", params=filters)
    assert response.status_code == 200
    markup = _Markup(response.text)
    main = markup.one("main")
    search = _marked(markup, "data-navigation-search")
    form = markup.testid("library-filters")
    query = markup.one("input", id="library-q")
    assert search.tag == "div" and search.attrs["role"] == "search"
    assert main not in search.ancestors and main in form.ancestors
    assert form.attrs["id"] == "library-filters" and form.attrs["method"] == "get" and form.attrs["action"] == "/library"
    assert query.attrs["form"] == form.attrs["id"] and search in query.ancestors and form not in query.ancestors
    submit = markup.one("button", within=search)
    assert submit.attrs["type"] == "submit" and submit.attrs["form"] == form.attrs["id"]
    assert not markup.find("input", within=form, name="q") and not markup.find("input", within=main, type="search")
    assert len(markup.find("input", type="search", name="q")) == 1
    assert _filter_values(markup, form) == library_catalog.normalize_filters(filters)
    options = markup.testid("library-filter-options")
    assert form in options.ancestors and "open" not in options.attrs
    assert markup.one("button", within=options).text.strip() == "Apply filters"
    ids = [node.attrs["id"] for node in markup.elements if "id" in node.attrs]
    assert len(ids) == len(set(ids))


def test_external_query_keeps_saved_view_and_empty_override_without_writing_state(viewer_client, viewer_user, db, monkeypatch):
    import httpx

    saved = {"q": "Saved search", "types": "book,ebook", "family": "book", "series": "Saga #1/2",
             "source": "source:999", "availability": "digital", "ownership": "neither", "wishlist": "no",
             "view": "items", "sort": "recent", "page_size": "48"}
    created = viewer_client.post("/api/library/views", data={"name": "Nav form ownership", **saved}, follow_redirects=False)
    assert created.status_code == 303
    view_id = db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0]
    before = tuple(db.iterdump())
    def forbidden(*args, **kwargs):
        raise AssertionError("Navigation search cannot query remote integrations")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    for query in ('Current 星 & "query"', ""):
        response = viewer_client.get("/library", params={"saved_view": view_id, "q": query})
        assert response.status_code == 200
        markup = _Markup(response.text)
        values = _filter_values(markup, markup.testid("library-filters"))
        expected = {**library_catalog.normalize_filters({**saved, "q": query}), "saved_view": str(view_id)}
        assert values == expected
        assert "page" not in values
        submitted = viewer_client.get("/library", params=values)
        assert submitted.status_code == 200
        submitted_markup = _Markup(submitted.text)
        assert _filter_values(submitted_markup, submitted_markup.testid("library-filters")) == expected
    assert tuple(db.iterdump()) == before


def test_direct_hidden_library_uses_its_existing_filter_form_without_changing_global_fallback(viewer_client, db):
    _setting(db, "nav_hidden_tabs", '["library"]')
    markup = _Markup(viewer_client.get("/library?q=Local").text)
    query = markup.one("input", id="library-q")
    assert query.attrs["form"] == "library-filters" and query.attrs["value"] == "Local"
    home = _Markup(viewer_client.get("/").text)
    search = _marked(home, "data-navigation-search")
    assert search.tag == "form" and search.attrs["action"] == "/browse"
    assert _form_values(home, search) == {"sort": "newest", "q": ""}


@pytest.mark.parametrize("msgid", ["Search your library", "Search"])
def test_nav_owned_labels_escape_real_gnu_translations_and_query_is_never_gettext(db, monkeypatch, msgid):
    import app.i18n as i18n

    hostile = '\" autofocus data-probe="yes"><nav-probe>translated</nav-probe> & "quoted"'
    raw = Markup('\" autofocus data-probe="yes"><query-probe>Raw 100%d 星</query-probe>')
    catalog = _gnu_catalogue({msgid: hostile})
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: catalog)
    html, _ = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent,
                      navigation_search_form="library-filters", filters={"q": raw})
    markup = _Markup(html)
    search = _marked(markup, "data-navigation-search")
    query = markup.one("input", within=search, id="library-q")
    assert query.attrs["value"] == str(raw) and raw not in catalog.calls
    if msgid == "Search your library":
        assert search.attrs["aria-label"] == hostile and query.attrs["placeholder"] == hostile
        assert markup.one("label", within=search).text == hostile
    else:
        assert markup.one("button", within=search).text == hostile
    assert not markup.find("nav-probe") and not markup.find("query-probe")
    assert not any("data-probe" in node.attrs or "autofocus" in node.attrs for node in markup.elements)


@pytest.mark.parametrize("count", [0, 1, 36])
def test_removed_recent_subtitle_does_not_remove_heading_or_change_native_and_buffer_selection(viewer_client, db, count):
    expected, trashed = _seed(db, count)
    markup = _Markup(viewer_client.get("/").text)
    recent = _marked(markup, "data-recent-shelf", "streaming-v1")
    heading = markup.one("div", within=recent, **{"class": "recent-shelf__heading"})
    title = markup.one("h2", within=heading, id="home-recent-heading")
    assert title.text == "Recently added" and markup.one("a", within=title).attrs["href"] == "/library?reset=1&view=items&sort=recent"
    assert not markup.find("p", within=heading) and "Across your collection" not in heading.text
    assert not [node for node in markup.find(within=recent) if "data-recent-count" in node.attrs]
    cards = [node for node in markup.find("article", within=recent) if "data-item-id" in node.attrs]
    live = [node for node in cards if not any(parent.tag == "template" for parent in node.ancestors)]
    assert [int(node.attrs["data-item-id"]) for node in live] == expected[:8]
    assert [int(node.attrs["data-item-id"]) for node in cards] == expected[:24]
    assert not any(node.attrs["data-item-id"] == str(trashed) for node in cards)
    if count:
        buffer = _marked(markup, "data-recent-overflow", within=recent)
        assert buffer.attrs["data-recent-total"] == str(min(count, 24)) and buffer.attrs["data-recent-total-label"]
        actions = [node.attrs["data-recent-action"] for node in markup.find("button", within=recent)]
        assert actions == ["previous", "next"]
