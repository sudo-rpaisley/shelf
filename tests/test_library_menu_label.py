"""Library menu label regressions; no historical/runtime pass is implied."""

from pathlib import Path

import pytest
from starlette.requests import Request

from app import media_presentation, nav
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, FragmentDOM, _feature, _gnu_catalogue


@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
@pytest.mark.parametrize("library_enabled", [False, True])
def test_library_menu_changes_only_summary_labels_not_authorized_destinations(db, user, library_enabled):
    import app.main as main

    _feature(db, "connected_library", library_enabled)
    tabs = nav.visible_tabs(user)
    model = nav.media_navigation(tabs, media_presentation.family_nodes(), intent_for=media_presentation.filter_intent)
    context = {"request": Request({"type": "http", "path": "/stats", "headers": [], "query_string": b""}),
               "user": user, "nav_tabs": tabs, "media_navigation": model}
    source = Path("app/templates/fragments/media_navigation.html").read_text()
    old_summary = "aria-label=\"{{ _('Browse media')|forceescape }}\"><span>{{ _('Media')|forceescape }}</span>"
    new_summary = "aria-label=\"{{ _('Browse library')|forceescape }}\"><span>{{ _('Library')|forceescape }}</span>"
    assert source.count(new_summary) == 1 and old_summary not in source
    current = FragmentDOM(main.templates.env.from_string(source).render(context)).root
    previous = FragmentDOM(main.templates.env.from_string(source.replace(new_summary, old_summary, 1)).render(context)).root
    assert [node.attrs for node in current.find("a")] == [node.attrs for node in previous.find("a")]
    assert [node.attrs for node in current.find("details")] == [node.attrs for node in previous.find("details")]
    (menu,) = current.find("details", **{"data-nav-browse": None})
    summary = menu.children[0]
    assert summary.attrs["aria-label"] == "Browse library"
    (label,) = summary.find("span")
    assert label.text == "Library"


@pytest.mark.parametrize("msgid", ["Library", "Browse library"])
def test_library_menu_visible_and_accessible_translations_remain_escaped(db, monkeypatch, msgid):
    import app.i18n as i18n
    from tests.test_media_navigation import _render

    hostile = '\" data-probe="yes"><probe data-probe="yes">Library</probe> & "quoted"'
    catalog = _gnu_catalogue({msgid: hostile})
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: catalog)
    _, dom = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent)
    (menu,) = dom.find("details", **{"data-nav-browse": None})
    if msgid == "Browse library":
        assert menu.children[0].attrs["aria-label"] == hostile
    else:
        (label,) = menu.children[0].find("span")
        assert label.text == hostile
    assert not dom.find("probe") and not any("data-probe" in node.attrs for node in dom.walk())
