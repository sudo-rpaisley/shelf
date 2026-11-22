function editArtworkNotice(message, type) {
    if (window.showToast) {
        window.showToast(message, type || 'info');
    }
}

function editArtworkPost(url, formData) {
    var token = window.csrfToken ? window.csrfToken() : '';
    var body = formData || new FormData();
    body.set('return_to', 'edit');
    return fetch(url, {
        method: 'POST',
        headers: token ? {'X-CSRF-Token': token} : {},
        body: body
    });
}

function editArtworkToastSucceeded(header) {
    try {
        var events = JSON.parse(header || '{}');
        return Boolean(events && events.showToast && events.showToast.type === 'success');
    } catch (err) { return false; }
}

function editArtworkSucceeded(response) {
    if (!response.ok) return false;
    var header = response.headers.get('HX-Trigger');
    try {
        var events = JSON.parse(header || '{}');
        if (events.showToast && events.showToast.type === 'error') return false;
    } catch (err) { return false; }
    return Boolean(header || response.headers.get('HX-Redirect'));
}

function editArtworkChoiceField(form, name) {
    var field = form.elements.namedItem(name);
    if (!field) {
        field = document.createElement('input');
        field.type = 'hidden';
        field.name = name;
        form.appendChild(field);
    }
    return field;
}

function editArtworkSetChoice(root, form, choice, button) {
    editArtworkChoiceField(form, 'cover_choice_url').value = choice.url || '';
    editArtworkChoiceField(form, 'cover_choice_edition_isbn').value = choice.edition_isbn || '';
    editArtworkChoiceField(form, 'cover_choice_edition_language').value = choice.edition_language || '';
    root.querySelectorAll('[data-edit-cover-select-url]').forEach(function (candidate) {
        candidate.setAttribute('aria-pressed', candidate === button ? 'true' : 'false');
    });
    var host = form.querySelector('[x-data="coverDrop"]');
    if (choice.url && host) host.dispatchEvent(new CustomEvent('edit-artwork-choice', {detail: choice}));
}

