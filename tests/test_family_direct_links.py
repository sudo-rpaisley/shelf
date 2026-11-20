"""Direct family-link regressions, authored only; no runtime pass is implied."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from app import media_presentation, nav
from tests.test_command_palette import _node, render_component
from tests.test_connected_layout import _Markup
from tests.test_media_navigation import ADMIN, EDITOR, VIEWER, _feature, _gnu_catalogue, _render, _setting
from tests.test_recent_shelf import _marked


def _configure(db, state):
    _feature(db, "connected_library", state != "disabled")
    _feature(db, "music", True)
    _feature(db, "periodicals", True)
    _setting(db, "nav_hidden_tabs", '["library"]' if state == "hidden" else '[]')


def _model(user):
    tabs = nav.visible_tabs(user)
    return nav.media_navigation(tabs, media_presentation.family_nodes(), intent_for=media_presentation.filter_intent)


@pytest.mark.parametrize("state", ["visible", "hidden", "disabled"])
@pytest.mark.parametrize("user", [ADMIN, EDITOR, VIEWER, None])
def test_every_eligible_family_name_is_the_exact_former_all_items_link_with_independent_native_arrow(db, state, user):
    _configure(db, state)
    model = _model(user)
    snapshot = deepcopy(model)
    html, _ = _render(user, media_presentation.family_nodes(), media_presentation.filter_intent, model=model)
    assert model == snapshot
    markup = _Markup(html)
    library_active = model["all_media"] is not None
    assert len([node for node in markup.elements if "data-nav-family" in node.attrs]) == len(model["families"])
    for family in model["families"]:
        branch = _marked(markup, "data-nav-family", family["key"])
        link = _marked(markup, "data-nav-family-link", family["key"], within=branch)
        label = _marked(markup, "data-nav-family-label", within=branch)
        assert link is label and link.tag == "a" and link.ancestors[-1] is branch
        assert link.attrs["href"] == family["path"] and link.text == family["label"]
        assert not any(parent.tag in {"summary", "button", "a"} for parent in link.ancestors)
        assert not any(attr.startswith(("on", "@", "x-", "hx-")) for attr in link.attrs)
        details = markup.one("details", within=branch)
        summary = markup.one("summary", within=details)
        assert details.ancestors[-1] is branch and summary.ancestors[-1] is details
        assert not markup.find("a", within=summary) and not markup.find("button", within=summary)
        names = [markup.one(id=value) for value in summary.attrs["aria-labelledby"].split()]
        assert names[0] is label and names[1].text == "Show formats"
        assert all(branch in name.ancestors for name in names)
        formats = markup.one("ul", within=details)
        assert not markup.find("a", within=formats, **{"data-nav-family-link": family["key"]})
        assert not [anchor for anchor in markup.find("a", within=branch) if anchor.text.strip() == "All items"]
        for fmt in family["formats"]:
            anchor = _marked(markup, "data-nav-format", fmt["key"], within=formats)
            assert anchor.attrs["href"] == fmt["path"]
        if family["landing"]:
            anchor = markup.one("a", within=formats, **{"data-nav-tab": family["landing"]["key"]})
            assert anchor.attrs["href"] == family["landing"]["path"]
        parsed = urlsplit(link.attrs["href"])
        assert parsed.path == ("/library" if library_active else "/browse")
        assert parse_qs(parsed.query) == ({"reset": ["1"], "view": ["items"], "sort": ["recent"],
                                            "types": [",".join(family["media_types"])]}
                                         if library_active else {"sort": ["newest"], "types": [",".join(family["media_types"])]})
    ids = [node.attrs["id"] for node in markup.elements if "id" in node.attrs]
    assert len(ids) == len(set(ids))


def test_all_seven_configured_families_reuse_registry_types_not_saved_family_aliases(db):
    _configure(db, "visible")
    model = _model(ADMIN)
    assert {family["key"] for family in model["families"]} == {node.key for node in media_presentation.family_nodes()}
    books = next(family for family in model["families"] if family["key"] == "books")
    query = parse_qs(urlsplit(books["path"]).query)
    assert set(query["types"][0].split(",")) == {"book", "ebook", "audiobook"}
    assert "family" not in query and not {"comic", "manga"} & set(query["types"][0].split(","))


def test_hidden_native_overviews_are_not_reintroduced_as_direct_family_links(db):
    _configure(db, "visible")
    _setting(db, "nav_hidden_tabs", '["music", "periodicals"]')
    model = _model(ADMIN)
    html, _ = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent, model=model)
    markup = _Markup(html)
    assert not [node for node in markup.elements if node.attrs.get("data-nav-family") in {"music", "periodicals"}]
    assert not markup.find("a", href="/music") and not markup.find("a", href="/periodicals")
    for family in model["families"]:
        assert _marked(markup, "data-nav-family-link", family["key"]).attrs["href"] == family["path"]


def test_pathless_family_is_plain_text_with_native_formats_and_never_an_invented_href(db):
    _configure(db, "visible")
    model = _model(ADMIN)
    family = model["families"][0]
    family["path"] = None
    html, _ = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent, model=model)
    markup = _Markup(html)
    branch = _marked(markup, "data-nav-family", family["key"])
    label = _marked(markup, "data-nav-family-label", within=branch)
    assert label.tag == "span" and "href" not in label.attrs and "tabindex" not in label.attrs
    assert not [node for node in markup.find("a", within=branch) if "data-nav-family-link" in node.attrs]
    assert markup.one("summary", within=branch).attrs["aria-labelledby"].split()[0] == label.attrs["id"]
    assert len([node for node in markup.find("a", within=branch) if "data-nav-format" in node.attrs]) == len(family["formats"])


@pytest.mark.parametrize("msgid", ["Books", "Show formats"])
def test_family_and_arrow_names_escape_real_gnu_text_without_metadata_derived_ids(db, monkeypatch, msgid):
    import app.i18n as i18n

    _configure(db, "visible")
    hostile = '\" data-probe="yes"><family-probe>caption</family-probe> & "quoted"'
    catalog = _gnu_catalogue({msgid: hostile})
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: catalog)
    model = _model(ADMIN)
    model["families"][0]["key"] = 'unsafe key " with spaces'
    html, _ = _render(ADMIN, media_presentation.family_nodes(), media_presentation.filter_intent, model=model)
    markup = _Markup(html)
    branch = _marked(markup, "data-nav-family", 'unsafe key " with spaces')
    label = _marked(markup, "data-nav-family-label", within=branch)
    summary = markup.one("summary", within=branch)
    names = [markup.one(id=value) for value in summary.attrs["aria-labelledby"].split()]
    assert label.attrs["id"] == "nav-family-label-1" and names[1].attrs["id"] == "nav-family-formats-1"
    assert (names[0] if msgid == "Books" else names[1]).text == hostile
    assert not markup.find("family-probe") and not any("data-probe" in node.attrs for node in markup.elements)


def test_command_palette_keeps_real_family_context_not_the_arrow_label(db):
    # The finite Node double checks inventory text, not native keyboard/focus.
    _configure(db, "visible")
    _node(render_component(), r"""
open(); query('Books');
const family = nav.querySelector('a[data-nav-family-link="books"]');
const familyResult = options().find(anchor => anchor.getAttribute('href') === family.getAttribute('href'));
assert.ok(familyResult);
assert.equal(familyResult.querySelector('span').textContent, 'Books');
assert.equal(familyResult.querySelector('small').textContent, 'Books');
assert.ok(!familyResult.textContent.includes('All items') && !familyResult.textContent.includes('Show formats'));
query('Book');
const format = nav.querySelector('a[data-nav-format="book"]');
const formatResult = options().find(anchor => anchor.getAttribute('href') === format.getAttribute('href'));
assert.ok(formatResult);
assert.equal(formatResult.querySelector('small').textContent, 'Books');
query('Show formats');
assert.equal(options().filter(anchor => !new URL(anchor.getAttribute('href'), window.location.origin).searchParams.has('q')).length, 0);
""")
