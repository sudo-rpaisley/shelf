"""RomM settings progress UI regressions.

The composed panel must be visible from job start, before discovery knows a total.
"""

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _render_romm_panel():
    env = Environment(loader=FileSystemLoader(Path("app/templates")), autoescape=True,
                      undefined=StrictUndefined)
    env.globals.update(_=lambda value, **kwargs: value % kwargs if kwargs else value,
                       feature_on=lambda key: True)
    return env.get_template("fragments/settings/romm.html").render(
        settings={}, romm_url_present=False, secrets_present={"romm_api_token": False},
        secrets_saved={"romm_api_token": False}, romm_sync_job={"state": "running", "current": 0, "total": 0},
        request=SimpleNamespace(query_params={}),
    )


def test_romm_progress_is_visible_before_total_is_known():
    template = _render_romm_panel()
    assert 'data-sync-state="running" data-sync-current="0" data-sync-total="0"' in template
    assert 'x-show="syncing" x-cloak class="mt-3" data-testid="romm-sync-progress"' in template
    assert 'x-show="syncTotal === 0">Discovering library…</span>' in template
    assert 'data-testid="romm-sync-indeterminate"' in template
    assert 'x-show="syncTotal > 0" x-text="syncProgress"' in template
    assert 'x-show="syncTotal > 0" x-text="syncPct"' in template


def test_romm_result_surfaces_incomplete_platforms():
    template = _render_romm_panel()
    assert "Incomplete RomM platforms:" in template
    assert 'x-text="result.incomplete_platforms"' in template
    assert 'x-text="result.errors"' in template
    assert 'x-show="result.incomplete_platforms"' in template
    assert "Re-run sync to retry the missing pages." in template
