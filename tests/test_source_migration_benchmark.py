"""Harness unit checks only: these tiny cases are NOT scale/timing evidence."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from time import perf_counter
import tracemalloc
from types import SimpleNamespace

import pytest

from scripts import benchmark_source_migration_full as bench


@pytest.mark.parametrize("number", [1, 9999, 10000, 31400])
def test_full_metadata_builder_uses_valid_source_payloads_not_identifier_padding(number):
    from app.services import sources
    from app.services.source_connectors.base import SourceRecord, safe_url

    metadata = bench.metadata(number)
    record = sources._record(SourceRecord(f"old:{number:06d}", "Remote title", "ebook", metadata=metadata,
                                         open_url=f"https://example.invalid/read/{number:06d}"), [])
    encoded = json.dumps(record.metadata, ensure_ascii=False).encode()
    assert len(encoded) == 8192 and json.loads(encoded) == metadata
    assert sources._provider_ids(metadata["provider_ids"]) == metadata["provider_ids"]
    assert all(word in metadata["description"] for word in ("رحلات", "阅读", "Истории", "पुस्तकालय", "https://example.invalid"))
    assert safe_url(record.open_url) == record.open_url


def test_benchmark_review_is_exactly_maximum_pairs_with_final_page_records():
    pairs = bench.mappings()
    assert len(pairs) == len({pair["old_record_id"] for pair in pairs}) == len({pair["new_record_id"] for pair in pairs}) == 100
    assert pairs[0] == {"old_record_id": 1, "new_record_id": 31401}
    assert pairs[-1] == {"old_record_id": 31400, "new_record_id": 62800}
    assert all(pair["new_record_id"] - pair["old_record_id"] == 31400 for pair in pairs)


def test_predeclared_protocol_keeps_requested_workload_and_budgets():
    assert (bench.COUNT, bench.PAIRS, bench.METADATA_BYTES, bench.SAMPLES) == (31400, 100, 8192, 5)
    assert bench.PROTOCOL["budgets"] == {"python_peak_delta_bytes": 32 * 1024**2,
                                         "warm_page_p95_seconds": 3.0, "writer_lock_p95_seconds": 5.0}
    assert bench.PROTOCOL["cold_connection_cycles"] == bench.PROTOCOL["warm_connection_cycles"] == 5
    assert "NOT cold OS cache" in bench.PROTOCOL["cold_definition"]


def test_nearest_rank_tail_is_reported_honestly_for_five_samples():
    assert bench.percentile95([2, 1, 5, 3, 4]) == 5
    assert bench.percentile95(range(1, 101)) == 95
    with pytest.raises(ValueError):
        bench.percentile95([])


def test_rival_proves_contention_after_primary_acquisition_then_really_writes(db):
    from app import config

    timeout_ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
    rival = bench.Contender(config.DATABASE_PATH, timeout_ms)
    rival.start()
    db.execute("BEGIN IMMEDIATE")
    acquired_at = perf_counter()
    try:
        rival.proceed_under_lock()
        db.execute("INSERT INTO settings (key, value) VALUES ('benchmark.primary', 'held')")
        db.commit()
        committed_at = perf_counter()
        evidence = rival.finish(acquired_at, committed_at)
    finally:
        db.rollback()
        rival.released.set()
    assert evidence["probe_code"] == sqlite3.SQLITE_BUSY
    assert acquired_at <= evidence["probe_at"] <= evidence["begin_trace_at"] < committed_at
    assert evidence["acquired_at"] >= evidence["begin_trace_at"]
    assert evidence["outcome"] == "committed" and evidence["within_timeout"]
    assert evidence["rows_changed"] == 1
    assert db.execute("SELECT value FROM settings WHERE key = 'benchmark.writer'").fetchone()[0] == "1"


def test_scan_accounting_refuses_even_one_missing_row(db, tmp_path):
    run = bench.Run(tmp_path)

    def truncated(conn):
        run.scans.append({"source_id": 1, "rows": bench.COUNT - 1, "complete": True})
        return {}

    try:
        with pytest.raises(AssertionError):
            run.measure(db, "unit-only-not-evidence", 0, "unit", truncated)
    finally:
        run.log.close()


def test_cli_cannot_point_artifacts_at_a_live_or_external_directory(tmp_path, monkeypatch):
    # Remain an external-path test even with pytest --basetemp inside .temp/.
    monkeypatch.setattr(bench, "ROOT", tmp_path / "worktree")
    with pytest.raises(SystemExit) as refused:
        bench.main(["--write-plan", "--artifact-root", str(tmp_path)])
    assert refused.value.code == 2


def test_full_applied_receipt_html_is_subject_to_the_unchanged_three_second_gate(tmp_path):
    run = bench.Run(tmp_path)
    run.samples = [{"mode": "warm", "phase": "receipt_applied", "writer": False,
                    "seconds": 3.01, "html_bytes": 100} for _ in range(5)]
    try:
        result = run.summarize()
        assert "warm/receipt_applied:page_latency" in result["failed_budgets"]
        assert result["budgets"]["warm_page_p95_seconds"] == 3.0
    finally:
        run.log.close()


@pytest.fixture
def candidate_repo(tmp_path, monkeypatch):
    """Real local commit objects; never patch BASE or execute the scale seed."""
    source = bench.ROOT
    repo = tmp_path / "candidate"
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(tmp_path),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                       cwd=repo, env=env, stderr=subprocess.PIPE).decode().strip()

    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(source), str(repo)],
                   env=env, check=True, capture_output=True)
    git("checkout", "--quiet", "--detach", bench.BASE)
    git("config", "user.name", "Benchmark Unit Test")
    git("config", "user.email", "benchmark-unit@example.invalid")
    git("config", "commit.gpgsign", "false")
    script = repo / "scripts/benchmark_source_migration_full.py"
    shutil.copyfile(source / "scripts/benchmark_source_migration_full.py", script)
    shutil.copyfile(source / "app/services/source_migration.py", repo / "app/services/source_migration.py")
    git("add", "scripts/benchmark_source_migration_full.py", "app/services/source_migration.py")
    git("commit", "--quiet", "-m", "Unit-only isolated source candidate")
    isolated = git("rev-parse", "HEAD")
    for name in ("app/database.py", "app/auth.py", "app/templates/source_migration_review.html"):
        path = repo / name
        suffix = b"\n<!-- unit-only integrated input -->\n" if name.endswith(".html") else b"\n# unit-only integrated input\n"
        path.write_bytes(path.read_bytes() + suffix)
    git("add", "app")
    git("commit", "--quiet", "-m", "Unit-only combined schema auth and template candidate")
    integrated = git("rev-parse", "HEAD")
    root = repo / ".temp/unit-only-not-benchmark-evidence"
    monkeypatch.setattr(bench, "ROOT", repo)
    monkeypatch.setattr(bench, "__file__", str(script))
    runtime_calls = []

    def forbidden_runtime(self):
        runtime_calls.append(True)
        raise AssertionError("CLI unit tests must not import runtime or seed the full fixture")

    monkeypatch.setattr(bench.Run, "setup_runtime", forbidden_runtime)
    yield SimpleNamespace(path=repo, root=root, script=script, git=git, env=env,
                          isolated=isolated, integrated=integrated)
    assert not runtime_calls


def _args(repo, action="--write-plan", flag="--integrated-revision", revision=None):
    return [action, "--artifact-root", str(repo.root), flag, revision or repo.integrated]


def _refused(args, capsys, message):
    with pytest.raises(SystemExit) as refused:
        bench.main(args)
    assert refused.value.code == 2
    assert message in capsys.readouterr().err


def test_original_isolated_protocol_default_and_service_only_whitelist_are_unchanged(candidate_repo, capsys):
    repo = candidate_repo
    assert bench.main(["--write-plan", "--artifact-root", str(repo.root)]) == 0
    plan = repo.root / "protocol.json"
    assert json.loads(plan.read_text()) == bench.PROTOCOL | {"source_revision": bench.BASE}
    plan.unlink()
    assert bench.main(_args(repo, flag="--source-revision", revision=repo.isolated)) == 0
    assert json.loads(plan.read_text()) == bench.PROTOCOL | {"source_revision": repo.isolated}
    _refused(_args(repo, flag="--source-revision"), capsys, "outside the assigned source-migration service")


def test_integrated_protocol_predeclares_all_committed_app_inputs_not_source_only_isolation(candidate_repo):
    repo = candidate_repo
    assert bench.main(_args(repo)) == 0
    plan = repo.root / "protocol.json"
    before = plan.read_bytes()
    protocol = json.loads(before)
    assert protocol["candidate_mode"] == "integrated-candidate"
    assert protocol["integrated_revision"] == repo.integrated
    assert protocol["base"] == bench.BASE == "a40813d168de4acf8d52c2d71c5d51eed0a41db7"
    assert "source_revision" not in protocol
    assert "NOT isolated" in protocol["evidence_scope"]
    assert protocol["integrated_fixture_invariants"] == {"initial_provenance_rows": 565200, "sqlite_busy_timeout_ms": 5000}
    assert protocol["harness_sha256"] == hashlib.sha256(repo.script.read_bytes()).hexdigest()
    assert protocol["application_tree"] == repo.git("rev-parse", repo.integrated + ":app")
    entries = repo.git("ls-tree", "-r", repo.integrated, "--", "app").splitlines()
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in entries}
    assert protocol["application_file_modes"] == modes
    assert set(protocol["application_sha256"]) == set(modes)
    assert {"app/database.py", "app/auth.py", "app/templates/source_migration_review.html"} <= modes.keys()
    for name, digest in protocol["application_sha256"].items():
        committed = subprocess.check_output(["git", "show", repo.integrated + ":" + name], cwd=repo.path, env=repo.env)
        assert digest == hashlib.sha256(committed).hexdigest() == hashlib.sha256((repo.path / name).read_bytes()).hexdigest()
    assert all(protocol[key] == value for key, value in bench.PROTOCOL.items())
    assert bench.main(_args(repo)) == 0
    assert plan.read_bytes() == before


@pytest.mark.parametrize("flag", ["--source-revision", "--integrated-revision"])
@pytest.mark.parametrize("revision", ["no-such-candidate", "HEAD:app"])
def test_cli_refuses_missing_or_noncommit_revision(candidate_repo, capsys, flag, revision):
    _refused(_args(candidate_repo, flag=flag, revision=revision), capsys, "existing committed revision")
    assert not (candidate_repo.root / "protocol.json").exists()


def test_cli_revision_modes_are_mutually_exclusive(candidate_repo, capsys):
    repo = candidate_repo
    _refused(_args(repo) + ["--source-revision", repo.isolated], capsys, "not allowed with argument")
    assert not (repo.root / "protocol.json").exists()


def test_integrated_mode_refuses_a_different_committed_checkout(candidate_repo, capsys):
    repo = candidate_repo
    _refused(_args(repo, revision=repo.isolated), capsys, "HEAD differs from the frozen committed revision")
    assert not (repo.root / "protocol.json").exists()


def test_integrated_mode_pins_commit_identity_even_when_a_new_head_has_identical_app_bytes(candidate_repo, capsys):
    repo = candidate_repo
    assert bench.main(_args(repo)) == 0
    before = (repo.root / "protocol.json").read_bytes()
    repo.git("commit", "--quiet", "--allow-empty", "-m", "Unit-only moved HEAD, unchanged app")
    assert repo.git("diff", "--name-only", repo.integrated, "HEAD", "--", "app") == ""
    _refused(_args(repo, action="--run"), capsys, "HEAD differs from the frozen committed revision")
    assert (repo.root / "protocol.json").read_bytes() == before


@pytest.mark.parametrize("action", ["--write-plan", "--run"])
@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked", "ignored"])
def test_integrated_cli_refuses_dirty_or_undeclared_app_without_reading_it(candidate_repo, capsys, action, dirty):
    repo = candidate_repo
    if action == "--run":
        assert bench.main(_args(repo)) == 0
    plan = repo.root / "protocol.json"
    before = plan.read_bytes() if plan.exists() else None
    if dirty in {"tracked", "staged"}:
        path = repo.path / "app/templates/source_migration_review.html"
        path.write_bytes(path.read_bytes() + b"\n<!-- changed -->\n")
        if dirty == "staged":
            repo.git("add", "app/templates/source_migration_review.html")
    else:
        # Refusal uses filenames/status, never the contents of undeclared files.
        (repo.path / "app" / (".env" if dirty == "ignored" else "undeclared.py")).touch()
    _refused(_args(repo, action=action), capsys,
             "undeclared ignored application files" if dirty == "ignored" else "dirty or has untracked files")
    assert (plan.read_bytes() if plan.exists() else None) == before


@pytest.mark.parametrize("action", ["--write-plan", "--run"])
@pytest.mark.parametrize("change", ["protocol", "harness", "switch-to-source", "switch-to-integrated"])
def test_predeclared_integrated_protocol_cannot_be_replaced_or_reinterpreted(candidate_repo, capsys, action, change):
    repo = candidate_repo
    initial = _args(repo, flag="--source-revision", revision=repo.isolated) if change == "switch-to-integrated" else _args(repo)
    assert bench.main(initial) == 0
    plan = repo.root / "protocol.json"
    if change == "protocol":
        value = json.loads(plan.read_text())
        value["budgets"]["writer_lock_p95_seconds"] = 999  # Invalid scratch plan, never executed.
        plan.write_text(bench.dump(value) + "\n")
    elif change == "harness":
        repo.script.write_bytes(repo.script.read_bytes() + b"\n# changed harness\n")
    before = plan.read_bytes()
    args = (_args(repo, action=action, flag="--source-revision", revision=repo.isolated)
            if change == "switch-to-source" else _args(repo, action=action))
    _refused(args, capsys, "different predeclared protocol" if action == "--write-plan" else "BEFORE executing measurements")
    assert plan.read_bytes() == before


def test_integrated_execution_requires_a_plan_even_for_an_otherwise_frozen_candidate(candidate_repo, capsys):
    repo = candidate_repo
    _refused(_args(repo, action="--run"), capsys, "BEFORE executing measurements")
    assert list(repo.root.iterdir()) == []


def test_runtime_still_requires_a_fresh_interpreter(candidate_repo, capsys):
    repo = candidate_repo
    assert "app.config" in sys.modules  # The normal small-DB autouse fixture imported it.
    assert bench.main(_args(repo)) == 0
    _refused(_args(repo, action="--run"), capsys, "fresh interpreter")


def test_isolated_runtime_still_refuses_current_app_different_from_the_source_commit(candidate_repo, monkeypatch, capsys):
    repo = candidate_repo
    assert bench.main(_args(repo, flag="--source-revision", revision=repo.isolated)) == 0
    monkeypatch.setattr(bench, "sys", SimpleNamespace(modules={}, version=sys.version))
    _refused(_args(repo, action="--run", flag="--source-revision", revision=repo.isolated), capsys,
             "Production app files differ from the frozen source revision")


@pytest.mark.parametrize("flag", ["--source-revision", "--integrated-revision"])
def test_actual_standalone_cli_writes_the_declared_mode_without_seeding(candidate_repo, flag):
    repo = candidate_repo
    revision = repo.isolated if flag == "--source-revision" else repo.integrated
    result = subprocess.run([sys.executable, str(repo.script), *_args(repo, flag=flag, revision=revision)],
                            cwd=repo.path, env=repo.env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PLAN ") and "SAMPLE" not in result.stdout
    assert sorted(path.name for path in repo.root.iterdir()) == ["protocol.json"]


def test_integrated_hash_check_does_not_trust_git_stat_cache(candidate_repo):
    repo = candidate_repo
    binding = bench.IntegratedBinding(repo.integrated)
    name = "app/templates/source_migration_review.html"
    repo.git("update-index", "--assume-unchanged", name)
    path = repo.path / name
    path.write_bytes(path.read_bytes() + b"\n<!-- hidden from git status -->\n")
    assert repo.git("status", "--porcelain", "--", "app") == ""
    with pytest.raises(RuntimeError, match="Application bytes/mode differ"):
        binding.check()


def test_integrated_binding_allows_only_ignored_python_caches_not_other_unversioned_inputs(candidate_repo):
    repo = candidate_repo
    binding = bench.IntegratedBinding(repo.integrated)
    cache = repo.path / "app/__pycache__"
    cache.mkdir()
    (cache / "fixture.cpython-314.pyc").touch()
    binding.check()
    (cache / "undeclared.py").touch()
    with pytest.raises(RuntimeError, match="undeclared ignored application files"):
        binding.check()


def test_integrated_declaration_refuses_committed_app_symlinks(candidate_repo, capsys):
    repo = candidate_repo
    (repo.path / "app/fixture-link").symlink_to("database.py")
    repo.git("add", "app/fixture-link")
    repo.git("commit", "--quiet", "-m", "Unit-only unsupported symbolic input")
    _refused(_args(repo, revision=repo.git("rev-parse", "HEAD")), capsys, "regular versioned files")
    assert not (repo.root / "protocol.json").exists()


@pytest.mark.parametrize("provenance_rows,timeout_ms", [(565200, 5000), (565199, 5000), (565200, 10000)])
def test_integrated_fixture_invariants_cannot_silently_measure_a_changed_workload(tmp_path, provenance_rows, timeout_ms):
    run = bench.Run(tmp_path, binding=object(), candidate_mode="integrated-candidate")
    run.fixture, run.timeout_ms = {"provenance_rows": provenance_rows}, timeout_ms
    try:
        if (provenance_rows, timeout_ms) == (565200, 5000):
            run.check_integrated_fixture()
        else:
            with pytest.raises(RuntimeError, match="do not relax the workload"):
                run.check_integrated_fixture()
    finally:
        run.log.close()


def test_integrated_manifest_is_bound_before_runtime_and_empty_unit_run_is_incomplete(candidate_repo, monkeypatch, capsys):
    repo = candidate_repo
    assert bench.main(_args(repo)) == 0
    protocol = json.loads((repo.root / "protocol.json").read_text())
    events, alarms = [], []

    def no_runtime(self):
        manifest = json.loads((self.path / "manifest.json").read_text())
        assert (self.path / "protocol.json").read_bytes() == (repo.root / "protocol.json").read_bytes()
        for key in ("candidate_mode", "integrated_revision", "application_tree", "application_sha256",
                    "application_file_modes", "harness_sha256", "integrated_fixture_invariants", "evidence_scope"):
            assert manifest[key] == protocol[key]
        assert manifest["candidate_revision"] == manifest["head"] == repo.integrated
        assert manifest["source_base"] == bench.BASE and "source_revision" not in manifest
        assert manifest["protocol_sha256"] == hashlib.sha256((self.path / "protocol.json").read_bytes()).hexdigest()
        events.append("manifest-before-runtime")

    monkeypatch.setattr(bench, "sys", SimpleNamespace(modules={}, version=sys.version))
    monkeypatch.setattr(bench.shutil, "disk_usage", lambda path: SimpleNamespace(free=4 * 1024**3))
    monkeypatch.setattr(bench.signal, "signal", lambda *args: None)
    monkeypatch.setattr(bench.signal, "alarm", alarms.append)
    monkeypatch.setattr(bench.Run, "setup_runtime", no_runtime)
    monkeypatch.setattr(bench.Run, "execute", lambda self: events.append("no-workload-unit-stub"))
    assert bench.main(_args(repo, action="--run")) == 2  # No measurements, never acceptance.
    assert events == ["manifest-before-runtime", "no-workload-unit-stub"]
    assert alarms == [45 * 60, 0]
    run_dir, = [path for path in repo.root.iterdir() if path.is_dir()]
    summary = json.loads((run_dir / "summary.json").read_text())
    assert not summary["complete"] and not summary["memory_complete"]
    assert summary["candidate_mode"] == "integrated-candidate" and summary["candidate_revision"] == repo.integrated
    assert "SAMPLE" not in capsys.readouterr().out


@pytest.mark.parametrize("mutation", [None, "template", "schema", "harness", "protocol", "head", "untracked"])
def test_phase_boundary_binding_checks_are_outside_measurements_and_fail_closed(candidate_repo, db, monkeypatch, mutation):
    repo = candidate_repo
    assert bench.main(_args(repo)) == 0
    binding = bench.IntegratedBinding(repo.integrated)
    binding.protocol_path = repo.root / "protocol.json"
    binding.protocol_bytes = binding.protocol_path.read_bytes()
    run = bench.Run(repo.root, binding=binding, candidate_mode="integrated-candidate", candidate_revision=repo.integrated)
    check, boundaries = binding.check, []

    def checked():
        assert not tracemalloc.is_tracing(), "Binding allocations must not pollute the memory sample"
        assert not db.in_transaction, "Binding hashes must not run under the measured transaction"
        boundaries.append(True)
        check()

    def tiny_action(conn):
        if mutation in {"template", "schema", "harness", "protocol"}:
            path = {"template": repo.path / "app/templates/source_migration_review.html",
                    "schema": repo.path / "app/database.py", "harness": repo.script,
                    "protocol": binding.protocol_path}[mutation]
            path.write_bytes(path.read_bytes() + b"\n# changed during unit phase\n")
        elif mutation == "head":
            repo.git("checkout", "--quiet", "--detach", repo.isolated)
        elif mutation == "untracked":
            (repo.path / "app/undeclared.py").touch()
        return {"unit_only": True}

    monkeypatch.setattr(binding, "check", checked)
    try:
        if mutation is None:
            assert run.measure(db, "memory", 0, "unit-only-not-scale-evidence", tiny_action) == {"unit_only": True}
            assert len(run.samples) == 1
        else:
            with pytest.raises(RuntimeError):
                run.measure(db, "memory", 0, "unit-only-not-scale-evidence", tiny_action)
            assert run.samples == []
        assert boundaries == [True, True]
        assert not tracemalloc.is_tracing() and not db.in_transaction
        raw = json.loads((run.path / "samples.jsonl").read_text())
        assert raw["candidate_mode"] == "integrated-candidate" and raw["candidate_revision"] == repo.integrated
        assert raw["mode"] == "memory"  # Connection/sample class is not candidate isolation mode.
        assert ("error" in raw) == (mutation is not None)
    finally:
        run.log.close()
