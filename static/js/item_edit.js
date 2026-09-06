function editSections() {
    var requested = window.location.hash ? window.location.hash.slice(1) : '';
    var allowed = ['general', 'artwork', 'series', 'identifiers', 'copies', 'media'];
    return {
        section: allowed.indexOf(requested) !== -1 ? requested : 'general',
        show(name) {
            this.section = name;
            if (window.history && window.history.replaceState) {
                window.history.replaceState(null, '', '#' + name);
            }
        },
        is(name) {
            return this.section === name;
        }
    };
}

function coverDrop() {
    return {
        dragging: false,
        preview: false,
        handleDrop(e) {
            this.dragging = false;
            var file = e.dataTransfer.files[0];
            if (file && file.type.startsWith('image/')) {
                var dt = new DataTransfer();
                dt.items.add(file);
                this.$refs.coverInput.files = dt.files;
                this.preview = URL.createObjectURL(file);
            }
        },
        handleFile(e) {
            var file = e.target.files[0];
            if (file) this.preview = URL.createObjectURL(file);
        }
    };
}

function editArtworkNotice(message, type) {
    if (window.showToast) {
        window.showToast(message, type || 'info');
    }
}

function editArtworkPost(url, formData) {
    var token = window.csrfToken ? window.csrfToken() : '';
    return fetch(url, {
        method: 'POST',
        headers: token ? {'X-CSRF-Token': token} : {},
        body: formData || new FormData()
    });
}

function installEditArtworkControls() {
    var root = document.querySelector('[data-item-edit-root]');
    if (!root) return;
    var itemId = root.dataset.itemId;
    if (!itemId) return;

    var urlInput = document.getElementById('cover_url');
    var urlButton = root.querySelector('[data-edit-cover-url]');
    if (urlInput && urlButton) {
        urlButton.addEventListener('click', async function () {
            var url = (urlInput.value || '').trim();
            if (!url) {
                editArtworkNotice('Paste an image URL first', 'error');
                urlInput.focus();
                return;
            }
            urlButton.disabled = true;
            try {
                var body = new FormData();
                body.append('url', url);
                var response = await editArtworkPost('/api/items/' + itemId + '/cover-url', body);
                if (!response.ok || !response.headers.get('HX-Redirect')) {
                    editArtworkNotice('Could not use that artwork URL', 'error');
                    return;
                }
                window.location.reload();
            } catch (err) {
                editArtworkNotice('Could not use that artwork URL', 'error');
            } finally {
                urlButton.disabled = false;
            }
        });
    }

    var removeButton = root.querySelector('[data-edit-cover-remove]');
    if (removeButton) {
        removeButton.addEventListener('click', async function () {
            if (!window.confirm('Remove this artwork?')) return;
            removeButton.disabled = true;
            try {
                var response = await editArtworkPost('/api/items/' + itemId + '/cover-remove');
                if (!response.ok || !response.headers.get('HX-Redirect')) {
                    editArtworkNotice('Could not remove artwork', 'error');
                    return;
                }
                window.location.reload();
            } catch (err) {
                editArtworkNotice('Could not remove artwork', 'error');
            } finally {
                removeButton.disabled = false;
            }
        });
    }

    var retryButton = root.querySelector('[data-edit-cover-retry]');
    if (retryButton) {
        retryButton.addEventListener('click', async function () {
            retryButton.disabled = true;
            try {
                var response = await editArtworkPost('/api/items/' + itemId + '/retry-cover');
                var result = response.ok ? await response.json() : {ok: false};
                if (!result.ok) {
                    editArtworkNotice(result.message || 'No artwork found', 'error');
                    return;
                }
                window.location.reload();
            } catch (err) {
                editArtworkNotice('Could not retry artwork lookup', 'error');
            } finally {
                retryButton.disabled = false;
            }
        });
    }

    root.addEventListener('click', async function (event) {
        var button = event.target.closest('[data-edit-cover-select-url]');
        if (!button || !root.contains(button)) return;
        var url = button.dataset.editCoverSelectUrl || '';
        if (!url) return;

        button.disabled = true;
        try {
            var body = new FormData();
            body.append('url', url);
            var query = document.getElementById('cover-query-' + itemId);
            if (query) body.append('query', query.value || '');
            var response = await editArtworkPost('/api/items/' + itemId + '/cover-select', body);
            if (!response.ok || !response.headers.get('HX-Redirect')) {
                editArtworkNotice('Could not use that artwork', 'error');
                return;
            }
            window.location.reload();
        } catch (err) {
            editArtworkNotice('Could not use that artwork', 'error');
        } finally {
            button.disabled = false;
        }
    });
}