function installEditArtworkControls() {
    var root = document.querySelector('[data-item-edit-root]');
    if (!root) return;
    var itemId = root.dataset.itemId;
    if (!itemId) return;
    var form = root.querySelector('#item-edit-form');
    var revision = 0;

    // A later upload supersedes the pending provider choice and its provenance.
    var upload = root.querySelector('#cover-input');
    if (form && upload) {
        upload.addEventListener('change', function () {
            revision += 1;
            editArtworkSetChoice(root, form, {});
        });
    }

    var urlInput = root.querySelector('#edit-cover-url');
    var urlButton = root.querySelector('[data-testid="edit-cover-url-submit"]');
    if (form && urlInput && urlButton) {
        var requests = new WeakMap();
        var outstanding = new Set();
        var previousBusy;
        function releaseManualRequest(xhr) {
            if (!outstanding.delete(xhr) || outstanding.size) return;
            if (previousBusy === null) form.removeAttribute('aria-busy');
            else form.setAttribute('aria-busy', previousBusy);
        }
        // URL is disabled during HTMX and is therefore absent from FormData.
        // Gate the native submit event (also requestSubmit/Enter), not a click
        // or the mixed-source backend check. Never auto-save on completion.
        form.addEventListener('submit', function (event) {
            if (!outstanding.size) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            editArtworkNotice(t('Wait for the artwork URL request to finish, then save your changes.'), 'info');
        }, true);
        function connectedEditor() {
            return root.isConnected && form.isConnected && urlButton.isConnected &&
                root.dataset.itemId === itemId && document.querySelector('[data-item-edit-root]') === root &&
                root.querySelector('#item-edit-form') === form && root.querySelector('#edit-cover-url') === urlInput &&
                root.querySelector('[data-testid="edit-cover-url-submit"]') === urlButton;
        }
        function parameter(config, name) {
            var values = config.parameters;
            return values && typeof values.get === 'function' ? values.get(name) : values && values[name];
        }
        async function refreshStoredPreview(expectedRevision) {
            try {
                var response = await fetch('/api/items/' + itemId + '/cover-status', {redirect: 'error'});
                if (!response.ok) return;
                var html = await response.text();
                if (!connectedEditor() || revision !== expectedRevision) return;
                var image = new DOMParser().parseFromString(html, 'text/html').querySelector('img[data-scan-cover]');
                var src = image && image.getAttribute('src');
                // Read the actual published path from the existing read-only
                // endpoint, never guess a filename or refetch the manual URL.
                if (src && src.startsWith('/covers/')) {
                    var host = form.querySelector('[x-data="coverDrop"]');
                    var fresh = src + (src.indexOf('?') === -1 ? '?' : '&') + 'artwork_preview=' + Date.now() + '-' + expectedRevision;
                    if (host) host.dispatchEvent(new CustomEvent('edit-artwork-choice', {detail: {url: fresh}}));
                }
            } catch (err) { /* A failed preview read never changes saved metadata or newer form intent. */ }
        }
        urlInput.addEventListener('input', function () { revision += 1; });
        root.addEventListener('htmx:beforeRequest', function (event) {
            var detail = event.detail || {};
            var config = detail.requestConfig || {};
            if (event.defaultPrevented || !connectedEditor() || detail.elt !== urlButton || !detail.xhr ||
                config.path !== '/api/items/' + itemId + '/cover-url' ||
                (config.verb || '').toLowerCase() !== 'post' || parameter(config, 'return_to') !== 'edit') return;
            var xhr = detail.xhr;
            if (outstanding.has(xhr)) return;
            var request = {value: parameter(config, 'url'), revision: revision, sent: false};
            requests.set(xhr, request);
            if (!outstanding.size) {
                previousBusy = form.getAttribute('aria-busy');
                form.setAttribute('aria-busy', 'true');
            }
            outstanding.add(xhr);
            function finish() {
                releaseManualRequest(xhr);
                requests.delete(xhr);
            }
            // Native XHR events still arrive if HTMX's issuer was detached or
            // its response handler failed before afterRequest could bubble.
            ['loadend', 'abort', 'error', 'timeout'].forEach(function (name) {
                xhr.addEventListener(name, finish, {once: true});
            });
            xhr.addEventListener('loadstart', function () { request.sent = true; }, {once: true});
            // Cancellation or a synchronous pre-send failure has no native
            // loadstart/completion. send() fires loadstart before returning.
            queueMicrotask(function () { if (!request.sent) finish(); });
        });
        // Observe the existing HTMX POST only. Do not install a second click
        // submitter: HTTP200 alone includes the route's error-toast refusal.
        document.body.addEventListener('htmx:afterRequest', function (event) {
            var detail = event.detail || {};
            var xhr = detail.xhr;
            var request = xhr && requests.get(xhr);
            releaseManualRequest(xhr);
            if (!request) return;
            requests.delete(xhr);
            if (!connectedEditor() || detail.successful !== true || detail.failed || typeof xhr.status !== 'number' || xhr.status < 200 || xhr.status >= 300 ||
                !editArtworkToastSucceeded(xhr.getResponseHeader('HX-Trigger')) || typeof request.value !== 'string' ||
                !request.value.trim() || urlInput.value !== request.value) return;
            urlInput.value = '';
            // A newer provider/file choice must survive a late manual result.
            if (revision !== request.revision) return;
            if (upload) upload.value = '';
            editArtworkSetChoice(root, form, {});
            revision += 1;
            refreshStoredPreview(revision);
        });
    }

    var removeButton = root.querySelector('[data-edit-cover-remove]');
    if (removeButton) {
        removeButton.addEventListener('click', async function () {
            if (!window.confirm('Remove this artwork?')) return;
            removeButton.disabled = true;
            removeButton.setAttribute('aria-busy', 'true');
            try {
                var response = await editArtworkPost('/api/items/' + itemId + '/cover-remove');
                if (!editArtworkSucceeded(response)) {
                    editArtworkNotice('Could not remove artwork', 'error');
                    return;
                }
                editArtworkNotice(t('Artwork removed. Your other edits are still unsaved.'), 'success');
            } catch (err) {
                editArtworkNotice('Could not remove artwork', 'error');
            } finally {
                removeButton.disabled = false;
                removeButton.removeAttribute('aria-busy');
            }
        });
    }

    var retryButton = root.querySelector('[data-edit-cover-retry]');
    if (retryButton) {
        retryButton.addEventListener('click', async function () {
            retryButton.disabled = true;
            retryButton.setAttribute('aria-busy', 'true');
            try {
                var response = await editArtworkPost('/api/items/' + itemId + '/retry-cover');
                var result = response.ok ? await response.json() : {ok: false};
                if (!result.ok) {
                    editArtworkNotice(result.message || 'No artwork found', 'error');
                    return;
                }
                editArtworkNotice(t('Artwork updated. Your other edits are still unsaved.'), 'success');
            } catch (err) {
                editArtworkNotice('Could not retry artwork lookup', 'error');
            } finally {
                retryButton.disabled = false;
                retryButton.removeAttribute('aria-busy');
            }
        });
    }

    root.addEventListener('click', function (event) {
        var button = event.target.closest('[data-edit-cover-select-url]');
        if (!button || !root.contains(button)) return;
        event.preventDefault();
        event.stopPropagation();
        var url = button.dataset.editCoverSelectUrl || '';
        if (!url || !form || !root.contains(form)) return;
        try {
            var choice = JSON.parse(button.dataset.editCoverValues || '{}');
            if (choice.url !== url || choice.return_to !== 'edit') return;
            revision += 1;
            if (upload) upload.value = '';
            editArtworkSetChoice(root, form, choice, button);
            editArtworkNotice(t('Artwork selected. Save Changes to apply it.'), 'info');
        } catch (err) {
            editArtworkNotice('Could not use that artwork', 'error');
        }
    });
}

document.addEventListener('DOMContentLoaded', installEditArtworkControls);
