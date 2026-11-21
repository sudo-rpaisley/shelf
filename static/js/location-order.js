(function () {
    'use strict';

    function start() {
        // Both hosts carry NODE ids, never legacy flat item locations. The
        // fork tree keeps auto-save; the native Arrange page keeps explicit
        // Save and its JSON endpoints. Neither installs on the other host.
        const nativeRoot = document.querySelector('[data-location-order]');
        const forkList = document.getElementById('location-copy-list');
        const list = nativeRoot ? nativeRoot.querySelector('[data-copy-list]') : forkList;
        if (!list || (nativeRoot && nativeRoot.dataset.canEdit !== '1')) return;
        const root = nativeRoot || list;
        const locationId = root.dataset.locationId;
        const status = nativeRoot ? root.querySelector('[data-order-status]') : document.getElementById('location-order-status');
        let dragging = null;
        let saving = false;

        function message(text, error) {
            if (!status) return;
            status.textContent = t(text);
            status.classList.toggle('text-shelf-error', !!error);
            status.classList.toggle('text-shelf-muted', !error);
        }
        function rows() { return Array.from(list.querySelectorAll('[data-copy-id]')); }
        function labels() {
            rows().forEach(function (row, index) {
                const label = row.querySelector('[data-position-label]');
                if (label) label.textContent = String(index + 1);
            });
        }
        async function post(path, body, json) {
            const response = await fetch(path, {
                method: 'POST',
                headers: {'Content-Type': json ? 'application/json' : 'application/x-www-form-urlencoded;charset=UTF-8', 'X-CSRF-Token': window.csrfToken()},
                body: json ? JSON.stringify(body) : new URLSearchParams(body).toString()
            });
            const data = await response.json().catch(function () { return {}; });
            if (!response.ok || data.ok === false) throw new Error(data.message || t('Could not save shelf order'));
        }
        async function saveOrder(button) {
            if (saving) return;
            saving = true;
            if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
            message('Saving…', false);
            const ids = rows().map(function (row) { return Number(row.dataset.copyId); });
            try {
                await post(nativeRoot ? '/api/locations/' + locationId + '/order' : '/api/location-tree/' + encodeURIComponent(locationId) + '/order', {copy_ids: nativeRoot ? ids : ids.join(',')}, !!nativeRoot);
                labels();
                message('Shelf order saved.', false);
            } catch (error) {
                message(error.message, true);
            } finally {
                saving = false;
                if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
            }
        }
        rows().forEach(function (row) {
            if (!row.hasAttribute('draggable')) return;
            row.tabIndex = 0;
            row.addEventListener('dragstart', function (event) {
                if (saving) { event.preventDefault(); return; }
                dragging = row;
                row.setAttribute('aria-grabbed', 'true');
                if (event.dataTransfer) { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', row.dataset.copyId); }
            });
            row.addEventListener('dragend', function () { row.removeAttribute('aria-grabbed'); dragging = null; });
            row.addEventListener('keydown', function (event) {
                if (event.target !== row || !event.altKey || saving) return;
                if (event.key === 'ArrowUp' && row.previousElementSibling) list.insertBefore(row, row.previousElementSibling);
                else if (event.key === 'ArrowDown' && row.nextElementSibling) list.insertBefore(row.nextElementSibling, row);
                else return;
                event.preventDefault();
                row.focus(); labels();
                if (nativeRoot) message('Order changed — save when ready.', false);
                else saveOrder();
            });
        });
        list.addEventListener('dragover', function (event) {
            if (!dragging || saving) return;
            event.preventDefault();
            const target = event.target.closest('[data-copy-id]');
            if (!target || target === dragging || !list.contains(target)) return;
            const before = event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
            list.insertBefore(dragging, before ? target : target.nextSibling);
            labels();
        });
        list.addEventListener('drop', function (event) {
            if (!dragging) return;
            event.preventDefault();
            if (nativeRoot) message('Order changed — save when ready.', false);
            else saveOrder();
        });
        if (!nativeRoot) return;
        const save = root.querySelector('[data-save-order]');
        if (save) save.addEventListener('click', function () { saveOrder(save); });
        root.querySelectorAll('[data-auto-order]').forEach(function (button) {
            button.addEventListener('click', async function () {
                if (saving) return;
                saving = true; button.disabled = true; button.setAttribute('aria-busy', 'true');
                message('Ordering…', false);
                try {
                    await post('/api/locations/' + locationId + '/auto-order', {sort_key: button.dataset.autoOrder}, true);
                    window.location.reload();
                } catch (error) { message(error.message, true); }
                finally { saving = false; button.disabled = false; button.removeAttribute('aria-busy'); }
            });
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
