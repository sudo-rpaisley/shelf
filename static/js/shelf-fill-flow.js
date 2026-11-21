// Shared by the inline and standalone Shelf Fill scanners. Only server-rendered
// fragments enter HTML sinks; codes, titles and destination labels use textContent.
function createShelfFillFlow(options) {
    var generation = 0;
    var revision = 0;
    var session = window.csrfToken();
    var busy = false;
    var pending = new Map();
    var collected = new WeakSet();
    var retired = new WeakSet();
    var configured = new WeakMap();
    var requests = new WeakMap();
    var locked = new Map();
    var origin = options.root.getAttribute('data-shelf-fill-origin') || 'scan';
    var pendingPanel = document.createElement('section');
    pendingPanel.className = 'space-y-2 mb-4';
    pendingPanel.setAttribute('aria-live', 'polite');
    options.results.before(pendingPanel);

    function capture() {
        var select = options.node();
        if (!options.active() || !select || !select.value || window.csrfToken() !== session) return null;
        return {node: select.value, generation: generation,
            label: select.options[select.selectedIndex].textContent.trim()};
    }

    function current(context) {
        var now = capture();
        return !!now && !!context && now.node === context.node && now.generation === context.generation;
    }

    function stamp(card, context) {
        card.dataset.shelfFillNode = context.node;
        card.dataset.shelfFillGeneration = context.generation;
        card.dataset.shelfFillLabel = context.label;
        card.querySelectorAll('form').forEach(function (form) {
            [['location_node_id', context.node], ['shelf_fill_origin', origin]].forEach(function (field) {
                var input = form.querySelector('[name="' + field[0] + '"]');
                if (!input) {
                    input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = field[0];
                    form.appendChild(input);
                }
                input.value = field[1];
            });
        });
    }

    function renderContinuations() {
        options.results.querySelectorAll('.scan-result[data-shelf-fill-node]').forEach(function (card) {
            if (!card.querySelector('form')) return;
            var context = contextFor(card);
            var notice = card.querySelector('[data-shelf-fill-resume]');
            if (current(context)) { if (notice) notice.remove(); return; }
            if (!notice) {
                notice = document.createElement('div');
                notice.setAttribute('data-shelf-fill-resume', '');
                notice.className = 'mt-3 border border-shelf-border rounded-lg p-3 text-sm';
                var text = document.createElement('p');
                text.textContent = t('Paused scan for:') + ' ' + context.label + '. ' + t('Select that shelf and explicitly resume, or start a fresh scan.');
                notice.appendChild(text);
                var resume = document.createElement('button');
                resume.type = 'button';
                resume.textContent = t('Resume this scan at the original shelf');
                resume.className = 'mt-2 px-3 py-1 border border-shelf-border rounded-lg';
                resume.addEventListener('click', function () {
                    var selected = capture();
                    if (busy || !selected || selected.node !== context.node) return;
                    stamp(card, selected); // Explicit opt-in to this generation, never an automatic POST.
                    renderContinuations();
                });
                notice.appendChild(resume);
                card.appendChild(notice);
            }
            var now = capture();
            notice.querySelector('button').disabled = busy || !now || now.node !== context.node;
        });
    }

    function renderPending() {
        pendingPanel.textContent = '';
        pending.forEach(function (entry) {
            var row = document.createElement('div');
            row.className = 'rounded-lg border border-shelf-warning/40 p-3 text-sm';
            var text = document.createElement('p');
            text.textContent = t('Saved, but not yet shelved:') + ' ' + entry.title + ' (#' + entry.id + ') · ' + entry.context.label;
            row.appendChild(text);
            var retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'mt-2 px-3 py-1 border border-shelf-border rounded-lg';
            retry.textContent = t('Retry shelving at this location');
            var now = capture();
            retry.disabled = busy || !now || now.node !== entry.context.node;
            retry.addEventListener('click', function () {
                // Returning to a shelf requires an explicit retry, not an
                // automatic placement from an obsolete request generation.
                var selected = capture();
                if (pending.get(entry.key) !== entry || busy || !selected || selected.node !== entry.context.node) return;
                entry.context = selected;
                entry.automatic = false;
                place(entry);
            });
            row.appendChild(retry);
            pendingPanel.appendChild(row);
        });
    }

    function setBusy(value) {
        busy = value;
        options.root.setAttribute('aria-busy', value ? 'true' : 'false');
        if (value) {
            var controls = options.root.querySelectorAll('[data-shelf-fill-lock], form button[type="submit"]');
            controls.forEach(function (control) {
                if (!locked.has(control)) locked.set(control, window.shelfBusyControls.lock(control, true));
            });
            var node = options.node();
            if (node && !locked.has(node)) locked.set(node, window.shelfBusyControls.lock(node, true));
        } else {
            locked.forEach(function (release) { release(); });
            locked.clear();
        }
        renderPending();
        renderContinuations();
    }

    function begin(context) {
        context = context || capture();
        if (busy || !current(context)) return null;
        revision++;
        setBusy(true);
        return context;
    }

    function contextFor(card) {
        if (!card || !card.dataset.shelfFillNode) return null;
        return {node: card.dataset.shelfFillNode, generation: Number(card.dataset.shelfFillGeneration),
            label: card.dataset.shelfFillLabel || card.dataset.shelfFillNode};
    }

    function prepare(html, context) {
        var template = document.createElement('template');
        template.innerHTML = html; // authenticated, escaped server fragment
        template.content.querySelectorAll('.scan-result').forEach(function (card) {
            stamp(card, context);
        });
        if (!current(context)) template.content.querySelectorAll('#shelf-fill-summary').forEach(function (node) { node.remove(); });
        return template;
    }

    function itemIdFor(card) {
        var link = card.querySelector('a[href^="/item/"]');
        var match = link && link.getAttribute('href').match(/^\/item\/([1-9][0-9]*)(?:\?|$)/);
        var id = card.dataset.savedItemId || card.dataset.scanItemId || (match && match[1]);
        return /^[1-9][0-9]*$/.test(id || '') ? id : null;
    }

    function retireRetries(receipt, id, node) {
        var key = node + ':' + id;
        var entry = pending.get(key);
        pending.delete(key);
        options.results.querySelectorAll('.scan-result[data-shelf-fill-node]').forEach(function (card) {
            if (card === receipt || card.dataset.shelfFillNode !== node || itemIdFor(card) !== id) return;
            // Retire ALL old retry surfaces, not just the separately rendered
            // pending row. Keep receipts and unrelated items/destinations intact.
            var retry = card.querySelector('form[action="/api/shelf-fill/place"], form[hx-post="/api/shelf-fill/place"]');
            if (retry || card.dataset.scanStatus === 'saved_unplaced' || (entry && card === entry.card)) {
                retired.add(card);
                card.remove();
            }
        });
    }

    function collect() {
        options.results.querySelectorAll('.scan-result[data-shelf-fill-node]').forEach(function (card) {
            if (retired.has(card) || collected.has(card)) return;
            collected.add(card);
            var status = card.dataset.scanStatus;
            var link = card.querySelector('a[href^="/item/"]');
            var id = itemIdFor(card);
            if (!id) return;
            var context = contextFor(card);
            var key = context.node + ':' + id;
            // Only an explicit physical-placement receipt for THIS item AND
            // destination resolves a retry. A generic Move card proves neither.
            if (card.dataset.scanItemId === id && card.dataset.shelfFillPlacedNode === context.node && card.querySelector('[data-testid="shelf-fill-position"]')) {
                retireRetries(card, id, context.node);
                return;
            }
            if (['added', 'duplicate', 'restored', 'promoted', 'saved_unplaced'].indexOf(status) === -1) return;
            if (!pending.has(key)) pending.set(key, {key: key, id: id, title: link ? link.textContent : '#' + id,
                context: context, card: card, automatic: status !== 'saved_unplaced' && current(context)});
        });
        renderPending();
        renderContinuations();
    }

    function finish() {
        setBusy(false);
        collect();
        var next = Array.from(pending.values()).find(function (entry) { return entry.automatic && current(entry.context); });
        if (next) { next.automatic = false; place(next); }
    }

    function insert(html, context) {
        var template = prepare(html, context);
        var summary = template.content.querySelector('#shelf-fill-summary');
        if (summary) {
            summary.removeAttribute('hx-swap-oob');
            var target = options.root.querySelector('#shelf-fill-summary');
            if (target && current(context)) target.replaceWith(summary);
            else summary.remove();
        }
        var card = template.content.querySelector('.scan-result');
        options.results.prepend(template.content);
        htmx.process(options.results);
        collect();
        return card;
    }

    async function place(entry) {
        if (pending.get(entry.key) !== entry) return;
        var context = begin(entry.context);
        if (!context) return;
        try {
            var body = new FormData();
            body.set('item_id', entry.id);
            body.set('location_node_id', context.node);
            body.set('shelf_fill_origin', origin);
            var response = await fetch('/api/shelf-fill/place', {
                method: 'POST', headers: {'X-CSRF-Token': session, 'HX-Request': 'true'}, body: body
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            var html = await response.text();
            if (pending.get(entry.key) !== entry) return;
            var template = prepare(html, context);
            // Physical mutation errors deliberately return HTTP200 fragments.
            var receipt = template.content.querySelector('[data-shelf-fill-placed-node]');
            if (!receipt || receipt.dataset.scanItemId !== entry.id || receipt.dataset.shelfFillPlacedNode !== context.node ||
                !receipt.querySelector('[data-testid="shelf-fill-position"]')) throw new Error('Not placed');
            if (entry.card.isConnected) entry.card.remove();
            insert(html, context);
            pending.delete(entry.key);
        } catch (error) {
            if (pending.get(entry.key) !== entry) return;
            entry.automatic = false; // Keep the item ID and its original node.
            showToast(t('Item is saved, but could not be placed. Use Retry shelving.'), 'error');
        } finally { finish(); }
    }

    document.body.addEventListener('htmx:configRequest', function (event) {
        var detail = event.detail;
        var source = detail.elt;
        if (source && retired.has(source.closest('.scan-result'))) { event.preventDefault(); return; }
        if (!source || !options.root.contains(source)) return;
        if (String(detail.verb).toLowerCase() === 'get' && detail.path === '/api/shelf-fill/summary') {
            var summaryContext = capture();
            if (summaryContext) configured.set(source, Object.assign({}, summaryContext, {summary: true, revision: revision}));
            return;
        }
        var card = source.closest('.scan-result');
        var context = contextFor(card);
        var main = source === options.form;
        if (!context && !(main && options.active())) return;
        var post = String(detail.verb).toLowerCase() === 'post';
        if (!post && !card) return;
        context = context || capture();
        if (busy || !current(context)) {
            event.preventDefault();
            renderContinuations();
            showToast(t('Select the original shelf and use Resume this scan, or start a fresh scan. Wait for any current scan to finish.'), 'warning');
            return;
        }
        if (main && post) detail.path = '/api/shelf-fill/scan';
        detail.parameters.location_node_id = context.node;
        detail.parameters.shelf_fill_origin = origin;
        if (detail.path === '/api/shelf-fill/scan') {
            delete detail.parameters.location_id;
        }
        configured.set(source, Object.assign({}, context, {card: card}));
    });
    document.body.addEventListener('htmx:beforeRequest', function (event) {
        var context = configured.get(event.detail.elt);
        configured.delete(event.detail.elt);
        if (!context) return;
        if (retired.has(context.card)) { event.preventDefault(); return; }
        if (event.defaultPrevented) return;
        if (!context.summary && !begin(context)) { event.preventDefault(); return; }
        requests.set(event.detail.xhr, context);
        queueMicrotask(function () {
            if (event.defaultPrevented && requests.get(event.detail.xhr) === context) {
                requests.delete(event.detail.xhr);
                if (!context.summary) finish();
            }
        });
    });

    // Native POSTs carry the same captured continuation. Do not let a stale
    // card navigate around the HTMX generation check when enhancement is off.
    options.root.addEventListener('submit', function (event) {
        var form = event.target;
        if (retired.has(form.closest('.scan-result'))) { event.preventDefault(); return; }
        if (form.getAttribute('hx-post')) return; // configRequest already guards enhanced submits.
        var context = contextFor(form.closest('.scan-result'));
        if (context && (busy || !current(context))) {
            event.preventDefault();
            renderContinuations();
            showToast(t('Resume this scan at its original shelf first, or start a fresh scan.'), 'warning');
        }
    });

    // A native confirmation can render its receipt into the originating page.
    options.results.querySelectorAll('.scan-result[data-shelf-fill-node]').forEach(function (card) {
        var context = contextFor(card);
        context.generation = generation;
        stamp(card, context);
    });
    collect();
    document.body.addEventListener('htmx:beforeSwap', function (event) {
        var context = requests.get(event.detail.xhr);
        if (!context) return;
        // A server write already dispatched cannot be undone here. Its old
        // response must not resurrect a retry superseded by a real receipt.
        if (retired.has(context.card)) { event.detail.shouldSwap = false; return; }
        if (context.summary) {
            if (!current(context) || context.revision !== revision) event.detail.shouldSwap = false;
        } else event.detail.serverResponse = prepare(event.detail.serverResponse, context).innerHTML;
    });
    document.body.addEventListener('htmx:afterRequest', function (event) {
        var context = requests.get(event.detail.xhr);
        if (!context) return;
        requests.delete(event.detail.xhr);
        if (!context.summary) finish();
    });

    return {
        begin: begin, finish: finish, insert: insert, current: current,
        changed: function () {
            generation++;
            pending.forEach(function (entry) { entry.automatic = false; });
            renderPending();
            renderContinuations();
        },
        hasContinuations: function () {
            return pending.size > 0 || !!options.results.querySelector('.scan-result[data-shelf-fill-node] form');
        },
        async scan(context, data) {
            data.delete('location_id');
            data.set('location_node_id', context.node);
            data.set('shelf_fill_origin', origin);
            var response = await fetch('/api/shelf-fill/scan', {
                method: 'POST', headers: {'X-CSRF-Token': session, 'HX-Request': 'true'}, body: data
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return insert(await response.text(), context);
        }
    };
}
