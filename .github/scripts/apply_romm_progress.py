from pathlib import Path

TEMPLATE = Path("app/templates/fragments/settings/romm.html")
TEST = Path("tests/test_romm_progress_ui.py")

text = TEMPLATE.read_text()
old = '''        <div x-show="syncing && syncTotal > 0" class="mt-3">
            <div class="flex justify-between text-xs text-shelf-muted mb-1"><span x-text="syncProgress"></span><span x-text="syncPct"></span></div>
            <div class="w-full bg-shelf-bg rounded-full h-2"><div class="bg-shelf-accent rounded-full h-2 transition-all duration-200" :style="syncWidth"></div></div>
            <p x-show="syncLastTitle" class="text-xs text-shelf-muted mt-1 truncate" x-text="syncLastTitle"></p>
        </div>
'''
new = '''        <div x-show="syncing" class="mt-3" data-testid="romm-sync-progress">
            <div class="flex justify-between text-xs text-shelf-muted mb-1">
                <span x-show="syncTotal > 0" x-text="syncProgress"></span>
                <span x-show="syncTotal === 0" x-cloak>Discovering RomM library&hellip;</span>
                <span x-show="syncTotal > 0" x-text="syncPct"></span>
            </div>
            <div class="w-full bg-shelf-bg rounded-full h-2 overflow-hidden">
                <div x-show="syncTotal > 0" class="bg-shelf-accent rounded-full h-2 transition-all duration-200" :style="syncWidth"></div>
                <div x-show="syncTotal === 0" x-cloak class="bg-shelf-accent rounded-full h-2 w-1/3 animate-pulse" data-testid="romm-sync-indeterminate"></div>
            </div>
            <p x-show="syncLastTitle" class="text-xs text-shelf-muted mt-1 truncate" x-text="syncLastTitle"></p>
        </div>
'''
if old not in text:
    raise SystemExit("RomM progress block did not match expected source")
TEMPLATE.write_text(text.replace(old, new, 1))

TEST.write_text('''"""RomM settings progress UI regressions."""\n\nfrom pathlib import Path\n\n\ndef test_romm_progress_is_visible_before_total_is_known():\n    template = Path("app/templates/fragments/settings/romm.html").read_text()\n    assert 'x-show="syncing" class="mt-3" data-testid="romm-sync-progress"' in template\n    assert "Discovering RomM library&hellip;" in template\n    assert 'data-testid="romm-sync-indeterminate"' in template\n    assert 'x-show="syncTotal > 0" x-text="syncProgress"' in template\n    assert 'x-show="syncTotal > 0" x-text="syncPct"' in template\n''')
