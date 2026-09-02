"""RomM settings progress UI regressions.

The progress panel must be visible from job start, before RomM discovery knows the final total.
"""

from pathlib import Path


def test_romm_progress_is_visible_before_total_is_known():
    template = Path("app/templates/fragments/settings/romm.html").read_text()
    assert 'x-show="syncing" class="mt-3" data-testid="romm-sync-progress"' in template
    assert "Discovering RomM library&hellip;" in template
    assert 'data-testid="romm-sync-indeterminate"' in template
    assert 'x-show="syncTotal > 0" x-text="syncProgress"' in template
    assert 'x-show="syncTotal > 0" x-text="syncPct"' in template
