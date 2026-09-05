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
    }
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

    var isbn = document.getElementById('isbn');
    if (!isbn || !isbn.form) return;
    isbn.form.addEventListener('submit', function () {
        if (isbn.value === isbn.defaultValue) isbn.disabled = true;
    });
});

// CSP build has no global fallback — register so x-data="coverDrop" resolves.
document.addEventListener('alpine:init', function () {
    Alpine.data('coverDrop', coverDrop);
});