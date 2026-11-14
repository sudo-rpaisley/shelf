"""Closed grammar, issued bindings, exact captures and deterministic FD races.

These are local bounded compatibility tests, not full SVG or atomic-FS claims.
"""

import base64
from dataclasses import FrozenInstanceError, replace
import errno
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from app import config
from app.services import library_artwork as artwork


RECTANGLE = (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450">'
             b'<rect width="300" height="450" fill="#3e6267"/></svg>')


def _svg(children=b'', attrs=b'', width=b'300', height=b'450'):
    return (b'<svg xmlns="http://www.w3.org/2000/svg" width="' + width
            + b'" height="' + height + b'" ' + attrs + b'>' + children + b'</svg>')


def _row(item_id=7, path='covers/7.svg'):
    return {'id': item_id, 'cover_path': path}


def _context(rows):
    return {'result_kind': 'items', 'items': rows}


def _prepare(content=RECTANGLE, *, name='7.svg', item_id=7, raw=None):
    (config.COVERS_DIR / name).write_bytes(content)
    raw = 'covers/' + name if raw is None else raw
    prepared = artwork.prepare_library_artwork(_context([_row(item_id, raw)]))
    return prepared, prepared.source_for(item_id, raw)


def _instrument(monkeypatch):
    """Track actual opens/closes/reads, retaining the platform capability probe."""
    real_open, real_close, real_read = os.open, os.close, os.read
    live, opened, reads = set(), [], []

    def open_fd(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        assert descriptor not in live
        live.add(descriptor)
        opened.append((path, flags, kwargs, descriptor))
        return descriptor

    def close_fd(descriptor):
        live.remove(descriptor)
        return real_close(descriptor)

    def read_fd(descriptor, amount):
        data = real_read(descriptor, amount)
        reads.append((descriptor, amount, data))
        return data

    monkeypatch.setattr(os, 'open', open_fd)
    monkeypatch.setattr(os, 'close', close_fd)
    monkeypatch.setattr(os, 'read', read_fd)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {open_fd})
    return live, opened, reads


@pytest.mark.parametrize('name', ['7.svg', 'square-artwork.svg', 'Album_19-X.SvG', 'x' * 128 + '.svg'])
@pytest.mark.parametrize('bare', [False, True])
def test_genuine_referenced_rectangle_has_exact_bytes_and_frozen_binding(name, bare):
    raw = name if bare else 'covers/' + name
    prepared, source = _prepare(name=name, raw=raw)
    assert type(source) is artwork.ValidatedLocalSvg
    assert source.item_id == 7 and source.raw_cover_path == raw
    assert source.captured_bytes == RECTANGLE
    assert base64.b64decode(source.image_src.removeprefix('data:image/svg+xml;base64,'), validate=True) == RECTANGLE
    assert (config.COVERS_DIR / name).read_bytes() == RECTANGLE
    assert prepared.source_for(8, raw) is None
    assert prepared.source_for(7, name if not bare else 'covers/' + name) is None
    for obj, field in ((source, 'item_id'), (prepared, '_issuer')):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
    assert type(prepared._sources) is MappingProxyType
    with pytest.raises(TypeError):
        prepared._sources[(7, raw)] = source
    with pytest.raises(TypeError):
        artwork.ValidatedLocalSvg()
    with pytest.raises(TypeError):
        artwork.ValidatedLocalSvg(image_src='data:image/svg+xml;base64,AAAA')
    with pytest.raises(TypeError):
        artwork.PreparedLibraryArtwork({(7, raw): source})
    with pytest.raises(TypeError):
        replace(source, item_id=8)


