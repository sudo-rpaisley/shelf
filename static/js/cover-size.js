/* Shared browser-local catalogue sizing, not server preferences or authentication.
 * Native range keys/drag remain browser-owned. No catalogue or preference HTTP. */
(function () {
    'use strict';

    // A repeated script evaluation (e.g. a fragment loader) must not add another
    // delegated handler or reset the current document's selection.
    const initialized = Symbol.for('shelf.coverSizeControl');
    if (document[initialized]) return;
    document[initialized] = true;

    const CONTROL = '[data-cover-size-control]';
    const INPUT = 'input[type="range"][data-cover-size-input]';
    // Equal native range positions on either side of the unchanged 100% size.
    // The upper half retains room to grow to 200%, without custom key handling.
    const percentages = [50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200];
    const defaultPosition = 5;
    const enhanced = new WeakSet();
    let nextId = 0;
    let position = defaultPosition;
    let appliedPosition = null;
    let restorationDisabled = false;
    let storage = null;
    let storageUnavailable = false;
    let status = '';
    const view = document.defaultView;

    // Capture the ORIGINAL server shell once, not a fragment, URL or input.
    // Keep signed 63-bit account IDs as strings: Number could alias two owners.
    const owner = (function () {
        const shell = document.documentElement;
        const account = typeof shell.getAttribute === 'function' ? shell.getAttribute('data-cover-size-account') : null;
        const mount = typeof shell.getAttribute === 'function' ? shell.getAttribute('data-cover-size-mount') : null;
        if (typeof account !== 'string' || !/^[1-9][0-9]{0,18}$/.test(account)
            || (account.length === 19 && account > '9223372036854775807')
            || typeof mount !== 'string' || mount.length > 1024
            || (mount !== '' && !mount.startsWith('/')) || /[\u0000-\u001f\u007f?#\\]/.test(mount)) return null;
        try {
            // V1 belonged to an older, unpublished physical scale. Never adopt it.
            return 'shelf.coverSize.v2:' + encodeURIComponent(mount) + ':u:' + account;
        } catch (_) {
            return null;
        }
    }());

    function storedPosition(value) {
        if (typeof value !== 'string' || value.length < 2 || value.length > 3
            || !/^(50|60|70|80|90|100|120|140|160|180|200)$/.test(value)) return defaultPosition;
        return percentages.indexOf(Number(value));
    }

    if (owner !== null && view) {
        try {
            storage = view.localStorage;
            position = storedPosition(storage.getItem(owner));
        } catch (_) {
            storageUnavailable = true;
        }
    }
    if (owner === null || storage === null || storageUnavailable) status = 'unavailable';

    function bounded(value) {
        if (typeof value !== 'number' || !Number.isFinite(value)) return defaultPosition;
        return Math.max(0, Math.min(percentages.length - 1, Math.round(value)));
    }

    function uniqueId() {
        let id;
        do { id = 'cover-size-input-' + (++nextId); } while (document.getElementById(id)
            || document.getElementById(id + '-note'));
        return id;
    }

    function sync() {
        position = bounded(position);
        const percentage = percentages[position];
        // 100% remains the original Home size. CSS sizes a layout OR its artwork,
        // never both, and never scales fonts, controls, printing or exports.
        const changed = appliedPosition !== position;
        if (changed) {
            document.documentElement.style.setProperty('--media-cover-scale', String(percentage / 100));
            appliedPosition = position;
        }
        for (const root of document.querySelectorAll(CONTROL)) {
            const input = root.querySelector(INPUT);
            if (!input || input.closest(CONTROL) !== root) continue;
            const output = root.querySelector('[data-cover-size-output]');
            if (!enhanced.has(input)) {
                input.id = uniqueId();
                const label = input.closest('label');
                if (label && root.contains(label)) label.htmlFor = input.id;
                const note = root.querySelector('.cover-size-control__note');
                if (note) {
                    note.id = input.id + '-note';
                    input.setAttribute('aria-describedby', note.id);
                }
                if (output) output.htmlFor = input.id;
                enhanced.add(input);
            }
            input.min = '0';
            input.max = String(percentages.length - 1);
            input.step = '1';
            input.value = String(position);
            input.setAttribute('aria-valuetext', percentage + '%');
            input.disabled = restorationDisabled;
            if (output) output.value = percentage + '%';
            const message = root.querySelector('[data-cover-size-status]');
            const state = restorationDisabled ? 'restored' : status;
            if (message) {
                message.textContent = state ? root.getAttribute('data-cover-size-' + state) || '' : '';
                message.hidden = !message.textContent;
            }
            root.setAttribute('data-cover-size-enhanced', 'true');
        }
        // No account/key/value payload or synthetic native-input event.
        if (changed && typeof document.dispatchEvent === 'function') {
            document.dispatchEvent(new Event('shelf:cover-size-changed'));
        }
    }

    function owned(element) {
        return element instanceof Element && element.isConnected
            && element.ownerDocument === document && element.closest(CONTROL);
    }

    function rangeChanged(event) {
        const input = event.target;
        const root = owned(input);
        if (restorationDisabled || event.defaultPrevented || event.isTrusted !== true || !root || !input.matches(INPUT)
            || !enhanced.has(input) || root.querySelector(INPUT) !== input || input.disabled) return;
        const next = bounded(input.valueAsNumber);
        if (next === position) { sync(); return; }
        position = next;
        // Only an actual user change may write the captured key. State changes
        // first, so the matching change event cannot retry a failed input write.
        if (event.isTrusted === true && owner !== null && storage !== null) {
            try {
                storage.setItem(owner, String(percentages[position]));
                storageUnavailable = false;
                status = 'saved';
            } catch (_) {
                storageUnavailable = true;
                status = 'unavailable';
            }
        }
        sync();
    }

    document.addEventListener('input', rangeChanged);
    document.addEventListener('change', rangeChanged);

    if (view) {
        view.addEventListener('storage', function (event) {
            if (restorationDisabled || owner === null || storage === null || event.storageArea !== storage) return;
            if (event.key !== owner && event.key !== null) return;
            if (event.key === null && event.newValue !== null) return;
            position = storedPosition(event.newValue);
            status = storageUnavailable ? 'unavailable' : '';
            sync(); // Never echo another tab's update back to storage.
        });
        view.addEventListener('pageshow', function (event) {
            if (event.persisted !== true) return;
            // A cached shell could belong to a previous login. Do not recapture
            // cookies/identity or write until a real server navigation refreshes it.
            restorationDisabled = true;
            sync();
        });
    }

    // Scan the live document, not event.detail: OOB events can reference nodes
    // that have already been replaced. Delegation needs no HTMX re-processing.
    document.addEventListener('htmx:afterSwap', sync);
    document.addEventListener('htmx:oobAfterSwap', sync);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sync, {once: true});
    else sync();
}());
