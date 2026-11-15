/* Home-only, request-free enhancement. Native links/details and eight real
 * cards are usable before this script. No metadata parsing or card rendering. */
(function () {
    'use strict';
    const installed = Symbol.for('shelf.recentShelf');
    if (document[installed]) return;
    document[installed] = true;
    const ROOT = '[data-recent-shelf="streaming-v1"]';
    const ACTION = 'button[type="button"][data-recent-action]';
    const observed = new Set();
    let scheduled = false;

    function owned(root) {
        return root instanceof Element && root.isConnected
            && root.ownerDocument === document && root.matches(ROOT);
    }

    function trackFor(root) {
        const track = root.querySelector('#home-recent-row');
        return track && track.closest(ROOT) === root ? track : null;
    }

    function enhance(root, track) {
        const template = root.querySelector('template[data-recent-overflow]');
        if (template) {
            const current = Array.from(track.querySelectorAll('.media-card'));
            const terminal = track.lastElementChild;
            const cells = Array.from(template.content.children);
            const expected = Number(template.getAttribute('data-recent-total'));
            const label = template.getAttribute('data-recent-total-label');
            const hasMore = template.getAttribute('data-recent-has-more');
            const ids = new Set(current.map(card => card.getAttribute('data-item-id')));
            if (template.closest(ROOT) !== root || !terminal || !terminal.querySelector('.media-shelf__view-more')
                || track.querySelectorAll('.media-shelf__view-more').length !== 1
                || template.content.querySelector('.media-shelf__view-more')
                || !Number.isInteger(expected) || expected > 24 || expected < 1
                || !label || (hasMore !== 'true' && hasMore !== 'false')
                || current.length > 8 || ids.size !== current.length
                || current.some(card => !/^[1-9][0-9]*$/.test(card.getAttribute('data-item-id') || ''))
                || cells.length > 16 || expected !== current.length + cells.length) return false;
            for (const cell of cells) {
                const card = cell.querySelector(':scope > article.media-card[data-card-presentation="shelf-preview-v1"]');
                const id = card && card.getAttribute('data-item-id');
                if (cell.tagName !== 'LI' || !cell.classList.contains('media-shelf__slot')
                    || !id || !/^[1-9][0-9]*$/.test(id) || ids.has(id)
                    || cell.querySelectorAll('.media-card').length !== 1) return false;
                ids.add(id);
            }
            const fragment = document.createDocumentFragment();
            for (const cell of cells) fragment.append(cell); // Move, never clone/re-render.
            track.insertBefore(fragment, terminal);
            const count = root.querySelector('[data-recent-count]');
            if (count) count.textContent = label;
            const hint = terminal.querySelector('.media-shelf__hint');
            if (hint) hint.hidden = hasMore !== 'true';
            template.remove();
        }
        // Removing this buffer is its completion state. HTMX can preserve the
        // wrapper while replacing its contents with a fresh eight + buffer.
        if (root.getAttribute('data-recent-enhanced') !== 'true') root.setAttribute('data-recent-enhanced', 'true');
        return true;
    }

    function refresh(root) {
        if (!owned(root)) return;
        const track = trackFor(root);
        if (!track || typeof track.scrollBy !== 'function' || !enhance(root, track)) {
            root.removeAttribute('data-recent-enhanced');
            for (const button of root.querySelectorAll(ACTION)) {
                if (button.closest(ROOT) !== root) continue;
                button.disabled = true;
                button.setAttribute('aria-disabled', 'true');
            }
            return;
        }
        const extent = Math.max(0, track.scrollWidth - track.clientWidth);
        const position = Math.abs(track.scrollLeft);
        for (const button of root.querySelectorAll(ACTION)) {
            if (button.closest(ROOT) !== root) continue;
            const action = button.getAttribute('data-recent-action');
            if (action !== 'previous' && action !== 'next') continue;
            button.disabled = false;
            // Keep the activated button focusable even when it reaches an edge.
            button.setAttribute('aria-disabled', String(action === 'previous' ? position <= 1 : position >= extent - 1));
        }
    }

    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
    function refreshAll() {
        scheduled = false;
        for (const root of observed) {
            if (!owned(root)) { if (observer) observer.unobserve(root); observed.delete(root); }
        }
        for (const root of document.querySelectorAll(ROOT)) {
            refresh(root);
            if (observer && !observed.has(root)) { observed.add(root); observer.observe(root); }
        }
    }
    function schedule() {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(refreshAll);
    }

    document.addEventListener('click', function (event) {
        if (!(event.target instanceof Element)) return;
        // The sizing owner legitimately prevents its button click without
        // emitting range events. Refresh layout even on that prevented click.
        const sizing = event.target.closest('button[data-cover-size-action]');
        if (sizing && sizing.closest('[data-cover-size-control]')) schedule();
        const button = event.target.closest(ACTION);
        const root = button && button.closest(ROOT);
        if (!owned(root) || event.defaultPrevented || event.button !== 0 || button.disabled) return;
        refresh(root);
        if (button.getAttribute('aria-disabled') === 'true') return;
        const track = trackFor(root);
        const action = button.getAttribute('data-recent-action');
        if (!track || (action !== 'previous' && action !== 'next')) return;
        event.preventDefault();
        const direction = getComputedStyle(track).direction === 'rtl' ? -1 : 1;
        track.scrollBy({left: direction * (action === 'next' ? 1 : -1) * track.clientWidth * 0.9, behavior: 'auto'});
        refresh(root);
    });
    document.addEventListener('scroll', function (event) {
        if (event.target instanceof Element && event.target.matches('.media-shelf__items')
            && owned(event.target.closest(ROOT))) schedule();
    }, true);
    for (const name of ['input', 'change']) document.addEventListener(name, function (event) {
        if (event.target instanceof Element && event.target.matches('[data-cover-size-input]')) schedule();
    });
    window.addEventListener('resize', schedule);
    document.addEventListener('htmx:afterSwap', refreshAll);
    document.addEventListener('htmx:oobAfterSwap', refreshAll);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refreshAll, {once: true});
    else refreshAll();
}());