@pytest.mark.parametrize('children, attrs', [
    (b'<rect width=".000001" height="4096" x="-4096" y="+4096" rx="1" ry="2"/>', b''),
    (b'<circle r="1.000001" cx="-2.5" cy=".5" fill="none" stroke="#ABC"/>', b'fill="#112233"'),
    (b'<ellipse rx="3" ry="4"/><line/><line x1="-1" y1="2" x2="3" y2="4"/>', b''),
    (b'<polyline points="0,0 1, 2\t3 ,4"/><polygon points="-1 -1,2 2,3 4"/>', b''),
    (b'<rect width="300" height="450"/><circle r="42"/><ellipse rx="2" ry="3"/>'
     b'<line stroke="#123"/><polyline points="0 0 1 1"/><polygon points="0 0 1 1 0 1"/>',
     b'viewBox="-4096 +4096 300 450" version="1.1" preserveAspectRatio="xMidYMid meet"'
     b' opacity=".5" fill-opacity="0" stroke-opacity="1" stroke-width="64" stroke-linecap="round" stroke-linejoin="bevel"'),
    (b'\n\t<rect width="1." height="+2.0" opacity=" 0.123456 "/>\r\n', b''),
])
def test_general_primitive_compositions_are_not_filename_or_color_special_cases(children, attrs):
    content = _svg(children, attrs)
    _, source = _prepare(content, name='general_primitives.svg')
    assert source is not None and source.captured_bytes == content


class _Hostile:
    def __hash__(self):
        raise AssertionError('must check exact primitive before hash')

    def __eq__(self, other):
        raise AssertionError('must check exact primitive before equality')

    def __str__(self):
        raise AssertionError('must not coerce metadata')

    __int__ = __str__


class _Int(int):
    __hash__, __eq__, __str__ = _Hostile.__hash__, _Hostile.__eq__, _Hostile.__str__


class _Text(str):
    __hash__, __eq__, __str__ = _Hostile.__hash__, _Hostile.__eq__, _Hostile.__str__


@pytest.mark.parametrize('item_id', [0, -1, 2**63, True, False, '7', 7.0, None, _Hostile(), _Int(7)],
                         ids=lambda value: type(value).__name__)
def test_exact_positive_bounded_id_before_hash_equality_conversion(monkeypatch, item_id):
    prepared, _ = _prepare()
    live, opened, reads = _instrument(monkeypatch)
    assert prepared.source_for(item_id, 'covers/7.svg') is None
    assert artwork.prepare_library_artwork(_context([_row(item_id)])).source_for(item_id, 'covers/7.svg') is None
    assert not live and not opened and not reads


@pytest.mark.parametrize('raw', [None, '', b'covers/7.svg', _Hostile(), _Text('covers/7.svg'),
    '/covers/7.svg', '../7.svg', 'covers/../7.svg', 'covers/x/7.svg', 'covers\\7.svg',
    'https://example.invalid/7.svg', '//example.invalid/7.svg', 'data:image/svg+xml;base64,AAAA',
    'covers/%2f7.svg', 'covers/%252f7.svg', 'covers/7.svg?x', 'covers/7.svg#x', ' covers/7.svg',
    'covers/7.svg\n', 'covers/é.svg', 'covers/' + 'x' * 129 + '.svg', 'covers/7.png'],
                         ids=lambda value: type(value).__name__)
def test_reference_rejection_does_zero_artwork_io(monkeypatch, raw):
    prepared, _ = _prepare()
    live, opened, reads = _instrument(monkeypatch)
    assert prepared.source_for(7, raw) is None
    assert artwork.prepare_library_artwork(_context([_row(path=raw)])).source_for(7, raw) is None
    assert not live and not opened and not reads


def test_only_factory_issued_sources_and_no_item_supplied_authority(monkeypatch):
    prepared, source = _prepare()
    fake = object.__new__(artwork.ValidatedLocalSvg)
    other, other_source = _prepare()
    for forged in (fake, other_source, {'image_src': source.image_src}, _Hostile()):
        # Deliberately corrupt an internal mapping to test issuance checking;
        # no constructor accepts this mapping from a caller.
        object.__setattr__(prepared, '_sources', MappingProxyType({(7, 'covers/7.svg'): forged}))
        assert prepared.source_for(7, 'covers/7.svg') is None
    calls = []
    monkeypatch.setattr(artwork, '_open_root', lambda: calls.append('open'))
    rows = [{**_row(path=None), 'validated_artwork_source': source, 'image_src': source.image_src,
             'captured_bytes': RECTANGLE, 'validated': True}]
    assert artwork.prepare_library_artwork(_context(rows)).source_for(7, None) is None
    assert calls == []
    assert other.source_for(7, 'covers/7.svg') is other_source


