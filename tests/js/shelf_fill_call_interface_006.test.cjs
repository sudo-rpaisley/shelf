// SOURCE-MODEL checks, not application/HTTP/browser passes. Drive the REAL
// scanPage registration, init, picker change listener and onScan with the REAL
// app.js + Shelf Fill helper. Only DOM, storage, camera pause and fetch are
// adapters. Reuse the existing review driver's DOM/FormData/fragment fixtures;
// do not introduce a second parser or execute its app/template subprocesses.
// Run: node --test tests/js/shelf_fill_call_interface_006.test.cjs
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const root = path.resolve(__dirname, '../..');
const sourceRoot = process.env.UI_BINDINGS_006_SOURCE_ROOT || root;
const driverPath = path.join(root, 'tests/shelf_fill_review_races_20261121.cjs');
const driver = fs.readFileSync(driverPath, 'utf8');
const boundary = 'const tests = [];';
assert.equal(driver.split(boundary).length, 2, 'explicit reusable-adapter boundary');
const adapter = {exports: {}};
vm.runInNewContext(driver.split(boundary)[0] + '\nmodule.exports = {Element, Data, receipt, added};', {
    module: adapter, __dirname: path.dirname(driverPath), process: {env: {}},
    require(name) {
        if (name === 'node:child_process') return {execFileSync() {
            throw new Error('Application/template subprocesses are forbidden in this source model');
        }};
        return require(name);
    }
}, {filename: driverPath});
const {Element, Data, receipt, added} = adapter.exports;
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const response = html => ({ok: true, status: 200, text: async () => html});

async function scanPageModel() {
    const listeners = new Map();
    const document = {addEventListener(name, fn) {
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(fn);
    }};
    document.body = new Element('body', document);
    document.createElement = tag => new Element(tag, document);
    document.querySelector = selector => document.body.querySelector(selector);
    document.querySelectorAll = selector => document.body.querySelectorAll(selector);
    document.getElementById = id => document.querySelector('#' + id);
    const host = document.body.appendChild(document.createElement('main'));
    host.setAttribute('data-off-modes', '');
    host.setAttribute('data-status-labels', '{}');
    host.setAttribute('data-shelf-fill-origin', 'scan');
    const form = host.appendChild(document.createElement('form'));
    form.setAttribute('action', '/api/scan');
    form.setAttribute('hx-post', '/api/scan');
    form.setAttribute('data-after-request', 'clear-scan-input');
    form.setAttribute('data-scan-results', '#scan-results');
    for (const [name, value] of [['isbn', ''], ['mode', 'add'], ['location_id', '7'], ['_csrf', 'model-csrf']]) {
        const input = form.appendChild(document.createElement('input'));
        input.name = name; input.value = value;
        if (name === 'isbn') input.id = 'isbn-input';
    }
    // Deliberately submit a FLAT id as an adapter input: the real helper must
    // delete it, not rely on Alpine's disabled field to prevent domain mixing.
    const submit = form.appendChild(document.createElement('button'));
    submit.type = 'submit';
    const results = host.appendChild(document.createElement('div'));
    results.id = 'scan-results';
    const indicator = document.body.appendChild(document.createElement('div'));
    indicator.id = 'htmx-indicator';
    const storage = new Map([
        ['shelf_scan_mode', 'shelf_fill'], ['shelf_fill_location', '41'], ['shelf_location', '7']
    ]);
    const calls = [], trace = [], toasts = [], registrations = new Map();
    let scanResponder, placeResponder;
    const scope = {
        document, window: {csrfToken: () => 'model-csrf'}, FormData: Data,
        localStorage: {getItem: key => storage.get(key) ?? null,
            setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key)},
        Alpine: {data: (name, factory) => registrations.set(name, factory)},
        htmx: {config: {}, process() { trace.push('htmx.process'); }},
        queueMicrotask, setTimeout() {}, console,
        async fetch(url, options) {
            const call = {url, method: options?.method || 'GET', headers: {...options?.headers},
                body: options?.body ? new Map(options.body) : null};
            calls.push(call);
            if (call.method === 'GET' && url === '/api/recent-scans?mode=shelf_fill') return response('');
            if (call.method === 'GET' && url === '/api/shelf-fill/location-picker') return response(
                '<div id="shelf-fill-location-wrap"><select id="shelf-fill-location" name="location_node_id">' +
                '<option value="41">Shelf A</option><option value="42">Shelf B</option></select></div>'
            );
            if (call.method === 'POST' && url === '/api/shelf-fill/scan' && scanResponder) return scanResponder(call);
            if (call.method === 'POST' && url === '/api/shelf-fill/place' && placeResponder) return placeResponder(call);
            throw new Error('Unexpected model request: ' + call.method + ' ' + url);
        }
    };
    vm.createContext(scope);
    // scan.html loads helper then scan.js; base.html loads app.js afterwards.
    // Alpine initialization runs only once all three real sources are present.
    for (const name of ['shelf-fill-flow.js', 'scan.js', 'app.js']) {
        vm.runInContext(fs.readFileSync(path.join(sourceRoot, 'static/js', name), 'utf8'), scope, {filename: name});
    }
    scope.showToast = (text, type) => toasts.push({text, type});
    for (const listener of listeners.get('alpine:init') || []) listener();
    assert.equal(registrations.get('scanPage'), scope.scanPage);
    const page = registrations.get('scanPage')();
    page.$el = host;
    page.$watch = () => {}; // No browser reactivity is claimed by this adapter.
    page.init(); // Creates the ONE helper instance through real production wiring.
    await settle();
    assert.equal(page.mode, 'shelf_fill');
    assert.equal(page.shelfLocation, '41');
    assert.equal(page.location, '7');
    assert.deepEqual(calls.map(call => call.url), ['/api/recent-scans?mode=shelf_fill', '/api/shelf-fill/location-picker']);
    const node = document.getElementById('shelf-fill-location');
    assert(node && node.value === '41', 'real picker loader restored the stored NODE');
    const pending = host.querySelector('section');
    assert(pending && host.querySelectorAll('section').length === 1, 'init installed exactly one flow');
    trace.length = 0; // The assertions below observe onScan, after boot/history settled.
    // Observe public calls, never replace their behavior or call them instead
    // of onScan. Internal placement is exclusively the real helper's work.
    for (const name of ['begin', 'scan', 'current', 'finish', 'changed']) {
        const original = page._shelfFlow[name];
        page._shelfFlow[name] = function (...args) {
            trace.push(name);
            return original.apply(this, args);
        };
    }
    return {page, host, form, submit, results, node, pending, storage, calls, trace, toasts,
        posts: () => calls.filter(call => call.method === 'POST'),
        scanWith(fn) { scanResponder = fn; },
        placeWith(fn) { placeResponder = fn; },
        changeNode(value) { node.value = value; node.emit('change', {}); }
    };
}

