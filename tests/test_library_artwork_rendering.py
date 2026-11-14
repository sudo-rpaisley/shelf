"""Real HTML/catalogue boundaries and owned framework-worker lifecycle tests."""

import asyncio
import base64
from contextlib import contextmanager
from copy import deepcopy
import threading

import anyio
import pytest

from app import config, features, media_presentation
from app.routers import library
from app.services import library_artwork as artwork, media_cards
from tests.conftest import _insert_item
from tests.test_library_artwork_sources import RECTANGLE, _context, _instrument, _row, _svg
from tests.test_media_cards import _environment, _HTML
from tests.test_media_page_cards import _HTML as _PageHTML


@pytest.mark.parametrize('path', ['/library', '/api/library/search'])
@pytest.mark.parametrize('view', ['items', 'groups', 'series'])
def test_real_native_html_uses_exact_captured_bytes_per_card_without_source_leak(viewer_client, db, path, view):
    expected = []
    for index, media_type in enumerate(('vinyl', 'cassette', 'video_game', 'book', 'audiobook', 'cd')):
        name = f'composition-{index}.SvG'
        content = _svg(b'<circle r="20" fill="#ABC"/><line x2="10" stroke="#123456"/>') if index % 2 == 0 else b'<invalid/>'
        (config.COVERS_DIR / name).write_bytes(content)
        item_id = _insert_item(db, isbn=None, title=f'{index} title', media_type=media_type,
                               cover_path='covers/' + name, series_name=f'Series {index}')
        expected.append((item_id, media_type, content, index % 2 == 0))
    db.commit()
    before = [tuple(row) for row in db.execute('SELECT * FROM items ORDER BY id')]
    response = viewer_client.get(path, params={'view': view, 'sort': 'title'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    root = _PageHTML(response.text).root
    nodes = root.find(**{'data-library-series': 'series 0'}) if view == 'series' else root.find(**{'data-library-member': str(expected[0][0])})
    assert len(nodes) == 1
    images = root.find('img', cls='media-artwork__image')
    assert len(images) == 3
    assert len(root.find(cls='media-artwork__placeholder')) == 3
    for item_id, media_type, content, valid in expected:
        node = (root.find(**{'data-library-series': f'series {expected.index((item_id, media_type, content, valid))}'})
                if view == 'series' else root.find(**{'data-library-member': str(item_id)}))[0]
        assert node.find(cls='media-artwork')[0].attrs['data-cover-shape'] == media_presentation.artwork_spec(media_type).shape
        if valid:
            image = node.find('img')[0]
            assert image.attrs == {'class': 'media-artwork__image', 'src': 'data:image/svg+xml;base64,' + base64.b64encode(content).decode(),
                                   'alt': '', 'loading': 'lazy', 'decoding': 'async',
                                   'data-cover-shape': media_presentation.artwork_spec(media_type).shape}
        else:
            assert not node.find('img')
    assert [tuple(row) for row in db.execute('SELECT * FROM items ORDER BY id')] == before
    for index in range(6):
        assert (config.COVERS_DIR / f'composition-{index}.SvG').read_bytes() == expected[index][2]
    assert type(response.context['prepared_library_artwork']) is artwork.PreparedLibraryArtwork
    assert 'data:' not in repr({key: value for key, value in response.context.items() if key in ('items', 'groups', 'series_results')})


def test_standalone_strictundefined_raw_svg_stays_placeholder_and_raster_attrs_exact():
    env, _ = _environment()
    for raw, expected in [('covers/7.svg', None), ('covers/7.png', '/covers/7.png'), (None, None)]:
        card = media_cards.build_media_card({'id': 7, 'title': 'Title', 'media_type': 'vinyl', 'cover_path': raw})
        root = _HTML(env.get_template('components/media_artwork.html').render(card=card)).root
        if expected:
            assert root.find('img')[0].attrs == {'class': 'media-artwork__image', 'src': expected, 'alt': '', 'loading': 'lazy', 'decoding': 'async'}
        else:
            assert not root.find('img') and len(root.find(cls='media-artwork__placeholder')) == 1


def test_standalone_result_macros_explicitly_clear_unrelated_source(viewer_client, db):
    # Copy a real complete context into a standalone StrictUndefined environment.
    ids = [_insert_item(db, isbn=None, title=f'{n}', media_type='vinyl', cover_path='covers/7.svg', series_name=f'S{n}') for n in range(2)]
    db.commit()
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    response = viewer_client.get('/library', params={'view': 'items'})
    env, _ = _environment()
    env.globals.update(build_media_card=media_cards.build_media_card, creator_label=config.creator_label)
    context = dict(response.context)
    prepared = context.pop('prepared_library_artwork')
    context['validated_artwork_source'] = prepared.source_for(ids[0], 'covers/7.svg')
    root = _HTML(env.get_template('fragments/library_results.html').render(context)).root
    assert not root.find('img') and len(root.find(cls='media-artwork__placeholder')) == 2
    response = viewer_client.get('/library', params={'view': 'series'})
    context = dict(response.context)
    context.pop('prepared_library_artwork')
    context['validated_artwork_source'] = prepared.source_for(ids[0], 'covers/7.svg')
    root = _HTML(env.get_template('fragments/library_results.html').render(context)).root
    assert not root.find('img') and len(root.find(cls='media-artwork__placeholder')) == 2


def test_json_auth_feature_off_absent_cleared_ineligible_and_offpage_do_no_artwork_io(viewer_client, db, monkeypatch):
    rows = [None, '', 'covers/cover.png', 'covers/not%2f.svg', 'covers/7.svg']
    ids = [_insert_item(db, isbn=None, title=f'{n}', cover_path=path) for n, path in enumerate(rows)]
    db.commit()
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    prepared_calls, root_calls = [], []
    original_prepare, original_open = artwork.prepare_library_artwork, artwork._open_root

    def prepare(context):
        prepared_calls.append(True)
        return original_prepare(context)

    def open_root():
        root_calls.append(True)
        return original_open()

    monkeypatch.setattr(artwork, 'prepare_library_artwork', prepare)
    monkeypatch.setattr(artwork, '_open_root', open_root)
    for path in ('/api/library/items', '/library?view=items&sort=title&page_size=4', '/api/library/search?q=absent'):
        response = viewer_client.get(path)
        assert response.status_code == 200
        if path == '/api/library/items':
            assert not prepared_calls and 'data:image' not in response.text
            assert 'prepared_library_artwork' not in response.json()
    assert root_calls == [] and len(prepared_calls) == 2
    # A deliberate clear must not inspect a retained file.
    db.execute('UPDATE items SET cover_path = NULL WHERE id = ?', (ids[-1],))
    db.commit()
    assert viewer_client.get('/library').status_code == 200
    assert root_calls == []
    features.set_feature_enabled(db, 'connected_library', False)
    db.commit()
    calls = len(prepared_calls)
    assert viewer_client.get('/library').status_code == 200
    assert viewer_client.get('/api/library/search').status_code == 403
    assert viewer_client.get('/api/library/items').status_code == 403
    assert len(prepared_calls) == calls and root_calls == []
    features.set_feature_enabled(db, 'connected_library', True)
    db.commit()
    viewer_client.cookies.clear()
    for path in ('/library', '/api/library/search', '/api/library/items'):
        assert viewer_client.get(path, follow_redirects=False).status_code == 303
    assert len(prepared_calls) == calls and root_calls == []


def test_html_preparation_is_once_in_framework_worker_after_existing_transaction_exit(viewer_client, db, monkeypatch):
    item_id = _insert_item(db, isbn=None, title='Actual row', cover_path='covers/7.svg')
    db.commit()
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    events, queries = [], []
    original_db, original_prepare, original_threadpool = library.get_db, artwork.prepare_library_artwork, library.run_in_threadpool
    inside = False
    loop_threads = []

    @contextmanager
    def tracked_db():
        nonlocal inside
        with original_db() as conn:
            inside = True
            loop_threads.append(threading.get_ident())
            conn.set_trace_callback(queries.append)
            try:
                yield conn
            finally:
                inside = False
                events.append('db-exit')

    def prepare(context):
        assert not inside and threading.get_ident() not in loop_threads
        events.append('physical-worker')
        before = len(queries)
        result = original_prepare(context)
        assert len(queries) == before
        return result

    async def pool(function, context):
        assert not inside and threading.get_ident() in loop_threads
        assert function is prepare
        events.append('framework-threadpool')
        return await original_threadpool(function, context)

    monkeypatch.setattr(library, 'get_db', tracked_db)
    monkeypatch.setattr(artwork, 'prepare_library_artwork', prepare)
    monkeypatch.setattr(library, 'run_in_threadpool', pool)
    for path in ('/library', '/api/library/search'):
        events.clear()
        response = viewer_client.get(path)
        assert response.status_code == 200
        assert events == ['db-exit', 'framework-threadpool', 'physical-worker']
        assert response.context['prepared_library_artwork'].source_for(item_id, 'covers/7.svg') is not None
    assert not any(query.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for query in queries)


def test_large_group_keeps_complete_catalogue_members_but_bounds_artwork_work(viewer_client, db, monkeypatch):
    ids = [_insert_item(db, isbn=None, title=f'{n:03d}', cover_path='covers/7.svg' if n >= 125 else None) for n in range(140)]
    for item_id in ids[1:]:
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (ids[0], item_id))
    db.commit()
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    before = viewer_client.get('/api/library/items', params={'sort': 'title'}).json()
    live, opened, reads = _instrument(monkeypatch)
    response = viewer_client.get('/library', params={'sort': 'title'})
    assert response.status_code == 200 and not live
    assert len(response.context['groups'][0]['members']) == 140
    assert len(_PageHTML(response.text).root.find(**{'data-matches': 'true'})) == 140
    assert response.context['groups'] == before['groups']
    assert (response.context['matching_count'], response.context['group_count']) == (140, 1)
    assert [entry[0] for entry in opened if entry[0] == '7.svg'] == ['7.svg'] * 3
    assert len(_PageHTML(response.text).root.find('img', cls='media-artwork__image')) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_error', [False, True])
async def test_repeated_task_cancellation_drains_real_worker_fds_and_preserves_worker_error(monkeypatch, worker_error):
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    context = _context([_row()])
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    live, opened, reads = _instrument(monkeypatch)
    original_read = artwork.os.read
    failure = RuntimeError('deterministic physical worker failure')

    def controlled_read(descriptor, amount):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), 'test controller failed to release the real worker'
        if worker_error:
            raise failure
        return original_read(descriptor, amount)

    monkeypatch.setattr(artwork.os, 'read', controlled_read)
    monkeypatch.setattr(library, '_read_context', lambda request, user: context)
    monkeypatch.setattr(library, '_html_context', lambda request, context: deepcopy(context))
    task = asyncio.create_task(library._prepared_html_context(None, {}))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert live and opened
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and live, 'request abandoned a still-running read'
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not live
    assert (caught.value.__cause__ is failure) is worker_error


