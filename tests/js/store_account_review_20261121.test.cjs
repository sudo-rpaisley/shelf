/* Standalone source regression: node --test tests/js/store_account_review_20261121.test.cjs
 * No app imports, server, browser, providers, or third-party packages. The IDB
 * double models exclusive readwrite callbacks; real multi-tab coverage is in
 * test_store_account_review_races_20261121.py (run only after candidate freeze).
 */
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const sourceRoot = process.env.STORE_ACCOUNT_REVIEW_SOURCE_ROOT || root;
const source = fs.readFileSync(path.join(sourceRoot, 'static/js/store.js'), 'utf8');
const key = 'shelf-store-queue';
const dataKey = 'shelf-store-data-owner:1';
const deferred = () => {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return {promise, resolve};
};
const settle = () => new Promise(resolve => setImmediate(resolve));
function element() {
    const classes = new Set(['hidden']);
    const listeners = new Map();
    return {
        textContent: '', value: '', disabled: false, children: [], attrs: {}, dataset: {},
        classList: {add(...xs) { xs.forEach(x => classes.add(x)); }, remove(...xs) { xs.forEach(x => classes.delete(x)); },
            contains(x) { return classes.has(x); }, toggle(x, on) { if (on) classes.add(x); else classes.delete(x); }},
        getAttribute(k) { return this.attrs[k] ?? null; },
        setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
        appendChild(el) { this.children.push(el); },
        addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); },
        emit(name) { return Promise.all((listeners.get(name) || []).map(fn => fn({target: this, preventDefault() {}}))); },
    };
}
function sharedStorage() {
    const values = new Map([['shelf-store-queue-owner', '1'], [key, '[]']]);
    let tail = Promise.resolve();
    let transactions = 0;
    const indexedDB = {
        open() {
            const request = {};
            queueMicrotask(() => {
                request.result = {
                    createObjectStore() {}, close() {},
                    transaction() {
                        transactions++;
                        const done = deferred();
                        const previous = tail;
                        tail = done.promise;
                        let aborted = false;
                        const tx = {
                            abort() { aborted = true; },
                            objectStore() { return {get() {
                                const read = {};
                                previous.then(() => {
                                    try { read.onsuccess(); }
                                    catch (error) { tx.error = error; aborted = true; }
                                    if (aborted) { if (tx.onabort) tx.onabort(); }
                                    else if (tx.oncomplete) tx.oncomplete();
                                    done.resolve();
                                });
                                return read;
                            }}; },
                        };
                        return tx;
                    },
                };
                if (request.onupgradeneeded) request.onupgradeneeded();
                request.onsuccess();
            });
            return request;
        },
    };
    return {values, indexedDB, get transactions() { return transactions; },
        hold() { const held = deferred(); const previous = tail; tail = previous.then(() => held.promise); return held.resolve; }};
}
function page(shared = sharedStorage(), clock = null) {
    const elements = new Map();
    let initialize;
    const el = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    };
    const ctx = {
        console, TextEncoder, Uint8Array, URL, Promise, setTimeout, clearTimeout,
        Date: clock ? class extends Date {
            constructor(...args) { super(...(args.length ? args : [clock.now])); }
            static now() { return clock.now; }
        } : Date,
        navigator: {onLine: false},
        location: {origin: 'https://shelf.test'},
        document: {
            cookie: 'csrf_token=unchanged-session', body: {dataset: {storeUserId: '1', storeSession: 'unchanged-session', storeCanSync: '1'}},
            getElementById: el, createElement: element,
            addEventListener(name, fn) { if (name === 'DOMContentLoaded') initialize = fn; },
            querySelector(selector) { return el((selector.match(/data-testid="([^"]+)"/) || [])[1] || selector); },
        },
        localStorage: {
            getItem(k) { const value = shared.values.get(k) ?? null; if (ctx.onRead) ctx.onRead(k); return value; },
            setItem(k, v) { if (ctx.failWrites && k === key) throw new Error('QuotaExceededError'); shared.values.set(k, String(v)); },
            removeItem(k) { shared.values.delete(k); },
        },
        indexedDB: shared.indexedDB,
        fetch() { throw new Error('Unexpected fetch'); },
        addEventListener() {},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    // Export only inside this test VM, not in shipped browser code.
    vm.runInContext(source.replace(/\}\)\(\);\s*$/, `
        globalThis.storeTest = {
            refreshData, enqueue, flushQueue, showVerdict, getQueue, loadLocalData, toggleCamera, updateStatus,
            getIndex: function () { return index; },
            getFetchedAt: function () { return fetchedAt; },
            isBlocked: function () { return sessionBlocked; },
            setAge: function (age) { fetchedAt = Date.now() - age; },
            setCanSync: function (value) { canSync = value; },
            prime: async function () {
                ownerId = '1'; pageSession = 'unchanged-session'; sessionTag = 'session-hash';
                DATA_KEY = '${dataKey}'; canSync = true;
                if (typeof AUTH_KEY !== 'undefined') AUTH_KEY = 'shelf-store-auth-owner:1';
                if (typeof accessEpoch !== 'undefined') accessEpoch = localStorage.getItem(AUTH_KEY) || '';
                await activateQueue();
            }
        };
    })();`), ctx);
    return {ctx, el, shared, api: ctx.storeTest, initialize: () => initialize()};
}
const snapshot = (code = '9780306406157', title = 'Owned control') => ({
    generated_at: new Date().toISOString(), count: 1,
    items: [{title, authors: null, owned: true, codes: [code]}],
});
function response(body, {url = '/api/store/data', status = 200, type = 'application/json', redirected = false} = {}) {
    return {ok: status >= 200 && status < 300, status, redirected,
        url: 'https://shelf.test' + url,
        headers: {get(name) { return name.toLowerCase() === 'content-type' ? type : null; }},
        json: async () => { if (typeof body === 'string') throw new SyntaxError('Unexpected token <'); return body; }};
}
async function primeOnline(p) {
    await p.api.prime();
    p.ctx.navigator.onLine = true;
    p.ctx.fetch = async () => response(snapshot());
    await p.api.refreshData();
}

