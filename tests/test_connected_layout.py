"""Native disclosures keep primary actions compact without dropping form state."""

from html.parser import HTMLParser

import pytest

from app.services import library_catalog, sources
from tests.conftest import _insert_item


class _Element:
    def __init__(self, tag, attrs, ancestors, position):
        self.tag = tag
        self.attrs = dict(attrs)
        self.ancestors = tuple(ancestors)
        self.position = position
        self.text = ""


class _Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, attrs, self.stack, len(self.elements))
        self.elements.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element.text += data

    def find(self, tag=None, *, within=None, **attrs):
        return [element for element in self.elements
                if (tag is None or element.tag == tag)
                and (within is None or within in element.ancestors)
                and all(element.attrs.get(key) == value for key, value in attrs.items())]

    def one(self, tag=None, *, within=None, **attrs):
        matches = self.find(tag, within=within, **attrs)
        assert len(matches) == 1
        return matches[0]

    def testid(self, value):
        return self.one(**{"data-testid": value})


def _text(element):
    return " ".join(element.text.split())


def _form_values(markup, form):
    """Successful native controls, regardless of their disclosure's open state."""
    values = {}
    for control in markup.find(within=form):
        attrs = control.attrs
        if control.tag not in {"input", "select", "textarea"} or "name" not in attrs:
            continue
        assert "disabled" not in attrs
        if attrs.get("type") in {"checkbox", "radio"} and "checked" not in attrs:
            continue
        if control.tag == "select":
            options = markup.find("option", within=control)
            selected = next((option for option in options if "selected" in option.attrs), options[0])
            values[attrs["name"]] = selected.attrs.get("value", _text(selected))
        else:
            values[attrs["name"]] = control.text if control.tag == "textarea" else attrs.get("value", "")
    return values


def test_library_optional_controls_are_closed_inside_the_search_form(viewer_client):
    response = viewer_client.get("/library")
    assert response.status_code == 200
    markup = _Markup(response.text)
    form = markup.testid("library-filters")
    options = markup.testid("library-filter-options")
    assert form.tag == "form" and form.attrs["method"] == "get" and form.attrs["action"] == "/library"
    assert options.tag == "details" and "open" not in options.attrs and form in options.ancestors
    assert _text(markup.one("summary", within=options)) == "Filters and display"
    search = markup.one("input", within=form, name="q")
    assert options not in search.ancestors
    assert _text(next(button for button in markup.find("button", within=form)
                      if options not in button.ancestors)) == "Search"
    types = markup.one(within=form, name="types")
    assert types.tag == "input" and types.attrs["type"] == "hidden" and types.attrs["value"] == ""
    assert form in types.ancestors and options not in types.ancestors
    assert _form_values(markup, form)["types"] == types.attrs["value"]
    selects = markup.find("select", within=form)
    assert {select.attrs["name"] for select in selects} == library_catalog.FILTER_KEYS - {"q", "types"}
    assert all(options in select.ancestors for select in selects)
    assert _text(markup.one("button", within=options)) == "Apply filters"
    assert _text(markup.one("a", within=options, href="/library?reset=1")) == "Clear filters"
    assert _form_values(markup, form) == library_catalog.DEFAULT_FILTERS
    presets = markup.one("nav", **{"aria-label": "Library presets"})
    assert len(markup.find("a", within=presets)) == len(library_catalog.PRESETS)
    assert not any(ancestor.tag == "details" for ancestor in presets.ancestors)
    assert options.position < markup.testid("library-results").position


@pytest.mark.parametrize("params,count", [
    ({}, 0),
    ({"q": "Visible search", "view": "groups", "sort": "title", "page_size": "24"}, 0),
    ({"family": "book", "sort": "recent", "page_size": "48"}, 3),
])
def test_library_summary_counts_only_nondefault_optional_controls(viewer_client, params, count):
    markup = _Markup(viewer_client.get("/library", params=params).text)
    options = markup.testid("library-filter-options")
    assert "open" not in options.attrs
    badges = markup.find(**{"data-testid": "library-active-filter-count"})
    if count:
        assert len(badges) == 1 and badges[0].attrs["data-count"] == str(count)
        assert options in badges[0].ancestors
    else:
        assert badges == []


