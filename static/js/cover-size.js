/* Live, document-only cover sizing. Native range keys and button activation are
 * left to the browser. No per-control handlers or retained detached DOM nodes. */
(function () {
    'use strict';

    // A repeated script evaluation (e.g. a fragment loader) must not add another
    // delegated handler or reset the current document's selection.
    const initialized = Symbol.for('shelf.coverSizeControl');
    if (document[initialized]) return;
    document[initialized] = true;

    const CONTROL = '[data-cover-size-control]';
    const INPUT = 'input[type="range"][data-cover-size-input]';
    const ACTION = 'button[type="button"][data-cover-size-action]';
    // Equal native range positions on either side of the unchanged 100% size.
    // The upper half retains room to grow to 200%, without custom key handling.
    const percentages = [50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200];
    const defaultPosition = 5;
    const enhanced = new WeakSet();
    let nextId = 0;
    let position = defaultPosition;

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
        // The sole CSS write: a finite, bounded unitless number. CSS applies it
        // once to legacy cards or opted-in whole-item layout containers, never
        // to artwork or fonts. CSS owns reflow and readable-size floors.
        document.documentElement.style.setProperty('--media-cover-scale', String(percentage / 100));
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
            input.disabled = false;
            if (output) output.value = percentage + '%';
            for (const button of root.querySelectorAll(ACTION)) {
                if (button.closest(CONTROL) !== root) continue;
                const action = button.getAttribute('data-cover-size-action');
                if (action === 'smaller') button.disabled = position === 0;
                else if (action === 'larger') button.disabled = position === percentages.length - 1;
                else if (action === 'reset') button.disabled = position === defaultPosition;
            }
            root.setAttribute('data-cover-size-enhanced', 'true');
        }
    }

    function owned(element) {
        return element instanceof Element && element.isConnected
            && element.ownerDocument === document && element.closest(CONTROL);
    }

    function rangeChanged(event) {
        const input = event.target;
        if (event.defaultPrevented || !owned(input) || !input.matches(INPUT) || input.disabled) return;
        position = bounded(input.valueAsNumber);
        sync();
    }

    document.addEventListener('input', rangeChanged);
    document.addEventListener('change', rangeChanged);
    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || !(event.target instanceof Element)) return;
        const button = event.target.closest(ACTION);
        if (!owned(button) || button.disabled) return;
        const action = button.getAttribute('data-cover-size-action');
        if (action === 'smaller') position = bounded(position - 1);
        else if (action === 'larger') position = bounded(position + 1);
        else if (action === 'reset') position = defaultPosition;
        else return;
        event.preventDefault();
        sync();
    });

    // Scan the live document, not event.detail: OOB events can reference nodes
    // that have already been replaced. Delegation needs no HTMX re-processing.
    document.addEventListener('htmx:afterSwap', sync);
    document.addEventListener('htmx:oobAfterSwap', sync);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sync, {once: true});
    else sync();
}());
