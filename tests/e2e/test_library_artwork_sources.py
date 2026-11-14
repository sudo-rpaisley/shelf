"""Genuine local images through real Library HTML and the unchanged server CSP.

The original short-phone journey is also run, unmodified, as a separate gate.
"""

import base64
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.test_connected_library import connected_library, _db, _item


pytestmark = pytest.mark.e2e
RECTANGLE = (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450">'
             b'<rect width="300" height="450" fill="#3e6267"/></svg>')
COMPOSITION = (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">'
               b'<rect width="300" height="450" fill="#123456"/><circle cx="100" cy="150" r="40" fill="#ABC"/>'
               b'<ellipse cx="160" cy="200" rx="30" ry="20" fill="none" stroke="#FFF"/>'
               b'<line x1="10" y1="10" x2="100" y2="200" stroke="#789" stroke-width="2"/>'
               b'<polyline points="0,0 100,100 0,200" fill="none" stroke="#000"/>'
               b'<polygon points="200 200 250 200 250 250" fill="#456"/></svg>')


@pytest.mark.parametrize('view', ['items', 'groups', 'series'])
def test_real_validated_images_decode_exactly_once_with_native_shapes_under_existing_csp(connected_library, view, tmp_path):
    server, page = connected_library, connected_library['page']
    expected = []
    names = ('7.svg', 'square-artwork.svg', 'ordinary-composition.SVG', 'Cassette_4.svg')
    with _db(server) as db:
        for number, (name, media_type) in enumerate(zip(names, ('vinyl', 'book', 'video_game', 'cassette'), strict=True)):
            content = RECTANGLE if number < 2 else COMPOSITION
            (server['data_dir'] / 'covers' / name).write_bytes(content)
            item_id = _item(db, f'{number} Native {media_type}', media_type,
                            cover_path='covers/' + name, series_name=f'Series {number}')
            expected.append((item_id, media_type, content, name))
        invalid = _item(db, '9 Invalid source', 'cd', cover_path='covers/hostile.svg', series_name='Series 9')
        (server['data_dir'] / 'covers' / 'hostile.svg').write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450"><script/></svg>')
        before = [tuple(row) for row in db.execute('SELECT id, media_type, cover_path FROM items ORDER BY id')]
    page.set_viewport_size({'width': 375, 'height': 568})
    requested_covers = []
    page.on('request', lambda request: requested_covers.append(request.url) if '/covers/' in request.url else None)
    response = page.goto(f"{server['url']}/library?view={view}&sort=title")
    assert response.status == 200 and response.headers['cache-control'] == 'no-store'
    csp = response.headers['content-security-policy']
    assert "img-src 'self' data: blob: https:" in csp
    # Pin the existing complete policy, including its existing style-only
    # allowance. This increment neither relaxes it nor invents a stricter one.
    assert csp == ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data: blob: https:; connect-src 'self'; font-src 'self' data:; "
                   "base-uri 'self'; form-action 'self'; frame-ancestors 'none';")
    results = page.get_by_test_id('library-results')
    expect(results.locator('img')).to_have_count(4)
    metrics = []
    for number, (item_id, media_type, content, name) in enumerate(expected):
        member = results.locator(f'[data-library-series="series {number}"]' if view == 'series' else f'[data-library-member="{item_id}"]')
        image = member.locator('img')
        expect(image).to_have_count(1)
        image.scroll_into_view_if_needed()
        expect(image).to_be_visible()
        expect(image).to_have_js_property('complete', True)
        expect(image).to_have_js_property('naturalWidth', 300)
        expect(image).to_have_js_property('naturalHeight', 450)
        source = image.get_attribute('src')
        assert source.startswith('data:image/svg+xml;base64,')
        assert base64.b64decode(source.split(',', 1)[1], validate=True) == content
        shape = 'square' if media_type == 'vinyl' else 'game' if media_type == 'video_game' else 'cassette' if media_type == 'cassette' else 'portrait'
        expect(image).to_have_attribute('data-cover-shape', shape)
        expect(member.locator('.media-artwork')).to_have_attribute('data-cover-shape', shape)
        expect(image).to_have_attribute('alt', '')
        expect(image).to_have_attribute('loading', 'lazy')
        expect(image).to_have_attribute('decoding', 'async')
        measure = image.evaluate('''element => {
            const box = element.getBoundingClientRect(), style = getComputedStyle(element);
            const scale = Math.min(box.width / element.naturalWidth, box.height / element.naturalHeight);
            return {width:box.width, height:box.height, fit:style.objectFit, position:style.objectPosition,
                contentWidth:element.naturalWidth * scale, contentHeight:element.naturalHeight * scale};
        }''')
        assert measure['fit'] == 'contain' and measure['position'] == '50% 50%'
        assert measure['width'] > 0 and measure['height'] > 0
        assert measure['contentWidth'] / measure['contentHeight'] == pytest.approx(2 / 3)
        assert measure['contentWidth'] <= measure['width'] + 1 and measure['contentHeight'] <= measure['height'] + 1
        if media_type == 'vinyl':
            assert abs(measure['width'] - measure['height']) <= 1
            assert measure['width'] >= 48
            assert measure['contentWidth'] < measure['width']
        metrics.append({'id': item_id, 'media_type': media_type, 'shape': shape, **measure})
        assert (server['data_dir'] / 'covers' / name).read_bytes() == content
    invalid_member = results.locator('[data-library-series="series 9"]' if view == 'series' else f'[data-library-member="{invalid}"]')
    expect(invalid_member.locator('img')).to_have_count(0)
    expect(invalid_member.locator('.media-artwork__placeholder')).to_have_count(1)
    assert requested_covers == [], 'SVG display must never fetch a mutable covers pathname'
    with _db(server) as db:
        assert [tuple(row) for row in db.execute('SELECT id, media_type, cover_path FROM items ORDER BY id')] == before
    api = page.request.get(f"{server['url']}/api/library/items?view={view}")
    assert api.status == 200 and 'data:image' not in api.text()
    assert 'prepared_library_artwork' not in api.json()
    (tmp_path / 'genuine-library-svg.json').write_text(json.dumps({'csp': csp, 'metrics': metrics, 'mutable_cover_requests': requested_covers}, indent=2))
    page.screenshot(path=str(tmp_path / 'genuine-library-svg.png'), full_page=True)