test('followed login HTML revokes unchanged-CSRF cache, without removing any queued scan', async () => {
    const p = page();
    await primeOnline(p);
    await p.api.enqueue('9780441172710');
    const before = p.shared.values.get(key);
    p.ctx.fetch = async () => response('<html>login</html>', {url: '/login', redirected: true, type: 'text/html'});
    await p.api.refreshData();
    assert.equal(p.api.isBlocked(), true);
    assert.equal(p.shared.values.get(dataKey), undefined);
    assert.deepEqual(Object.keys(p.api.getIndex()), []);
    assert.equal(p.shared.values.get(key), before);
    assert.equal(p.ctx.document.cookie, 'csrf_token=unchanged-session');
});

test('true network failure retains a still-valid owner/session catalogue', async () => {
    const p = page();
    await primeOnline(p);
    const cache = p.shared.values.get(dataKey);
    p.ctx.fetch = async () => { throw new TypeError('Failed to fetch'); };
    await p.api.refreshData();
    assert.equal(p.api.isBlocked(), false);
    assert.equal(p.api.getIndex()['9780306406157'].title, 'Owned control');
    assert.equal(p.shared.values.get(dataKey), cache);
});

test('body-stream network error or abort retains valid cache and settles refresh busy state', async () => {
    for (const name of ['TypeError', 'AbortError']) {
        const p = page();
        await primeOnline(p);
        const cache = p.shared.values.get(dataKey);
        const interrupted = response(null);
        interrupted.json = async () => { const error = new Error('body stream interrupted'); error.name = name; throw error; };
        p.ctx.fetch = async () => interrupted;
        await p.api.refreshData();
        assert.equal(p.api.isBlocked(), false);
        assert.equal(p.shared.values.get(dataKey), cache);
        assert.equal(p.api.getIndex()['9780306406157'].title, 'Owned control');
        assert.equal(p.el('status-line').attrs['aria-busy'], undefined);
    }
});

test('unexpected non-JSON or malformed JSON is not treated as an offline outage', async () => {
    for (const bad of [response('<html>oops</html>', {type: 'text/html'}), response('<malformed>'), response({error: 'auth'})]) {
        const p = page();
        await primeOnline(p);
        p.ctx.fetch = async () => bad;
        await p.api.refreshData();
        assert.equal(p.api.isBlocked(), true);
        assert.equal(p.shared.values.get(dataKey), undefined);
    }
});

test('reverse-order catalogue completion cannot replace a newer snapshot', async () => {
    const p = page();
    await primeOnline(p);
    const old = deferred();
    p.ctx.fetch = () => old.promise;
    const first = p.api.refreshData();
    p.ctx.fetch = async () => response(snapshot('9780306406157', 'New generation'));
    await p.api.refreshData();
    old.resolve(response(snapshot('9780306406157', 'Stale generation')));
    await first;
    assert.equal(p.api.getIndex()['9780306406157'].title, 'New generation');
    assert.equal(p.el('status-line').attrs['aria-busy'], undefined);
});

test('an older refusal cannot revoke a newer successful catalogue generation', async () => {
    const p = page();
    await primeOnline(p);
    const old = deferred();
    p.ctx.fetch = () => old.promise;
    const first = p.api.refreshData();
    p.ctx.fetch = async () => response(snapshot());
    await p.api.refreshData();
    old.resolve(response({}, {status: 403}));
    await first;
    assert.equal(p.api.isBlocked(), false);
    assert.equal(p.api.getIndex()['9780306406157'].title, 'Owned control');
});

