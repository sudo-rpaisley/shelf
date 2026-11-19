"""Collection mix is Stats-only; additive source contracts, not executed results."""

import pytest

from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_features_gate import off


def _seed_mix(db):
    for number, media_type in enumerate(("book", "cd", "cd")):
        _insert_item(db, title=f"Mix item {number}", isbn=None, media_type=media_type)
    _insert_item(db, title="Trashed mix item", isbn=None, media_type="dvd",
                 deleted_at="2026-01-01 00:00:00")
    db.commit()


@pytest.mark.parametrize("populated", (False, True))
def test_home_omits_mix_but_preserves_summary_recent_items_and_stats_access(viewer_client, db, populated):
    if populated:
        _seed_mix(db)
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not [node for node in markup.find("h2") if node.text.strip() == "Collection mix"]
    assert not [node for node in markup.elements if "data-home-mix-row" in node.attrs]
    assert markup.testid("home-summary")
    assert markup.testid("home-recent-items")
    type_count = markup.testid("home-media-types")
    assert markup.find("p", within=type_count)[-1].text.strip() == ("2" if populated else "0")
    assert not [node for node in markup.find("h2") if node.text.strip() == "Useful places"]
    navigation = markup.one("nav", **{"aria-label": "Primary navigation"})
    assert markup.find("a", within=navigation, href="/stats")
    assert "Scan item" not in response.text and "Add by hand" not in response.text


def test_stats_keeps_live_type_counts_labels_and_proportional_bars(viewer_client, db):
    _seed_mix(db)
    response = viewer_client.get("/stats")
    assert response.status_code == 200
    assert {row["media_type"]: row["c"] for row in response.context["by_type"]} == {"cd": 2, "book": 1}
    markup = _Markup(response.text)
    (heading,) = [node for node in markup.find("h2") if node.text.strip() == "By Media Type"]
    panel = heading.ancestors[-1]
    labels = response.context["media_types"]
    assert [node.text.strip() for node in markup.find("span", within=panel)] == [
        labels["cd"], "2", labels["book"], "1",
    ]
    bars = [node for node in markup.find("div", within=panel) if "style" in node.attrs]
    assert len(bars) == 2
    widths = [float(node.attrs["style"].removeprefix("width: ").removesuffix("%")) for node in bars]
    assert widths == pytest.approx([200 / 3, 100 / 3])
    assert "Trashed mix item" not in response.text


def test_disabling_stats_does_not_restore_collection_mix_on_home(viewer_client, db):
    _seed_mix(db)
    off("stats")
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert not markup.find("a", href="/stats")
    assert not [node for node in markup.find("h2") if node.text.strip() == "Collection mix"]
    assert not [node for node in markup.elements if "data-home-mix-row" in node.attrs]
    assert markup.testid("home-summary") and markup.testid("home-recent-items")
