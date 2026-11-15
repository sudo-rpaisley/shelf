"""Additive phone-wrap-v1 HTTP/template boundaries, not runtime qualification.

The standalone cases render the real results template in the existing strict
GNU/Jinja harness. Only fixture data and the explicit presentation seam vary.
"""

from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

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
GEOMETRY = "Smaller items fit more per row when space allows. Text stays readable; results per page are unchanged."
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
    assert panel.attrs["role"] == "group" and panel.attrs["aria-label"] == "Item size"
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


def _assert_palette_live_status(markup):
    root = markup.testid("library-page")
    palettes = [node for node in markup.elements if "data-command-palette" in node.attrs]
    assert len(palettes) == 1, "exactly one authenticated palette owner"
    statuses = markup.find("p", within=palettes[0], role="status")
    assert len(statuses) == 1, "exactly one palette live status"
    status = statuses[0]
    marked = [node for node in markup.elements if "data-command-palette-status" in node.attrs]
    assert marked == [status], "exactly one palette live status marker on its native owner"
    assert status.tag == "p" and palettes[0] in status.ancestors
    assert root not in status.ancestors, "palette live status is outside Library"
    assert {key: status.attrs.get(key) for key in ("role", "aria-live", "aria-atomic")} == {
        "role": "status", "aria-live": "polite", "aria-atomic": "true",
    }
    return status


def _assert_library_notice(markup):
    root = markup.testid("library-page")
    help_node = _help(markup)
    notices = [node for node in markup.find("p", within=root, role="status")
               if node is not markup.testid("library-counts")]
    assert len(notices) == 1, "Library owns exactly one notice"
    notice = notices[0]
    assert _text(notice) == "Choose a view name between 1 and 80 characters.", "Library notice text"
    assert help_node not in notice.ancestors, "Library notice stays outside Help"
    return notice


@pytest.fixture
def library_notice_document(viewer_client):
    assert viewer_client.post("/api/library/views", data={"name": ""}, follow_redirects=False).status_code == 303
    response = viewer_client.get("/library?q=unmatched-query")
    assert response.status_code == 200
    return response.text


def test_library_notice_oracle_ignores_unrelated_external_status_and_preserves_palette(library_notice_document):
    html = library_notice_document
    assert html.count("<main ") == 1
    modified = html.replace("<main ", '<p role="status" aria-live="polite">Unrelated external status</p><main ', 1)
    markup = _Markup(modified)
    _assert_palette_live_status(markup)
    _assert_library_notice(markup)
    external = [node for node in markup.find("p", role="status") if _text(node) == "Unrelated external status"]
    assert len(external) == 1 and markup.testid("library-page") not in external[0].ancestors


@pytest.mark.parametrize("mutation", (
    "missing-library-notice", "duplicate-library-notice", "empty-extra-library-notice",
    "notice-inside-help", "removed-palette-status",
))
def test_notice_ownership_oracle_rejects_real_document_mutations(library_notice_document, mutation):
    html = library_notice_document
    markup = _Markup(html)
    root, counts = markup.testid("library-page"), markup.testid("library-counts")
    # Bind the mutation by native ownership/role, NOT by an expected notice string.
    spans = list(re.finditer(r'<p\b(?=[^>]*\brole="status")[^>]*>.*?</p>', html, re.S))
    nodes = markup.find("p", role="status")
    assert len(spans) == len(nodes)
    owned = [span for node, span in zip(nodes, spans, strict=True) if root in node.ancestors and node is not counts]
    assert len(owned) == 1
    span = owned[0]
    paragraph = span.group()
    if mutation == "removed-palette-status":
        modified, count = re.subn(r'<p\b[^>]*\bdata-command-palette-status\b[^>]*>.*?</p>', '', html, count=1, flags=re.S)
        assert count == 1
        mutant = _Markup(modified)
        _assert_library_notice(mutant)
        with pytest.raises(AssertionError, match="exactly one palette live status"):
            _assert_palette_live_status(mutant)
        return
    replacement = {"missing-library-notice": "", "duplicate-library-notice": paragraph * 2,
                   "empty-extra-library-notice": paragraph + '<p role="status"></p>',
                   "notice-inside-help": ""}[mutation]
    modified = html[:span.start()] + replacement + html[span.end():]
    if mutation == "notice-inside-help":
        help_summary = re.search(r'<details\b[^>]*data-library-help="phone-wrap-v1"[^>]*>.*?</summary>', modified, re.S)
        assert help_summary
        modified = modified[:help_summary.end()] + paragraph + modified[help_summary.end():]
    mutant = _Markup(modified)
    _assert_palette_live_status(mutant)
    message = "Library notice stays outside Help" if mutation == "notice-inside-help" else "Library owns exactly one notice"
    with pytest.raises(AssertionError, match=message):
        _assert_library_notice(mutant)


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
    _assert_palette_live_status(empty)
    _assert_library_notice(empty)
    assert help_node not in empty.one("h2", within=empty.testid("library-results")).ancestors
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


