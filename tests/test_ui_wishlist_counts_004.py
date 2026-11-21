"""Home links and cross-filter totals distinguish personal/shared wishlists."""

import pytest

from tests.conftest import _insert_item
from tests.test_ui_phase4_contracts_20261121 import _Element, _Markup


def _ownership_options(response, path):
    assert response.status_code == 200
    root = _Markup(response.text).root
    select = root.one("select", id="owned-filter")
    assert ("hx-swap-oob" in select.attrs) == (path == "/api/search")
    options = select.find_all("option")
    assert len(options) == 4
    return root, {option.attrs["value"]: " ".join(option.text.split()) for option in options}


@pytest.mark.parametrize("lending", [False, True])
def test_home_personal_wishlist_link_matches_its_members_and_keeps_shared_summary(
    viewer_client, viewer_user, editor_user, db, lending,
):
    from app.features import set_feature_enabled
    from app.services import libraries
    from app.services.home_dashboard import dashboard_summary

    set_feature_enabled(db, "lending", lending)
    _insert_item(db, title="Only the acquisition list", isbn=None, owned=0, wishlisted=True)
    owned_wish = _insert_item(
        db, title="My owned personal wish", isbn=None, media_type="dvd", owned=1,
        personal_user_id=viewer_user["id"], personal_state={"wishlist": 1},
    )
    unowned_wish = _insert_item(
        db, title="My unowned personal wish", isbn=None, owned=0,
        personal_user_id=viewer_user["id"], personal_state={"wishlist": 1},
    )
    _insert_item(db, title="Only another person's wish", isbn=None, owned=0,
                 personal_user_id=editor_user["id"], personal_state={"wishlist": 1})
    private = libraries.create_library(db, "Not in my wishlist census")
    _insert_item(db, title="Hidden personal wish", isbn=None, owned=0, _library_id=private["id"],
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Trashed personal wish", isbn=None, owned=0, deleted_at="2026-01-01",
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    db.commit()
    assert dashboard_summary(db)["wishlist_count"] == 1

    response = viewer_client.get("/")
    assert response.status_code == 200
    root = _Markup(response.text).root
    summary = root.one(**{"data-testid": "home-summary"})
    assert len([child for child in summary.children if isinstance(child, _Element)]) == (6 if lending else 5)
    links = root.find_all("a", href="/browse?owned=0")
    assert len(links) == 1
    link = links[0]
    assert link.within(summary)
    assert [paragraph.text for paragraph in link.find_all("p")] == ["My wishlist", "2"]
    shared = [paragraph for paragraph in root.find_all("p") if paragraph.text == "Acquisition wishlist: 1"]
    assert len(shared) == 1
    assert not shared[0].within(summary)
    ancestor = shared[0]
    while ancestor is not None:
        assert ancestor.tag != "a"
        assert "hidden" not in ancestor.attrs and "x-cloak" not in ancestor.attrs
        assert not {"hidden", "sr-only"}.intersection(ancestor.attrs.get("class", "").split())
        assert "display:none" not in ancestor.attrs.get("style", "").replace(" ", "")
        ancestor = ancestor.parent

    destination = viewer_client.get(link.attrs["href"])
    assert destination.status_code == 200
    grid = _Markup(destination.text).root.one("template", **{"x-if": "viewMode === 'grid'"}).one(
        "div", **{"data-testid": "item-grid"},
    )
    cards = grid.find_all("a", **{"data-item-id": None})
    assert len(cards) == 2
    assert sorted(int(card.attrs["data-item-id"]) for card in cards) == sorted([owned_wish, unowned_wish])
    for excluded in ("Only the acquisition list", "Only another person's wish", "Hidden personal wish", "Trashed personal wish"):
        assert excluded not in destination.text
    stats = viewer_client.get("/api/v1/stats")
    assert stats.status_code == 200
    assert stats.json()["items"]["wishlist"] == 1
    assert dashboard_summary(db)["wishlist_count"] == 1


@pytest.mark.parametrize("path", ["/browse", "/api/search"])
@pytest.mark.parametrize("owned", ["", "1", "0", "none"])
def test_an_owned_personal_wish_counts_once_in_ownership_all(viewer_client, viewer_user, db, path, owned):
    _insert_item(db, title="Owned and personally wanted", isbn=None, owned=1,
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    db.commit()
    response = viewer_client.get(path, params={"owned": owned})
    root, options = _ownership_options(response, path)
    assert options == {"": "All (1)", "1": "Owned (1)", "0": "Wishlist (1)", "none": "Not owned or wishlisted (0)"}
    assert root.one("span", id="collection-count").text == ("(0)" if owned == "none" else "(1)")


@pytest.mark.parametrize("path", ["/browse", "/api/search"])
@pytest.mark.parametrize("media_type,owned,language,expected,filtered", [
    ("book", "", "eng", (4, 2, 1, 2), 4),
    ("book", "1", "eng", (4, 2, 1, 2), 2),
    ("book", "0", "eng", (4, 2, 1, 2), 1),
    ("book", "none", "eng", (4, 2, 1, 2), 2),
    ("dvd", "0", "eng", (1, 0, 1, 0), 1),
    ("dvd", "1", "eng", (1, 0, 1, 0), 0),
    ("book", "none", "fra", (1, 0, 0, 1), 1),
    ("magazine", "0", "eng", (0, 0, 0, 0), 0),
])
def test_ownership_all_excludes_only_ownership_and_retains_type_query_language_and_acl(
    viewer_client, viewer_user, editor_user, db, path, media_type, owned, language, expected, filtered,
):
    from app.services import libraries

    _insert_item(db, title="Census overlapping book", isbn=None, owned=1, language="eng",
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Census owned book", isbn=None, owned=1, language="eng")
    _insert_item(db, title="Census acquisition only", isbn=None, owned=0, language="eng", wishlisted=True)
    _insert_item(db, title="Census other person's wish", isbn=None, owned=0, language="eng",
                 personal_user_id=editor_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Census neither French book", isbn=None, owned=0, language="fra")
    _insert_item(db, title="Census personal DVD", isbn=None, media_type="dvd", owned=0, language="eng",
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Outside the query", isbn=None, owned=1, language="eng")
    private = libraries.create_library(db, "Hidden ownership census")
    _insert_item(db, title="Census hidden book", isbn=None, owned=1, language="eng", _library_id=private["id"],
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Census trashed book", isbn=None, owned=1, language="eng", deleted_at="2026-01-01",
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    db.commit()
    response = viewer_client.get(path, params={
        "media_type_filter": media_type, "owned": owned, "language": language, "q": "Census",
    })
    root, options = _ownership_options(response, path)
    total, possession, wishes, neither = expected
    assert options == {
        "": f"All ({total})", "1": f"Owned ({possession})", "0": f"Wishlist ({wishes})",
        "none": f"Not owned or wishlisted ({neither})",
    }
    assert root.one("span", id="collection-count").text == f"({filtered})"
    for title in ("Census hidden book", "Census trashed book", "Outside the query"):
        assert title not in response.text


def test_ownership_census_keeps_the_original_disjoint_one_one_one_case(db, viewer_user):
    from app import browse_filters
    from app.services import browse_counts

    _insert_item(db, title="Owned", isbn=None, owned=1)
    _insert_item(db, title="Shared and personal member", isbn=None, owned=0, wishlisted=True,
                 personal_user_id=viewer_user["id"], personal_state={"wishlist": 1})
    _insert_item(db, title="Neither", isbn=None, owned=0)
    counts = browse_counts.filter_counts(
        db, browse_filters.values_from({"owned": "0"}), 1, user=viewer_user,
    )
    assert counts["ownership_total"] == 3
    assert (counts["owned_count"], counts["wishlist_count"], counts["neither_count"]) == (1, 1, 1)
    assert counts["filtered_total"] == 1