def test_collapsed_library_controls_preserve_saved_view_and_explicit_query_overrides(viewer_client, viewer_user, db):
    saved = {"q": "Saved search", "media_type": "ebook", "family": "book", "series": "Saga #1/2",
             "source": "source:999", "availability": "digital", "ownership": "neither", "wishlist": "no",
             "view": "items", "sort": "recent", "page_size": "48"}
    response = viewer_client.post("/api/library/views", data={"name": "Compact view", **saved}, follow_redirects=False)
    assert response.status_code == 303
    view_id = db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0]
    overrides = {"q": "Current 星", "availability": ""}
    response = viewer_client.get("/library", params={"saved_view": view_id, **overrides})
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert "open" not in markup.testid("library-filter-options").attrs
    values = _form_values(markup, markup.testid("library-filters"))
    expected = {**library_catalog.normalize_filters({**saved, **overrides}), "saved_view": str(view_id)}
    assert values == expected
    assert markup.testid("library-active-filter-count").attrs["data-count"] == "9"
    submitted = viewer_client.get("/library", params=values)
    submitted_markup = _Markup(submitted.text)
    assert _form_values(submitted_markup, submitted_markup.testid("library-filters")) == expected


@pytest.mark.parametrize("saved", [False, True])
def test_item_personal_summary_keeps_notes_and_forms_in_separate_closed_disclosures(viewer_client, db, saved):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    url = f"/api/library/items/{item_id}/personal"
    fields = {"status": "in_progress", "progress": "0", "rating": "4", "favourite": "1",
              "on_list": "1", "notes": "private notes stay out of the compact summary"}
    if saved:
        assert viewer_client.post(url, data=fields, follow_redirects=False).status_code == 303
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    markup = _Markup(response.text)
    panel = markup.testid("personal-library")
    editor = markup.testid("personal-library-editor")
    summary = markup.testid("personal-library-summary")
    assert editor.tag == "details" and "open" not in editor.attrs
    assert _text(markup.one("summary", within=editor)) == "Update my activity"
    assert editor not in summary.ancestors
    assert fields["notes"] not in summary.text
    if saved:
        for value in ("In progress", "Progress: 0%", "Rating: 4/5", "Favourite", "My List / Up Next"):
            assert value in _text(summary)
    else:
        assert _text(summary) == "No personal status"
    forms = markup.find("form", within=panel)
    assert len(forms) == (2 if saved else 1)
    assert all(editor in form.ancestors for form in forms)
    values = _form_values(markup, markup.testid("personal-library-form"))
    assert set(values) == ({"_csrf", "status", "progress", "rating", "notes", "favourite", "on_list"}
                           if saved else {"_csrf", "status", "progress", "rating", "notes"})
    if saved:
        assert {key: values[key] for key in fields} == fields
    history_summary = next(element for element in markup.find("summary", within=panel)
                           if _text(element) == "My recent history")
    history = history_summary.ancestors[-1]
    assert history.tag == "details" and "open" not in history.attrs
    assert editor not in history.ancestors and history not in editor.ancestors


@pytest.mark.parametrize("action", ["get", "save", "reset", "error"])
def test_personal_fragments_keep_the_editor_open_for_continued_activity(viewer_client, db, action):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    url = f"/api/library/items/{item_id}/personal"
    assert viewer_client.post(url, data={"notes": "before"}, follow_redirects=False).status_code == 303
    if action == "get":
        response = viewer_client.get(url)
    else:
        response = viewer_client.post(url + ("/reset" if action == "reset" else ""),
                                      data={"progress": "101"} if action == "error" else {"notes": "after"},
                                      headers={"HX-Request": "true"})
    assert response.status_code == (422 if action == "error" else 200)
    assert response.context["personal_editor_open"] is True
    markup = _Markup(response.text)
    assert "open" in markup.testid("personal-library-editor").attrs
    if action == "error":
        assert markup.one("p", role="alert").text.strip()
        # The error itself also opens the editor when no fragment override is set.
        template = response.context["request"].app.state.templates.env.get_template("fragments/personal_library.html")
        html = template.render(**{**response.context, "personal_editor_open": False})
        assert "open" in _Markup(html).testid("personal-library-editor").attrs


def _source(db, *, provider="opds", status="never", enabled=True):
    credentials = {"username": "private-reader", "password": "private-source-secret"} if provider == "opds" else {
        "token": "private-source-secret",
    } if provider == "jellyfin" else {}
    db.execute("BEGIN IMMEDIATE")
    source_id = sources.create_instance(db, provider=provider, name="Configured source", enabled=enabled,
                                        base_url="" if provider == "links" else "https://source.example.test",
                                        credentials=credentials)
    db.execute("UPDATE source_instances SET last_status = ?, last_attempt_at = ?, last_sync_at = ? WHERE id = ?",
               (status, None if status == "never" else "2026-11-12 10:00:00",
                None if status == "never" else "2026-11-11 09:00:00", source_id))
    db.commit()
    return source_id