function writeBarcodeField(input, value) {
    if (!input) return;
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

function applyScannedBarcode(target, decodedText, scanMode, supplementTargetId) {
    var raw = String(decodedText || '').trim();
    var digits = raw.replace(/\D/g, '');

    if (scanMode === 'periodical-carrier') {
        var carrierLength = 0;
        if (digits.length === 15 || digits.length === 18) carrierLength = 13;
        if (digits.length === 14 || digits.length === 17) carrierLength = 12;

        if (carrierLength) {
            writeBarcodeField(target, digits.slice(0, carrierLength));
            var supplementTarget = document.getElementById(supplementTargetId || '');
            writeBarcodeField(supplementTarget, digits.slice(carrierLength));
            return true;
        }

        writeBarcodeField(target, raw);
        return true;
    }

    if (scanMode === 'periodical-supplement') {
        var supplement = '';
        if (digits.length === 15 || digits.length === 18) {
            supplement = digits.slice(13);
        } else if (digits.length === 14 || digits.length === 17) {
            supplement = digits.slice(12);
        } else if (digits.length === 2 || digits.length === 5) {
            supplement = digits;
        } else {
            return false;
        }
        writeBarcodeField(target, supplement);
        return true;
    }

    writeBarcodeField(target, raw);
    return true;
}

function installBarcodeFieldScanner() {
    var modal = document.getElementById('edit-barcode-scanner');
    var closeButton = document.getElementById('edit-barcode-scanner-close');
    var html5Reader = document.getElementById('edit-barcode-camera-reader');
    var zxingContainer = document.getElementById('edit-barcode-zxing-container');
    var status = document.getElementById('edit-barcode-scanner-status');
    var buttons = document.querySelectorAll('[data-scan-barcode-target]');
    if (!modal || !closeButton || !html5Reader || !zxingContainer || !status || !buttons.length) return;

    var scanner = false;
    var target = false;
    var scanMode = '';
    var supplementTargetId = '';
    var closing = false;

    function showModal() {
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }

    function hideModal() {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        html5Reader.classList.remove('hidden');
        zxingContainer.classList.add('hidden');
    }

    async function stopScanner() {
        if (closing) return;
        closing = true;
        var active = scanner;
        scanner = false;
        if (active) {
            try { await active.stop(); } catch (e) {}
        }
        hideModal();
        closing = false;
    }

    async function startScanner(input, mode, supplementId) {
        await stopScanner();
        target = input;
        scanMode = mode || '';
        supplementTargetId = supplementId || '';
        status.textContent = scanMode === 'periodical-supplement'
            ? 'Point the camera at the magazine barcode and include the add-on if possible.'
            : 'Point the camera at the barcode.';
        showModal();

        if (!window.createBarcodeScanner) {
            status.textContent = 'Barcode scanner could not be loaded.';
            return;
        }

        scanner = window.createBarcodeScanner({
            html5ElId: 'edit-barcode-camera-reader',
            videoEl: 'edit-barcode-zxing-video',
            html5Config: { fps: 10, qrbox: { width: 280, height: 100 }, aspectRatio: 1.5 },
            forceZxing: scanMode === 'periodical-supplement',
            onDecode: function (decodedText) {
                if (!target) return;
                if (!applyScannedBarcode(target, decodedText, scanMode, supplementTargetId)) {
                    status.textContent = 'No 2- or 5-digit add-on was detected. Try again or enter it manually.';
                    return;
                }
                var completedTarget = target;
                target = false;
                scanMode = '';
                supplementTargetId = '';
                stopScanner().then(function () {
                    completedTarget.focus();
                    completedTarget.select();
                });
            }
        });

        if (scanner.engine === 'zxing') {
            html5Reader.classList.add('hidden');
            zxingContainer.classList.remove('hidden');
        } else {
            html5Reader.classList.remove('hidden');
            zxingContainer.classList.add('hidden');
        }

        try {
            await scanner.start();
        } catch (err) {
            scanner = false;
            if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
                status.textContent = 'Camera access requires HTTPS.';
            } else {
                status.textContent = 'Camera access was denied. Check browser permissions for this site.';
            }
        }
    }

    buttons.forEach(function (button) {
        button.addEventListener('click', function () {
            var input = document.getElementById(button.dataset.scanBarcodeTarget);
            if (input) {
                startScanner(
                    input,
                    button.dataset.scanBarcodeMode || '',
                    button.dataset.scanBarcodeSupplementTarget || ''
                );
            }
        });
    });

    closeButton.addEventListener('click', function () {
        target = false;
        stopScanner();
    });
    modal.addEventListener('click', function (event) {
        if (event.target === modal) {
            target = false;
            stopScanner();
        }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.classList.contains('hidden')) {
            target = false;
            stopScanner();
        }
    });
}

// Existing libraries can contain ISBN-shaped identifiers written by older
// Shelf versions before checksum validation existed. Do not resubmit an
// unchanged ISBN when saving an unrelated edit: the server only needs to
// validate the identifier when the user actually changes it. Disabling the
// unchanged field at submit time keeps legacy rows editable without weakening
// validation for new values (disabled controls are not included in form data).
document.addEventListener('DOMContentLoaded', function () {
    installBarcodeFieldScanner();
    installEditArtworkControls();

    var isbn = document.getElementById('isbn');
    if (!isbn || !isbn.form) return;
    isbn.form.addEventListener('submit', function () {
        if (isbn.value === isbn.defaultValue) isbn.disabled = true;
    });
});

// CSP build has no global fallback — register component names explicitly.
document.addEventListener('alpine:init', function () {
    Alpine.data('editSections', editSections);
    Alpine.data('coverDrop', coverDrop);
});
