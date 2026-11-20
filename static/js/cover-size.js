/* Shared browser-local catalogue sizing, not server preferences or authentication.
 * Native range keys/drag remain browser-owned. History uses a read-only owner
 * check, never catalogue/provider requests or preference HTTP writes. */
(function () {
    'use strict';

    // A repeated script evaluation (e.g. a fragment loader) must not add another
    // delegated handler or reset the current document's selection.
    const initialized = Symbol.for('shelf.coverSizeControl');
    if (document[initialized]) return;
    document[initialized] = true;

    const CONTROL = '[data-cover-size-control]';
    const INPUT = 'input[type="range"][data-cover-size-input]';
    const RETRY = 'button[data-cover-size-retry]';
    // Equal native range positions on either side of the unchanged 100% size.
    // The upper half retains room to grow to 200%, without custom key handling.
    const percentages = [50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200];
    const defaultPosition = 5;
    const enhanced = new WeakSet();
    let nextId = 0;
    let position = defaultPosition;
    let appliedPosition = null;
    let confirmation = '';
    let pendingConfirmation = null;
    let unsavedChoice = false;
    let storage = null;
    let storageUnavailable = false;
    let status = '';
    const view = document.defaultView;

    // Capture the ORIGINAL server shell once, not a fragment, URL or input.
    // Keep signed 63-bit account IDs as strings: Number could alias two owners.
    const identity = (function () {
        const shell = document.documentElement;
        const account = typeof shell.getAttribute === 'function' ? shell.getAttribute('data-cover-size-account') : null;
        const mount = typeof shell.getAttribute === 'function' ? shell.getAttribute('data-cover-size-mount') : null;
        if (typeof account !== 'string' || !/^[1-9][0-9]{0,18}$/.test(account)
            || (account.length === 19 && account > '9223372036854775807')
            || typeof mount !== 'string' || mount.length > 1024
            || (mount !== '' && !mount.startsWith('/')) || /[\u0000-\u001f\u007f?#\\]/.test(mount)) return null;
        try {
            // V1 belonged to an older, unpublished physical scale. Never adopt it.
            return Object.freeze({account, mount,
                key: 'shelf.coverSize.v2:' + encodeURIComponent(mount) + ':u:' + account});
        } catch (_) {
            return null;
        }
    }());
    const owner = identity === null ? null : identity.key;

    // The readable CSRF cookie is only an in-memory sign-in-change fence. The
    // server resolves identity; the marker is never copied into request data,
    // storage, logs or account identity. Session refresh preserves this marker.
    function sessionMarker() {
        try {
            const marker = view && typeof view.csrfToken === 'function' ? view.csrfToken() : null;
            return typeof marker === 'string' && marker.length > 0 && marker.length <= 256 ? marker : null;
        } catch (_) {
            return null;
        }
    }
    let confirmedSession = owner === null ? null : sessionMarker();
    let savingAllowed = owner !== null && confirmedSession !== null;
    if (owner !== null && !savingAllowed) confirmation = 'unverified';

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
            input.disabled = false; // Even an unverified restored page can resize locally.
            if (output) output.value = percentage + '%';
            const message = root.querySelector('[data-cover-size-status]');
            const state = confirmation || status;
            if (message) {
                message.textContent = state ? root.getAttribute('data-cover-size-' + state) || '' : '';
                message.hidden = !message.textContent;
            }
            const retry = root.querySelector(RETRY);
            if (retry) {
                retry.hidden = owner === null || !['unverified', 'changed'].includes(confirmation);
                retry.disabled = retry.hidden;
            }
            root.setAttribute('data-cover-size-persistence', confirmation === 'checking' ? 'checking'
                : savingAllowed && !storageUnavailable && storage !== null ? 'ready' : 'page-only');
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

    function cancelConfirmation() {
        const attempt = pendingConfirmation;
        pendingConfirmation = null; // Fence callbacks even if abort settles late.
        if (attempt) {
            view.clearTimeout(attempt.timer);
            attempt.controller.abort();
        }
    }

    function savingPermitted() {
        if (!savingAllowed) return false;
        if (sessionMarker() === confirmedSession) return true;
        savingAllowed = false;
        confirmation = 'changed';
        cancelConfirmation();
        return false;
    }

    async function confirmOwner() {
        cancelConfirmation();
        savingAllowed = false;
        confirmation = 'checking';
        sync();
        const marker = sessionMarker();
        if (identity === null || !view || marker === null || typeof view.fetch !== 'function'
            || typeof view.AbortController !== 'function') {
            confirmation = 'unverified';
            sync();
            return;
        }
        const attempt = {controller: new view.AbortController(), timer: null};
        pendingConfirmation = attempt;
        attempt.timer = view.setTimeout(function () {
            if (pendingConfirmation !== attempt) return;
            cancelConfirmation();
            confirmation = 'unverified';
            sync();
        }, 5000);
        try {
            // Set pathname on the current origin, rather than interpreting a
            // mount as a host/URL. No query, hash, supplied URL or account payload.
            const url = new URL(view.location.href);
            url.pathname = identity.mount.replace(/\/$/, '') + '/api/ui/cover-size-owner';
            url.search = '';
            url.hash = '';
            const response = await view.fetch(url.href, {method: 'GET', mode: 'same-origin',
                credentials: 'same-origin', cache: 'no-store', redirect: 'error',
                headers: {'Accept': 'application/json'}, signal: attempt.controller.signal});
            if (pendingConfirmation !== attempt) return;
            if (!response.ok || response.redirected
                || (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'application/json') {
                throw new Error('Owner confirmation unavailable');
            }
            const current = await response.json();
            if (pendingConfirmation !== attempt) return;
            if (sessionMarker() !== marker || !current
                || current.account !== identity.account || current.mount !== identity.mount) {
                confirmation = 'changed';
                return;
            }
            confirmedSession = marker;
            savingAllowed = true;
            confirmation = '';
            // Re-read updates missed while frozen. Never overwrite a local user
            // edit or queue it for saving when an asynchronous check completes.
            try {
                storage = view.localStorage;
                const saved = storage.getItem(owner);
                if (!unsavedChoice) position = storedPosition(saved);
                storageUnavailable = false;
            } catch (_) {
                storageUnavailable = true;
            }
            status = storageUnavailable ? 'unavailable' : unsavedChoice ? 'unsaved' : '';
        } catch (_) {
            if (pendingConfirmation === attempt) confirmation = 'unverified';
        } finally {
            if (pendingConfirmation === attempt) {
                view.clearTimeout(attempt.timer);
                pendingConfirmation = null;
                sync();
            }
        }
    }

    function rangeChanged(event) {
        const input = event.target;
        const root = owned(input);
        if (event.defaultPrevented || event.isTrusted !== true || !root || !input.matches(INPUT)
            || !enhanced.has(input) || root.querySelector(INPUT) !== input || input.disabled) return;
        const maySave = savingPermitted();
        const next = bounded(input.valueAsNumber);
        if (next === position) { sync(); return; }
        position = next;
        unsavedChoice = true;
        // Only an actual user change may write the captured key. State changes
        // first, so the matching change event cannot retry a failed input write.
        if (maySave && owner !== null && storage !== null) {
            try {
                storage.setItem(owner, String(percentages[position]));
                storageUnavailable = false;
                status = 'saved';
                unsavedChoice = false;
            } catch (_) {
                storageUnavailable = true;
                status = 'unavailable';
            }
        }
        sync();
    }

    document.addEventListener('input', rangeChanged);
    document.addEventListener('change', rangeChanged);
    document.addEventListener('click', function (event) {
        const button = event.target;
        const root = owned(button);
        if (event.defaultPrevented || event.isTrusted !== true || !root || !button.matches(RETRY)
            || button.disabled || button.hidden || root.querySelector(RETRY) !== button) return;
        confirmOwner(); // A user retry verifies only; it never saves pending input.
    });

    if (view) {
        view.addEventListener('storage', function (event) {
            if (owner === null || storage === null || event.storageArea !== storage) return;
            if (event.key !== owner && event.key !== null) return;
            if (event.key === null && event.newValue !== null) return;
            if (!savingPermitted()) { sync(); return; }
            if (unsavedChoice) return; // A delayed storage event cannot discard a local-only edit.
            position = storedPosition(event.newValue);
            status = storageUnavailable ? 'unavailable' : '';
            sync(); // Never echo another tab's update back to storage.
        });
        view.addEventListener('pageshow', function (event) {
            // History can restore native form values after pageshow. Reconcile
            // at the next paint too, using current state, never captured input.
            if (typeof view.requestAnimationFrame === 'function') view.requestAnimationFrame(sync);
            if (event.persisted !== true) { sync(); return; }
            confirmOwner();
        });
        view.addEventListener('pagehide', function () {
            savingAllowed = false;
            confirmation = 'checking';
            cancelConfirmation();
        });
    }

    // Scan the live document, not event.detail: OOB events can reference nodes
    // that have already been replaced. Delegation needs no HTMX re-processing.
    document.addEventListener('htmx:afterSwap', sync);
    document.addEventListener('htmx:oobAfterSwap', sync);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sync, {once: true});
    else sync();
}());