function assertScan(call, code) {
    assert.equal(call.url, '/api/shelf-fill/scan');
    assert.equal(call.headers['X-CSRF-Token'], 'model-csrf');
    assert.equal(call.headers['HX-Request'], 'true');
    assert.equal(call.body.get('isbn'), code);
    assert.equal(call.body.get('mode'), 'shelf_fill');
    assert.equal(call.body.get('location_node_id'), '41');
    assert.equal(call.body.get('shelf_fill_origin'), 'scan');
    assert.equal(call.body.get('_csrf'), 'model-csrf');
    assert.equal(call.body.has('location_id'), false);
}

function assertPlace(call, id) {
    assert.equal(call.url, '/api/shelf-fill/place');
    assert.equal(call.headers['X-CSRF-Token'], 'model-csrf');
    assert.equal(call.headers['HX-Request'], 'true');
    assert.deepEqual(Object.fromEntries(call.body), {item_id: id, location_node_id: '41', shelf_fill_origin: 'scan'});
}

test('source-model: real scanPage init/onScan captures NODE before camera await and places saved ID exactly once', async () => {
    const h = await scanPageModel();
    let releasePause;
    h.page.scanner = {pause() {
        h.trace.push('camera.pause');
        return new Promise(resolve => { releasePause = resolve; });
    }};
    h.scanWith(() => response(added(917)));
    h.placeWith(() => response(receipt(917, '41')));
    const task = h.page.onScan('9780000000917');
    assert.deepEqual(h.trace.slice(0, 2), ['begin', 'camera.pause']);
    assert.equal(h.host.getAttribute('aria-busy'), 'true');
    assert.equal(h.node.disabled, true);
    assert.equal(h.posts().length, 0, 'camera pause has not resolved');
    releasePause();
    await task;
    await settle();
    assert.deepEqual(h.posts().map(call => call.url), ['/api/shelf-fill/scan', '/api/shelf-fill/place']);
    assertScan(h.posts()[0], '9780000000917');
    assertPlace(h.posts()[1], '917');
    assert.deepEqual(h.trace.filter(name => ['begin', 'scan', 'current', 'finish'].includes(name)),
        ['begin', 'scan', 'current', 'finish']);
    assert.equal(h.page.scanResult.ok, true);
    assert.equal(h.pending.children.length, 0);
    assert.equal(h.results.querySelectorAll('[data-shelf-fill-placed-node="41"]').length, 1);
    assert.equal(h.host.getAttribute('aria-busy'), 'false');
    assert.equal(h.node.disabled, false);
    assert.equal(h.submit.disabled, false);
    assert.deepEqual(h.toasts, []);
    await settle();
    assert.equal(h.posts().length, 2, 'no repeated placement or retry loop');
});

