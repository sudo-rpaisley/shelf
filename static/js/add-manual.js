/* Progressive manual-entry controls. No automatic provider lookup or writes;
   copied fields are the existing finite item-template subset, never identity. */
(function () {
    'use strict';
    var instances = new WeakMap();

    function init(form) {
        if (form.dataset.manualEnhanced) return;
        form.dataset.manualEnhanced = 'true';
        var type = form.elements.namedItem('media_type');
        function applicability() {
            var retired = form.hasAttribute('data-add-prepared') && form.querySelector('[name="context"]').disabled;
            form.querySelectorAll('fieldset[data-manual-types]').forEach(function (group) {
                var applies = group.dataset.manualTypes.split(' ').indexOf(type ? type.value : '') !== -1;
                group.hidden = !applies;
                group.disabled = retired || !applies;
            });
        }
        form.addEventListener('change', applicability);
        form.addEventListener('submit', applicability, true);
        instances.set(form, {apply: applicability, cancel: function () {}});
        applicability();

        var copy = form.querySelector('[data-manual-copy]');
        if (!copy || !window.fetch || !window.AbortController) return;
        var query = copy.querySelector('[data-copy-query]');
        var results = copy.querySelector('[data-copy-results]');
        var status = copy.querySelector('[data-copy-status]');
        var serial = 0, pending = null;
        function cancel() {
            ++serial;
            if (pending) pending.abort();
            pending = null;
            results.replaceChildren();
        }
        instances.get(form).cancel = cancel;
        async function read(url) {
            if (!form.isConnected) return;
            cancel();
            var version = serial, controller = new AbortController();
            pending = controller;
            try {
                var response = await fetch(url, {signal: controller.signal, credentials: 'same-origin'});
                if (!response.ok) throw new Error('Copy unavailable');
                var value = await response.json();
                if (version !== serial || !form.isConnected) return;
                return value;
            } catch (error) {
                if (version === serial && !controller.signal.aborted) status.textContent = form.dataset.copyError;
            } finally {
                if (version === serial) pending = null;
            }
        }
        function assign(name, value) {
            var field = form.elements.namedItem(name);
            if (!field || value === null || value === undefined) return;
            if (field.tagName === 'SELECT' && !Array.from(field.options).some(function (option) { return option.value === String(value); })) return;
            field.value = String(value);
            field.dispatchEvent(new Event('change', {bubbles: true}));
        }
        async function pick(id) {
            if (!/^[1-9][0-9]{0,18}$/.test(String(id))) return;
            var url = new URL(form.dataset.copyUrl, window.location.origin);
            url.pathname = url.pathname.replace(/\/1\/copy-template$/, '/' + id + '/copy-template');
            if (url.origin !== window.location.origin) return;
            var data = await read(url.href);
            if (!data || data.error) return;
            // A bound captured format is never changed by a copy template.
            if (type && type.tagName === 'SELECT') assign('media_type', data.media_type);
            ['authors', 'publisher', 'publish_year', 'series_name', 'location_id'].forEach(function (name) { assign(name, data[name]); });
            if (type && type.value === 'video_game') assign('platform', data.platform);
            status.textContent = form.dataset.copySuccess;
        }
        copy.querySelector('[data-copy-search]').addEventListener('click', async function () {
            status.textContent = '';
            var text = query.value.trim();
            if (!text) { cancel(); return; }
            var url = new URL(form.dataset.suggestUrl, window.location.origin);
            if (url.origin !== window.location.origin) return;
            url.searchParams.set('q', Array.from(text).slice(0, 200).join(''));
            var data = await read(url.href);
            if (!Array.isArray(data)) return;
            data.slice(0, 10).forEach(function (row) {
                if (!row || typeof row.title !== 'string' || !/^[1-9][0-9]{0,18}$/.test(String(row.id))) return;
                var item = document.createElement('li'), button = document.createElement('button');
                button.type = 'button';
                button.className = 'text-sm text-shelf-accent2 underline';
                button.textContent = row.title + (row.authors ? ' — ' + row.authors : '');
                button.addEventListener('click', function () { pick(row.id); });
                item.append(button);
                results.append(item);
            });
        });
        query.addEventListener('input', function () { cancel(); status.textContent = ''; });
        query.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.isComposing) {
                event.preventDefault(); // Find, never the surrounding manual Save.
                copy.querySelector('[data-copy-search]').click();
            }
        });
        copy.hidden = false;
    }

    function enhance(root) {
        if (root.matches && root.matches('form[data-manual-entry]')) init(root);
        root.querySelectorAll('form[data-manual-entry]').forEach(init);
    }
    enhance(document);
    window.addEventListener('pageshow', function () {
        document.querySelectorAll('form[data-manual-entry]').forEach(function (form) {
            var instance = instances.get(form);
            if (instance) instance.apply();
        });
    });
    function cancelWithin(root) {
        var forms = Array.from(root.querySelectorAll('form[data-manual-entry]'));
        if (root.matches && root.matches('form[data-manual-entry]')) forms.push(root);
        forms.forEach(function (form) { var instance = instances.get(form); if (instance) instance.cancel(); });
    }
    window.addEventListener('pagehide', function () { cancelWithin(document); });
    document.addEventListener('htmx:beforeCleanupElement', function (event) { cancelWithin(event.target); });
    document.addEventListener('htmx:afterSwap', function (event) { enhance(event.detail.target); });
    document.addEventListener('shelf:scan-result', function (event) { enhance(event.target); });
})();
