from app.services import media_groups
from app.services.item_write import insert_item
from tests.conftest import _insert_item


def test_related_items_are_transitive(db):
    a = _insert_item(db, title="Work", isbn=None, media_type="book")
    b = _insert_item(db, title="Work", isbn=None, media_type="ebook")
    c = _insert_item(db, title="Work", isbn=None, media_type="audiobook")
    media_groups.link_items(db, a, b, "format")
    media_groups.link_items(db, b, c, "format")

    assert media_groups.related_ids(db, a) == [b, c]
    assert media_groups.related_ids(db, c) == [a, b]


def test_book_family_groups_formats_and_multiple_audiobook_editions(db):
    book = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn="9780747532699",
        media_type="book", authors="J. K. Rowling", publish_year=1997,
    )
    ebook = _insert_item(
        db, title="Harry Potter and the Philosopher’s Stone", isbn="9781781100219",
        media_type="ebook", authors="J. K. Rowling", publish_year=2015,
    )
    audio_one = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn="9781855496700",
        media_type="audiobook", authors="J. K. Rowling", narrator="Stephen Fry", publish_year=1999,
    )
    audio_two = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn="9781781102367",
        media_type="audiobook", authors="J. K. Rowling", narrator="Jim Dale", publish_year=2016,
    )

    media_groups.auto_link_family(db, "book")

    assert set(media_groups.related_ids(db, book)) == {ebook, audio_one, audio_two}


def test_game_family_groups_platform_versions_across_release_years(db):
    snes = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="snes", publish_year=1994,
    )
    gba = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="gba", publish_year=2001,
    )
    pc = _insert_item(
        db, title="Example Quest", isbn=None, media_type="video_game",
        platform="pc", publish_year=2004,
    )
    later_port = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="ps5", publish_year=2025,
    )

    media_groups.auto_link_family(db, "game")

    assert set(media_groups.related_ids(db, snes)) == {gba, pc, later_port}


def test_game_family_keeps_distinct_subtitles_separate(db):
    ocarina = _insert_item(
        db, title="The Legend of Zelda: Ocarina of Time", isbn=None,
        media_type="digital_game", platform="n64",
    )
    majora = _insert_item(
        db, title="The Legend of Zelda: Majora's Mask", isbn=None,
        media_type="digital_game", platform="n64",
    )

    media_groups.auto_link_family(db, "game")

    assert majora not in media_groups.related_ids(db, ocarina)


def test_manual_link_can_join_different_media_families(db):
    book = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn=None, media_type="book"
    )
    game = _insert_item(
        db, title="Harry Potter and the Philosopher's Stone", isbn=None,
        media_type="video_game", platform="pc",
    )

    assert media_groups.link_items(db, book, game, "related") is True
    assert media_groups.related_ids(db, book) == [game]
    assert media_groups.has_manual_group_edge(db, book, game) is True

    assert media_groups.remove_manual_group_edges(db, book, game) == 1
    assert media_groups.related_ids(db, book) == []


def test_search_candidates_excludes_current_connected_group(db):
    book = _insert_item(db, title="Dune", isbn=None, media_type="book", authors="Frank Herbert")
    audio = _insert_item(db, title="Dune", isbn=None, media_type="audiobook", authors="Frank Herbert")
    game = _insert_item(db, title="Dune", isbn=None, media_type="video_game", platform="pc")
    other = _insert_item(db, title="Dune Messiah", isbn=None, media_type="book", authors="Frank Herbert")
    media_groups.link_items(db, book, audio, "format")

    blank = media_groups.search_candidates(db, book, "")
    assert [row["id"] for row in blank] == [game]

    searched = media_groups.search_candidates(db, book, "Dune")
    ids = [row["id"] for row in searched]
    assert audio not in ids
    assert game in ids
    assert other in ids


def test_normal_insert_joins_existing_same_work_group_immediately(db):
    book = insert_item(
        db,
        title="Dune",
        authors="Frank Herbert",
        media_type="book",
        isbn="9780441172719",
        source="manual",
    )
    audio = insert_item(
        db,
        title="Dune",
        authors="Frank Herbert",
        media_type="audiobook",
        isbn="9781427201432",
        source="manual",
    )

    assert media_groups.related_ids(db, book) == [audio]


def test_provider_batch_insert_defers_grouping_until_batch_end(db):
    book = insert_item(
        db,
        title="Dune",
        authors="Frank Herbert",
        media_type="book",
        isbn="9780441172719",
        source="manual",
    )
    audio = insert_item(
        db,
        title="Dune",
        authors="Frank Herbert",
        media_type="audiobook",
        isbn="9781427201432",
        source="audiobookshelf",
    )

    assert media_groups.related_ids(db, book) == []
    media_groups.auto_link_family(db, "book")
    assert media_groups.related_ids(db, book) == [audio]