HOSTILE = [
    b'', b'not xml', b'\xef\xbb\xbf' + RECTANGLE, RECTANGLE.decode().encode('utf-16'), b'\x1f\x8b' + RECTANGLE,
    b'<?xml version="1.0"?>' + RECTANGLE, b'<?xml version="1.0" encoding="ASCII"?>' + RECTANGLE,
    b'<?target content?>' + RECTANGLE, b'<!--comment-->' + RECTANGLE,
    b'<!DOCTYPE svg>' + RECTANGLE, b'<!DOCTYPE svg SYSTEM "file:///etc/passwd">' + RECTANGLE,
    b'<!DOCTYPE svg [<!ENTITY x "x">]>' + RECTANGLE,
    RECTANGLE.replace(b'<rect', b'<![CDATA[ ]]><rect'), RECTANGLE.replace(b'<rect', b'<!--x--><rect'),
    RECTANGLE.replace(b'<rect', b'<?x y?><rect'), RECTANGLE.replace(b'<rect', b'&#32;<rect'),
    RECTANGLE.replace(b'#3e6267', b'&#35;123'), RECTANGLE.replace(b'<rect', b'x<rect'),
    RECTANGLE.replace(b'<rect', b'\xc3\xa9<rect'), RECTANGLE + RECTANGLE,
    RECTANGLE.replace(b' xmlns="http://www.w3.org/2000/svg"', b''),
    RECTANGLE.replace(b'http://www.w3.org/2000/svg', b'http://other.invalid'),
    RECTANGLE.replace(b'<svg ', b'<s:svg xmlns:s="http://www.w3.org/2000/svg" ').replace(b'</svg>', b'</s:svg>'),
    RECTANGLE.replace(b'<svg ', b'<svg xmlns:p="http://www.w3.org/2000/svg" '),
    RECTANGLE.replace(b'<rect ', b'<rect xmlns="http://www.w3.org/2000/svg" '),
    RECTANGLE.replace(b'<rect ', b'<rect xmlns="" '),
    RECTANGLE.replace(b'<rect ', b'<rect xmlns:p="http://other.invalid" '),
    RECTANGLE.replace(b'<rect ', b'<rect xml:base="https://other.invalid" '),
    RECTANGLE.replace(b'<rect ', b'<rect width="1" '),
]
for tag in ('script', 'style', 'g', 'svg', 'text', 'path', 'image', 'use', 'a', 'foreignObject',
            'animate', 'set', 'filter', 'linearGradient', 'pattern', 'defs', 'title', 'desc'):
    HOSTILE.append(_svg(f'<{tag}/>'.encode()))
for attr in ('id="x"', 'class="x"', 'style="fill:red"', 'transform="scale(2)"', 'onload="x()"',
             'href="#x"', 'xlink:href="#x"', 'filter="url(#x)"', 'fill="red"', 'fill="url(#x)"',
             'stroke="currentColor"', 'stroke="inherit"', 'stroke-linejoin="arcs"', 'stroke-linecap="triangle"'):
    HOSTILE.append(_svg(b'<rect width="2" height="3" ' + attr.encode() + b'/>'))


@pytest.mark.parametrize('content', HOSTILE)
def test_hostile_structural_xml_and_namespace_declaration_events_reject(content):
    _, source = _prepare(content)
    assert source is None


@pytest.mark.parametrize('value', ['0', '-1', '4096.000001', '1e2', 'NaN', 'Infinity', '1px',
    '1%', '1.0000001', '1 2', '1,2', '+', '.', '0000000000000000000000001', '1\u00a0'])
def test_root_dimension_closed_decimal_grammar(value):
    _, source = _prepare(_svg(width=value.encode()))
    assert source is None


