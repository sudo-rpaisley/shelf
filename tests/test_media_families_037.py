"""Focused coverage for the 0.37 media-family Home/Browse recovery."""

from app import browse_filters, media_families
from app.services import home_dashboard
from app.services.item_write import insert_item


def test_family_registry_covers_current_user_facing_groups():
    assert list(media_families.MEDIA_FAMILIES) == [
        "books",
        "comics",
        "periodicals",
        "music",
        "film",
        "games",
        "audiobooks",
    ]
    assert set(media_families.types_for_family("comics")) == {"comic", "manga"}
    assert set(media_families.types_for_family("music")) == {
        "vinyl", "cassette", "cd", "digital_music"
    }


def test_family_cards_roll_up_concrete_media_type_counts():
    rows = [
        {"media_type": "comic", "item_count": 2},
        {"media_type": "manga", "item_count": 3},
        {"media_type": "vinyl", "item_count": 4},
    ]
    cards = {card["key"]: card for card in media_families.cards(rows)}
    assert cards["comics"]["count"] == 5
    assert cards["music"]["count"] == 4
    assert cards["books"]["count"] == 0
    assert cards["comics"]["href"] == "/browse?media_family_filter=comics"


def test_unknown_family_filter_matches_nothing():
    where, params = browse_filters.build_where({"media_family_filter": "not-a-family"})
    assert where == "WHERE 1 = 0"
    assert params == []


def test_family_filter_expands_to_all_family_media_types():
    where, params = browse_filters.build_where({"media_family_filter": "comics"})
    assert "i.media_type IN" in where
    assert params == ["comic", "manga"]
    assert browse_filters.BY_NAME["media_family_filter"].prefix == "Family"


def test_dashboard_summary_exposes_family_counts(db):
    insert_item(db, title="Comic", media_type="comic", owned=1)
    insert_item(db, title="Manga", media_type="manga", owned=1)
    insert_item(db, title="Record", media_type="vinyl", owned=1)

    summary = home_dashboard.dashboard_summary(db)
    families = {card["key"]: card for card in summary["families"]}
    assert families["comics"]["count"] == 2
    assert families["music"]["count"] == 1
