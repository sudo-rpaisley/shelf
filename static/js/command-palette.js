/* Finite navigation, never an action interpreter. No requests, storage or
   policy reconstruction: the rendered authorized navigation is the inventory. */
(function () {
    'use strict';

    // An approval boundary, NOT an inventory: a path is offered only when a
    // marked anchor actually exists in the authorized navigation. Exact page
    // paths exclude API/token/logout, provider actions and mutation subpaths.
    var pages = new Set([
        '/browse', '/library', '/scan', '/intake', '/shelf-fill', '/store',
        '/series', '/music', '/periodicals', '/discover', '/stats', '/activity',
        '/reading-life', '/account/source-progress', '/sources',
        '/source-migrations', '/needs-attention', '/trash', '/labels', '/audits',
        '/duplicates', '/settings', '/logs'
    ]);
    var navSelector = 'a[href][data-nav-primary],a[href][data-nav-tab],a[href][data-nav-family-link],a[href][data-nav-format],a[href][data-nav-management]';

    function bounded(value) { return Array.from(value.trim()).slice(0, 200).join(''); }

    function safeHref(raw) {
        if (!raw || raw !== raw.trim() || /[\\\s\u0000-\u001f\u007f]/.test(raw)) return null;
        if (!/^\/(?!\/)/.test(raw) && !/^https?:\/\//i.test(raw)) return null;
        try {
            // Do not resolve against a potentially different <base> URL.
            var url = new URL(raw, window.location.origin);
            var rawPath = raw.replace(/^https?:\/\/[^/]+/i, '').split(/[?#]/)[0];
            if (url.origin !== window.location.origin || url.username || url.password || url.hash ||
                rawPath !== url.pathname || !pages.has(url.pathname) || url.search.length > 4096) return null;
            var seen = new Set();
            for (var pair of url.searchParams) {
                var key = pair[0], value = pair[1];
                if (seen.has(key) || (url.pathname !== '/browse' && url.pathname !== '/library')) return null;
                seen.add(key);
                if (key === 'sort' && /^(newest|recent)$/.test(value)) continue;
                if (key === 'reset' && value === '1') continue;
                if (key === 'view' && value === 'items') continue;
                if (key === 'types' && value.length <= 512 && /^[a-z][a-z0-9_]*(?:,[a-z][a-z0-9_]*)*$/.test(value)) continue;
                if (key === 'q' && Array.from(value).length <= 200) continue;
                return null; // Includes redirect/next/return URLs and unknown action parameters.
            }
            return url.pathname + url.search;
        } catch (error) { return null; }
    }

    function keyFor(href) {
        var url = new URL(href, window.location.origin);
        url.searchParams.sort();
        return url.pathname + url.search;
    }

    function contextFor(anchor) {
        var branch = anchor.closest('[data-nav-family],[data-nav-group]');
        var summary = branch && branch.querySelector('summary');
        return summary ? summary.textContent.trim().replace(/\s+/g, ' ') : '';
    }

    function inventory() {
        var anchors = [];
        document.querySelectorAll('[data-media-navigation]').forEach(function (nav) {
            anchors = anchors.concat(Array.from(nav.querySelectorAll(navSelector)));
        });
        // Primary Browse wins even if an equivalent All media link appears first.
        anchors.sort(function (a, b) { return Number(b.hasAttribute('data-nav-primary')) - Number(a.hasAttribute('data-nav-primary')); });
        var seen = new Set(), entries = [], primary = null;
        anchors.forEach(function (anchor) {
            // Collapsed details and Alpine's closed Account menu are legitimate
            // inventory. Do NOT use geometry/display to reconstruct visibility.
            if (anchor.closest('[hidden],[inert],[aria-hidden="true"],form') || anchor.hasAttribute('download') ||
                Array.from(anchor.attributes).some(function (attr) { return /^(on|hx-|data-hx-)/i.test(attr.name); })) return;
            var href = safeHref(anchor.getAttribute('href'));
            var label = anchor.textContent.trim().replace(/\s+/g, ' ');
            if (!href || !label) return;
            if (!primary && anchor.getAttribute('data-nav-primary') === 'browse' &&
                ['/browse', '/library'].indexOf(new URL(href, window.location.origin).pathname) !== -1) primary = href;
            var key = keyFor(href);
            if (seen.has(key)) return;
            seen.add(key);
            entries.push({href: href, label: label, context: contextFor(anchor)});
        });
        return {entries: entries, primary: primary};
    }

    function searchHref(primary, query) {
        if (!primary || !query) return null;
        var url = new URL(primary, window.location.origin);
        url.searchParams.set('q', query); // Bound text is encoded, never interpreted as a URL.
        return safeHref(url.pathname + url.search);
    }

    function visible(element) {
        return !!element && element.isConnected && !element.disabled &&
            !element.closest('[hidden],[inert],[aria-hidden="true"]') &&
            window.getComputedStyle(element).visibility !== 'hidden' && element.getClientRects().length > 0;
    }

    function otherOverlay(dialog) {
        return Array.from(document.querySelectorAll('dialog[open],[aria-modal="true"],[role="dialog"],#shortcut-modal,[x-show="showAccount"],[x-data="accountModal"],[data-testid="account-menu-panel"]'))
            .some(function (element) { return element !== dialog && visible(element); });
    }

    function editable(element) {
        return !!element && (element.isContentEditable || !!element.closest('input,textarea,select,[role="textbox"]'));
    }

    function shortcut(event) {
        return event.key.toLowerCase() === 'k' && event.ctrlKey !== event.metaKey &&
            !event.altKey && !event.shiftKey;
    }

    function init() {
        var root = document.querySelector('[data-command-palette]');
        if (!root || root.getAttribute('data-command-palette-enhanced') === 'true') return;
        var dialog = root.querySelector('[data-command-palette-dialog]');
        var launcher = root.querySelector('[data-command-palette-launcher]');
        var input = root.querySelector('[data-command-palette-query]');
        var results = root.querySelector('[data-command-palette-results]');
        var status = root.querySelector('[data-command-palette-status]');
        var close = root.querySelector('[data-command-palette-close]');
        var fallback = root.querySelector('[data-command-palette-fallback]');
        if (!dialog || typeof dialog.showModal !== 'function' || !launcher || !input || !results || !status || !close || !fallback) return;
        var composing = false, opener = null, entries = [], selected = -1, outsideDown = false;

        function choose(index, scroll) {
            selected = entries.length ? (index + entries.length) % entries.length : -1;
            Array.from(results.querySelectorAll('[data-command-palette-result]')).forEach(function (anchor, i) {
                anchor.setAttribute('aria-selected', String(i === selected));
                if (i === selected) {
                    input.setAttribute('aria-activedescendant', anchor.id);
                    if (scroll) anchor.scrollIntoView({block: 'nearest'});
                }
            });
            if (selected < 0) input.removeAttribute('aria-activedescendant');
        }

        function matching() {
            var current = inventory(), query = bounded(input.value);
            var words = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
            var matches = current.entries.filter(function (entry) {
                var text = (entry.label + ' ' + entry.context).toLocaleLowerCase();
                return words.every(function (word) { return text.indexOf(word) !== -1; });
            });
            var href = searchHref(current.primary, query);
            if (href) matches.push({href: href, label: dialog.getAttribute('data-search-label').replace('{query}', query), context: ''});
            return matches;
        }

        function render() {
            entries = matching();
            selected = -1; // A newly empty query must never activate an old selection.
            input.removeAttribute('aria-activedescendant');
            results.replaceChildren();
            entries.forEach(function (entry, i) {
                var item = document.createElement('li'), anchor = document.createElement('a');
                item.setAttribute('role', 'presentation');
                anchor.className = 'command-palette__result';
                anchor.id = 'command-palette-result-' + i;
                anchor.setAttribute('data-command-palette-result', '');
                anchor.setAttribute('role', 'option');
                anchor.setAttribute('href', entry.href);
                anchor.tabIndex = -1;
                var label = document.createElement('span');
                label.textContent = entry.label;
                anchor.append(label);
                if (entry.context) {
                    var context = document.createElement('small');
                    context.textContent = entry.context;
                    anchor.append(context);
                }
                item.append(anchor);
                results.append(item);
            });
            status.textContent = entries.length ? dialog.getAttribute('data-count-label').replace('{count}', String(entries.length)) : dialog.getAttribute('data-empty-label');
            choose(0, false);
        }

        function restoreFocus() {
            var restored = [opener, root.querySelector('[data-command-palette-launcher]'), document.querySelector('[data-nav-primary="browse"]')].some(function (target) {
                if (!visible(target) || target === document.body || target === document.documentElement) return false;
                target.focus();
                return document.activeElement === target;
            });
            if (!restored) {
                var main = document.querySelector('main');
                if (main) {
                    var hadTabindex = main.hasAttribute('tabindex');
                    if (!hadTabindex) main.setAttribute('tabindex', '-1');
                    main.focus();
                    // Removing tabindex while focused blurs main in Chromium.
                    // Keep the temporary focus target only until focus leaves.
                    if (!hadTabindex) main.addEventListener('blur', function () {
                        if (main.getAttribute('tabindex') === '-1') main.removeAttribute('tabindex');
                    }, {once: true});
                }
            }
            opener = null;
        }

        function openPalette(trigger) {
            if (dialog.open || otherOverlay(dialog)) return false;
            opener = trigger;
            input.value = '';
            composing = false;
            render();
            dialog.showModal();
            launcher.setAttribute('aria-expanded', 'true');
            input.setAttribute('aria-expanded', 'true');
            input.focus();
            return true;
        }

        function closePalette() { if (dialog.open) dialog.close(); }

        function allowed(href) { return matching().some(function (entry) { return entry.href === href; }); }

        launcher.addEventListener('click', function () { openPalette(launcher); });
        close.addEventListener('click', closePalette);
        input.addEventListener('input', render);
        dialog.addEventListener('close', function () {
            if (dialog.open) return; // A queued close event must not reset a reopened palette.
            launcher.setAttribute('aria-expanded', 'false');
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            entries = [];
            selected = -1;
            restoreFocus();
        });
        dialog.addEventListener('cancel', function (event) {
            event.preventDefault();
            if (!composing) closePalette();
        });
        function outside(event) {
            var box = dialog.getBoundingClientRect();
            return event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);
        }
        dialog.addEventListener('pointerdown', function (event) { outsideDown = outside(event); });
        dialog.addEventListener('click', function (event) {
            if (outsideDown && outside(event)) closePalette();
            outsideDown = false;
            var anchor = event.target.closest('[data-command-palette-result]');
            if (anchor && !allowed(anchor.getAttribute('href'))) { event.preventDefault(); render(); }
            // Authorized result links retain their native pointer/link defaults.
        });

        // Capture at the dialog, before app.js/browse.js's document listeners.
        // stopPropagation (not just preventDefault) also owns printable keys on
        // the close button. Native input editing and IME defaults remain intact.
        dialog.addEventListener('keydown', function (event) {
            event.stopPropagation();
            if (event.defaultPrevented || composing || event.isComposing || event.keyCode === 229) return;
            if (event.key === 'Escape' || shortcut(event)) {
                event.preventDefault();
                if (!event.repeat) closePalette();
            } else if (event.key === 'Tab') {
                event.preventDefault();
                var controls = Array.from(dialog.querySelectorAll('button,input,a[href],[tabindex]')).filter(function (element) { return element.tabIndex >= 0 && visible(element); });
                var index = controls.indexOf(document.activeElement);
                if (controls.length) controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length].focus();
            } else if (event.target === input && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey &&
                       ['ArrowDown', 'ArrowUp', 'Enter'].indexOf(event.key) !== -1) {
                event.preventDefault();
                if (event.repeat) return;
                if (event.key === 'Enter') {
                    var entry = entries[selected];
                    if (entry && allowed(entry.href)) window.location.assign(entry.href);
                    else render();
                } else choose(selected + (event.key === 'ArrowDown' ? 1 : -1), true);
            }
        }, true);
        ['keyup', 'keypress'].forEach(function (type) {
            dialog.addEventListener(type, function (event) { event.stopPropagation(); }, true);
        });
        window.addEventListener('compositionstart', function () { composing = true; }, true);
        window.addEventListener('compositionend', function () { composing = false; }, true);
        // Bubble outside, so descendant handlers may claim/prevent the shortcut.
        window.addEventListener('keydown', function (event) {
            var target = event.composedPath()[0];
            if (dialog.open || event.defaultPrevented || event.repeat || event.isComposing || composing ||
                event.keyCode === 229 || !shortcut(event) || editable(target) || editable(document.activeElement) || otherOverlay(dialog)) return;
            if (openPalette(document.activeElement)) { event.preventDefault(); event.stopPropagation(); }
        });
        fallback.hidden = true;
        launcher.hidden = false;
        root.setAttribute('data-command-palette-enhanced', 'true');
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
})();