test('cross-tab enqueue interleaving is serialized, not blind last-writer-wins', async () => {
    const shared = sharedStorage();
    const a = page(shared), b = page(shared);
    await a.api.prime(); await b.api.prime();
    let second;
    a.ctx.onRead = k => {
        if (k !== key) return;
        a.ctx.onRead = null;
        second = b.api.enqueue('9780306406157');
    };
    await a.api.enqueue('9780441172710');
    await second;
    assert.deepEqual(JSON.parse(shared.values.get(key)).map(e => e.isbn).sort(), ['9780306406157', '9780441172710']);
    assert.ok(shared.transactions >= 4, 'every migration/enqueue must acquire the shared transaction');
});

test('a delayed successful batch cannot delete another tab\'s newer scan of the same code', async () => {
    const a = page();
    const b = page(a.shared);
    await a.api.prime(); await b.api.prime();
    await a.api.enqueue('9780306406157');
    const held = deferred();
    a.ctx.navigator.onLine = true;
    a.ctx.fetch = () => held.promise;
    const flushing = a.api.flushQueue();
    await settle();
    b.ctx.navigator.onLine = true;
    b.ctx.fetch = async () => response({results: [{code: '9780306406157', isbn: '9780306406157', status: 'duplicate', owned: true}]}, {url: '/api/store/queue'});
    await b.api.flushQueue();
    await b.api.enqueue('9780306406157');
    held.resolve(response({results: [{code: '9780306406157', isbn: '9780306406157', status: 'duplicate', owned: true}]}, {url: '/api/store/queue'}));
    await flushing;
    assert.deepEqual(JSON.parse(a.shared.values.get(key)).map(e => e.isbn), ['9780306406157']);
});

test('another tab\'s same-CSRF revocation fences an already-held successful flush', async () => {
    const a = page(), b = page(a.shared);
    await primeOnline(a); await b.api.prime();
    await a.api.enqueue('9780441172710');
    const before = a.shared.values.get(key);
    const held = deferred();
    a.ctx.fetch = () => held.promise;
    const flushing = a.api.flushQueue();
    await settle();
    b.ctx.navigator.onLine = true;
    b.ctx.fetch = async () => response('<login>', {url: '/login', redirected: true, type: 'text/html'});
    await b.api.refreshData();
    held.resolve(response({results: [{code: '9780441172710', isbn: '9780441172710', status: 'added_bare', owned: false}]}, {url: '/api/store/queue'}));
    await flushing;
    assert.equal(a.shared.values.get(key), before);
    assert.deepEqual(Object.keys(a.api.getIndex()), []);
    assert.equal(a.el('sync-now').attrs['aria-busy'], undefined);
});

test('strict invalid prefix remains queued and unindexed while later valid batches drain at 50 max', async () => {
    const p = page();
    await p.api.prime();
    const invalid = Array.from({length: 51}, (_, i) => 'BAD' + i);
    for (const code of [...invalid, '9780306406157']) await p.api.enqueue(code);
    const sizes = [];
    p.ctx.navigator.onLine = true;
    p.ctx.fetch = async (url, opts) => {
        const codes = JSON.parse(opts.body).isbns;
        sizes.push(codes.length);
        return response({results: codes.map(code => ({code, isbn: code,
            status: code === '9780306406157' ? 'duplicate' : 'invalid', owned: code === '9780306406157'}))}, {url: '/api/store/queue'});
    };
    await p.api.flushQueue();
    assert.deepEqual(sizes, [50, 2]);
    assert.deepEqual(JSON.parse(p.shared.values.get(key)).map(e => e.isbn), invalid);
    assert.equal(Object.keys(p.api.getIndex()).length, 1);
});

test('storage failure never claims saved or deletes/clears the manual code', async () => {
    const p = page();
    await p.api.prime();
    p.ctx.failWrites = true;
    p.el('isbn-input').value = '9780441172710';
    await assert.rejects(async () => p.api.showVerdict(p.el('isbn-input').value), /QuotaExceededError/);
    assert.equal(p.el('isbn-input').value, '9780441172710');
    assert.deepEqual(JSON.parse(p.shared.values.get(key)), []);
});

test('24h expiry cannot keep showing an owned verdict, including after an offline network error', async () => {
    const p = page();
    await primeOnline(p);
    p.api.setAge(24 * 60 * 60 * 1000 + 1);
    p.ctx.fetch = async () => { throw new TypeError('offline'); };
    await p.api.refreshData();
    p.ctx.navigator.onLine = false;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'LIBRARY UNAVAILABLE');
    assert.equal(p.api.getIndex()['9780306406157'], undefined);
    assert.equal(JSON.parse(p.shared.values.get(key)).length, 1);
});

