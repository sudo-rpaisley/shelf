(function () {
    'use strict';

    function init() {
        const list = document.getElementById('location-copy-list');
        if (!list) return;

        const locationId = list.dataset.locationId;
        const status = document.getElementById('location-order-status');
        let dragged = null;

        function rows() {
            return Array.from(list.querySelectorAll('[data-copy-id]'));
        }

        function refreshLabels() {
            rows().forEach(function (row, index) {
                const label = row.querySelector('[data-position-label]');
                if (label) label.textContent = String(index + 1);
            });
        }

        async function saveOrder() {
            const ids = rows().map(function (row) { return row.dataset.copyId; });
            if (status) status.textContent = 'Saving order…';
            const body = new URLSearchParams();
            body.set('copy_ids', ids.join(','));
            try {
                const response = await fetch('/api/location-tree/' + encodeURIComponent(locationId) + '/order', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                        'X-CSRF-Token': window.csrfToken ? window.csrfToken() : ''
                    },
                    body: body.toString()
                });
                if (!response.ok) throw new Error('save failed');
                if (status) status.textContent = 'Order saved.';
            } catch (error) {
                if (status) status.textContent = 'Could not save the new order. Reload the page and try again.';
            }
        }

        rows().forEach(function (row) {
            if (!row.hasAttribute('draggable')) return;

            row.addEventListener('dragstart', function (event) {
                dragged = row;
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', row.dataset.copyId);
                row.style.opacity = '0.55';
            });

            row.addEventListener('dragend', function () {
                row.style.opacity = '';
                dragged = null;
            });

            row.addEventListener('dragover', function (event) {
                if (!dragged || dragged === row) return;
                event.preventDefault();
                const box = row.getBoundingClientRect();
                const before = event.clientY < box.top + box.height / 2;
                list.insertBefore(dragged, before ? row : row.nextSibling);
                refreshLabels();
            });

            row.addEventListener('drop', function (event) {
                event.preventDefault();
                refreshLabels();
                saveOrder();
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
