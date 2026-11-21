'use strict';

// Pure component tests with controlled request completion/failure and HTMX's
// confirm -> sync/abort -> configRequest ordering. The real-browser companion
// exercises the vendored HTMX and actual server HTML; this is not a browser pass.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(process.env.BROWSE_004_SOURCE || path.join(root, 'static/js/browse.js'), 'utf8');

function component() {
    const controls = [
        {name: 'q', value: 'A & B'},
        {name: 'media_type_filter', value: 'book'},
        {name: 'sort', value: 'title_desc'},
        {name: 'reading_status', value: 'read'},
        {name: 'location_filter', value: '19'},
        {name: 'author_filter', value: '37'},
        {name: 'series', value: 'Series + one'},
        {name: 'view', value: 'grid'},
        {name: 'media_family_filter', value: 'books'},
        {name: 'source_filter', value: 'test'},
        {name: 'owned', value: '1'},
        {name: 'lent_out', value: ''},
        {name: 'tag', value: 'A & tag'},
        {name: 'language', value: 'en'},
        {name: 'collection', value: 'Collection + one'},
        {name: 'disabled_probe', value: 'must not serialize', disabled: true},
    ];
    const listeners = new Map();
    const attributes = new Map();
    const form = {
        setAttribute(name, value) { attributes.set(name, value); },
        getAttribute(name) { return attributes.get(name) || null; },
        requestSubmit() { return this.dispatch('submit'); },
        addEventListener(name, listener) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(listener);
        },
        dispatch(name, extra = {}) {
            const event = {target: form, currentTarget: form, defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; }, ...extra};
            for (const listener of listeners.get(name) || []) listener(event);
            return event;
        },
    };
    const calls = [];
    const requests = [];
    const responses = [];
    const active = new Map();
    const storage = new Map();
    const document = {
        body: {},
        addEventListener() {},
        getElementById(id) { return id === 'browse-filters' ? form : null; },
        querySelectorAll(selector) {
            const match = /^\[name="([^"]+)"\]$/.exec(selector);
            return match ? controls.filter(c => c.name === match[1]) : [];
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    };
    class FormData {
        constructor(actual) {
            assert.equal(actual, form, 'serialize the genuine Browse form');
            this.values = controls.filter(c => !c.disabled).map(c => [c.name, c.value]);
        }
        [Symbol.iterator]() { return this.values[Symbol.iterator](); }
    }
    // Deliberately model replacement BEFORE configRequest, as in HTMX 2.0.4.
    // A duplicate guard there is too late: it aborts B, then cancels itself.
    function issue(method, url, options, elt, triggeringEvent) {
        const detail = {elt, path: url, triggeringEvent};
        const inherited = elt === form || controls.includes(elt);
        const dispatch = name => inherited && form.dispatch(name, {detail}).defaultPrevented;
        if (dispatch('htmx:confirm')) return null;
        const replace = inherited && form.getAttribute('hx-sync') === 'this:replace';
        const owner = replace ? form : elt;
        if (replace && active.has(owner)) active.get(owner).abort();
        if (dispatch('htmx:configRequest')) return null;
        const parsed = new URL(url, 'https://shelf.invalid');
        // Native controls include their sibling filters. An explicit form
        // source serializes the form too: putting the same params in the URL
        // would duplicate q, unlike HTMX's values override/merge.
        const params = new URLSearchParams(parsed.search);
        if (inherited) {
            const fields = new URLSearchParams(new FormData(form));
            for (const [name, value] of fields) params.append(name, value);
        }
        for (const [name, value] of Object.entries(options.values || {})) params.set(name, value);
        let resolve, reject;
        const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
        const request = {method, url: parsed.pathname + '?' + params, options, owner, promise,
            aborted: false, done: false,
            abort() {
                if (this.done) return;
                this.aborted = this.done = true;
                active.delete(owner);
                form.dispatch('htmx:sendAbort', {detail: {elt, xhr: this}});
                reject(new Error('request aborted'));
            },
            respond() {
                if (this.done) return false;
                this.done = true;
                active.delete(owner);
                responses.push(params.get('q'));
                form.dispatch('htmx:afterRequest', {detail: {elt, xhr: this, successful: true}});
                resolve();
                return true;
            },
            fail() {
                this.done = true;
                active.delete(owner);
                form.dispatch('htmx:sendError', {detail: {elt, xhr: this}});
                reject(new Error('request failed'));
            },
        };
        // Native HTMX requests have no returned promise. Keep this driver's
        // rejection observable for assert.rejects without unhandled rejections.
        promise.catch(() => {});
        calls.push(request);
        requests.push(request);
        active.set(owner, request);
        return request;
    }
    function nativeRequest(elt, type = 'keyup') {
        return issue('GET', '/api/search', {}, elt, {type});
    }
    const htmx = {
        ajax(method, url, options) {
            return issue(method, url, options, options.source || document.body)?.promise;
        },
        trigger(elt, name) {
            calls.push({elt, name, viewAtSerialization: controls.find(c => c.name === 'view').value});
            if (listeners.has('submit')) nativeRequest(elt, name);
        },
    };
    const context = vm.createContext({document, FormData, URLSearchParams, htmx,
        window: {location: {search: ''}},
        sessionStorage: {getItem(key) { return storage.get(key) || null; }},
        localStorage: {getItem(key) { return storage.get(key) || null; },
            setItem(key, value) { storage.set(key, value); }},
    });
    vm.runInContext(source, context, {filename: 'browse.js'});
    const page = context.browsePage();
    return {page, form, controls, calls, requests, responses, nativeRequest, storage};
}