@pytest.mark.parametrize('children, attrs', [
    (b'<rect width="0" height="1"/>', b''), (b'<rect width="1" height="1" rx="0"/>', b''),
    (b'<circle r="-1"/>', b''), (b'<circle/>', b''), (b'<ellipse rx="1"/>', b''),
    (b'<line x1="-4096.000001"/>', b''), (b'<line x1="1e0"/>', b''),
    (b'<rect width="1" height="1"><line/></rect>', b''),
    (b'<line/>', b'viewBox="0 0 0 1"'), (b'<line/>', b'viewBox="0 0 1 1 2"'),
    (b'<line/>', b'version="2.0"'), (b'<line/>', b'preserveAspectRatio="none"'),
    (b'<line/>', b'opacity="1.000001"'), (b'<line/>', b'fill-opacity="-.000001"'),
    (b'<line/>', b'stroke-width="64.000001"'), (b'<line/>', b'stroke-width="-1"'),
])
def test_element_specific_geometry_and_presentation_rejections(children, attrs):
    assert _prepare(_svg(children, attrs))[1] is None


@pytest.mark.parametrize('points', ['0 0', '0 0 1', '0 0 1 1 2', ',0 0 1 1', '0 0 1 1,',
    '0,,0 1 1', '0 0,,1 1', '0 0 1-1', '0 0 1e0 1', '0 0 4097 1'])
def test_malformed_points_are_not_repaired(points):
    assert _prepare(_svg(b'<polyline points="' + points.encode() + b'"/>'))[1] is None


def test_element_vertex_attribute_and_numeric_budget_boundaries():
    assert _prepare(_svg(b'<line/>' * 127))[1] is not None  # root included
    assert _prepare(_svg(b'<line/>' * 128))[1] is None
    points = b' '.join([b'0 1'] * 128)
    assert _prepare(_svg(b'<polygon points="' + points + b'"/>'))[1] is not None
    assert _prepare(_svg(b'<polygon points="' + points + b' 0 1"/>'))[1] is None
    padded = b' ' * (4096 - len(points)) + points
    assert _prepare(_svg(b'<polyline points="' + padded + b'"/>'))[1] is not None
    assert _prepare(_svg(b'<polyline points=" ' + padded + b'"/>'))[1] is None
    # 15*256 + 254 + 2 root numbers = the exact 4096 numeric-token cap.
    children = b'<polyline points="' + points + b'"/>'
    last = b'<polyline points="' + b' '.join([b'0 1'] * 127) + b'"/>'
    assert _prepare(_svg(children * 15 + last))[1] is not None
    assert _prepare(_svg(children * 16))[1] is None
    attrs = b' '.join(f'unknown{n}="0"'.encode() for n in range(17))
    assert _prepare(_svg(b'<line ' + attrs + b'/>'))[1] is None
    assert artwork._MAX_ATTRIBUTES == 16 and artwork._MAX_DEPTH == 2


@pytest.mark.parametrize('size, issued', [(65535, True), (65536, True), (65537, False)])
def test_file_size_boundary_and_eof_is_observed(monkeypatch, size, issued):
    content = RECTANGLE + b' ' * (size - len(RECTANGLE))
    (config.COVERS_DIR / '7.svg').write_bytes(content)
    live, opened, reads = _instrument(monkeypatch)
    source = artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg')
    assert (source is not None) is issued
    assert not live
    if issued:
        assert source.captured_bytes == content and reads[-1][2] == b''
    else:
        assert not reads  # precheck rejects known oversized files
    assert all(flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC for _, flags, _, _ in opened)
    assert opened[-1][1] & os.O_NONBLOCK


@pytest.mark.parametrize('problem', ['leaf-symlink', 'root-symlink', 'ancestor-symlink', 'hardlink', 'fifo', 'directory', 'missing'])
def test_unsafe_files_and_roots_never_consume_contents(monkeypatch, tmp_path, problem):
    path = config.COVERS_DIR / '7.svg'
    target = tmp_path / 'actual.svg'
    target.write_bytes(RECTANGLE)
    if problem == 'leaf-symlink':
        path.symlink_to(target)
    elif problem == 'hardlink':
        os.link(target, path)
    elif problem == 'fifo':
        os.mkfifo(path)
    elif problem == 'directory':
        path.mkdir()
    elif problem in ('root-symlink', 'ancestor-symlink'):
        path.write_bytes(RECTANGLE)
        linked = tmp_path / 'linked'
        linked.symlink_to(config.COVERS_DIR if problem == 'root-symlink' else config.COVERS_DIR.parent, target_is_directory=True)
        monkeypatch.setattr(config, 'COVERS_DIR', linked if problem == 'root-symlink' else linked / 'covers')
    live, opened, reads = _instrument(monkeypatch)
    assert artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg') is None
    assert not live and not reads


