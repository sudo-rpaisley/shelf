// Standalone owned-JS regression runner. No application, server, browser,
// provider, build or npm dependency is loaded. The deliberately small DOM and
// HTMX event adapter test our lifecycle/state contracts, NOT browser/HTMX itself.
// Run: node tests/shelf_fill_review_races_20261121.cjs
// Actual retry-card fixtures are rendered with Jinja, without importing app.
// Set SHELF_REVIEW_PYTHON to the project's Python interpreter if needed.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const sourceRoot = process.env.SHELF_FILL_REVIEW_SOURCE_ROOT || root;

function decode(s) {
    return s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
function matches(el, selector) {
    const descendant = selector.indexOf(' ');
    if (descendant !== -1) {
        const [ancestor, ...rest] = selector.split(' ');
        return matches(el, rest.join(' ')) && !!el.parentNode?.closest(ancestor);
    }
    const attr = /\[([\w-]+)(?:([\^]?=)"([^"]*)")?\]/g;
    let found;
    while ((found = attr.exec(selector))) {
        const value = el.getAttribute(found[1]);
        if (value === null || (found[2] === '=' && value !== found[3]) ||
            (found[2] === '^=' && !value.startsWith(found[3]))) return false;
    }
    selector = selector.replace(attr, '');
    const tag = selector.match(/^[\w-]+/);
    if (tag && el.tagName !== tag[0].toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/);
    if (id && el.id !== id[1]) return false;
    return [...selector.matchAll(/\.([\w-]+)/g)].every(m => el.className.split(' ').includes(m[1]));
}
class Element {
    constructor(tag, doc) {
        this.tagName = tag.toUpperCase(); this.doc = doc; this.attrs = {}; this.children = [];
        this.listeners = {}; this.disabled = false; this.style = {}; this._text = ''; this._value = '';
        this.dataset = new Proxy({}, {
            get: (_, key) => this.getAttribute('data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())) ?? undefined,
            set: (_, key, value) => { this.setAttribute('data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase()), value); return true; }
        });
        if (this.tagName === 'TEMPLATE') this.content = new Element('fragment', doc);
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; }
    removeAttribute(k) { delete this.attrs[k]; }
    get id() { return this.getAttribute('id') || ''; } set id(v) { this.setAttribute('id', v); }
    get name() { return this.getAttribute('name') || ''; } set name(v) { this.setAttribute('name', v); }
    get type() { return this.getAttribute('type') || ''; } set type(v) { this.setAttribute('type', v); }
    get className() { return this.getAttribute('class') || ''; } set className(v) { this.setAttribute('class', v); }
    get value() { return this._value || this.getAttribute('value') || ''; } set value(v) { this._value = String(v); this.setAttribute('value', v); }
    get options() { return this.children; }
    get selectedIndex() { return this.options.findIndex(o => o.value === this.value); }
    get elements() { return this.querySelectorAll('input, button, select'); }
    get firstChild() { return this.children[0] || null; }
    get firstElementChild() { return this.firstChild; }
    get isConnected() { return this === this.doc.body || !!this.parentNode?.isConnected; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(value) { this.children.forEach(c => { c.parentNode = null; }); this.children = []; this._text = String(value); }
    get innerHTML() {
        if (this.content) return this.content.innerHTML;
        return this._text + this.children.map(c => `<${c.tagName.toLowerCase()} ${Object.entries(c.attrs).map(([k,v]) => `${k}="${v}"`).join(' ')}>${c.innerHTML}</${c.tagName.toLowerCase()}>`).join('');
    }
    set innerHTML(html) {
        const target = this.content || this;
        target.textContent = '';
        const stack = [target];
        for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
            if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
            if (!token.startsWith('<')) { stack.at(-1)._text += decode(token); continue; }
            const tag = token.match(/^<([\w-]+)/)?.[1];
            if (!tag) continue;
            const child = new Element(tag, this.doc);
            for (const a of token.slice(tag.length + 1).matchAll(/([\w-]+)(?:="([^"]*)")?/g)) child.setAttribute(a[1], decode(a[2] || ''));
            stack.at(-1).appendChild(child);
            if (!['input','img','br'].includes(tag)) stack.push(child);
        }
    }
    appendChild(child) {
        if (child.tagName === 'FRAGMENT') { [...child.children].forEach(c => this.appendChild(c)); return child; }
        child.remove(); child.parentNode = this; this.children.push(child); return child;
    }
    prepend(child) {
        const children = child.tagName === 'FRAGMENT' ? [...child.children] : [child];
        children.reverse().forEach(c => { c.remove(); c.parentNode = this; this.children.unshift(c); });
    }
    before(child) {
        const parent = this.parentNode, index = parent.children.indexOf(this);
        child.remove(); child.parentNode = parent; parent.children.splice(index, 0, child);
    }
    replaceWith(child) { this.before(child); this.remove(); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; }
    contains(child) { return child === this || this.children.some(c => c.contains(child)); }
    closest(selector) { return matches(this, selector) ? this : this.parentNode?.closest(selector) || null; }
    querySelectorAll(selector) {
        const selectors = selector.split(',').map(s => s.trim()), result = [];
        for (const child of this.children) {
            if (selectors.some(s => matches(child, s))) result.push(child);
            result.push(...child.querySelectorAll(selector));
        }
        return result;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    emit(name, detail, target = this) {
        const e = {detail, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
        (this.listeners[name] || []).forEach(fn => fn(e)); return e;
    }
    click() { if (!this.disabled) this.emit('click', {}); }
    focus() { this.doc.activeElement = this; }
}
class Data extends Map {
    constructor(form) { super(); if (form) form.elements.forEach(c => { if (c.name && !c.disabled) this.set(c.name, c.value); }); }
    set(k,v) { return super.set(k, String(v)); }
}
function harness(page = 'shelf_fill', realForm = false) {
    const doc = {addEventListener() {}};
    doc.body = new Element('body', doc);
    doc.createElement = tag => new Element(tag, doc);
    doc.querySelector = s => doc.body.querySelector(s);
    doc.getElementById = id => doc.querySelector('#' + id);
    const host = doc.body.appendChild(doc.createElement('main'));
    host.setAttribute('data-shelf-fill-origin', page);
    const template = doc.createElement('template');
    if (realForm) template.innerHTML = actualMainForm(page);
    const form = host.appendChild(realForm ? template.content.querySelector('form') : doc.createElement('form'));
    if (!realForm) {
        form.setAttribute('data-after-request', 'clear-scan-input');
        form.setAttribute('hx-target', page === 'scan' ? '#scan-results' : '#shelf-fill-results');
    }
    const submit = realForm ? form.querySelector('button[type="submit"]') : form.appendChild(doc.createElement('button'));
    submit.type = 'submit';
    const intentionallyDisabled = form.appendChild(doc.createElement('button'));
    intentionallyDisabled.type = 'submit'; intentionallyDisabled.disabled = true;
    const input = realForm ? form.querySelector('[name="isbn"]') : form.appendChild(doc.createElement('input')); input.name = 'isbn';
    input.id = page === 'scan' ? 'isbn-input' : 'shelf-fill-barcode';
    const node = host.appendChild(doc.createElement('select'));
    for (const [value,label] of [['41','Shelf A'],['42','Shelf B']]) {
        const option = node.appendChild(doc.createElement('option')); option.value = value; option.textContent = label;
    }
    node.value = '41';
    const results = host.appendChild(doc.createElement('div'));
    results.id = page === 'scan' ? 'scan-results' : 'shelf-fill-results';
    const bar = doc.body.appendChild(doc.createElement('div')); bar.id = 'htmx-indicator';
    const calls = [], toasts = [], storage = new Map();
    let active = true, responder = async () => { throw new Error('deliberate transport failure'); };
    const scope = {document: doc, window: {csrfToken: () => 'test-token'}, htmx: {config: {}, process() {}},
        setTimeout() {}, queueMicrotask, console, FormData: Data,
        localStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v)},
        fetch: async (url, options) => { calls.push({url, options}); return responder(url, options); }};
    vm.createContext(scope);
    for (const f of ['app.js','shelf-fill-flow.js','scan.js','shelf-fill.js']) vm.runInContext(fs.readFileSync(path.join(sourceRoot, 'static/js', f), 'utf8'), scope, {filename:f});
    scope.showToast = (text,type) => toasts.push({text,type});
    const flow = scope.createShelfFillFlow({root:host,form,results,node:()=>node,active:()=>active});
    return {doc,host,form,submit,intentionallyDisabled,input,node,results,flow,calls,toasts,scope,
        pending: () => host.querySelector('section'),
        switchNode(value) { node.value = value; flow.changed(); },
        setActive(value) { active = value; flow.changed(); },
        respond(fn) { responder = fn; },
        request(source = form, url = source.getAttribute('hx-post') || '/api/shelf-fill/scan', verb = 'post') {
            const detail = {elt: source, path: url, verb, parameters: {location_id:'7', ...Object.fromEntries(new Data(source))}};
            const config = doc.body.emit('htmx:configRequest', detail);
            if (config.defaultPrevented) return {config, detail};
            detail.xhr = {}; detail.requestConfig = {triggeringEvent: {submitter: submit}};
            return {config, detail, before: doc.body.emit('htmx:beforeRequest', detail)};
        },
        finish(request, successful = true, html = '') {
            if (html) {
                const detail = {...request.detail, serverResponse:html};
                doc.body.emit('htmx:beforeSwap', detail);
                if (detail.shouldSwap !== false) {
                    const template = doc.createElement('template'); template.innerHTML = detail.serverResponse;
                    results.prepend(template.content);
                }
            }
            doc.body.emit('htmx:afterRequest', {...request.detail, successful});
        }
    };
}
const receipt = (id, node, status = 'moved') => `<div class="scan-result" data-scan-status="${status}" data-scan-item-id="${id}" data-shelf-fill-placed-node="${node}"><a href="/item/${id}" data-scan-title>Saved title</a><span data-testid="shelf-fill-position">#2</span><span data-scan-badge>shelved</span></div>`;
const saved = id => `<div class="scan-result" data-scan-status="saved_unplaced" data-saved-item-id="${id}"><a href="/item/${id}" data-scan-title>Saved &lt;title&gt;</a><form action="/api/shelf-fill/place" hx-post="/api/shelf-fill/place"><input name="item_id" value="${id}"><button type="submit">Retry</button></form><span data-scan-badge>saved; not shelved</span></div>`;
const added = id => `<div class="scan-result" data-scan-status="added"><a href="/item/${id}" data-scan-title>Saved title</a></div>`;
function actualSaved(id, node, origin) {
    return execFileSync(process.env.SHELF_REVIEW_PYTHON || 'python3', ['-c', `
import sys
from pathlib import Path
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader(Path(sys.argv[1]) / 'app/templates'), autoescape=True)
env.globals.update(_=lambda text, **values: text % values if values else text, cover_shape=lambda media: 'book')
print(env.get_template('fragments/scan_result.html').render(
    status='saved_unplaced', item_id=int(sys.argv[2]), location_node_id=int(sys.argv[3]),
    shelf_fill_origin=sys.argv[4], title='Saved <actual title>', media_type='book',
    message='Actual placement failure', request=SimpleNamespace(cookies={'csrf_token': 'test-token'})))
`, root, String(id), String(node), origin], {encoding: 'utf8'});
}
function actualMainForm(page) {
    return execFileSync(process.env.SHELF_REVIEW_PYTHON || 'python3', ['-c', `
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader
templates = Path(sys.argv[1]) / 'app/templates'
source = (templates / (sys.argv[2] + '.html')).read_text()
form = re.search(r'<form\\b[^>]*data-after-request="clear-scan-input"[\\s\\S]*?</form>', source)
assert form, 'actual scanner form is missing'
env = Environment(loader=FileSystemLoader(templates), autoescape=True)
env.globals['_'] = lambda text, **values: text % values if values else text
print(env.from_string(form.group()).render(shelf_fill_receipt=False, media_types={}, game_platforms={}, locations=[],
    location_nodes=[], borrowers=[], request=SimpleNamespace(cookies={'csrf_token': 'test-token'})))
`, root, page], {encoding: 'utf8'});
}
const settle = async () => { for (let i=0;i<20;i++) await Promise.resolve(); };
const tests = [];
function test(name, run) { tests.push({name,run}); }

for (const page of ['scan','shelf_fill']) for (const outcome of ['success','error','abort']) {
    test(`${page}: original disabled state survives ${outcome} and a second typed scan`, async () => {
        const h = harness(page);
        for (let i=0;i<2;i++) {
            h.input.value = 'code-' + i;
            const request = h.request();
            assert.equal(h.submit.disabled,true);
            h.finish(request, outcome === 'success', outcome === 'success' ? receipt(80+i,'41') : '');
            await settle();
            assert.equal(h.submit.disabled,false);
            assert.equal(h.intentionallyDisabled.disabled,true);
            assert.equal(h.input.value,'');
            assert.equal(h.doc.activeElement,h.input);
        }
        assert.equal(h.toasts.length, outcome === 'success' ? 2 : 0, 'one toast per typed result, no duplicate hook');
        assert.equal(h.calls.length,0);
    });
}
test('late beforeRequest cancellation releases the global lease without afterRequest', async () => {
    const h = harness();
    h.doc.body.addEventListener('htmx:beforeRequest', e => e.preventDefault());
    h.request();
    await settle();
    assert.equal(h.submit.disabled,false);
    assert(h.flow.begin(), 'cancelled flow releases its lease too');
    h.flow.finish();
});
test('shared leases restore once in either release order; busy-only inputs do not own disabled state', () => {
    const h = harness(), locks = h.scope.window.shelfBusyControls;
    const first = locks.lock(h.submit,true), second = locks.lock(h.submit,true);
    second(); assert.equal(h.submit.disabled,true);
    first(); first(); assert.equal(h.submit.disabled,false);
    const busyOnly = locks.lock(h.input,false);
    h.input.disabled = true; // Another component owns this change, not the busy marker.
    busyOnly(); assert.equal(h.input.disabled,true);
});
test('standalone keyboard completion clears/refocuses the actual form and toasts exactly once', () => {
    const h = harness();
    for (let i=0;i<2;i++) {
        h.input.value = 'keyboard-' + i;
        h.results.innerHTML = receipt(801+i,'41');
        h.doc.body.emit('htmx:afterRequest', {elt:h.form, xhr:{}, successful:true});
        assert.equal(h.input.value,'');
        assert.equal(h.doc.activeElement,h.input);
    }
    assert.equal(h.toasts.length,2);
});
test('saved_unplaced keeps the actual ID across shelves and failed retries; never re-Adds', async () => {
    const h = harness(), context = h.flow.begin();
    h.flow.insert(saved(917),context); h.flow.finish();
    assert.equal(h.calls.length,0, 'an initial placement refusal is not an automatic retry loop');
    h.switchNode('42'); assert.equal(h.pending().querySelector('button').disabled,true);
    h.setActive(false); h.setActive(true); h.switchNode('41');
    h.pending().querySelector('button').click(); await settle();
    assert.equal(h.calls.length,1);
    assert.equal(h.calls[0].url,'/api/shelf-fill/place');
    assert.equal(h.calls[0].options.body.get('item_id'),'917');
    assert.equal(h.calls[0].options.body.get('location_node_id'),'41');
    assert.match(h.pending().textContent,/#917/);
    h.switchNode('42'); h.switchNode('41');
    h.respond(async () => ({ok:true,text:async()=>receipt(917,'41')}));
    h.pending().querySelector('button').click(); await settle();
    assert.equal(h.pending().children.length,0);
    assert.equal(h.calls.length,2);
    assert(h.calls.every(c => c.url === '/api/shelf-fill/place'));
});
test('a confirmed rescan reconciles only the same item AND actual destination', async () => {
    const h = harness(), context = h.flow.begin();
    h.flow.insert(added(918),context); h.flow.finish(); await settle();
    assert.equal(h.calls.length,1, 'deferred placement failed');
    h.flow.insert('<div class="scan-result" data-scan-status="moved" data-scan-item-id="918">Generic Move elsewhere</div>',context);
    h.flow.insert(receipt(919,'41'),context);
    h.flow.insert(receipt(918,'42'),context);
    assert.equal(h.pending().children.length,1, 'none prove placement of 918 at 41');
    h.flow.insert(receipt(918,'41'),context); h.flow.finish(); await settle();
    assert.equal(h.pending().children.length,0);
    assert.equal(h.calls.length,1, 'stale retry must not append again after a successful rescan');
});
for (const page of ['scan', 'shelf_fill']) {
    test(`${page}: confirmed rescan retires EVERY actual saved_unplaced retry form for only that item/destination`, async () => {
        const h = harness(page, true), context = h.flow.begin();
        assert.equal(h.form.getAttribute('method'), 'post');
        assert.equal(h.form.getAttribute('action'), page === 'scan' ? '/api/scan' : '/api/shelf-fill/scan');
        assert.equal(h.form.getAttribute('hx-post'), page === 'scan' ? '/api/scan' : '/api/shelf-fill/scan');
        assert.equal(h.form.getAttribute('data-scan-results'), page === 'scan' ? '#scan-results' : '#shelf-fill-results');
        assert.equal(h.form.querySelector('[name="_csrf"]').value, 'test-token');
        h.flow.insert(actualSaved(930, '41', page), context);
        const oldCard = h.results.querySelector('[data-saved-item-id="930"]');
        const oldForm = oldCard.querySelector('form');
        const oldPendingButton = h.pending().querySelector('button');
        h.flow.insert(actualSaved(930, '41', page), context);
        const matchingCards = h.results.querySelectorAll('[data-saved-item-id="930"]');
        h.flow.insert(actualSaved(931, '41', page), context);
        const otherItem = h.results.querySelector('[data-saved-item-id="931"]');
        h.flow.insert(actualSaved(930, '42', page), {...context, node: '42', label: 'Shelf B'});
        const otherDestination = h.results.querySelector('[data-shelf-fill-node="42"]');
        const unrelated = h.results.appendChild(h.doc.createElement('aside'));
        unrelated.textContent = 'Keep unrelated result content';
        h.flow.finish();
        assert.equal(oldForm.getAttribute('method'), 'post');
        assert.equal(oldForm.getAttribute('action'), '/api/shelf-fill/place');
        assert.equal(oldForm.getAttribute('hx-post'), '/api/shelf-fill/place');
        assert.equal(oldForm.getAttribute('hx-target'), 'closest .scan-result');
        assert.equal(oldForm.getAttribute('hx-swap'), 'outerHTML');
        assert.equal(oldForm.querySelector('[name="_csrf"]').value, 'test-token');
        const retry = h.request(oldForm, '/api/shelf-fill/place');
        assert.equal(retry.config.defaultPrevented, false);
        assert.equal(retry.detail.parameters.item_id, '930');
        assert.equal(retry.detail.parameters.location_node_id, '41');
        assert.equal(retry.detail.parameters.shelf_fill_origin, page);
        h.finish(retry, false); // failed placement stays saved/retryable, never re-Add
        const rescan = h.request();
        assert.equal(rescan.detail.path, '/api/shelf-fill/scan', 'the real scanner form routes through Shelf Fill on BOTH pages');
        h.finish(rescan, true, receipt(930, '41'));
        await settle();
        assert(matchingCards.every(card => !card.isConnected), 'all old matching actionable cards must be retired, not just the pending Map entry');
        assert.equal(h.results.querySelectorAll('[data-saved-item-id="930"][data-shelf-fill-node="41"]').length, 0);
        assert(otherItem.isConnected && otherDestination.isConnected && unrelated.isConnected, 'different items, destinations and unrelated nodes are untouched');
        h.flow.insert(receipt(932, '41'), context); // a later Y placement must not be reordered by the old X retry
        const stale = h.request(oldForm, '/api/shelf-fill/place');
        assert.equal(stale.config.defaultPrevented, true, 'a retained enhanced-form reference cannot issue another placement');
        assert.equal(h.host.emit('submit', {}, oldForm).defaultPrevented, true, 'native fallback shares the retirement guard');
        oldPendingButton.click();
        await settle();
        assert.equal(h.calls.length, 0, 'a retained old pending-button listener cannot append X after Y');
        assert.match(h.pending().textContent, /#931/);
        assert.match(h.pending().textContent, /Shelf B/);
    });
    test(`${page}: an already-issued old retry response cannot resurrect retired controls`, async () => {
        const h = harness(page), context = h.flow.begin();
        h.flow.insert(actualSaved(933, '41', page), context); h.flow.finish();
        const oldForm = h.results.querySelector('form');
        const oldRequest = h.request(oldForm, '/api/shelf-fill/place');
        h.flow.insert(receipt(933, '41'), context);
        h.finish(oldRequest, true, actualSaved(933, '41', page));
        await settle();
        assert.equal(h.results.querySelector('[data-saved-item-id="933"]') === null, true, 'a retired retry response cannot reinsert its actual form');
        assert.equal(h.pending().children.length, 0);
        assert.equal(h.flow.hasContinuations(), false);
        assert.equal(h.submit.disabled, false);
        assert.equal(h.calls.length, 0);
    });
}
for (const [status, endpoint] of [['legacy_incomplete','/api/shelf-fill/scan'],['not_found','/api/items/manual'],
    ['periodical_confirm','/api/periodicals/confirm']]) test(`A -> B -> A ${status} card offers explicit resume and preserves free text`, async () => {
    const h = harness(), context = h.flow.begin();
    h.flow.insert(`<div class="scan-result" data-scan-status="${status}"><form hx-post="${endpoint}"><input name="legacy_supplement" value="12345"><input name="condition" value="handwritten grade"><input name="notes" value="do not erase"><button type="submit">Continue</button></form></div>`,context);
    h.flow.finish(); h.switchNode('42'); h.switchNode('41');
    const card = h.results.querySelector('.scan-result'), form = card.querySelector('form');
    assert.equal(h.flow.current(context),false);
    assert.equal(h.request(form,endpoint).config.defaultPrevented,true);
    const resume = card.querySelector('[data-shelf-fill-resume] button');
    assert(resume && !resume.disabled);
    resume.click();
    assert.equal(h.calls.length,0, 'resume alone never submits or places');
    assert.equal(form.querySelector('[name="condition"]').value,'handwritten grade');
    assert.equal(form.querySelector('[name="notes"]').value,'do not erase');
    const request = h.request(form,endpoint);
    assert.equal(request.config.defaultPrevented,false);
    assert.equal(request.detail.parameters.location_node_id,'41');
    assert.equal(request.detail.parameters.location_id, endpoint === '/api/shelf-fill/scan' ? undefined : '7',
        'NODE never replaces a flat location on an ordinary continuation');
    h.finish(request);
});
test('a stale XHR keeps its saved ID at the captured node, even after A -> B -> A', async () => {
    const h = harness('scan'), request = h.request();
    h.switchNode('42'); h.switchNode('41');
    h.finish(request,true,added(920)); await settle();
    assert.equal(h.calls.length,0);
    assert.match(h.pending().textContent,/#920/);
    assert.equal(h.pending().querySelector('button').disabled,false, 'only explicit retry may resume the new generation');
});
test('camera request freezes node/origin before await; a late no-insert error queues nothing', async () => {
    const h = harness('scan'), context = h.flow.begin(), data = new Data();
    data.set('isbn','typed <code>'); data.set('location_id','7');
    let resolve;
    h.respond(() => new Promise(r => { resolve = r; }));
    const task = h.flow.scan(context,data);
    h.switchNode('42');
    assert.equal(h.calls[0].options.body.get('location_node_id'),'41');
    assert.equal(h.calls[0].options.body.has('location_id'),false);
    assert.equal(h.calls[0].options.body.get('shelf_fill_origin'),'scan');
    assert.equal(h.calls[0].options.headers['X-CSRF-Token'],'test-token');
    resolve({ok:true,text:async()=>'<div class="scan-result" data-scan-status="error"><p data-scan-error>Nothing saved</p></div>'});
    await task; h.flow.finish();
    assert.equal(h.pending().children.length,0);
    assert.equal(h.calls.length,1);
    assert.equal(h.node.value,'42');
});
test('recent-history reload cannot discard an unresolved Shelf Fill card', () => {
    const h = harness('scan'), page = h.scope.scanPage();
    page._shelfFlow = h.flow;
    const context = h.flow.begin();
    h.flow.insert(saved(921),context); h.flow.finish();
    page.loadRecentScans('add');
    assert.equal(h.calls.length,0);
    assert(h.results.querySelector('[data-saved-item-id="921"]'));
});

(async () => {
    let failed = 0;
    for (const {name,run} of tests) {
        try { await run(); console.log('PASS ' + name); }
        catch (e) { failed++; console.error('FAIL ' + name + '\n' + e.stack); }
    }
    console.log(`${tests.length-failed}/${tests.length} standalone owned-JS checks passed`);
    process.exitCode = failed ? 1 : 0;
})();
