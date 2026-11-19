"""Full manual metadata and bounded upload regressions; authored, not executed."""

from urllib.parse import urlsplit

import pytest

from app import config
from app.add_manual_fields import EXTRA_FIELDS
from app.add_media_http import MANUAL_BODY_BYTES
from app.database import get_db
from app.services import covers, item_write, manual_cover
from tests.test_add_media_boundaries import raw_request
from tests.test_add_media_search import ISBN, form, io, pairs, post, replace, snapshot
from tests.test_add_media_save import prepared, receipt, stored, submit


IMAGE = b'\x89PNG\r\n\x1a\n' + bytes(200)
EXTRAS = {'subtitle': 'A subtitle', 'description': 'First line\nSecond line 星', 'notes': 'Notes\nkeep tabs\there',
          'page_count': '412', 'series_position': '2.5', 'narrator': 'A narrator', 'duration_mins': '721',
          'asin': 'B012345678', 'manual_value': '15.25', 'date_started': '2026-01-02', 'date_finished': '2026-02-03'}


@pytest.fixture(autouse=True)
def no_unexpected_image_fetch(monkeypatch, io):
    attempts = []
    async def refused(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('No real image URL request is authorized by this test')
    monkeypatch.setattr(manual_cover, 'fetch_content', refused)
    yield
    assert attempts == [], 'A swallowed unexpected image request is still a failure'


def root_manual(client):
    response = client.get('/add')
    return form(response, 'add-media-manual')


def multipart(values, files=()):
    boundary = 'shelf-manual-boundary'
    body = bytearray()
    for key, value in values:
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode('utf-8'))
        body.extend(b'\r\n')
    for name, data in files:
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="cover.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
        body.extend(data)
        body.extend(b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())
    return bytes(body), f'multipart/form-data; boundary={boundary}'


def send_multipart(client, selected, changes, files=()):
    body, media = multipart(replace(pairs(selected), **changes), files)
    return client.post(selected['attrs']['action'], content=body, headers={'Content-Type': media}, follow_redirects=False)


@pytest.mark.parametrize('mode', ('add', 'wishlist'))
@pytest.mark.parametrize('transport', ('urlencoded', 'multipart'))
def test_root_manual_saves_all_metadata_independent_identifiers_and_original_receipt(editor_client, editor_user, db, mode, transport):
    selected = root_manual(editor_client)
    changes = {'mode': mode, 'media_type': 'audiobook', 'title': 'Manual audiobook', 'authors': 'Manual author',
               'publisher': 'Manual publisher', 'publish_year': '2001', 'series_name': 'Series', 'language': 'en',
               'isbn': ISBN, 'upc': '078073003501', 'reading_status': 'read', **EXTRAS}
    if transport == 'urlencoded':
        response = submit(editor_client, selected, **changes)
    else:
        response = send_multipart(editor_client, selected, changes)
    _, actual = receipt(editor_client, response, editor_user['id'])
    row = stored(db, actual.item_id)
    assert actual.status == ('added' if mode == 'add' else 'wishlisted')
    assert row['source'] == 'manual' and row['media_type'] == 'audiobook'
    assert row['isbn'] == ISBN and row['upc'] == '0078073003501'
    for name, value in EXTRAS.items():
        expected = int(value) if name in {'page_count', 'duration_mins'} else float(value) if name in {'manual_value', 'series_position'} else value
        assert row[name] == expected, name
    assert row['reading_status'] == 'read' and row['cover_path'] is None


@pytest.mark.parametrize('media_type', tuple(config.MEDIA_TYPES))
def test_immediate_manual_form_supports_every_concrete_format_without_a_hidden_book_default(editor_client, editor_user, db, media_type):
    selected = root_manual(editor_client)
    response = submit(editor_client, selected, mode='add', media_type=media_type, title='Explicit format', platform='switch' if media_type == 'video_game' else '')
    _, actual = receipt(editor_client, response, editor_user['id'])
    assert stored(db, actual.item_id)['media_type'] == media_type