def _assert_standalone_series_explanations(markup):
    results = markup.testid("library-results")
    assert results in _paragraph(markup, SERIES).ancestors
    assert results in _paragraph(markup, AVAILABILITY).ancestors
    forbidden = {"data-library-presentation", "data-library-member-presentation", "data-library-help",
                 "data-cover-size-panel", "data-cover-size-control"}
    assert not any(forbidden & node.attrs.keys() for node in markup.elements)
    assert not markup.find(id="library-cover-size-description")


def test_standalone_series_index_retains_one_original_explanation_and_native_bindings(viewer_client, db):
    series_name = "A 星 #1/2"
    later = _item(db, "Later <edition>", series_name=series_name, series_position=2, authors="Raw creator & more")
    earlier = _item(db, "Earlier & complete", series_name=series_name, series_position=0.5)
    _item(db, "Another local series", media_type="ebook", series_name="Zeta / other")
    db.commit()
    domain = _snapshot(db)
    before = viewer_client.get("/api/library/items", params={"view": "series"}).json()
    response = viewer_client.get("/api/library/search?view=series")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<!DOCTYPE" not in response.text
    markup = _Markup(response.text)
    _assert_standalone_series_explanations(markup)
    assert _text(markup.testid("library-counts")) == "3 matching items in 3 groups"
    nodes = [node for node in markup.elements if "data-library-series" in node.attrs]
    rows = response.context["series_results"]
    assert len(nodes) == len(rows) == len(before["series_results"]) == 2
    for node, row, native in zip(nodes, rows, before["series_results"], strict=True):
        assert node.attrs["data-library-series"] == row["key"] == native["key"]
        assert _text(markup.one("h3", within=node)) == row["name"] == native["name"]
        assert row["representative"] == native["representative"]
        assert (row["item_count"], row["group_count"]) == (native["item_count"], native["group_count"])
        link = markup.one("a", within=node)
        assert link.attrs["href"] == row["url"]
        query = parse_qs(urlsplit(link.attrs["href"]).query)
        assert query["series"] == [row["key"]] and query["view"] == ["series"]
    drilldown = viewer_client.get("/api/library/search", params={"view": "series", "series": series_name})
    assert drilldown.status_code == 200
    drill = _Markup(drilldown.text)
    members = [node for node in drill.elements if "data-library-member" in node.attrs]
    assert [node.attrs["data-library-member"] for node in members] == [str(earlier), str(later)]
    for member, item_id, title in zip(members, (earlier, later), ("Earlier & complete", "Later <edition>"), strict=True):
        assert "data-library-member-presentation" not in member.attrs
        link = drill.one("a", within=member, href=f"/item/{item_id}?from=library")
        assert _text(drill.one("h3", within=link)) == title
    _paragraph(drill, AVAILABILITY)
    assert not any("data-cover-size-control" in node.attrs or "data-library-help" in node.attrs for node in drill.elements)
    assert viewer_client.get("/api/library/items", params={"view": "series"}).json() == before
    assert _snapshot(db) == domain


@pytest.mark.parametrize("mode,hosted", (
    pytest.param(ABSENT, ABSENT, id="both-undefined"),
    pytest.param(ABSENT, True, id="undefined-mode-host-true"),
    pytest.param("unrecognized-mode", True, id="unknown-mode-host-true"),
    pytest.param(MODE, ABSENT, id="undefined-host"),
    pytest.param(MODE, False, id="host-false"),
    pytest.param(MODE, 1, id="host-integer-not-literal-true"),
    pytest.param(MODE, "true", id="host-string-not-literal-true"),
))
def test_strict_series_fragment_retains_explanations_without_literal_owned_host(viewer_client, db, mode, hosted):
    _item(db, "Strict series representative", series_name="Strict 星 #1/2")
    db.commit()
    response = viewer_client.get("/api/library/search?view=series")
    assert response.status_code == 200
    context = dict(response.context)
    for key in ("library_presentation_mode", "library_explanations_hosted"):
        context.pop(key, None)
    if mode is not ABSENT:
        context["library_presentation_mode"] = mode
    if hosted is not ABSENT:
        context["library_explanations_hosted"] = hosted
    native = deepcopy({key: context[key] for key in ("series_results", "groups", "items", "filters")})
    env, _ = _environment()
    env.globals.update(build_media_card=media_cards.build_media_card, creator_label=config.creator_label)
    markup = _Markup(env.get_template("fragments/library_results.html").render(context))
    _assert_standalone_series_explanations(markup)
    assert {key: context[key] for key in native} == native
    assert _text(markup.testid("library-counts")) == "1 matching item in 1 group"