test('queue-only owned evidence expires after 24h without ANY successful catalogue GET', async () => {
    const clock = {now: 0};
    const p = page(sharedStorage(), clock);
    await p.api.prime();
    p.ctx.navigator.onLine = true;
    p.ctx.fetch = async () => { throw new TypeError('catalogue transport unavailable'); };
    await p.api.refreshData();
    assert.deepEqual(Object.keys(p.api.getIndex()), []);
    assert.equal(p.shared.values.get(dataKey), undefined, 'no catalogue snapshot has ever been saved');
    await p.api.enqueue('9780306406157');
    p.ctx.fetch = async () => response({results: [{code: '9780306406157', isbn: '9780306406157', title: 'Queue-only owned', status: 'duplicate', owned: true}]}, {url: '/api/store/queue'});
    await p.api.flushQueue();
    p.ctx.navigator.onLine = false;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'OWNED');
    assert.equal(p.shared.values.get(key), '[]', 'a genuine acknowledgement drains its exact entry');
    clock.now = 86400000;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'OWNED', 'evidence remains valid at the documented inclusive bound');
    clock.now++;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'LIBRARY UNAVAILABLE');
    assert.equal(p.api.getIndex()['9780306406157'], undefined);
    assert.equal(JSON.parse(p.shared.values.get(key)).length, 1, 'expired evidence queues for a real later check');
});

test('one queue acknowledgement never freshens other codes in an older full catalogue', async () => {
    const clock = {now: 1000};
    const p = page(sharedStorage(), clock);
    await primeOnline(p);
    const catalogueTime = p.api.getFetchedAt();
    clock.now += 86400000 - 100;
    await p.api.enqueue('9780140328721');
    p.ctx.fetch = async () => response({results: [{code: '9780140328721', isbn: '9780140328721', status: 'duplicate', owned: true}]}, {url: '/api/store/queue'});
    await p.api.flushQueue();
    assert.equal(p.api.getFetchedAt(), catalogueTime);
    clock.now += 101;
    p.ctx.navigator.onLine = false;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'LIBRARY UNAVAILABLE');
    assert.equal(p.api.getIndex()['9780306406157'], undefined);
    await p.api.showVerdict('9780140328721');
    assert.equal(p.el('verdict-label').textContent, 'OWNED', 'the independently verified code has its own remaining lifetime');
    clock.now += 86400000;
    await p.api.showVerdict('9780140328721');
    assert.equal(p.el('verdict-label').textContent, 'LIBRARY UNAVAILABLE');
});

test('a genuine EMPTY catalogue fetched at clock zero is available, then expires', async () => {
    const clock = {now: 0};
    const p = page(sharedStorage(), clock);
    await p.api.prime();
    p.ctx.navigator.onLine = true;
    p.ctx.fetch = async () => response({generated_at: '1970-01-01T00:00:00Z', count: 0, items: []});
    await p.api.refreshData();
    p.ctx.navigator.onLine = false;
    await p.api.showVerdict('9780306406157');
    assert.equal(p.el('verdict-label').textContent, 'NOT IN LIBRARY');
    const reloaded = page(p.shared, clock);
    await reloaded.api.prime();
    reloaded.api.loadLocalData();
    await reloaded.api.showVerdict('9780306406157');
    assert.equal(reloaded.el('verdict-label').textContent, 'NOT IN LIBRARY', 'zero is a real persisted fetch timestamp, not missing data');
    clock.now = 86400001;
    await reloaded.api.showVerdict('9780306406157');
    assert.equal(reloaded.el('verdict-label').textContent, 'LIBRARY UNAVAILABLE');
});

test('queue evidence resets on authoritative replacement, refusal, and backward clock changes', async () => {
    for (const reset of ['catalogue', 'refusal', 'clock']) {
        const clock = {now: 1000};
        const p = page(sharedStorage(), clock);
        await p.api.prime();
        await p.api.enqueue('9780306406157');
        p.ctx.navigator.onLine = true;
        p.ctx.fetch = async () => response({results: [{code: '9780306406157', isbn: '9780306406157', status: 'duplicate', owned: true}]}, {url: '/api/store/queue'});
        await p.api.flushQueue();
        if (reset === 'clock') { clock.now--; p.api.updateStatus(); }
        else {
            p.ctx.fetch = async () => reset === 'refusal' ? response({}, {status: 403})
                : response({generated_at: '1970-01-01T00:00:01Z', count: 0, items: []});
            await p.api.refreshData();
        }
        assert.deepEqual(Object.keys(p.api.getIndex()), [], reset);
        assert.equal(p.shared.values.get(key), '[]', 'no reset may resurrect/drain unrelated queue entries');
        assert.equal(p.api.isBlocked(), reset === 'refusal');
    }
});

