"""Harness unit checks only: these tiny cases are NOT scale/timing evidence."""

import json
import sqlite3
from time import perf_counter

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


def test_cli_cannot_point_artifacts_at_a_live_or_external_directory(tmp_path):
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
