"""A deleted graph vertex cannot provide an invisible relationship bridge."""

from app.services import item_write, media_groups
from tests.conftest import _insert_item


def test_related_component_does_not_traverse_a_trashed_intermediary(db):
    a = _insert_item(db, title="First", isbn=None)
    bridge = _insert_item(db, title="Bridge", isbn=None)
    c = _insert_item(db, title="Last", isbn=None)
    media_groups.link_items(db, a, bridge, link_type="format")
    media_groups.link_items(db, bridge, c, link_type="adaptation")
    assert media_groups.related_ids(db, a) == [bridge, c]
    item_write.trash_item(db, bridge)
    assert media_groups.related_ids(db, a) == []
    assert media_groups.related_ids(db, c) == []
    assert media_groups.direct_links(db, bridge) == []
    item_write.restore_item(db, bridge)
    assert media_groups.related_ids(db, a) == [bridge, c]


def test_other_live_path_still_connects_related_items_after_bridge_trashed(db):
    a, bridge, c = [_insert_item(db, title=name, isbn=None) for name in ("A", "B", "C")]
    media_groups.link_items(db, a, bridge)
    media_groups.link_items(db, bridge, c)
    media_groups.link_items(db, a, c, link_type="adaptation")
    item_write.trash_item(db, bridge)
    assert media_groups.related_ids(db, a) == [c]


def test_unlink_removes_legacy_reversed_edge(db):
    a, b = [_insert_item(db, title=name, isbn=None) for name in ("A", "B")]
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (b, a))
    assert media_groups.unlink_items(db, a, b) is True
    assert media_groups.related_ids(db, a) == []
    assert media_groups.unlink_items(db, a, b) is False


def test_link_does_not_contradict_an_existing_reversed_adaptation(db):
    a, b = [_insert_item(db, title=name, isbn=None) for name in ("A", "B")]
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'adaptation')", (b, a))
    assert media_groups.link_items(db, a, b, link_type="format") is False
    assert db.execute("SELECT link_type FROM item_links").fetchall()[0][0] == "adaptation"


def test_abs_auto_link_does_not_turn_a_graphic_adaptation_into_a_format(db):
    from app.services import audiobookshelf
    prose = _insert_item(db, title="Dune", authors="Frank Herbert", isbn=None, media_type="audiobook", abs_id="prose-audio")
    _insert_item(db, title="Dune", authors="Frank Herbert", isbn=None, media_type="comic")
    db.commit()
    assert audiobookshelf.auto_link([prose]) == 0


def test_abs_auto_link_respects_a_legacy_reversed_related_relationship(db):
    from app.services import audiobookshelf
    a = _insert_item(db, title="A Work", authors="A Creator", isbn=None, media_type="book")
    b = _insert_item(db, title="A Work", authors="A Creator", isbn=None, media_type="audiobook", abs_id="audio")
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'related')", (b, a))
    db.commit()
    assert audiobookshelf.auto_link([b]) == 0
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 1
