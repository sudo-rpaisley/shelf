"""Native manual/camera presentation contracts; not browser or camera evidence."""

from pathlib import Path

from tests.test_add_media_search import io, pairs, search
from tests.test_connected_layout import _Markup
from tests.test_command_launcher import _marked


EXTRA_FIELDS = {'subtitle', 'description', 'notes', 'page_count', 'series_position', 'narrator',
                'duration_mins', 'asin', 'manual_value', 'date_started', 'date_finished'}


def test_native_manual_starts_simple_but_keeps_every_optional_editor_field(editor_client, io):
    markup = _Markup(editor_client.get('/add').text)
    manual = markup.testid('add-media-manual')
    advanced = markup.testid('manual-advanced')
    assert advanced.tag == 'details' and 'open' not in advanced.attrs and manual in advanced.ancestors
    assert markup.one('summary', within=advanced).text.strip() == 'Advanced options'
    controls = {node.attrs['name']: node for node in markup.find(within=manual)
                if node.tag in {'input', 'select', 'textarea'} and 'name' in node.attrs}
    assert EXTRA_FIELDS <= controls.keys()
    for name in EXTRA_FIELDS | {'publisher', 'publish_year', 'isbn', 'upc', 'series_name', 'language', 'platform', 'location_id', 'tags', 'reading_status', 'cover_url'}:
        assert advanced in controls[name].ancestors, name
    for name in {'title', 'authors', 'media_type', 'mode', 'cover'}:
        assert advanced not in controls[name].ancestors, name
    assert controls['location_id'].tag == 'select' and controls['platform'].tag == 'select'
    assert {'source', 'musicbrainz_release_id', 'hardcover_book_id', 'token_version', 'history'}.isdisjoint(controls)


def test_barcode_button_is_at_search_end_and_never_a_submit_control(editor_client, io):
    markup = _Markup(editor_client.get('/add').text)
    search_form = markup.testid('add-media-search')
    field = markup.one('input', within=search_form, name='q')
    button = markup.one('button', within=search_form, **{'data-add-camera': 'barcode'})
    assert button.attrs['type'] == 'button' and 'hidden' in button.attrs
    assert button.attrs['data-camera-target'] == field.attrs['id']
    assert button.ancestors[-1] is field.ancestors[-1] and field.position < button.position
    assert button.attrs['aria-label'] == 'Scan a barcode into the search field'
    dialog = _marked(markup, 'data-add-camera-dialog', 'dialog')
    assert not any(ancestor.tag == 'form' for ancestor in dialog.ancestors)
    assert 'open' not in dialog.attrs
    assert markup.one('h2', id=dialog.attrs['aria-labelledby'])
    assert markup.one('p', id=dialog.attrs['aria-describedby'])
    assert all(control.attrs.get('type') == 'button' for control in markup.find('button', within=dialog))


def test_advanced_search_is_native_retains_values_and_is_not_the_main_query(editor_client, io):
    markup = _Markup(editor_client.get('/add').text)
    advanced = markup.testid('add-search-advanced')
    assert advanced.tag == 'details' and 'open' not in advanced.attrs
    search_form = markup.testid('add-media-search')
    assert search_form in advanced.ancestors
    assert advanced not in markup.one('input', within=search_form, name='q').ancestors
    assert {node.attrs['name'] for node in markup.find(within=advanced) if 'name' in node.attrs} == {'provider', 'artist', 'title_hint', 'platform', 'country'}


def test_legacy_manual_panel_has_shared_extras_upload_and_capture_without_losing_copy_picker(editor_client):
    markup = _Markup(editor_client.get('/scan?add=manual').text)
    panel = markup.one('div', **{'data-manual-host': 'panel'})
    form = markup.one('form', within=panel)
    controls = {node.attrs['name'] for node in markup.find(within=form) if 'name' in node.attrs}
    assert EXTRA_FIELDS <= controls and {'title', 'authors', 'cover', 'media_type', 'isbn', 'upc'} <= controls
    assert markup.one('button', within=form, **{'data-add-camera': 'photo'}).attrs['type'] == 'button'
    assert markup.one('input', within=form, **{'x-model': 'copyQuery'})
    assert form.attrs['hx-include'] == '#scan-mode, #default-tags, #default-reading-status'


def test_contextual_controller_source_has_no_io_or_auto_submit_and_owns_retirement():
    source = Path('static/js/add-camera.js').read_text()
    assert 'window.createBarcodeScanner' in source and 'window.createPhotoCapture' in source
    assert 'session.retired = true' in source and 'session.starting.catch' in source
    assert "addEventListener('pagehide'" in source and "addEventListener('visibilitychange'" in source
    assert "addEventListener('cancel'" in source and 'URL.revokeObjectURL' in source
    assert 'new DataTransfer()' in source and "new Event('input'" in source
    for forbidden in ('fetch(', 'XMLHttpRequest', '.submit(', '.requestSubmit(', 'localStorage', 'sessionStorage'):
        assert forbidden not in source