@pytest.mark.parametrize('flag', ['O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK', 'dir-fd'])
def test_missing_platform_capabilities_fail_closed_before_open(monkeypatch, flag):
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    live, opened, reads = _instrument(monkeypatch)
    if flag == 'dir-fd':
        monkeypatch.setattr(os, 'supports_dir_fd', set())
    else:
        monkeypatch.delattr(os, flag)
    assert artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg') is None
    assert not live and not opened and not reads


def test_root_walk_component_limit_and_relative_root(monkeypatch, tmp_path):
    # A relative path begins from a pinned cwd descriptor; neither path resolves symlinks.
    monkeypatch.chdir(tmp_path)
    current = Path('.')
    for count in range(64):
        current /= 'd'
        current.mkdir()
    (current / '7.svg').write_bytes(RECTANGLE)
    monkeypatch.setattr(config, 'COVERS_DIR', current)
    assert artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg') is not None
    current /= 'd'
    current.mkdir()
    (current / '7.svg').write_bytes(RECTANGLE)
    monkeypatch.setattr(config, 'COVERS_DIR', current)
    live, opened, reads = _instrument(monkeypatch)
    assert artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg') is None
    assert not live and not opened and not reads


@pytest.mark.parametrize('operation', ['open', 'fstat', 'read'])
@pytest.mark.parametrize('error', [errno.EACCES, errno.EIO, errno.ENOENT])
def test_expected_fs_errors_close_all_descriptors(monkeypatch, operation, error):
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    live, opened, reads = _instrument(monkeypatch)
    original = getattr(os, operation)

    def fail(*args, **kwargs):
        if operation != 'open' or kwargs.get('dir_fd') is not None and args[0] == '7.svg':
            raise OSError(error, 'synthetic deterministic error')
        return original(*args, **kwargs)

    monkeypatch.setattr(os, operation, fail)
    if operation == 'open':
        monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {fail})
    assert artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg') is None
    assert not live


@pytest.mark.parametrize('mutation', ['rename', 'symlink-replace', 'grow', 'truncate', 'in-place', 'restore-size'])
def test_controlled_descriptor_races_never_fetch_subsequent_path(monkeypatch, mutation):
    path = config.COVERS_DIR / '7.svg'
    content = RECTANGLE + b' ' * 9000
    path.write_bytes(content)
    replacement = config.COVERS_DIR / 'replacement.svg'
    replacement.write_bytes(_svg(b'<script/>'))
    live, opened, reads = _instrument(monkeypatch)
    original_read = os.read
    changed = []

    def racing_read(descriptor, amount):
        data = original_read(descriptor, min(amount, 100))
        if not changed:
            changed.append(True)
            if mutation in ('rename', 'symlink-replace'):
                path.rename(config.COVERS_DIR / 'old.svg')
                if mutation == 'rename':
                    replacement.rename(path)
                else:
                    path.symlink_to(replacement)
            elif mutation == 'grow':
                with path.open('ab') as stream:
                    stream.write(b'x' * 65536)
            elif mutation == 'truncate':
                path.write_bytes(b'')
            elif mutation == 'in-place':
                with path.open('r+b') as stream:
                    stream.write(b'<!DOCTYPE evil>')
            else:
                path.write_bytes(b'X' * len(content))
        return data

    monkeypatch.setattr(os, 'read', racing_read)
    source = artwork.prepare_library_artwork(_context([_row()])).source_for(7, 'covers/7.svg')
    assert changed and not live
    if mutation in ('rename', 'symlink-replace'):
        # Rename may change descriptor ctime and reject; if issued, only old bytes.
        assert source is None or source.captured_bytes == content
    else:
        assert source is None
    assert sum(len(data) for _, _, data in reads) <= 65537


