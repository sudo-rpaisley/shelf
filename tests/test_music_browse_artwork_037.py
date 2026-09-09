"""Focused guards for the Shelf 0.37 Music browse/artwork refresh."""

from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "music.html"


def test_music_library_renders_existing_local_cover_art():
    source = TEMPLATE.read_text()
    assert "{% if item.cover_path %}" in source
    assert 'src="/{{ item.cover_path }}"' in source
    assert 'data-testid="music-library-grid"' in source
    assert "data-music-artwork" in source


def test_music_library_has_artwork_fallback_and_exact_release_search():
    source = TEMPLATE.read_text()
    assert "aspect-square" in source
    assert 'data-testid="music-release-search"' in source
    assert "Search MusicBrainz" in source
    assert 'action="/api/music/add"' in source


def test_music_links_into_the_shared_family_filtered_collection():
    source = TEMPLATE.read_text()
    assert 'href="/browse?media_family_filter=music"' in source
    assert 'href="/music/item/{{ item.id }}"' in source
