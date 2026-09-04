// Komga per-library sync selection and Comic/Manga classification.
// Kept separate from components-settings.js so the feature can evolve without
// making the already-large settings component bundle harder to maintain.
document.addEventListener('alpine:init', function () {
    Alpine.data('komgaLibrariesTyped', function () {
        return {
            libs: false,
            libsLoading: false,
            libsError: false,
            libsSaving: false,
            cleaning: false,
            cleanResult: false,

            excludedIds() {
                return this.libs.filter(function (lib) { return !lib.included; })
                    .map(function (lib) { return lib.id; });
            },

            mediaTypes() {
                var result = {};
                this.libs.forEach(function (lib) {
                    result[lib.id] = lib.media_type;
                });
                return result;
            },

            get hasExcluded() {
                return Boolean(this.libs && this.excludedIds().length);
            },

            get cleanResultLabel() {
                if (!this.cleanResult) return '';
                return 'Removed ' + this.cleanResult.deleted + ' synced items; detached ' + this.cleanResult.detached + ' existing Shelf items.';
            },

            toggleLib(id) {
                var lib = this.libs.find(function (candidate) { return candidate.id === id; });
                if (lib) lib.included = !lib.included;
            },

            setMediaType(id, event) {
                var lib = this.libs.find(function (candidate) { return candidate.id === id; });
                if (!lib || !event || !event.target) return;
                var value = event.target.value;
                if (value === 'digital_comic' || value === 'digital_manga') {
                    lib.media_type = value;
                }
            },

            loadLibs() {
                this.libsLoading = true;
                this.libsError = false;
                fetch('/api/sync/komga/libraries')
                    .then(function (response) { return response.json(); })
                    .then((data) => {
                        if (data.ok) this.libs = data.libraries;
                        else this.libsError = data.message;
                        this.libsLoading = false;
                    })
                    .catch(() => {
                        this.libsError = 'Failed to load libraries';
                        this.libsLoading = false;
                    });
            },

            saveLibs() {
                this.libsSaving = true;
                fetch('/api/sync/komga/libraries', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': window.csrfToken()
                    },
                    body: JSON.stringify({
                        excluded: this.excludedIds(),
                        media_types: this.mediaTypes()
                    })
                })
                    .then(function (response) { return response.json(); })
                    .then((data) => {
                        this.libsSaving = false;
                        if (data.ok) {
                            var suffix = data.reclassified ? ' · reclassified ' + data.reclassified + ' existing item' + (data.reclassified === 1 ? '' : 's') : '';
                            showToast('Komga library settings saved' + suffix);
                        } else {
                            showToast(data.message || 'Save failed', 'error');
                        }
                    })
                    .catch(() => {
                        this.libsSaving = false;
                        showToast('Save failed', 'error');
                    });
            },

            cleanup() {
                if (!confirm('Remove Komga-synced Shelf items from unchecked libraries? Komga itself is not touched. Existing Shelf comics or Manga linked to Komga are kept and only detached.')) return;
                this.cleaning = true;
                this.cleanResult = false;
                fetch('/api/sync/komga/libraries', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': window.csrfToken()
                    },
                    body: JSON.stringify({
                        excluded: this.excludedIds(),
                        media_types: this.mediaTypes()
                    })
                })
                    .then(() => fetch('/api/sync/komga/libraries/cleanup', {
                        method: 'POST',
                        headers: {'X-CSRF-Token': window.csrfToken()}
                    }))
                    .then(function (response) { return response.json(); })
                    .then((data) => {
                        this.cleaning = false;
                        this.cleanResult = data;
                        if (data.ok) showToast('Komga cleanup complete');
                    })
                    .catch(() => {
                        this.cleaning = false;
                        showToast('Cleanup failed', 'error');
                    });
            }
        };
    });
});
