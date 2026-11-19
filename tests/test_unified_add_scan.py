"""Unified workspace contracts, authored only; no runtime qualification claimed."""

import json
from urllib.parse import urlsplit

import pytest

from app import nav
from app.database import set_setting
from app.features import set_feature_enabled
from tests.test_add_media_search import form, io, no_cookie, pairs, snapshot, value
from tests.test_connected_layout import _Markup
from tests.test_command_launcher import _marked


def test_default_workspace_has_full_manual_form_but_no_live_scan_components(editor_client, db, io):
    before = snapshot(db)
    response = editor_client.get('/add')
    assert response.status_code == 200
    no_cookie(response)
    markup = _Markup(response.text)
    root = _marked(markup, 'data-add-scan-workspace', 'div')
    assert root.attrs['data-tool'] == 'add'
    assert markup.one('h1', within=root).text.strip() == 'Add / Scan'
    assert not any(node.attrs.get('x-data') in {'scanPage', 'intakePage', 'shelfFillPage'} for node in markup.find())
    assert not markup.find(id='audit-strip') and not markup.find(id='scan-results')
    manual = form(response, 'add-media-manual')
    assert manual['attrs']['enctype'] == 'multipart/form-data'
    assert 'data-add-prepared' not in manual['attrs']
    assert value(manual, 'media_type') == ''
    assert value(manual, 'mode') == 'add'
    assert value(manual, 'context') and value(manual, 'title') == ''
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize('canonical,legacy,component,tool', [
    ('/add?tool=scan', '/scan', 'scanPage', 'scan'),
    ('/add?tool=photo', '/intake', 'intakePage', 'photo'),
    ('/add?tool=shelf', '/shelf-fill', 'shelfFillPage', 'shelf'),
])
def test_selected_tool_and_compatible_url_share_one_shell_without_other_tools(editor_client, db, io, canonical, legacy, component, tool):
    before = snapshot(db)
    for path in (canonical, legacy):
        response = editor_client.get(path)
        assert response.status_code == 200
        markup = _Markup(response.text)
        assert _marked(markup, 'data-add-scan-workspace', 'div').attrs['data-tool'] == tool
        active = [node.attrs['x-data'] for node in markup.find() if node.attrs.get('x-data') in {'scanPage', 'intakePage', 'shelfFillPage'}]
        assert active == [component]
        assert not markup.find(**{'data-testid': 'add-media-search'})
        assert len(markup.find('h1')) == 1
    assert snapshot(db) == before and io.calls == []


@pytest.mark.parametrize('tool,feature', [('photo', 'intake'), ('shelf', 'shelf_fill')])
def test_canonical_tools_do_not_bypass_original_feature_dependency(editor_client, db, tool, feature):
    set_feature_enabled(db, feature, False)
    db.commit()
    response = editor_client.get('/add', params={'tool': tool})
    assert response.status_code == 200
    assert 'data-testid="feature-disabled"' in response.text
    assert 'data-add-scan-workspace' not in response.text


@pytest.mark.parametrize('tool', ('add', 'scan', 'photo', 'shelf'))
def test_viewer_does_not_gain_editor_tools(viewer_client, tool):
    response = viewer_client.get('/add', params={'tool': tool}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/browse'
    assert 'data-add-scan-workspace' not in response.text


@pytest.mark.parametrize('mode', ('add', 'wishlist', 'lend', 'return', 'move', 'inventory', 'lookup', 'quick_rate'))
def test_scan_action_links_seed_any_offered_action_without_seeding_a_submission(editor_client, mode):
    response = editor_client.get('/add', params={'tool': 'scan', 'scan_mode': mode})
    assert response.status_code == 200
    markup = _Markup(response.text)
    scanner = markup.one('div', **{'x-data': 'scanPage'})
    assert scanner.attrs['data-seed-mode'] == mode
    assert 'data-seed-code' not in scanner.attrs
    assert markup.one('select', id='scan-action').attrs['@change'] == 'setMode(mode)'


def test_disabled_explicit_action_is_refused_not_changed_to_a_stored_operation(editor_client, db):
    set_feature_enabled(db, 'lending', False)
    db.commit()
    response = editor_client.get('/add?tool=scan&scan_mode=lend')
    assert response.status_code == 422
    assert 'x-data="scanPage"' not in response.text


def test_navigation_consolidates_tools_but_keeps_store_and_visibility_policy(editor_client, db):
    set_setting(db, 'vision_provider', 'ollama')
    db.commit()
    nav.invalidate_cache()
    markup = _Markup(editor_client.get('/add').text)
    primary = _marked(markup, 'data-media-navigation', 'nav')
    entry = markup.testid('add-media-entry')
    assert entry.text.strip() == 'Add / Scan' and urlsplit(entry.attrs['href']).path == '/add'
    assert primary in entry.ancestors
    assert not markup.find('details', **{'data-nav-group': 'add'})
    assert not [node for node in markup.find('a', within=primary) if node.attrs.get('data-nav-tab') in {'scan', 'intake', 'shelf-fill'}]
    assert markup.one('a', within=primary, **{'data-nav-tab': 'store'})
    tools = markup.one('nav', **{'aria-label': 'Add and scan tools'})
    assert any('Photo Intake' in link.text for link in markup.find('a', within=tools))
    set_setting(db, 'nav_hidden_tabs', json.dumps(['scan', 'intake', 'shelf-fill']))
    db.commit()
    nav.invalidate_cache()
    hidden = _Markup(editor_client.get('/add').text)
    assert hidden.testid('add-media-entry')
    tool_nav = hidden.one('nav', **{'aria-label': 'Add and scan tools'})
    assert [node.text.strip() for node in hidden.find('a', within=tool_nav)] == ['Search / Manual']


@pytest.mark.parametrize('query', ('tool=unknown', 'tool=add&scan_mode=move', 'tool=scan&scan_mode=delete', 'tool=scan&mode=wishlist', 'tool=photo&receipt=x', 'tool=scan&tool=photo'))
def test_workspace_query_remains_finite_and_unambiguous(editor_client, db, io, query):
    before = snapshot(db)
    response = editor_client.get('/add?' + query)
    assert response.status_code in {400, 422}
    assert 'x-data="scanPage"' not in response.text and snapshot(db) == before and io.calls == []


def test_legacy_audit_seed_and_manual_copy_links_keep_their_fields(editor_client):
    scan = editor_client.get('/scan?mode=add&code=9780441172719')
    markup = _Markup(scan.text)
    assert markup.one('div', **{'x-data': 'scanPage'}).attrs['data-seed-code'] == '9780441172719'
    manual = editor_client.get('/scan?add=manual&from=not-an-id')
    assert manual.status_code == 200 and 'data-manual-open="true"' in manual.text