for (const inputChange of ['new code', 'edit back to empty', 'unchanged empty', 'already occupied']) {
    test(`held camera admission failure preserves input ownership: ${inputChange}`, async () => {
        const p = page();
        await p.initialize(); // install the REAL input/submit listeners, not a test-only generation hook
        const decode = {};
        p.ctx.createBarcodeScanner = options => {
            decode.run = options.onDecode;
            return {engine: 'zxing', start: async () => {}, stop: async () => {}};
        };
        if (inputChange === 'already occupied') p.el('isbn-input').value = '9780140328721';
        p.api.toggleCamera();
        const release = p.shared.hold();
        decode.run('9780306406157');
        await settle();
        assert.equal(p.shared.values.get(key), '[]', 'waiting on coordination is NOT a saved acknowledgement');
        if (inputChange === 'new code' || inputChange === 'edit back to empty') {
            p.el('isbn-input').value = '9780140328721';
            await p.el('isbn-input').emit('input');
            if (inputChange === 'edit back to empty') { p.el('isbn-input').value = ''; await p.el('isbn-input').emit('input'); }
        }
        p.ctx.failWrites = true;
        release();
        await settle();
        const expected = inputChange === 'unchanged empty' ? '9780306406157' : inputChange === 'edit back to empty' ? '' : '9780140328721';
        assert.equal(p.el('isbn-input').value, expected);
        assert.equal(p.shared.values.get(key), '[]', 'failed storage admits no scan and creates no false ACK');
        assert.match(p.el('store-session-warning').textContent, /9780306406157/, 'the failed camera code stays visible separately');
        assert.match(p.el('store-session-warning').textContent, /not saved|could not be saved/);
    });
}

test('owner/session mismatch never loads a cached catalogue or permits a flush', async () => {
    const p = page();
    await p.api.prime();
    for (const overrides of [{owner: '2'}, {session: 'other-session'}, {fetched_at: Date.now() - 86400001}]) {
        p.shared.values.set(dataKey, JSON.stringify({owner: '1', session: 'session-hash', fetched_at: Date.now(), data: snapshot(), ...overrides}));
        p.api.loadLocalData();
        assert.deepEqual(Object.keys(p.api.getIndex()), []);
    }
    await p.api.enqueue('9780441172710');
    const before = p.shared.values.get(key);
    p.ctx.document.cookie = 'csrf_token=another-login';
    p.ctx.navigator.onLine = true;
    await p.api.flushQueue();
    assert.equal(p.shared.values.get(key), before);
    assert.equal(p.api.isBlocked(), true);
});

test('unowned legacy overflow is preserved separately, never claimed by the next owner', async () => {
    const p = page();
    p.shared.values.delete('shelf-store-queue-owner');
    const raw = JSON.stringify(Array.from({length: 251}, (_, i) => ({isbn: String(100000 + i), at: 'legacy'})));
    p.shared.values.set(key, raw);
    await p.api.prime();
    assert.equal(p.shared.values.get(key), '[]');
    const archives = [...p.shared.values.keys()].filter(k => k.startsWith('shelf-store-queue-unassigned:'));
    assert.equal(archives.length, 1);
    assert.equal(p.shared.values.get(archives[0]), raw);
});

test('owned legacy overflow gets stable IDs, not truncation; editing permission is still required', async () => {
    const p = page();
    p.shared.values.set(key, JSON.stringify(Array.from({length: 251}, (_, i) => ({isbn: String(100000 + i), at: 'legacy'}))));
    await p.api.prime();
    const before = p.shared.values.get(key);
    const queue = JSON.parse(before);
    assert.equal(queue.length, 251);
    assert.equal(new Set(queue.map(e => e.id)).size, 251);
    p.api.setCanSync(false);
    p.ctx.navigator.onLine = true;
    await p.api.flushQueue();
    assert.equal(p.shared.values.get(key), before);
    assert.match(p.el('sync-hint').textContent, /editing access is required/);
});

test('no IndexedDB means no unlocked queue writer or false saved claim', async () => {
    const p = page();
    p.ctx.indexedDB = undefined;
    await assert.rejects(p.api.prime(), /Safe queue storage is unavailable/);
    assert.equal(p.shared.values.get(key), '[]');
});

