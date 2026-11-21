"""`scripts/ci_context.py` holds the one predicate every artifact check
consults: `staleness_is_enforceable()`. `scripts/stamp_test_badges.py` used to
carry its own copy (and `tests/test_badge_stamp.py` a second one) — this file
tests the single home directly so a future call site can trust it without
re-deriving the same three cases. D7 changes upstream7e0369e7's PR exception:
pull requests, pushes to main and local runs all enforce committed artifacts.

**Every case here monkeypatches `GITHUB_EVENT_NAME` explicitly.** `make test`
itself runs under `GITHUB_EVENT_NAME=pull_request` on a PR build, so a test
that trusts the runner's ambient environment would read as passing there for
the wrong reason. `tests/test_badge_stamp.py:97-101` states the same rule.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "ci_context.py"
_spec = importlib.util.spec_from_file_location("ci_context", _SCRIPT)
ci_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_context)


class TestStalenessIsEnforceable:
    def test_pull_request_is_not_enforceable(self, monkeypatch):
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        # Original upstream case name retained for origin-qualified mapping.
        assert ci_context.staleness_is_enforceable() is True

    def test_push_is_enforceable(self, monkeypatch):
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        assert ci_context.staleness_is_enforceable() is True

    def test_unset_is_enforceable(self, monkeypatch):
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        assert ci_context.staleness_is_enforceable() is True

    def test_read_at_call_time_not_import_time(self, monkeypatch):
        """Changing events after import must not disarm the D7 tripwire."""
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        assert ci_context.staleness_is_enforceable() is True
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        assert ci_context.staleness_is_enforceable() is True


class TestMain:
    def test_main_exits_0_when_enforceable(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        assert ci_context.main() == 0
        assert "enforceable" in capsys.readouterr().out

    def test_main_exits_1_when_not_enforceable(self, monkeypatch, capsys):
        """Historical upstream case: PRs now have the exact enforcing exit code."""
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        assert ci_context.main() == 0
        output = capsys.readouterr().out
        assert "enforceable" in output
        assert "advisory" not in output