@pytest.mark.asyncio
async def test_anyio_level_cancellation_waits_for_physical_completion(monkeypatch):
    context = _context([])
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    scope_box, completed, rendered = [], [], []
    original = artwork.prepare_library_artwork

    def worker(context):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        result = original(context)
        completed.append(True)
        return result

    monkeypatch.setattr(artwork, 'prepare_library_artwork', worker)
    monkeypatch.setattr(library, '_read_context', lambda request, user: context)
    monkeypatch.setattr(library, '_html_context', lambda request, context: rendered.append(True))

    async def request():
        with anyio.CancelScope() as scope:
            scope_box.append(scope)
            await library._prepared_html_context(None, {})

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(started.wait(), 5)
        scope_box[0].cancel()
        for _ in range(3):
            await asyncio.sleep(0)
            assert not task.done() and not completed
    finally:
        release.set()
    await task
    assert completed == [True] and not rendered


@pytest.mark.asyncio
async def test_uncancelled_unexpected_worker_error_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(library, '_read_context', lambda request, user: _context([]))

    def fail(context):
        raise RuntimeError('unexpected worker failure')

    monkeypatch.setattr(artwork, 'prepare_library_artwork', fail)
    with pytest.raises(RuntimeError, match='unexpected worker failure'):
        await library._prepared_html_context(None, {})


def test_preparation_preserves_input_and_has_no_sql_provider_or_permission_authority(monkeypatch):
    import sqlite3
    from app import database, features
    from app.services import covers

    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    context = _context([{**_row(), 'image_src': 'https://example.invalid/fake.svg', 'validated': True}])
    before = deepcopy(context)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('No SQL, permissions or acquisition in artwork preparation')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(database, 'get_db', forbidden)
    monkeypatch.setattr(features, 'feature_enabled', forbidden)
    monkeypatch.setattr(covers, 'download_cover', forbidden)
    prepared = artwork.prepare_library_artwork(context)
    assert context == before and not calls
    assert prepared.source_for(7, 'covers/7.svg').captured_bytes == RECTANGLE