test('source-model: a real picker change to NODE42 before completion blocks auto-place; back41 requires explicit retry', async () => {
    const h = await scanPageModel();
    let completeScan;
    h.scanWith(() => new Promise(resolve => { completeScan = resolve; }));
    h.placeWith(() => response(receipt(918, '41')));
    const task = h.page.onScan('9780000000918');
    assert.equal(h.posts().length, 1);
    assertScan(h.posts()[0], '9780000000918');
    // Model a programmatic selection/change while the request is in flight;
    // native disabled controls/real browser events are outside this model.
    h.changeNode('42');
    assert.equal(h.page.shelfLocation, '42');
    assert.equal(h.storage.get('shelf_fill_location'), '42');
    completeScan(response(added(918)));
    await task;
    await settle();
    assert.equal(h.posts().length, 1, 'stale result must never auto-place at 41 OR 42');
    assert.equal(h.page.scanResult, false, 'late camera overlay is not current');
    assert.match(h.pending.textContent, /#918.*Shelf A/);
    assert.equal(h.pending.querySelector('button').disabled, true);
    h.changeNode('41');
    await settle();
    assert.equal(h.posts().length, 1, 'returning to the same node is not opt-in');
    assert.equal(h.storage.get('shelf_fill_location'), '41');
    const retry = h.pending.querySelector('button');
    assert.equal(retry.disabled, false);
    retry.click();
    await settle();
    assert.deepEqual(h.posts().map(call => call.url), ['/api/shelf-fill/scan', '/api/shelf-fill/place']);
    assertPlace(h.posts()[1], '918');
    assert.equal(h.pending.children.length, 0);
    assert.equal(h.node.value, '41');
    assert.equal(h.results.querySelector('[data-scan-item-id="918"]').dataset.shelfFillPlacedNode, '41');
});

const invalidReceipts = [
    ['missing receipt', '<div class="scan-result" data-scan-status="error">Not placed</div>'],
    ['wrong item', receipt(999, '41')],
    ['wrong node', receipt(919, '42')],
    ['missing position', receipt(919, '41').replace('<span data-testid="shelf-fill-position">#2</span>', '')]
];
for (const [reason, html] of invalidReceipts) {
    test(`source-model: HTTP200 ${reason} keeps real onScan saved-ID retry until a precise placement receipt`, async () => {
        const h = await scanPageModel();
        h.scanWith(() => response(added(919)));
        h.placeWith(() => response(html));
        await h.page.onScan('9780000000919');
        await settle();
        assert.equal(h.posts().length, 2, 'one scan and one failed automatic placement, never re-Add');
        assertScan(h.posts()[0], '9780000000919');
        assertPlace(h.posts()[1], '919');
        assert.match(h.pending.textContent, /#919.*Shelf A/);
        assert.equal(h.pending.querySelector('button').disabled, false);
        assert.equal(h.results.querySelector('[data-scan-status="added"]').getAttribute('data-shelf-fill-node'), '41');
        assert.equal(h.results.querySelector('[data-shelf-fill-placed-node]'), null);
        assert.deepEqual(h.toasts, [{text: 'Item is saved, but could not be placed. Use Retry shelving.', type: 'error'}]);
        h.placeWith(() => response(receipt(919, '41')));
        h.pending.querySelector('button').click();
        await settle();
        assert.equal(h.posts().length, 3);
        assertPlace(h.posts()[2], '919');
        assert.equal(h.pending.children.length, 0);
        assert.equal(h.results.querySelector('[data-scan-status="added"]'), null);
        assert.equal(h.results.querySelector('[data-scan-item-id="919"]').dataset.shelfFillPlacedNode, '41');
        assert.equal(h.node.disabled, false);
    });
}