test('explicit submit enhances the real form and serializes every successful filter once', () => {
    const {page, form, controls, calls} = component();
    page.viewMode = 'list';
    page.enhanceSearchForm();
    const event = form.dispatch('submit');
    assert.equal(event.defaultPrevented, true, 'JS submit must not navigate via native /browse GET');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'GET');
    const url = new URL(calls[0].url, 'https://shelf.invalid');
    assert.equal(url.pathname, '/api/search');
    assert.deepEqual([...url.searchParams], controls.filter(c => !c.disabled).map(c => [c.name, c.value]));
    assert.equal(url.searchParams.get('view'), 'list');
    assert.equal(url.searchParams.getAll('q').length, 1);
    assert.equal(calls[0].options.target, '#item-grid');
    assert.equal(calls[0].options.swap, 'innerHTML');
});

test('Enter keyup/native search or a pending debounce cannot repeat the submitted q request', () => {
    const {page, form, controls, calls, nativeRequest} = component();
    page.enhanceSearchForm();
    form.dispatch('submit');
    for (const trigger of ['keyup', 'search']) {
        assert.equal(nativeRequest(controls[0], trigger), null, trigger);
    }
    assert.equal(calls.length, 1);
    form.dispatch('input', {target: controls[0]});
    assert.ok(nativeRequest(controls[0]), 'a genuine subsequent edit must search');
    assert.ok(nativeRequest(controls[1], 'change'), 'normal filter changes stay HTMX-owned');
});

test('Enter uses genuine form submission and prevents the browser search default, but not IME composition', () => {
    const {page, form, controls, calls} = component();
    page.enhanceSearchForm();
    assert.equal(form.dispatch('keydown', {target: controls[0], key: 'Enter'}).defaultPrevented, true);
    assert.equal(calls.length, 1);
    assert.equal(form.dispatch('keydown', {target: controls[0], key: 'Enter', isComposing: true}).defaultPrevented, false);
    assert.equal(calls.length, 1);
});

test('a changed filter or repeated deliberate submit still issues a new search', () => {
    const {page, form, controls, calls, nativeRequest} = component();
    page.enhanceSearchForm();
    form.dispatch('submit');
    controls[1].value = 'dvd';
    assert.ok(nativeRequest(controls[0]), 'changed filter values must not be deduplicated');
    form.dispatch('submit');
    assert.equal(calls.length, 3);
    for (const call of calls.slice(1)) {
        assert.equal(new URL(call.url, 'https://shelf.invalid').searchParams.get('media_type_filter'), 'dvd');
    }
});

test('view hidden control is written before HTMX serializes a toggle in either direction', () => {
    const {page, calls, storage} = component();
    page.setView('list');
    assert.equal(calls[0].viewAtSerialization, 'list');
    assert.equal(storage.get('shelf-view'), 'list');
    page.setView('grid');
    assert.equal(calls[1].viewAtSerialization, 'grid');
});

