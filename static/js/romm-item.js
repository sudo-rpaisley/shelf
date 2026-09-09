(function () {
    async function addAction() {
        const match = window.location.pathname.match(/^\/item\/(\d+)$/);
        if (!match) return;
        let data;
        try {
            const response = await fetch('/api/romm/items/' + match[1] + '/action');
            data = await response.json();
        } catch (_) {
            return;
        }
        if (!data || !data.ok || !data.url) return;

        // The refreshed item page provides an explicit service-action mount.
        // Keep the old structural lookup as a compatibility fallback for pages
        // rendered from an older template during rolling upgrades.
        const mount = document.querySelector('[data-testid="item-existing-service-actions"]');
        const card = document.querySelector('.bg-shelf-card.rounded-xl.border.border-shelf-border');
        const metadata = card && card.querySelector('.flex-1.min-w-0');
        const target = mount || metadata;
        if (!target || target.querySelector('[data-romm-action]')) return;

        const wrapper = document.createElement('div');
        wrapper.dataset.rommAction = '1';
        const link = document.createElement('a');
        link.href = data.url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'Open in RomM';
        link.className = 'inline-flex items-center px-3 py-1.5 bg-shelf-accent/20 text-shelf-accent2 rounded-lg text-sm hover:bg-shelf-accent/30 transition-colors';
        wrapper.appendChild(link);

        if (mount) {
            mount.appendChild(wrapper);
        } else {
            const grid = metadata.querySelector('.grid.grid-cols-2');
            if (grid) metadata.insertBefore(wrapper, grid);
            else metadata.appendChild(wrapper);
        }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addAction); else addAction();
})();