def test_aggregate_read_limit_includes_rejected_captures_and_growth_probes(monkeypatch):
    for number in range(8):
        # Entire files are captured before parser rejection, including hostile tails.
        (config.COVERS_DIR / f'{number}.svg').write_bytes(RECTANGLE + b'X' * (65536 - len(RECTANGLE)))
    live, opened, reads = _instrument(monkeypatch)
    prepared = artwork.prepare_library_artwork(_context([_row(n + 1, f'covers/{n}.svg') for n in range(8)]))
    assert not live and not prepared._sources
    assert sum(len(data) for _, _, data in reads) == 262144
    assert [name for name, _, _, _ in opened if str(name).endswith('.svg')] == [f'{n}.svg' for n in range(4)]


def test_row_attempt_and_uri_occurrence_caps_do_not_traverse_remaining_groups(monkeypatch):
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    visited = []

    def members():
        for number in range(10000):
            visited.append(number)
            yield _row(number + 1, None if number < 127 else 'covers/7.svg')

    representative = _row(10001, None)
    context = {'result_kind': 'groups', 'groups': [{'representative': representative, 'members': members()}]}
    live, opened, reads = _instrument(monkeypatch)
    assert not artwork.prepare_library_artwork(context)._sources
    assert visited == list(range(127)) and not live and not opened and not reads
    rows = [_row(n + 1) for n in range(40)]
    prepared = artwork.prepare_library_artwork(_context(rows))
    assert len(prepared._sources) == 32
    assert len([entry for entry in opened if entry[0] == '7.svg']) == 32
    assert sum(len(source.image_src) for source in prepared._sources.values()) == 32 * len(next(iter(prepared._sources.values())).image_src)
    assert sum(len(source.image_src) for source in prepared._sources.values()) <= 393216
    # Exercise the response guard independently of the tighter aggregate-read cap.
    monkeypatch.setattr(artwork, '_MAX_URI_CHARS', 2 * len(next(iter(prepared._sources.values())).image_src))
    assert len(artwork.prepare_library_artwork(_context(rows))._sources) == 2
    assert not live


def test_failed_eligible_attempts_are_charged_and_series_order_is_native(monkeypatch):
    attempted = []
    original = artwork._capture

    def capture(root, name, budget):
        attempted.append(name)
        return original(root, name, budget)

    monkeypatch.setattr(artwork, '_capture', capture)
    rows = [_row(n + 1, f'covers/{n}.svg') for n in range(40)]
    context = {'result_kind': 'series', 'series_results': [{'representative': row} for row in rows]}
    assert not artwork.prepare_library_artwork(context)._sources
    assert attempted == [f'{n}.svg' for n in range(32)]


def test_group_representative_then_remaining_members_without_context_mutation(monkeypatch):
    rows = [_row(n, f'covers/{n}.svg') for n in (9, 3, 7)]
    for row in rows:
        (config.COVERS_DIR / f'{row["id"]}.svg').write_bytes(RECTANGLE)
    context = {'result_kind': 'groups', 'groups': [{'representative': rows[2], 'members': rows}]}
    live, opened, reads = _instrument(monkeypatch)
    prepared = artwork.prepare_library_artwork(context)
    assert [entry[0] for entry in opened if str(entry[0]).endswith('.svg')] == ['7.svg', '9.svg', '3.svg']
    assert list(prepared._sources) == [(7, 'covers/7.svg'), (9, 'covers/9.svg'), (3, 'covers/3.svg')]
    assert context['groups'][0]['members'] is rows and len(rows) == 3 and not live


def test_lookup_is_pure_after_file_and_input_mapping_changes(monkeypatch):
    row = _row()
    (config.COVERS_DIR / '7.svg').write_bytes(RECTANGLE)
    prepared = artwork.prepare_library_artwork(_context([row]))
    source = prepared.source_for(7, 'covers/7.svg')
    row.update(id=8, cover_path=None)
    (config.COVERS_DIR / '7.svg').write_bytes(b'hostile replacement')
    live, opened, reads = _instrument(monkeypatch)
    assert prepared.source_for(7, 'covers/7.svg') is source
    assert source.captured_bytes == RECTANGLE
    assert not prepared.source_for(8, None)
    assert not live and not opened and not reads
