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

document.addEventListener('DOMContentLoaded', installEditArtworkControls);
