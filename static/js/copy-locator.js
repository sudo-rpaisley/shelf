/* Native exact-copy anchors are the fallback. No storage, history rewrite,
   requests or mutations: enhance focus and clearance only, including on Back. */
(function () {
    'use strict';
    if (window.__shelfCopyLocator) return;
    window.__shelfCopyLocator = true;

    function clearance() {
        var nav = document.querySelector('[data-media-navigation]');
        var height = nav ? nav.getBoundingClientRect().height : 0;
        document.documentElement.style.setProperty('--copy-locator-clearance', Math.ceil(height + 16) + 'px');
    }

    function anchorTarget() {
        // Never interpret metadata or arbitrary fragments as CSS selectors.
        var match = /^#(copy-[1-9][0-9]*|copy-locator-open-[1-9][0-9]*)$/.exec(window.location.hash);
        if (!match) return null;
        var node = document.getElementById(match[1]);
        if (!node) return null;
        if (node.matches('[data-copy-locator-target]') || node.matches('a.copy-locator-link')) return node;
        return null;
    }

    function focusAnchor() {
        clearance();
        var node = anchorTarget();
        if (!node) return;
        node.focus({preventScroll: true});
        node.scrollIntoView({behavior: 'instant', block: 'start'});
    }

    function ready() {
        clearance();
        var nav = document.querySelector('[data-media-navigation]');
        if (nav && window.ResizeObserver) new ResizeObserver(clearance).observe(nav);
        focusAnchor();
    }

    window.addEventListener('hashchange', focusAnchor);
    // History restoration can apply the old scroll offset after pageshow
    // (e.g. the user scrolled up to Back to item). Align on the next frame,
    // after that native restoration, without rewriting or cancelling history.
    window.addEventListener('pageshow', function () {
        window.requestAnimationFrame(focusAnchor);
    });
    window.addEventListener('resize', clearance);
    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
        var link = event.target.closest('a[data-copy-locator-jump]');
        // Re-activating the same hash produces no hashchange, but should still
        // put keyboard focus on the selected copy. Do not cancel navigation.
        if (link && link.hash === window.location.hash) focusAnchor();
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, {once: true});
    else ready();
}());
