from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_item_edit_uses_sectioned_editor_and_exposes_artwork_controls():
    template = (ROOT / "app/templates/item_edit.html").read_text(encoding="utf-8")
    picker = (ROOT / "app/templates/fragments/cover_search.html").read_text(encoding="utf-8")
    script = (ROOT / "static/js/item_edit.js").read_text(encoding="utf-8")

    assert 'x-data="editSections"' in template
    assert 'data-testid="edit-section-nav"' in template
    assert 'data-testid="edit-section-general"' in template
    assert 'data-testid="edit-section-artwork"' in template
    assert 'data-testid="edit-section-identifiers"' in template
    assert 'data-testid="edit-section-copies"' in template

    assert 'id="cover_url"' in template
    assert 'data-edit-cover-url' in template
    assert 'data-edit-cover-remove' in template
    assert 'name="cover"' in template
    assert 'hx-get="/api/items/{{ item.id }}/cover-search"' in template

    assert 'data-edit-cover-select-url="{{ c.url }}"' in picker
    assert 'hx-post="/api/items/{{ item_id }}/cover-select"' not in picker
    assert 'Alpine.data(\'editSections\', editSections)' in script
    assert "'X-CSRF-Token'" in script
