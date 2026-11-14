/* Native disclosure navigation. No Alpine, inline handlers, requests or storage.
 * Enter/Space/click/Tab remain browser defaults; this only enhances disclosure
 * state, desktop hover, outside dismissal and Escape focus return. */
(function () {
    'use strict';

    function enhance(root) {
        if (root.getAttribute('data-nav-enhanced')) return;
        const shell = root.querySelector('details[data-nav-shell]');
        if (!shell) return;
        const main = shell.parentElement;
        const browse = root.querySelector('details[data-nav-browse]');
        const primary = root.querySelector('[data-nav-primary="browse"]');
        const disclosures = Array.from(root.querySelectorAll('details[data-nav-disclosure]'));
        const hoverOpened = new Set();
        let returnPrimaryFocus = false;
        const desktop = window.matchMedia('(min-width: 64rem)');
        const hover = window.matchMedia('(hover: hover) and (pointer: fine)');
        const summary = details => details.querySelector(':scope > summary');
        const parentDisclosure = details => details.parentElement.closest('details[data-nav-disclosure]');
        const persistent = details => details === shell && desktop.matches;
        // Explore is in-flow quick navigation, not a flyout or accordion peer.
        // Its native open state belongs to the user, including across resizes.
        const quick = details => details.getAttribute('data-nav-group') === 'explore';

        function sync(details) {
            summary(details).setAttribute('aria-expanded', String(details.open));
            if (details === shell) {
                if (!desktop.matches && !shell.open && document.activeElement === primary) summary(shell).focus();
                root.setAttribute('data-nav-menu-open', String(shell.open));
            }
        }

        function close(details) {
            // Reverse DOM order closes nested state too, so reopening a mobile
            // branch never resurrects a forgotten sideways desktop flyout.
            for (const candidate of disclosures.slice().reverse()) {
                if (details.contains(candidate) && !persistent(candidate)
                    && (!quick(candidate) || candidate === details)) {
                    hoverOpened.delete(candidate);
                    candidate.open = false;
                    sync(candidate);
                }
            }
        }

        for (const details of disclosures) {
            summary(details).addEventListener('click', function (event) {
                // Pointer entry may already have opened this native details.
                // Do not let that same first mouse click immediately close it.
                // A second click and all keyboard activation remain native.
                if (event.detail > 0 && desktop.matches && hoverOpened.has(details)) {
                    hoverOpened.delete(details);
                    event.preventDefault();
                }
            });
            details.addEventListener('toggle', function () {
                if (persistent(details) && !details.open) details.open = true;
                if (details.open) {
                    const parent = parentDisclosure(details);
                    for (const peer of disclosures) {
                        if (!quick(details) && !quick(peer) && peer !== details
                            && parentDisclosure(peer) === parent) close(peer);
                    }
                    if (details === shell && !desktop.matches && browse) browse.open = true;
                } else {
                    close(details);
                }
                sync(details);
            });
            sync(details);
        }

        for (const region of root.querySelectorAll('[data-nav-hover]')) {
            const details = region.querySelector('details[data-nav-disclosure]');
            if (!details) continue;
            region.addEventListener('pointerenter', function (event) {
                if (event.pointerType !== 'mouse' || !desktop.matches || !hover.matches) return;
                const active = document.activeElement;
                // A passing pointer must not close a keyboard user's branch
                // while it contains focus. Hovering within that branch is fine.
                const focusedPeer = disclosures.some(peer => peer.open && !persistent(peer) && !quick(peer)
                    && peer !== details && !peer.contains(details) && peer.contains(active));
                if (!focusedPeer && !details.open) {
                    hoverOpened.add(details);
                    details.open = true;
                }
            });
            region.addEventListener('pointerleave', function (event) {
                hoverOpened.delete(details);
                if (event.pointerType === 'mouse' && desktop.matches
                    && !details.contains(document.activeElement)) close(details);
            });
        }

        root.addEventListener('keydown', function (event) {
            if (event.key !== 'Escape') return;
            let details = event.target.closest('details[data-nav-disclosure]');
            while (details && (!details.open || persistent(details))) details = parentDisclosure(details);
            if (!details) details = disclosures.find(candidate => candidate.open && !persistent(candidate) && !quick(candidate));
            if (!details) return;
            event.preventDefault();
            event.stopPropagation();
            close(details);
            summary(details).focus();
        });

        document.addEventListener('pointerdown', function (event) {
            returnPrimaryFocus = !desktop.matches && document.activeElement === primary
                && !main.contains(event.target);
            if (!main.contains(event.target)) close(shell);
        });
        document.addEventListener('click', function () {
            // A non-focusable outside click can clear the focus returned during
            // pointerdown. Do not override a real outside control's own focus.
            if (returnPrimaryFocus && !shell.open && document.activeElement === document.body) summary(shell).focus();
            returnPrimaryFocus = false;
        });
        root.addEventListener('focusout', function (event) {
            if (!main.contains(event.relatedTarget)) close(shell);
        });

        function layout() {
            const active = document.activeElement;
            const hadFocus = shell.contains(active) || active === primary;
            close(shell);
            shell.open = desktop.matches;
            sync(shell);
            if (hadFocus) {
                // An explicit empty nav context has no Browse/primary link.
                // Keep focus visible on the existing brand, without inventing tabs.
                const target = desktop.matches
                    ? (active === primary ? primary : browse ? summary(browse) : primary || root.querySelector('a[href="/"]'))
                    : summary(shell);
                if (target) target.focus();
            }
        }
        desktop.addEventListener('change', layout);
        layout();
        root.setAttribute('data-nav-enhanced', 'true');
    }

    function start() {
        for (const root of document.querySelectorAll('[data-media-navigation]')) enhance(root);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
    else start();
}());