test('enhancement is wired at init and leaves native no-JS GET fallback intact', () => {
    assert.match(source, /init\(\)\s*\{[\s\S]*?this\.enhanceSearchForm\(\);/);
    const template = fs.readFileSync(path.join(root, 'app/templates/browse.html'), 'utf8');
    assert.match(template, /<form[^>]*id="browse-filters"[^>]*action="\/browse"[^>]*method="get"/);
    assert.match(template, /<noscript><button type="submit"/);
    assert.equal((template.match(/<input\b[^>]*name="q"/g) || []).length, 1);
});

test('held native A is aborted by form submit B, so releasing A cannot supersede B', () => {
    const {page, form, controls, requests, responses, nativeRequest} = component();
    page.enhanceSearchForm();
    assert.equal(form.getAttribute('hx-sync'), 'this:replace', 'native descendants must inherit one replacing owner');
    const old = nativeRequest(controls[0]);
    controls[0].value = 'B';
    form.dispatch('input', {target: controls[0]});
    form.requestSubmit();
    const latest = requests.at(-1);
    assert.equal(latest.options.source, form, 'programmatic search must share the genuine native request owner');
    assert.equal(latest.owner, old.owner);
    assert.equal(old.aborted, true);
    assert.equal(latest.respond(), true);
    assert.equal(old.respond(), false);
    assert.deepEqual(responses, ['B']);
});

test('implicit duplicates are rejected before sync replacement can abort the held Enter request', () => {
    const {page, form, controls, requests, nativeRequest} = component();
    page.enhanceSearchForm();
    form.requestSubmit();
    const latest = requests[0];
    for (const type of ['keyup', 'search']) {
        assert.equal(nativeRequest(controls[0], type), null);
        assert.equal(latest.aborted, false, type + ' must not abort the valid submitted request');
    }
    assert.equal(requests.length, 1);
    assert.equal(latest.respond(), true);
});

test('native filters and both view transitions replace an outstanding programmatic search', () => {
    const {page, form, controls, requests, nativeRequest} = component();
    page.enhanceSearchForm();
    form.requestSubmit();
    let old = requests.at(-1);
    controls[1].value = 'dvd';
    form.dispatch('change', {target: controls[1]});
    const filter = nativeRequest(controls[1], 'change');
    assert.equal(old.aborted, true, 'native filter must replace the submitted XHR');
    assert.equal(filter.owner, form);
    for (const mode of ['list', 'grid']) {
        old = requests.at(-1);
        page.setView(mode);
        const latest = requests.at(-1);
        assert.equal(old.aborted, true, mode + ' must replace the prior XHR');
        assert.equal(latest.owner, form);
        assert.equal(new URL(latest.url, 'https://shelf.invalid').searchParams.get('view'), mode);
    }
});

test('network failure does not suppress a deliberate same-value retry', async () => {
    const {page, form, requests, nativeRequest, controls} = component();
    page.enhanceSearchForm();
    form.requestSubmit();
    const failed = requests[0];
    failed.fail();
    await assert.rejects(failed.promise, /request failed/);
    assert.equal(nativeRequest(controls[0], 'search'), null, 'implicit duplicate stays suppressed');
    form.requestSubmit();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].url, failed.url);
    assert.equal(requests[1].respond(), true, 'deliberate retry must remain usable');
});

test('initial restore uses the same form owner and successful controls without duplicate parameters', () => {
    const {page, form, controls, requests, storage} = component();
    page.enhanceSearchForm();
    page.urlFilterNames = () => controls.filter(c => !c.disabled && c.name !== 'view').map(c => c.name);
    storage.set('shelf-browse-qs', 'q=Restored&media_type_filter=dvd&collection=Saved+collection');
    page.viewMode = 'list';
    assert.equal(page.restoreFilters(), true);
    const latest = requests[0];
    assert.equal(latest.options.source, form);
    assert.equal(latest.owner, form);
    const params = new URL(latest.url, 'https://shelf.invalid').searchParams;
    assert.equal(params.get('q'), 'Restored');
    assert.equal(params.get('media_type_filter'), 'dvd');
    assert.equal(params.get('collection'), 'Saved collection');
    assert.equal(params.get('view'), 'list');
    for (const control of controls.filter(c => !c.disabled)) assert.equal(params.getAll(control.name).length, 1);
});

test('sort-only initial restore shares the form owner and synchronously serializes list view', () => {
    const {page, form, controls, requests, storage} = component();
    page.enhanceSearchForm();
    controls.find(c => c.name === 'sort').querySelector = selector => selector === 'option[value="title_desc"]';
    storage.set('shelf-sort', 'title_desc');
    page.viewMode = 'list';
    page.restoreSort();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.source, form);
    assert.equal(requests[0].owner, form);
    const params = new URL(requests[0].url, 'https://shelf.invalid').searchParams;
    assert.equal(params.get('sort'), 'title_desc');
    assert.equal(params.get('view'), 'list');
    assert.equal(controls.find(c => c.name === 'view').value, 'list');
    assert.equal(params.getAll('q').length, 1);
});

test('the lifecycle driver matches the vendored HTMX pre-config replacement and inherited this owner', () => {
    const vendor = fs.readFileSync(path.join(root, 'static/vendor/htmx-2.0.4.min.js'), 'utf8');
    const issue = vendor.slice(vendor.indexOf('function de('), vendor.indexOf('function Nn('));
    assert.ok(issue.indexOf('htmx:confirm') < issue.indexOf('hx-sync'));
    assert.ok(issue.indexOf('hx-sync') < issue.indexOf('htmx:configRequest'));
    assert.ok(issue.includes('if(I==="this"){h=Se(r,"hx-sync")}'));
    assert.ok(issue.includes('if(d==="replace"){he(h,"htmx:abort")}'));
    assert.ok(vendor.includes('de(t,n,y(r.source),r.event,'), 'ajax uses the supplied source, not the target');
});