@pytest.mark.parametrize("provider", ["opds", "jellyfin"])
@pytest.mark.parametrize("status", ["never", "ok", "partial", "error", "syncing"])
def test_source_status_and_actions_precede_configuration_for_every_sync_status(admin_client, db, provider, status):
    source_id = _source(db, provider=provider, status=status)
    response = admin_client.get(f"/sources/{source_id}")
    assert response.status_code == 200
    markup = _Markup(response.text)
    report = markup.testid("source-status")
    configuration = markup.testid("source-configuration")
    assert configuration.tag == "details" and ("open" in configuration.attrs) == (status == "never")
    assert _text(markup.one("summary", within=configuration)) == "Configuration"
    assert report.position < configuration.position and configuration not in report.ancestors
    assert "Last attempt" in report.text and "Last complete success" in report.text
    if status != "never":
        assert "2026-11-12 10:00:00" in report.text and "2026-11-11 09:00:00" in report.text
    for suffix, label in (("test", "Test connection"), ("sync", "Sync now")):
        form = markup.one("form", action=f"/api/sources/{source_id}/{suffix}")
        assert form.attrs["method"] == "post" and report in form.ancestors
        assert configuration not in form.ancestors
        assert _text(markup.one("button", within=form)) == label
    settings = markup.one("form", within=configuration, action=f"/api/sources/{source_id}/settings")
    values = _form_values(markup, settings)
    assert {"_csrf", "name", "base_url", "browser_url", "options_json", "enabled"} <= values.keys()
    for credential in (("username", "password") if provider == "opds" else ("token",)):
        assert values[credential] == ""
    assert "Leave all credential fields blank to keep them" in configuration.text
    assert "private-source-secret" not in response.text and "private-reader" not in response.text


@pytest.mark.parametrize("role_fixture", ["viewer_client", "editor_client", "admin_client"])
def test_source_configuration_and_creation_controls_remain_admin_only(request, db, role_fixture):
    client = request.getfixturevalue(role_fixture)
    source_id = _source(db, status="ok")
    detail = client.get(f"/sources/{source_id}", follow_redirects=False)
    overview = client.get("/sources")
    assert overview.status_code == 200
    markup = _Markup(overview.text)
    creation_forms = markup.find("form", action="/api/sources")
    if role_fixture == "admin_client":
        assert detail.status_code == 200 and _Markup(detail.text).testid("source-configuration")
        assert len(creation_forms) == 1
        assert not any(ancestor.tag == "details" for ancestor in creation_forms[0].ancestors)
    else:
        assert detail.status_code == 303 and detail.headers["location"] == "/browse"
        assert creation_forms == []
        assert "source.example.test" not in overview.text
        assert not markup.find("a", href=f"/sources/{source_id}")


def test_new_source_creation_opens_configuration_for_initial_settings(admin_client):
    created = admin_client.post("/api/sources", data={"provider": "opds", "name": "New source",
                                                     "base_url": "https://source.example.test", "enabled": "1"},
                                follow_redirects=False)
    assert created.status_code == 303
    page = admin_client.get(created.headers["location"])
    assert page.status_code == 200
    markup = _Markup(page.text)
    configuration = markup.testid("source-configuration")
    assert "open" in configuration.attrs
    settings = markup.one("form", within=configuration)
    assert _form_values(markup, settings)["name"] == "New source"
    assert _text(markup.one("button", within=settings)) == "Save settings"


def test_manual_source_keeps_configuration_open_without_fake_test_or_sync(admin_client, db):
    source_id = _source(db, provider="links", status="ok")
    item_id = _insert_item(db, isbn=None)
    record_id = sources.attach_record(db, source_id, item_id=item_id, external_id="public:opaque/1",
                                      open_url="https://public.example.test/record/1")
    db.commit()
    response = admin_client.get(f"/sources/{source_id}")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert "open" in markup.testid("source-configuration").attrs
    for suffix in ("test", "sync"):
        assert not markup.find("form", action=f"/api/sources/{source_id}/{suffix}")
    assert "Manual links only" in markup.testid("source-status").text
    assert markup.one("form", action=f"/api/sources/{source_id}/records")
    assert markup.one("form", action=f"/api/sources/{source_id}/records/{record_id}/detach")
    assert "External record ID: public:opaque/1" in _text(markup.one("article"))
    link = markup.one("a", href="https://public.example.test/record/1")
    assert link.attrs["target"] == "_blank" and link.attrs["rel"] == "noopener noreferrer"
