from app.services import media_groups
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


def test_game_family_groups_platform_versions_but_not_distant_remake(db):
    snes = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="snes", publish_year=1994,
    )
    gba = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="gba", publish_year=1995,
    )
    pc = _insert_item(
        db, title="Example Quest", isbn=None, media_type="video_game",
        platform="pc", publish_year=1996,
    )
    remake = _insert_item(
        db, title="Example Quest", isbn=None, media_type="digital_game",
        platform="ps5", publish_year=2025,
    )

    media_groups.auto_link_family(db, "game")

    assert set(media_groups.related_ids(db, snes)) == {gba, pc}
    assert remake not in media_groups.related_ids(db, snes)


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
