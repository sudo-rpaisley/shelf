"""Stable native caption and one genuine status, without changing filter state."""

import pytest

from app.services import library_catalog
from tests.test_connected_layout import _Markup, _form_values, _text


def _assert_caption(markup, count):
    details = markup.testid("library-filter-options")
    assert details.tag == "details" and "open" not in details.attrs
    assert details.attrs["data-library-filter-layout"] == "caption-status-v1"
    summary = markup.one("summary", within=details)
    assert summary.ancestors[-1] is details
    assert _text(summary) == "Filters and display" and "role" not in summary.attrs
    body = markup.one("div", within=details, **{"class": "library-filter-options__body"})
    assert body.ancestors[-1] is details
    badges = markup.find(**{"data-testid": "library-active-filter-count"})
    if count:
        assert len(badges) == 1
        badge = badges[0]
        assert badge.ancestors[-1] is details and summary not in badge.ancestors
        assert summary.position < badge.position < body.position
        assert badge.attrs["data-count"] == str(count)
        assert summary.attrs["aria-describedby"] == badge.attrs["id"]
        assert len(markup.find(id=badge.attrs["id"])) == 1
        assert _text(badge) == f"· {count} active option" + ("s" if count != 1 else "")
        assert not {"aria-hidden", "hidden", "role", "tabindex"} & badge.attrs.keys()
    else:
        assert badges == [] and "aria-describedby" not in summary.attrs
    assert all(body in control.ancestors for control in markup.find("select", within=details))
    assert body in markup.one("button", within=details).ancestors
    assert body in markup.one("a", within=details, href="/library?reset=1").ancestors
    form = markup.testid("library-filters")
    assert details not in markup.one("input", within=form, name="q").ancestors
    assert details not in markup.one("input", within=form, name="types").ancestors
    assert details not in markup.testid("library-saved-views").ancestors
    return _form_values(markup, form)


@pytest.mark.parametrize("params,count", (
    pytest.param({}, 0, id="default"),
    pytest.param({"q": "Current 星"}, 0, id="query-only"),
    pytest.param({"family": "book"}, 1, id="singular"),
    pytest.param({"reset": "1", "view": "items", "sort": "recent"}, 2, id="canonical"),
    pytest.param({"family": "book", "sort": "recent", "page_size": "48"}, 3, id="three"),
))
def test_filter_caption_and_one_real_count_remain_distinct(viewer_client, params, count):
    response = viewer_client.get("/library", params=params)
    assert response.status_code == 200
    assert _assert_caption(_Markup(response.text), count) == library_catalog.normalize_filters(params)
    if count:
        assert f'data-testid="library-active-filter-count" data-count="{count}"' in response.text


@pytest.mark.parametrize("client_name", ("viewer_client", "editor_client", "admin_client"))
def test_canonical_status_does_not_change_the_authorized_form_or_native_submission(request, client_name):
    client = request.getfixturevalue(client_name)
    response = client.get("/library?reset=1&view=items&sort=recent")
    values = _assert_caption(_Markup(response.text), 2)
    submitted = client.get("/library", params=values)
    assert submitted.status_code == 200
    assert _assert_caption(_Markup(submitted.text), 2) == values


def test_saved_view_nine_options_and_explicit_clears_keep_every_successful_value(viewer_client, viewer_user, db):
    saved = {"q": "Saved search", "media_type": "ebook", "family": "book", "series": "Saga #1/2",
             "source": "source:999", "availability": "digital", "ownership": "neither", "wishlist": "no",
             "view": "items", "sort": "recent", "page_size": "48"}
    assert viewer_client.post("/api/library/views", data={"name": "Caption preservation", **saved}, follow_redirects=False).status_code == 303
    view_id = db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0]
    overrides = {"q": "Current 星", "availability": ""}
    response = viewer_client.get("/library", params={"saved_view": view_id, **overrides})
    assert response.status_code == 200
    values = _assert_caption(_Markup(response.text), 9)
    assert values == {**library_catalog.normalize_filters({**saved, **overrides}), "saved_view": str(view_id)}
    assert _assert_caption(_Markup(viewer_client.get("/library", params=values).text), 9) == values
