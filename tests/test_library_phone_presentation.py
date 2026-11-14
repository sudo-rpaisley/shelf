"""Additive phone-wrap-v1 HTTP/template boundaries, not runtime qualification.

The standalone cases render the real results template in the existing strict
GNU/Jinja harness. Only fixture data and the explicit presentation seam vary.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from app import config, features
from app.services import library_catalog, media_cards
from tests.test_connected_layout import _Markup, _form_values, _text
from tests.test_cover_size_integration import _marked
from tests.test_library_catalog import _copy, _item, _link, _record, _source
from tests.test_media_cards import _environment


ROOT = Path(__file__).resolve().parents[1]
MODE = "phone-wrap-v1"
STYLE = "/static/css/library-presentation.source.css"
INTRO = "Your local catalogue, physical copies and synced digital holdings in one place."
GEOMETRY = "Percentages request a card size, not more cards per row. Small cards stop shrinking to keep text readable."
AVAILABILITY = ("Availability uses local copies and cached source records, not a live connection check. "
                "All filters must match the same item; source and availability must match the same holding.")
GROUPING = "Groups use explicit format links only. Every live edition is shown, including editions that do not match your filters."
SERIES = "Series are grouped by their local names, preserving punctuation and scripts. No remote series lookup is needed."
ABSENT = object()


def _paragraph(markup, text):
    nodes = [node for node in markup.find("p") if _text(node) == text]
    assert len(nodes) == 1, (text, len(nodes))
    return nodes[0]


def _help(markup):
    help_node = markup.one("details", **{"data-library-help": MODE})
    assert "open" not in help_node.attrs
    summary = markup.one("summary", within=help_node)
    assert summary.ancestors[-1] is help_node and _text(summary)
    assert "role" not in summary.attrs and "tabindex" not in summary.attrs
    assert summary.position < markup.testid("library-filter-options").position
    assert summary.position < markup.testid("library-saved-views").position
    return help_node


def _snapshot(db):
    tables = ("items", "item_copies", "locations", "item_links", "source_instances", "source_records",
              "settings", "library_saved_views", "list_items", "user_item_state", "user_item_events")
    return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in tables}


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
@pytest.mark.parametrize("view", ("items", "groups"))
def test_full_page_owns_one_native_panel_help_and_unchanged_successful_controls(request, db, client_name, view):
    client = request.getfixturevalue(client_name)
    item_id = _item(db, "A local title", series_name="A local series")
    _copy(db, item_id)
    db.commit()
    before = _snapshot(db)
    response = client.get("/library", params={"reset": "1", "view": view, "sort": "recent"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    root = markup.testid("library-page")
    assert root.attrs.get("data-library-presentation") == MODE
    member = markup.one(**{"data-library-member": str(item_id)})
    assert member.attrs.get("data-library-member-presentation") == MODE
    help_node = _help(markup)
    panel = _marked(markup, "data-cover-size-panel")
    assert panel.attrs["role"] == "group" and panel.attrs["aria-label"] == "Cover size"
    assert root in panel.ancestors and panel in help_node.ancestors
    geometry = _paragraph(markup, GEOMETRY)
    assert geometry.attrs["id"] == panel.attrs["aria-describedby"] == "library-cover-size-description"
    assert panel in geometry.ancestors and help_node in geometry.ancestors
    assert len(markup.find(id=geometry.attrs["id"])) == 1
    assert help_node in _paragraph(markup, INTRO).ancestors
    assert help_node in _paragraph(markup, AVAILABILITY).ancestors
    if view == "groups":
        assert help_node in _paragraph(markup, GROUPING).ancestors
    else:
        assert not [node for node in markup.find("p") if _text(node) == GROUPING]
    control = _marked(markup, "data-cover-size-control")
    assert panel in control.ancestors and help_node not in control.ancestors
    assert not any(node.tag in {"details", "form", "article"} for node in control.ancestors)
    slider = _marked(markup, "data-cover-size-input", tag="input", within=control)
    assert slider.ancestors[-1].tag == "label"
    assert tuple(slider.attrs[key] for key in ("type", "min", "max", "step", "value")) == (
        "range", "50", "100", "10", "100",
    )
    assert "disabled" in slider.attrs and "name" not in slider.attrs
    output = _marked(markup, "data-cover-size-output", within=control)
    assert output.tag == "output" and _text(output) == "100%"
    assert _paragraph(markup, "This page only; not saved.") in markup.find("p", within=control)
    buttons = markup.find("button", within=control)
    assert [_text(button) for button in buttons] == ["Smaller", "Larger", "Reset"]
    assert all(button.attrs["type"] == "button" and "disabled" in button.attrs for button in buttons)
    presets = markup.one("nav", **{"aria-label": "Library presets"})
    links = markup.find("a", within=presets)
    assert len(links) == len(library_catalog.PRESETS) == 6
    assert [link.attrs["href"] for link in links] == [library_catalog.library_url(values)
                                                    for _, values in library_catalog.PRESETS]
    connected = markup.one("nav", **{"aria-label": "Connected library"})
    assert [link.attrs["href"] for link in markup.find("a", within=connected)] == (
        ["/sources", "/activity"] + (["/needs-attention"] if client_name != "viewer_client" else [])
    )
    form = markup.testid("library-filters")
    assert form.attrs["method"] == "get" and form.attrs["action"] == "/library"
    values = _form_values(markup, form)
    assert values == library_catalog.normalize_filters({"view": view, "sort": "recent"})
    for node in (presets, connected, markup.one("input", within=form, name="q"),
                 markup.one("label", within=form, **{"for": "library-q"}),
                 markup.testid("library-counts")):
        assert help_node not in node.ancestors
    assert _text(markup.testid("library-counts")) == "1 matching item in 1 group"
    assert "1 physical copy" in member.text and not markup.find("details", within=member)
    detail = markup.one("a", within=member, href=f"/item/{item_id}?from=library")
    assert _text(markup.one("h3", within=detail)) == "A local title"
    assert not markup.find("a", within=member, href=f"/item/{item_id}/edit")
    submitted = client.get("/library", params=values)
    assert submitted.status_code == 200
    submitted_markup = _Markup(submitted.text)
    assert _form_values(submitted_markup, submitted_markup.testid("library-filters")) == values
    assert _snapshot(db) == before


@pytest.mark.parametrize("drilldown", (False, True), ids=("index", "card-containing-drilldown"))
def test_series_has_native_help_but_never_advertises_cover_geometry(viewer_client, db, drilldown):
    later = _item(db, "Later", series_name="星 #1/2", series_position=2)
    earlier = _item(db, "Earlier", series_name="星 #1/2", series_position=0.5)
    db.commit()
    params = {"reset": "1", "view": "series", **({"series": "星 #1/2"} if drilldown else {})}
    response = viewer_client.get("/library", params=params)
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.testid("library-page").attrs.get("data-library-presentation") == MODE
    help_node = _help(markup)
    assert help_node in _paragraph(markup, INTRO).ancestors
    assert help_node in _paragraph(markup, AVAILABILITY).ancestors
    assert not any("data-cover-size-panel" in node.attrs or "data-cover-size-control" in node.attrs
                   for node in markup.elements)
    assert not markup.find(id="library-cover-size-description") and GEOMETRY not in help_node.text
    if drilldown:
        assert [node.attrs["data-library-member"] for node in markup.find()
                if "data-library-member" in node.attrs] == [str(earlier), str(later)]
        assert all(node.attrs.get("data-library-member-presentation") == MODE for node in markup.find()
                   if "data-library-member" in node.attrs)
    else:
        assert help_node in _paragraph(markup, SERIES).ancestors
        assert markup.one(**{"data-library-series": "星 #1/2"})


@pytest.mark.parametrize("mode,hosted", (
    pytest.param(ABSENT, ABSENT, id="both-undefined"),
    pytest.param(ABSENT, True, id="mode-undefined-host-true"),
    pytest.param(None, True, id="none-mode"),
    pytest.param("", True, id="empty-mode"),
    pytest.param("future-mode", True, id="unknown-mode"),
    pytest.param(MODE + " ", True, id="nonexact-mode"),
    pytest.param(MODE, ABSENT, id="host-undefined"),
    pytest.param(MODE, False, id="host-false"),
    pytest.param(MODE, None, id="host-none"),
    pytest.param(MODE, 1, id="host-integer-not-literal-true"),
    pytest.param(MODE, "true", id="host-string-not-literal-true"),
    pytest.param(MODE, True, id="explicit-hosted-mode"),
))
def test_strict_standalone_fragment_marks_only_exact_mode_and_suppresses_only_literal_host_flag(
    viewer_client, db, mode, hosted,
):
    item_id = _item(db, "Strict standalone")
    db.commit()
    response = viewer_client.get("/api/library/search")
    assert response.status_code == 200
    context = dict(response.context)
    for key in ("library_presentation_mode", "library_explanations_hosted"):
        context.pop(key, None)
    if mode is not ABSENT:
        context["library_presentation_mode"] = mode
    if hosted is not ABSENT:
        context["library_explanations_hosted"] = hosted
    catalogue = deepcopy({key: context[key] for key in ("items", "groups", "series_results", "filters")})
    env, _ = _environment()
    env.globals.update(build_media_card=media_cards.build_media_card, creator_label=config.creator_label)
    # A coherent producer fixture hosts its single copy only for the authorized
    # pair. It does not pretend that an old whole renderer understands this flag.
    wrapper = env.from_string('''<div data-library-presentation="phone-wrap-v1">
        {% if library_presentation_mode|default('') == 'phone-wrap-v1'
              and library_explanations_hosted|default(false) is sameas true %}
        <details><summary>Library help fixture</summary><p>{{ availability }}</p><p>{{ grouping }}</p></details>
        {% endif %}{% include "fragments/library_results.html" %}</div>''')
    markup = _Markup(wrapper.render(context, availability=AVAILABILITY, grouping=GROUPING))
    member = markup.one(**{"data-library-member": str(item_id)})
    assert member.attrs.get("data-library-member-presentation") == (MODE if mode == MODE else None)
    for text in (AVAILABILITY, GROUPING):
        paragraph = _paragraph(markup, text)
        assert (markup.testid("library-results") not in paragraph.ancestors) is (mode == MODE and hosted is True)
    assert {key: context[key] for key in catalogue} == catalogue
    assert _text(markup.testid("library-counts")) == "1 matching item in 1 group"


def test_unversioned_http_fragment_and_shared_component_keep_the_legacy_contract(viewer_client, db):
    item_id = _item(db, "Legacy standalone")
    db.commit()
    response = viewer_client.get("/api/library/search", params={
        "library_presentation_mode": MODE, "library_explanations_hosted": "true", "cover_size": "50",
    })
    assert response.status_code == 200 and "<!DOCTYPE" not in response.text
    markup = _Markup(response.text)
    assert "data-library-member-presentation" not in markup.one(**{"data-library-member": str(item_id)}).attrs
    _paragraph(markup, AVAILABILITY)
    _paragraph(markup, GROUPING)
    assert not markup.find(**{"data-library-presentation": MODE})
    env, _ = _environment()
    card = media_cards.build_media_card({"id": item_id, "title": "Legacy standalone", "media_type": "book"})
    template = env.get_template("components/media_card.html")
    assert template.render(card=card) == template.render(
        card=card, library_presentation_mode=MODE, library_explanations_hosted=True,
    )


def test_singleton_library_only_asset_is_real_and_not_a_new_precached_private_surface(viewer_client):
    markup = _Markup(viewer_client.get("/library").text)
    style = markup.one("link", rel="stylesheet", href=STYLE)
    assert markup.one("head") in style.ancestors
    assert style.position > markup.one("link", rel="stylesheet", href="/static/css/app.css").position
    response = viewer_client.get(STYLE)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    assert 'data-library-presentation' in response.text and 'data-library-member-presentation' in response.text
    source = (ROOT / "static/css/input.css").read_text()
    assert source.count('@import "./library-presentation.source.css";') == 1
    assert source.index('@import "./library-presentation.source.css";') > source.index('@import "./cover-size.source.css";')
    assert STYLE not in (ROOT / "static/sw.js").read_text()
    for path in ("/", "/music", "/browse"):
        assert not _Markup(viewer_client.get(path).text).find("link", href=STYLE)


def test_counts_notices_empty_and_member_availability_do_not_move_into_help(viewer_client, db):
    item_id = _item(db, "Unrecorded holdings")
    db.commit()
    response = viewer_client.get("/library?view=items")
    markup = _Markup(response.text)
    help_node = _help(markup)
    member = markup.one(**{"data-library-member": str(item_id)})
    availability = _paragraph(markup, "No recorded holdings · availability unknown")
    assert member in availability.ancestors and help_node not in availability.ancestors
    assert help_node not in markup.testid("library-counts").ancestors
    assert viewer_client.post("/api/library/views", data={"name": ""}, follow_redirects=False).status_code == 303
    empty = _Markup(viewer_client.get("/library?q=unmatched-query").text)
    help_node = _help(empty)
    assert _text(empty.testid("library-counts")) == "0 matching items in 0 groups"
    notices = [node for node in empty.find("p", role="status")
               if node is not empty.testid("library-counts")]
    assert len(notices) == 1
    notice = notices[0]
    assert _text(notice) == "Choose a view name between 1 and 80 characters."
    assert help_node not in notice.ancestors
    assert help_node not in empty.one("h2").ancestors
    assert help_node not in _paragraph(empty, "Try fewer filters or sync a source. Only locally catalogued, live items are searched.").ancestors


def test_group_members_metadata_and_json_remain_complete_without_new_edit_authority(viewer_client, db):
    paper = _item(db, "Raw <title> 星", subtitle="Raw subtitle & more", authors="Raw creator",
                  series_name="Saga #1/2", series_position=0.5)
    digital = _item(db, "Other edition", media_type="ebook")
    _link(db, paper, digital)
    _copy(db, paper)
    _record(db, _source(db), digital)
    db.commit()
    params = {"availability": "physical"}
    before = viewer_client.get("/api/library/items", params=params).json()
    domain = _snapshot(db)
    response = viewer_client.get("/library", params=params)
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.testid("library-page").attrs.get("data-library-presentation") == MODE
    group = markup.one(**{"data-library-group": str(paper)})
    assert group.attrs["data-member-ids"] == f"{paper},{digital}" and group.attrs["data-matching-ids"] == str(paper)
    for item_id, matches in ((paper, "true"), (digital, "false")):
        member = markup.one(**{"data-library-member": str(item_id)})
        assert member.attrs.get("data-library-member-presentation") == MODE
        assert member.attrs["data-matches"] == matches
        assert len(markup.find("h3", within=member)) == 1 and not markup.find("details", within=member)
        assert not markup.find("a", within=member, href=f"/item/{item_id}/edit")
    text = _text(markup.one(**{"data-library-member": str(paper)}))
    assert text.index(f"#{paper}") < text.index("Saga #1/2 · 0.5") < text.index("Matches filters")
    assert "Raw <title> 星" in text and "Raw subtitle & more" in text and "Raw creator" in text
    assert viewer_client.get("/api/library/items", params=params).json() == before
    assert not {"library_presentation_mode", "library_explanations_hosted", "prepared_library_artwork"} & before.keys()
    assert _snapshot(db) == domain


def test_feature_off_and_anonymous_do_not_gain_the_presentation_surface(viewer_client, db):
    features.set_feature_enabled(db, "connected_library", False)
    db.commit()
    response = viewer_client.get("/library")
    assert response.status_code == 200 and "Connected library is turned off" in response.text
    assert not _Markup(response.text).find(**{"data-library-presentation": MODE})
    assert not _Markup(response.text).find("link", href=STYLE)
    assert viewer_client.get("/api/library/search").status_code == 403
    viewer_client.cookies.clear()
    response = viewer_client.get("/library", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
