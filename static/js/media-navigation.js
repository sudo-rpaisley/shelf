/* Native disclosure navigation. No Alpine, inline handlers, requests or storage.
 * Enter/Space/click/Tab remain browser defaults; this only enhances disclosure
 * state, desktop hover, outside dismissal and Escape focus return. */
(function () {
    'use strict';

    function replaceShellOpener(shell, toggle) {
        const native = shell.querySelector(':scope > summary');
        const tree = shell.querySelector('[data-testid="nav-menu-panel"]');
        if (!native || !tree || tree.parentElement !== shell) return null;
        const wasOpen = shell.open;
        const active = document.activeElement;
        const oldID = tree.getAttribute('id');
        const hook = native.getAttribute('data-testid');
        const oldMode = shell.getAttribute('data-nav-shell-button');
        let button = null;
        let listening = false;
        let changed = false;

        function restoreAttribute(element, name, value) {
            if (value === null) element.removeAttribute(name);
            else element.setAttribute(name, value);
        }

        try {
            // Real capabilities, not a browser/environment identity. An older
            // DOM or stylesheet keeps the existing native enhancement below.
            if (typeof document.createElement !== 'function' || typeof document.getElementById !== 'function'
                || typeof shell.insertBefore !== 'function' || typeof shell.removeAttribute !== 'function'
                || typeof native.removeAttribute !== 'function' || typeof tree.removeAttribute !== 'function'
                || typeof native.cloneNode !== 'function' || typeof window.getComputedStyle !== 'function'
                || !window.CSS || typeof window.CSS.supports !== 'function'
                || !window.CSS.supports('selector(details::details-content)')
                || !window.CSS.supports('content-visibility', 'visible')
                || !window.CSS.supports('display', 'contents')) return null;

            button = document.createElement('button');
            if (!button || button.tagName !== 'BUTTON' || typeof button.checkVisibility !== 'function'
                || typeof button.remove !== 'function' || typeof button.removeEventListener !== 'function') return null;
            button.type = 'button';
            button.className = 'media-navigation__shell-button';
            const label = native.cloneNode(true);
            while (label.firstChild) button.appendChild(label.firstChild);
            let id = oldID;
            if (!id || Array.from(document.querySelectorAll('[id]')).some(element => element !== tree && element.id === id)) {
                let suffix = 1;
                do { id = 'media-navigation-tree-' + suffix++; } while (document.getElementById(id));
            }
            changed = true;
            tree.setAttribute('id', id);
            button.setAttribute('aria-controls', id);
            button.addEventListener('click', toggle);
            listening = true;
            shell.insertBefore(button, tree); // summary stays first; Browse still precedes these same details
            shell.setAttribute('data-nav-shell-button', 'checking');
            shell.open = false;
            button.setAttribute('aria-expanded', 'false');

            // CSS.supports alone does not prove that the loaded sheet contains
            // the opt-in rules, or that a closed details really paints its button.
            const pseudo = window.getComputedStyle(shell, '::details-content');
            const painted = button.getBoundingClientRect();
            if (shell.open || pseudo.contentVisibility !== 'visible' || pseudo.display !== 'contents'
                || window.getComputedStyle(native).display === 'none'
                || window.getComputedStyle(tree).display !== 'none' || tree.getClientRects().length
                || !button.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
                || painted.width <= 0 || painted.height <= 0) throw new Error('Closed shell button is not available');

            shell.open = wasOpen;
            button.setAttribute('aria-expanded', String(wasOpen));
            if (hook !== null) {
                native.removeAttribute('data-testid');
                button.setAttribute('data-testid', hook);
            }
            shell.setAttribute('data-nav-shell-button', 'ready');
            const target = active === native ? button : active;
            if (target && document.activeElement !== target && typeof target.focus === 'function') target.focus();
            return button;
        } catch (_) {
            // Roll back partial insertion, listener/hook transfer, IDs and the
            // probe state. This optional upgrade must not abort native behavior.
            if (button) {
                if (listening) button.removeEventListener('click', toggle);
                if (typeof button.remove === 'function') button.remove();
            }
            if (changed) {
                restoreAttribute(shell, 'data-nav-shell-button', oldMode);
                restoreAttribute(tree, 'id', oldID);
                restoreAttribute(native, 'data-testid', hook);
                shell.open = wasOpen;
                if (active && document.activeElement !== active && typeof active.focus === 'function') active.focus();
            }
            return null;
        }
    }

    function enhance(root) {
        if (root.getAttribute('data-nav-enhanced')) return;
        const shell = root.querySelector('details[data-nav-shell]');
        if (!shell) return;
        const main = shell.parentElement;
        const browse = root.querySelector('details[data-nav-browse]');
        const primary = root.querySelector('[data-nav-primary="browse"]');
        const tree = shell.querySelector('[data-testid="nav-menu-panel"]');
        const disclosures = Array.from(root.querySelectorAll('details[data-nav-disclosure]'));
        const hoverOpened = new Set();
        let returnShellFocus = false;
        let lastTreeFocus = null;
        const desktop = window.matchMedia('(min-width: 64rem)');
        const hover = window.matchMedia('(hover: hover) and (pointer: fine)');
        const summary = details => details.querySelector(':scope > summary');
        const parentDisclosure = details => details.parentElement.closest('details[data-nav-disclosure]');
        const persistent = details => details === shell && desktop.matches;
        // Explore is in-flow quick navigation, not a flyout or accordion peer.
        // Its native open state belongs to the user, including across resizes.
        const quick = details => details.getAttribute('data-nav-group') === 'explore';
        let shellButton = null;
        let processedShellOpen = shell.open;
        let lastOpenerFocus = null;
        let layoutDesktop = desktop.matches;
        const opener = details => details === shell && shellButton ? shellButton : summary(details);
        const desktopFocus = () => browse ? summary(browse) : primary || root.querySelector('a[href="/"]');

        function sync(details) {
            summary(details).setAttribute('aria-expanded', String(details.open));
            if (details === shell) {
                if (shellButton) shellButton.setAttribute('aria-expanded', String(shell.open));
                if (!desktop.matches && !shell.open && document.activeElement === primary) opener(shell).focus();
                // Closing details programmatically can blur a focused tree
                // descendant before its toggle event. Return only lost/internal
                // focus, never a genuine outside control's newly acquired focus.
                if (!shell.open && lastTreeFocus && (document.activeElement === lastTreeFocus
                    || document.activeElement === document.body)) opener(shell).focus();
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

        function toggled(details) {
            if (persistent(details) && !details.open && !shellButton) details.open = true;
            if (details === shell) {
                // Probe/initial layout toggles may already be queued. Process
                // actual state changes once, not those synthetic setup events.
                if (processedShellOpen === shell.open) {
                    sync(shell);
                    return;
                }
                processedShellOpen = shell.open;
            }
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
        }

        shellButton = replaceShellOpener(shell, function () {
            const hadFocus = document.activeElement === shellButton;
            shell.open = !shell.open;
            toggled(shell);
            // Reopening a closed desktop shell hides its opener in favor of
            // the visible row. Keep a real user's focus in that row.
            if (hadFocus && desktop.matches && shell.open) desktopFocus().focus();
        });

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
                toggled(details);
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
            opener(details).focus();
        });

        document.addEventListener('pointerdown', function (event) {
            if (!main.contains(event.target)) lastOpenerFocus = null;
            returnShellFocus = !desktop.matches && (document.activeElement === primary || shell.contains(document.activeElement))
                && !main.contains(event.target);
            if (!main.contains(event.target)) close(shell);
        });
        document.addEventListener('click', function () {
            // A non-focusable outside click can clear the focus returned during
            // pointerdown. Do not override a real outside control's own focus.
            if (returnShellFocus && !shell.open && document.activeElement === document.body) opener(shell).focus();
            returnShellFocus = false;
        });
        root.addEventListener('focusin', function (event) {
            lastTreeFocus = tree && tree.contains(event.target) ? event.target : null;
            lastOpenerFocus = event.target === opener(shell) ? event.target : null;
        });
        root.addEventListener('focusout', function (event) {
            if (event.relatedTarget && event.relatedTarget !== opener(shell)) lastOpenerFocus = null;
            if (event.relatedTarget && !shell.contains(event.relatedTarget)) lastTreeFocus = null;
            if (!main.contains(event.relatedTarget)) close(shell);
        });

        function layout() {
            const active = document.activeElement;
            // CSS can hide the open-shell button before matchMedia dispatches
            // change, leaving body focused. Remember that genuine opener focus
            // across the seam, but never a deliberate outside focus/click.
            const hadFocus = shell.contains(active) || active === primary
                || (desktop.matches !== layoutDesktop && lastOpenerFocus && active === document.body);
            layoutDesktop = desktop.matches;
            close(shell);
            shell.open = desktop.matches;
            processedShellOpen = shell.open;
            sync(shell);
            if (hadFocus) {
                // An explicit empty nav context has no Browse/primary link.
                // Keep focus visible on the existing brand, without inventing tabs.
                const target = desktop.matches
                    ? (active === primary ? primary : desktopFocus())
                    : opener(shell);
                if (target) {
                    target.focus();
                    lastOpenerFocus = target === opener(shell) ? target : null;
                }
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