// Minimal DOM contract double for the delegated feature listener. This pins
// generation, owner, cleanup and whole-root replacement; browser/Alpine focus
// semantics are deliberately left to the real E2E regression source.
function featuresPage() {
    const listeners = [];
    const docs = new Map();
    const calls = {destroy: [], init: [], alerts: [], requests: []};
    const panelSelector = '[data-testid="settings-section-content"] [x-show="tab === \'features\'"]';
    const makePanel = label => ({...element(), label, children: [], childNodes: [],
        contains() { return false; }, replaceChildren(...children) { this.childNodes = children; },
        querySelectorAll(selector) { return selector.includes('form') ? this.childNodes : []; }});
    const makeAccount = api => ({...element(), dataset: {accountOwner: '1', accountSession: 'unchanged-session', accountApi: api},
        contains() { return false; }, replaceWith(next) { current.account = next; }});
    const current = {account: makeAccount('1'), panel: makePanel('initial')};
    const document = {
        activeElement: null,
        addEventListener(name, fn) { if (name === 'submit') listeners.push(fn); },
        querySelector(selector) {
            if (selector === '[data-account-root]') return current.account;
            if (selector === panelSelector) return current.panel;
            return null;
        },
        querySelectorAll(selector) { return current.panel.querySelectorAll(selector); },
    };
    const ctx = {
        document, console, URL, URLSearchParams,
        location: {href: 'https://shelf.test/settings', origin: 'https://shelf.test'},
        csrfToken: () => 'unchanged-session', t: value => value,
        alert(message) { calls.alerts.push(message); },
        FormData: class {constructor(form) { this.form = form; } forEach(fn) { Object.entries(this.form.fields).forEach(([k, v]) => fn(v, k)); }},
        DOMParser: class {parseFromString(html) { return docs.get(html); }},
        Alpine: {
            mutateDom(fn) { fn(); },
            destroyTree(node) { calls.destroy.push(node); },
            initTree(node) { calls.init.push(node); },
        },
        fetch(url, opts) { calls.requests.push({url, opts}); throw new Error('Unexpected feature fetch'); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(sourceRoot, 'static/js/components-settings.js'), 'utf8'), ctx);
    function makeForm({endpoint = '/api/settings/features/api', fields = {enabled: '0'}, disabled = false} = {}) {
        const button = {...element(), textContent: 'Turn off', disabled: false};
        button.disabled = disabled;
        const form = {...element(), fields, action: 'https://shelf.test' + endpoint,
            matches() { return true; }, querySelector() { return button; }};
        return {button, form};
    }
    function submit(options) {
        const {form, button} = makeForm(options);
        current.panel.childNodes.push(form);
        const event = {target: form, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
        return {promise: listeners.at(-1)(event), button, form};
    }
    function reply(name, api, owner = '1') {
        const account = makeAccount(api);
        account.dataset.accountOwner = owner;
        const panel = makePanel(name);
        docs.set(name, {querySelector(selector) {
            return selector === '[data-account-root]' ? account : selector === panelSelector ? panel : null;
        }});
        return {ok: true, status: 200, url: 'https://shelf.test/settings', text: async () => name};
    }
    return {ctx, calls, current, submit, reply, makeForm};
}

test('feature refresh replaces the WHOLE authoritative account root, not only the dropdown', async () => {
    const f = featuresPage();
    const oldAccount = f.current.account;
    f.ctx.fetch = async () => f.reply('API off', '0');
    const request = f.submit();
    await request.promise;
    assert.notEqual(f.current.account, oldAccount);
    assert.equal(f.current.account.dataset.accountApi, '0');
    assert.deepEqual(f.calls.destroy, [oldAccount]);
    assert.deepEqual(f.calls.init, [f.current.account]);
    assert.deepEqual(f.calls.alerts, []);
    assert.equal(request.button.disabled, false);
    assert.equal(request.form.attrs['aria-busy'], undefined);
});

test('reverse-order feature replies cannot restore older API/modal flags; both busy states settle', async () => {
    const f = featuresPage();
    const old = deferred();
    f.ctx.fetch = () => old.promise;
    const first = f.submit();
    f.ctx.fetch = async () => f.reply('newer API off', '0');
    const second = f.submit();
    await settle();
    old.resolve(f.reply('older API on', '1'));
    await first.promise;
    await second.promise;
    assert.equal(f.current.account.dataset.accountApi, '0');
    assert.equal(f.calls.destroy.length, 1);
    for (const request of [first, second]) {
        assert.equal(request.button.disabled, false);
        assert.equal(request.button.attrs['aria-busy'], undefined);
        assert.equal(request.form.dataset.featureSaving, undefined);
    }
});

test('feature/profile POST execution is globally serialized BEFORE the server can mutate', async () => {
    const f = featuresPage();
    const heldBeforeExecution = deferred();
    const admitted = [];
    const executed = [];
    let serverApi = '1';
    f.ctx.fetch = async (url, opts) => {
        admitted.push(url);
        if (url.endsWith('/features/api')) {
            await heldBeforeExecution.promise;
            serverApi = opts.body.get('enabled');
        } else {
            assert.equal(opts.body.get('profile'), 'everything');
            serverApi = '1';
        }
        executed.push(url);
        return f.reply('server response ' + executed.length, serverApi);
    };
    const first = f.submit({fields: {enabled: '0'}});
    const second = f.submit({endpoint: '/api/settings/profile', fields: {profile: 'everything'}});
    await settle();
    const beforeRelease = admitted.slice();
    heldBeforeExecution.resolve();
    await Promise.all([first.promise, second.promise]);
    assert.equal(f.current.account.dataset.accountApi, serverApi, 'rendered API/token controls match FINAL server state, not just the newest-looking response');
    assert.deepEqual(beforeRelease, ['https://shelf.test/api/settings/features/api'], 'the newer POST cannot execute ahead of the held older mutation');
    assert.deepEqual(executed, ['https://shelf.test/api/settings/features/api', 'https://shelf.test/api/settings/profile']);
    assert.equal(serverApi, '1', 'the later profile intent is not dropped');
    assert.equal(f.current.account.dataset.accountApi, serverApi, 'rendered API/token controls match FINAL server state');
    assert.equal(f.calls.destroy.length, 1, 'only the final generation replaces the authoritative account/modal root');
    for (const request of [first, second]) {
        assert.equal(request.button.disabled, false);
        assert.equal(request.form.dataset.featureSaving, undefined);
        assert.equal(request.form.attrs['aria-busy'], undefined);
    }
});

test('queued mutation takes fresh live form fields at admission but preserves the captured intent', async () => {
    const f = featuresPage();
    const held = deferred();
    const bodies = [];
    f.ctx.fetch = async (url, opts) => {
        bodies.push(new URLSearchParams(opts.body));
        if (bodies.length === 1) await held.promise;
        return f.reply('response ' + bodies.length, '0');
    };
    const first = f.submit({endpoint: '/api/settings/features/series', fields: {enabled: '0'}});
    const second = f.submit({fields: {enabled: '0', _csrf: 'old-render', render_revision: 'old-render'}});
    const fresh = f.makeForm({fields: {enabled: '1', _csrf: 'fresh-render', render_revision: 'fresh-render'}});
    f.current.panel.childNodes = [first.form, fresh.form]; // the queued source form is now detached
    held.resolve();
    await Promise.all([first.promise, second.promise]);
    assert.equal(bodies.length, 2);
    assert.equal(bodies[1].get('_csrf'), 'fresh-render');
    assert.equal(bodies[1].get('render_revision'), 'fresh-render');
    assert.equal(bodies[1].get('enabled'), '0', 'refreshing a toggle must not invert the user\'s desired value');
    for (const request of [first, second, fresh]) {
        assert.equal(request.button.disabled, false);
        assert.equal(request.form.attrs['aria-busy'], undefined);
        assert.equal(request.form.dataset.featureSaving, undefined);
    }
});

test('queued old-owner/session mutations never issue a POST after account change', async () => {
    for (const change of ['owner', 'csrf']) {
        const f = featuresPage(), held = deferred(), admitted = [];
        f.ctx.fetch = async url => { admitted.push(url); await held.promise; return f.reply('old response', '0'); };
        const first = f.submit({endpoint: '/api/settings/features/series'});
        const second = f.submit();
        if (change === 'owner') f.current.account.dataset.accountOwner = '2';
        else f.ctx.csrfToken = () => 'new-session';
        held.resolve();
        await Promise.all([first.promise, second.promise]);
        assert.deepEqual(admitted, ['https://shelf.test/api/settings/features/series'], change);
        assert.equal(f.calls.destroy.length, 0);
        assert.equal(first.button.disabled, false);
        assert.equal(second.button.disabled, false);
    }
});

test('a failed older mutation advances the queue without unlocking another request\'s busy control', async () => {
    const f = featuresPage(), failed = deferred(), heldSecond = deferred();
    let count = 0;
    f.ctx.fetch = async () => {
        if (++count === 1) { await failed.promise; throw new Error('first POST failed'); }
        await heldSecond.promise;
        return f.reply('recovered', '0');
    };
    const first = f.submit({endpoint: '/api/settings/features/series'});
    const second = f.submit();
    failed.resolve();
    await first.promise;
    await settle();
    assert.equal(count, 2, 'an error must not poison the global queue');
    assert.equal(first.button.disabled, false);
    assert.equal(second.button.disabled, true, 'older cleanup cannot release the newer form\'s lease');
    assert.equal(second.form.attrs['aria-busy'], 'true');
    heldSecond.resolve();
    await second.promise;
    assert.equal(second.button.disabled, false);
    assert.equal(second.form.attrs['aria-busy'], undefined);
});

test('rebinding queued work to another queued form reference-counts the SAME busy control', async () => {
    const f = featuresPage();
    const turns = [deferred(), deferred(), deferred()];
    const bodies = [];
    f.ctx.fetch = async (url, opts) => {
        const index = bodies.length;
        bodies.push(new URLSearchParams(opts.body));
        await turns[index].promise;
        return f.reply('turn ' + index, index === 2 ? '1' : '0');
    };
    const first = f.submit({endpoint: '/api/settings/features/series'});
    const second = f.submit({fields: {enabled: '0'}});
    const third = f.submit({fields: {enabled: '1'}});
    // The second source was replaced. Its admission must acquire the live
    // third form, whose own later intent ALREADY owns a busy lease.
    f.current.panel.childNodes = [first.form, third.form];
    turns[0].resolve();
    await first.promise;
    await settle();
    assert.equal(bodies.length, 2);
    assert.equal(third.button.disabled, true);
    turns[1].resolve();
    await second.promise;
    await settle();
    assert.equal(bodies.length, 3);
    assert.equal(bodies[1].get('enabled'), '0');
    assert.equal(bodies[2].get('enabled'), '1');
    assert.equal(third.button.disabled, true, 'release of the rebound second request cannot unlock the still-running third');
    assert.equal(third.form.dataset.featureSaving, '1');
    assert.equal(third.form.attrs['aria-busy'], 'true');
    turns[2].resolve();
    await third.promise;
    for (const request of [first, second, third]) {
        assert.equal(request.button.disabled, false);
        assert.equal(request.form.dataset.featureSaving, undefined);
        assert.equal(request.form.attrs['aria-busy'], undefined);
    }
    assert.equal(f.current.account.dataset.accountApi, '1');
});

test('feature busy cleanup retains an originally disabled submit button', async () => {
    const f = featuresPage();
    f.ctx.fetch = async () => f.reply('saved', '0');
    const request = f.submit({disabled: true});
    await request.promise;
    assert.equal(request.button.disabled, true);
    assert.equal(request.button.textContent, 'Turn off');
    assert.equal(request.form.dataset.featureSaving, undefined);
    assert.equal(request.form.attrs['aria-busy'], undefined);
});

test('a failed LAST mutation reconciles authoritative state after an earlier successful POST', async () => {
    const f = featuresPage(), held = deferred(), methods = [];
    let serverApi = '1';
    f.ctx.fetch = async (url, opts) => {
        methods.push(opts.method || 'GET');
        if (url.endsWith('/features/api')) {
            await held.promise;
            serverApi = '0';
            return f.reply('saved API off', serverApi);
        }
        if (url.endsWith('/profile')) throw new Error('profile response failed');
        assert.equal(url, '/settings');
        return f.reply('fresh final server state', serverApi);
    };
    const first = f.submit();
    const second = f.submit({endpoint: '/api/settings/profile', fields: {profile: 'everything'}});
    held.resolve();
    await Promise.all([first.promise, second.promise]);
    assert.equal(f.current.account.dataset.accountApi, serverApi, 'discarding earlier HTML cannot leave API/token controls stale when the later mutation fails');
    assert.deepEqual(methods, ['POST', 'POST', 'GET']);
    assert.equal(f.calls.destroy.length, 1);
    assert.equal(f.calls.alerts.length, 1, 'reconciliation does not pretend the failed mutation succeeded');
    assert.equal(first.button.disabled, false);
    assert.equal(second.button.disabled, false);
});

test('changed account or CSRF cannot apply an outstanding feature response', async () => {
    for (const change of ['owner', 'csrf']) {
        const f = featuresPage();
        const held = deferred();
        f.ctx.fetch = () => held.promise;
        const request = f.submit();
        if (change === 'owner') f.current.account.dataset.accountOwner = '2';
        else f.ctx.csrfToken = () => 'new-session';
        const account = f.current.account;
        held.resolve(f.reply('old response', '0'));
        await request.promise;
        assert.equal(f.current.account, account);
        assert.equal(f.calls.destroy.length, 0);
        assert.equal(request.button.disabled, false);
        assert.equal(request.form.attrs['aria-busy'], undefined);
    }
});

test('feature error, abort and unexpected server owner all settle busy controls', async () => {
    for (const name of ['Error', 'AbortError', 'owner-mismatch']) {
        const f = featuresPage();
        f.ctx.fetch = async () => {
            if (name === 'owner-mismatch') return f.reply('wrong owner', '0', '2');
            const error = new Error('connection stopped'); error.name = name; throw error;
        };
        const account = f.current.account;
        const request = f.submit();
        await request.promise;
        assert.equal(f.current.account, account);
        assert.equal(request.button.disabled, false);
        assert.equal(request.button.textContent, 'Turn off');
        assert.equal(request.form.attrs['aria-busy'], undefined);
        assert.equal(f.calls.alerts.length, name === 'AbortError' ? 0 : 1);
    }
});