@pytest.mark.parametrize('changes', [
    {'media_type': ''}, {'page_count': '-1'}, {'page_count': '1.5'}, {'duration_mins': 'NaN'},
    {'series_position': 'inf'}, {'manual_value': '-0.01'}, {'manual_value': '1000000000001'},
    {'date_finished': '2026-02-30'}, {'asin': 'wrong'}, {'notes': 'x' * 10001}, {'subtitle': 'x' * 201},
    {'notes': 'bad\x00control'}, {'upc': '9780441172719'}, {'isbn': '9780441172718'},
])
def test_invalid_advanced_values_or_identifiers_do_not_create_any_catalogue_state(editor_client, db, changes):
    selected = root_manual(editor_client)
    before = snapshot(db)
    response = submit(editor_client, selected, title='Not saved', mode='add', **({'media_type': 'audiobook'} | changes))
    assert response.status_code in {400, 422}
    assert snapshot(db) == before


def test_bound_manual_intent_cannot_be_relabelled_by_new_controls(editor_client, db):
    selected = prepared(editor_client, 'manual', 'wishlist', media_type='cassette')
    before = snapshot(db)
    response = submit(editor_client, selected, title='No relabelling', mode='add', media_type='book')
    assert response.status_code == 409 and snapshot(db) == before


def test_upload_is_explicit_unique_and_does_not_change_duplicate_artwork(editor_client, editor_user, db):
    selected = root_manual(editor_client)
    response = send_multipart(editor_client, selected, {'title': 'Covered book', 'media_type': 'book', 'isbn': ISBN}, [('cover', IMAGE)])
    _, first = receipt(editor_client, response, editor_user['id'])
    row = stored(db, first.item_id)
    path = row['cover_path']
    assert path.startswith(f'covers/{first.item_id}-') and (config.COVERS_DIR / path.split('/')[-1]).read_bytes() == IMAGE
    before = snapshot(db)
    selected = root_manual(editor_client)
    _, second = receipt(editor_client, send_multipart(editor_client, selected, {'title': 'Do not replace', 'media_type': 'book', 'isbn': ISBN, 'notes': 'Do not overwrite'}, [('cover', IMAGE + b'other')]), editor_user['id'])
    assert second.status == 'duplicate' and second.item_id == first.item_id
    assert stored(db, first.item_id) == row
    assert snapshot(db)[1] == before[1]


@pytest.mark.parametrize('files', [[('cover', b'not an image')], [('other', IMAGE)], [('cover', IMAGE), ('cover', IMAGE)]])
def test_invalid_or_multiple_files_are_refused_before_insertion(editor_client, db, files):
    selected = root_manual(editor_client)
    before = snapshot(db)
    response = send_multipart(editor_client, selected, {'title': 'No upload', 'media_type': 'book'}, files)
    assert response.status_code == 400 and snapshot(db) == before


def test_image_and_url_conflict_is_not_silently_resolved(editor_client, db):
    selected = root_manual(editor_client)
    before = snapshot(db)
    response = send_multipart(editor_client, selected, {'title': 'Conflict', 'media_type': 'book', 'cover_url': 'https://public.example/cover.png'}, [('cover', IMAGE)])
    assert response.status_code == 422 and snapshot(db) == before


def test_actor_is_revalidated_after_the_image_url_await(editor_client, editor_user, db, monkeypatch):
    selected = root_manual(editor_client)
    before = snapshot(db)
    async def change_actor(url):
        with get_db() as connection:
            connection.execute('UPDATE users SET token_version = token_version + 1 WHERE id = ?', (editor_user['id'],))
        return IMAGE
    monkeypatch.setattr(manual_cover, 'fetch_content', change_actor)
    response = submit(editor_client, selected, title='Must not save', media_type='book', cover_url='https://public.example/cover.png')
    assert response.status_code == 401 and snapshot(db) == before


