"""Approved CI transition: collection errors cannot masquerade as badge counts."""

from types import SimpleNamespace

import pytest

from tests.test_badge_stamp import stamp_test_badges as upstream_badges
from tests.test_badge_stamp_fork import stamp_test_badges as main_badges


@pytest.fixture(params=[upstream_badges, main_badges], ids=["upstream7e0369e7", "main43e244f5"])
def badge_script(request):
    """Exercise each retained lineage's real script module, not a fake counter."""
    return request.param


@pytest.mark.parametrize("event", ["pull_request", "push", None])
@pytest.mark.parametrize("returncode", [1, 2, 3, 4, 5, 10, -9, -15])
def test_nonzero_collection_is_refused_even_with_a_count_line(monkeypatch, badge_script, event, returncode):
    if event is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setattr(badge_script.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=returncode,
                                        stdout="12 tests collected in 0.01s\n",
                                        stderr="synthetic collection failure"))
    with pytest.raises(badge_script.BadgeParseError, match=rf"exit {returncode}"):
        badge_script.collect_count(["tests/"])


@pytest.mark.parametrize("event", ["pull_request", "push", None])
@pytest.mark.parametrize("stdout, expected", [
    ("1 test collected in 0.01s\n", 1),
    ("12 tests collected in 0.01s\n", 12),
])
def test_successful_collection_parses_counts_without_running_tests(monkeypatch, badge_script, event, stdout, expected):
    if event is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(badge_script.subprocess, "run", run)
    assert badge_script.collect_count(["tests/", "--ignore=tests/e2e"]) == expected
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:3] == ["-m", "pytest"]
    assert "--co" in command
    assert "--ignore=tests/e2e" in command
    assert kwargs["capture_output"] is True and kwargs["text"] is True


@pytest.mark.parametrize("event", ["pull_request", "push", None])
def test_success_exit_without_a_parseable_count_is_not_a_successful_badge(monkeypatch, badge_script, event):
    if event is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setattr(badge_script.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="not a count\n", stderr=""))
    with pytest.raises(badge_script.BadgeParseError, match="no 'N tests collected' line"):
        badge_script.collect_count(["tests/"])
