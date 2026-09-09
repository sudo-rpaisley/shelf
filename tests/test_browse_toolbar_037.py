"""Focused guards for the compact 0.37 Collection toolbar."""

import re
from pathlib import Path

from app import media_families


TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "browse.html"


def test_collection_toolbar_keeps_primary_controls_together():
    source = TEMPLATE.read_text()
    assert 'data-testid="collection-toolbar"' in source
    assert 'data-testid="search-input"' in source
    assert source.count('data-testid="search-input"') == 1
    assert 'data-testid="filters-toggle"' in source
    assert 'data-testid="sort-control"' in source
    assert 'data-testid="view-toggle"' in source
    assert 'data-testid="select-mode-toggle"' in source
    assert 'data-testid="column-picker"' in source


def test_family_selector_matches_shared_family_registry():
    source = TEMPLATE.read_text()
    block = re.search(
        r'<select id="family-filter".*?</select>', source, flags=re.S
    ).group(0)
    option_values = re.findall(r'<option value="([^"]+)"', block)
    assert option_values == ["", *media_families.MEDIA_FAMILIES.keys()]
    assert 'name="media_family_filter"' in block
    assert "filter_includes('media_family_filter')" in block


def test_specialist_filters_live_in_collapsible_panel():
    source = TEMPLATE.read_text()
    assert 'data-testid="filters-panel"' in source
    for name in (
        "media_family_filter", "media_type_filter", "location_filter", "owned",
        "lent_out", "reading_status", "tag", "language",
    ):
        assert f'name="{name}"' in source