def test_cover_failure_is_a_truthful_successful_item_receipt_not_a_retry(editor_client, editor_user, db, monkeypatch):
    async def unavailable(url):
        return None
    monkeypatch.setattr(manual_cover, 'fetch_content', unavailable)
    selected = root_manual(editor_client)
    _, actual = receipt(editor_client, submit(editor_client, selected, title='Saved without artwork', media_type='book', cover_url='https://public.example/cover.png'), editor_user['id'])
    assert actual.status == 'added' and actual.cover_warning is True
    assert stored(db, actual.item_id)['cover_path'] is None
    assert db.execute('SELECT COUNT(*) FROM items').fetchone()[0] == 1


def test_legacy_manual_api_uses_the_same_extra_fields_not_cosmetic_inputs(editor_client, db):
    response = editor_client.post('/api/items/manual', data={'title': 'Legacy audiobook', 'media_type': 'audiobook', **EXTRAS})
    assert response.status_code == 200
    row = dict(db.execute('SELECT * FROM items WHERE title = ?', ('Legacy audiobook',)).fetchone())
    assert row['description'] == EXTRAS['description'] and row['duration_mins'] == 721 and row['manual_value'] == 15.25


@pytest.mark.asyncio
async def test_new_manual_stream_cap_stops_before_tail_and_other_caps_stay_unchanged(editor_client, db):
    root_manual(editor_client)
    before = snapshot(db)
    for path, limit in [('/add/manual', MANUAL_BODY_BYTES), ('/add/search', 8192), ('/add/preview', 8192), ('/add/save', 130000)]:
        first = b'title=' + b'x' * (limit - 6)
        response, reads = await raw_request(editor_client, 'POST', path, chunks=[first, b'x', b'UNREAD'])
        assert response.status_code == 413 and response.json()['code'] == 'body_too_large'
        assert reads == [first, b'x']
    assert snapshot(db) == before


def test_multipart_duplicate_fields_and_invalid_utf8_are_not_reinterpreted(editor_client, db):
    selected = root_manual(editor_client)
    values = replace(pairs(selected), title='Original', media_type='book')
    before = snapshot(db)
    for fields in (values + [('title', 'Second')], values):
        body, media = multipart(fields, [('cover', IMAGE)])
        if fields is values:
            body = body.replace(b'Original', b'Bad\xfftext')
        response = editor_client.post(selected['attrs']['action'], content=body, headers={'Content-Type': media}, follow_redirects=False)
        assert response.status_code == 400
        assert snapshot(db) == before


def test_accepted_and_refused_multipart_files_are_closed(editor_client, monkeypatch):
    from starlette.datastructures import UploadFile
    closed = []
    original = UploadFile.close
    async def close_file(file):
        await original(file)
        closed.append(file.file.closed)
    monkeypatch.setattr(UploadFile, 'close', close_file)
    selected = root_manual(editor_client)
    for image, expected in ((IMAGE, 303), (b'invalid', 400)):
        response = send_multipart(editor_client, selected, {'title': 'File closure', 'media_type': 'book'}, [('cover', image)])
        assert response.status_code == expected
    assert closed == [True, True]


def test_unfinished_multipart_tail_is_refused_and_its_unreturned_spool_is_closed(editor_client, db, monkeypatch):
    import starlette.formparsers as parser_module
    original, opened = parser_module.SpooledTemporaryFile, []
    def tracked(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(parser_module, 'SpooledTemporaryFile', tracked)
    selected = root_manual(editor_client)
    body, media = multipart(replace(pairs(selected), title='Unfinished', media_type='book'), [('cover', IMAGE)])
    body = body.rsplit(b'\r\n--shelf-manual-boundary--\r\n', 1)[0]
    before = snapshot(db)
    response = editor_client.post(selected['attrs']['action'], content=body, headers={'Content-Type': media}, follow_redirects=False)
    assert response.status_code == 400 and snapshot(db) == before
    assert len(opened) == 1 and opened[0].closed
