#!/usr/bin/env python3
"""Opt-in, fixed-workload X2 evidence; never run against an existing database.

Run with the project Python environment from the repository root:
  python scripts/benchmark_source_migration_full.py --write-plan
  python scripts/benchmark_source_migration_full.py --run

For combined schema/UI evidence, use a NEW artifact root and the SAME explicit
--integrated-revision COMMIT for --write-plan and --run. That mode binds the
entire committed app/ tree and measures the integrated candidate, NOT isolated
source-service evidence. It does not accept legacy/unknown-observer latency.

Artifacts/databases stay under .temp/. Production code is not patched. Exit 1
means measured budgets failed, 2 means incomplete/invalid evidence, 0 means the
declared local budgets passed (not Docker or host-load-independent acceptance).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing, contextmanager
import cProfile
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tracemalloc
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = "a40813d168de4acf8d52c2d71c5d51eed0a41db7"
COUNT, PAIRS, METADATA_BYTES, SAMPLES = 31400, 100, 8192, 5
BUDGETS = {"python_peak_delta_bytes": 32 * 1024**2, "warm_page_p95_seconds": 3.0,
           "writer_lock_p95_seconds": 5.0}
INTEGRATED_INVARIANTS = {"initial_provenance_rows": 565200, "sqlite_busy_timeout_ms": 5000}
PROTOCOL = {
    "base": BASE, "records_per_source": COUNT, "sources": 2, "metadata_utf8_bytes": METADATA_BYTES,
    "reviewed_pairs": PAIRS, "page_size_per_source": 50, "cold_connection_cycles": SAMPLES,
    "warm_connection_cycles": SAMPLES, "budgets": BUDGETS,
    "contender_budget": "successful real write+commit within observed PRAGMA busy_timeout; no raised timeout",
    "cold_definition": "new SQLite connection/cache, NOT cold OS cache or cold Python process",
    "timing": "untraced; BEGIN call/acquired/action end/COMMIT start/end, DB+context and HTML separately",
    "page_budget_scope": "all cold/warm read-only HTML phases, INCLUDING the full applied receipt",
    "barrier": "preopened rival; primary acquired -> zero-wait SQLITE_BUSY proof -> real BEGIN trace -> primary action",
    "memory": "separate full-size cycle; per-phase tracemalloc peak minus baseline, exclude traced latency from p95",
    "profile": "one additional full-size cProfile cycle without contention, excluded from timing p95",
    "coverage": "transparent inventory row counters; every scan must finish with 31400 rows",
    "page_positions": "first, middle and final 50-record pages, rotated across cycles",
    "manual_provenance": "record_initial -> real manual title/publisher edit -> record_observation; every item",
    "network": "socket audit guard; no server/lifespan/connector calls, no credentials",
    "limits": {"wall_seconds": 45 * 60, "artifact_bytes": 4 * 1024**3},
    "acceptance_limit": "local shared-host evidence only; not Docker or CPU/IO/host-load-independent acceptance",
}
PARAGRAPH = (
    "Catalogue journeys examine reading, memory, translation and public libraries. "
    "رحلات القراءة تحفظ الذاكرة وتفتح أبواب المعرفة والمكتبات. "
    "阅读与旅行连接城市、历史和图书馆。 言葉と物語は世界をつなぎます。 "
    "Истории о книгах, памяти, переводе и городских библиотеках. "
    "पुस्तकालय, यात्राएँ और कहानियाँ भाषा तथा स्मृति को जोड़ते हैं। "
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def resolve_revision(parser, value):
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", value + "^{commit}"],
            cwd=ROOT, stderr=subprocess.PIPE, text=True,
        ).strip()
    except subprocess.CalledProcessError:
        parser.error("Candidate revision must resolve to an existing committed revision")


class IntegratedBinding:
    """Frozen committed inputs, checked OUTSIDE latency/allocation windows.

    All versioned regular app files are bound, including database DDL, auth,
    routers, templates and static assets; unknown/untracked inputs are refused.
    Only ignored Python bytecode caches may be generated during imports. No
    ignored configuration/key file is opened to perform this check.
    """

    def __init__(self, revision):
        self.revision = revision
        self.protocol_path = self.protocol_bytes = None
        self.harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.application_tree = self.git("rev-parse", revision + ":app").decode().strip()
        entries = self.git("ls-tree", "-r", "-z", revision, "--", "app").split(b"\0")
        self.application_file_modes, objects = {}, {}
        for entry in filter(None, entries):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise RuntimeError("Integrated app binding requires regular versioned files, not symlinks/submodules")
            path = os.fsdecode(name)
            self.application_file_modes[path], objects[path] = mode, oid
        if not objects:
            raise RuntimeError("Integrated revision has no versioned app files")
        # One Git batch, not a process per file or a checkout of different code.
        oids = sorted(set(objects.values()))
        blobs = self.git("cat-file", "--batch", input=("\n".join(oids) + "\n").encode())
        digests, offset = {}, 0
        for oid in oids:
            end = blobs.index(b"\n", offset)
            actual, kind, size = blobs[offset:end].decode().split()
            size, offset = int(size), end + 1
            if actual != oid or kind != "blob" or blobs[offset + size:offset + size + 1] != b"\n":
                raise RuntimeError("Invalid committed application blob binding")
            digests[oid] = hashlib.sha256(blobs[offset:offset + size]).hexdigest()
            offset += size + 1
        if offset != len(blobs):
            raise RuntimeError("Incomplete committed application blob binding")
        self.application_sha256 = {path: digests[oid] for path, oid in objects.items()}
        self.check()

    @staticmethod
    def git(*args, **kwargs):
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.PIPE, **kwargs)

    def declaration(self):
        return {"candidate_mode": "integrated-candidate", "integrated_revision": self.revision,
                "application_tree": self.application_tree, "application_sha256": self.application_sha256,
                "application_file_modes": self.application_file_modes, "harness_sha256": self.harness_sha256,
                "integrated_fixture_invariants": INTEGRATED_INVARIANTS,
                "evidence_scope": "combined committed app/schema/templates; NOT isolated source-service or legacy/unknown-observer acceptance"}

    def check(self):
        if self.git("rev-parse", "HEAD").decode().strip() != self.revision:
            raise RuntimeError("Integrated candidate HEAD differs from the frozen committed revision")
        if self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "app"):
            raise RuntimeError("Integrated candidate app is dirty or has untracked files")
        ignored = self.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", "app")
        for name in filter(None, ignored.split(b"\0")):
            path = Path(os.fsdecode(name))
            if "__pycache__" not in path.parts or path.suffix != ".pyc":
                raise RuntimeError("Integrated candidate has undeclared ignored application files")
        for name, expected in self.application_sha256.items():
            path = ROOT / name
            if path.resolve() != path or not path.is_file():
                raise RuntimeError("Integrated application file is missing, nonregular or symlinked: " + name)
            mode = path.stat().st_mode
            if (not stat.S_ISREG(mode) or bool(mode & 0o111) != (self.application_file_modes[name] == "100755")
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise RuntimeError("Application bytes/mode differ from the frozen integrated revision: " + name)
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != self.harness_sha256:
            raise RuntimeError("Benchmark harness changed after integrated protocol declaration")
        if self.protocol_path is not None and self.protocol_path.read_bytes() != self.protocol_bytes:
            raise RuntimeError("Predeclared integrated protocol changed during execution")


def percentile95(values):
    if not values:
        raise ValueError("Cannot report a percentile without samples")
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def metadata(number):
    stem = f"9781234{number:05d}"
    isbn = stem + str((-sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(stem))) % 10)
    value = {
        "provider_ids": {"Asin": f"B{number:09d}", "Isbn13": isbn},
        "subtitle": f"Reading journeys · مكتبة · 図書館 · पुस्तकालय · {number:06d}",
        "publisher": "Open Library Editions · طبعات المكتبة · Издания",
        "published_date": "2026-11-01", "language": "mul", "page_count": 300 + number % 200,
        "series_name": "World libraries · 世界の図書館", "series_position": number,
        "genres": ["Literature", "Travel", "تاريخ", "图书馆", "История", "कहानियाँ"],
        "description": "",
    }
    room = METADATA_BYTES - len(json.dumps(value, ensure_ascii=False).encode())
    text = (f"Volume {number:06d}. Public catalogue https://example.invalid/books/{number:06d} . "
            + PARAGRAPH * 30).encode()[:room].decode("utf-8", errors="ignore")
    # At most three terminal sentence dots complete a partial UTF-8 codepoint.
    value["description"] = text + "." * (room - len(text.encode()))
    assert len(json.dumps(value, ensure_ascii=False).encode()) == METADATA_BYTES
    return value


def mappings():
    return [{"old_record_id": 1 + i * (COUNT - 1) // (PAIRS - 1),
             "new_record_id": COUNT + 1 + i * (COUNT - 1) // (PAIRS - 1)} for i in range(PAIRS)]


class Contender:
    """A real post-acquisition rival, not an uncontended preflight timing."""

    def __init__(self, path, timeout_ms):
        self.path, self.timeout_ms = path, timeout_ms
        self.ready, self.locked, self.attempting, self.released = (threading.Event() for _ in range(4))
        self.evidence = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or "error" in self.evidence:
            raise RuntimeError(f"Rival not ready: {self.evidence}")

    def _run(self):
        try:
            with closing(sqlite3.connect(str(self.path), timeout=self.timeout_ms / 1000)) as conn:
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == self.timeout_ms
                self.ready.set()
                if not self.locked.wait(5):
                    raise RuntimeError("Primary never released its acquired-lock barrier")
                conn.execute("PRAGMA busy_timeout=0")
                self.evidence["probe_at"] = time.perf_counter()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    self.evidence["probe_code"] = error.sqlite_errorcode
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                else:
                    conn.rollback()
                    raise RuntimeError("Zero-wait rival did not contend with a held writer lock")
                conn.execute(f"PRAGMA busy_timeout={self.timeout_ms}")

                def trace(sql):
                    if sql == "BEGIN IMMEDIATE":
                        self.evidence["begin_trace_at"] = time.perf_counter()
                        self.attempting.set()

                conn.set_trace_callback(trace)
                self.evidence["attempt_at"] = time.perf_counter()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    self.evidence["acquired_at"] = time.perf_counter()
                    conn.execute("INSERT INTO settings (key, value) VALUES ('benchmark.writer', '1') "
                                 "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
                    conn.commit()
                    self.evidence["rows_changed"] = conn.total_changes
                    assert self.evidence["rows_changed"] == 1
                    self.evidence["outcome"] = "committed"
                except sqlite3.OperationalError as error:
                    self.evidence.update(outcome="busy", code=error.sqlite_errorcode, message=str(error))
                    conn.rollback()
                self.evidence["finished_at"] = time.perf_counter()
                self.evidence["seconds"] = self.evidence["finished_at"] - self.evidence["attempt_at"]
                conn.set_trace_callback(None)
                if not self.released.wait(120):
                    raise RuntimeError("Primary did not finish within diagnostic rival wait")
                if self.evidence["outcome"] == "busy":
                    # Diagnostic only: never turn a timed-out sample into a pass.
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
                    self.evidence["post_release_probe"] = "acquired_then_rolled_back"
        except Exception as error:
            self.evidence["error"] = repr(error)
        finally:
            self.ready.set()
            self.attempting.set()

    def proceed_under_lock(self):
        self.locked.set()
        if not self.attempting.wait(5) or "begin_trace_at" not in self.evidence:
            raise RuntimeError(f"No real rival BEGIN trace while primary held lock: {self.evidence}")

    def finish(self, acquired_at, committed_at):
        self.released.set()
        self.thread.join(10)
        if self.thread.is_alive() or "error" in self.evidence:
            raise RuntimeError(f"Invalid rival: {self.evidence}")
        assert acquired_at <= self.evidence["probe_at"] <= self.evidence["begin_trace_at"] < committed_at
        self.evidence["configured_timeout_ms"] = self.timeout_ms
        self.evidence["within_timeout"] = (self.evidence["outcome"] == "committed"
                                            and self.evidence["seconds"] <= self.timeout_ms / 1000)
        return self.evidence


class Run:
    def __init__(self, root, *, binding=None, candidate_mode="isolated-source", candidate_revision=None):
        self.root = root
        self.binding = binding
        self.identity = {"candidate_mode": candidate_mode, "candidate_revision": candidate_revision}
        self.path = root / (datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        self.path.mkdir()
        self.log = (self.path / "samples.jsonl").open("x")
        self.samples, self.scans, self.network_attempts = [], [], []
        self.started = time.perf_counter()

    def emit(self, value):
        self.log.write(dump(value | self.identity) + "\n")
        self.log.flush()

    def check_binding(self):
        if self.binding is not None:
            self.binding.check()

    def check_integrated_fixture(self):
        if self.binding is not None and (
                self.fixture["provenance_rows"] != INTEGRATED_INVARIANTS["initial_provenance_rows"]
                or self.timeout_ms != INTEGRATED_INVARIANTS["sqlite_busy_timeout_ms"]):
            raise RuntimeError("Integrated fixture differs from predeclared provenance/SQLite timeout; do not relax the workload")

    def check_size(self):
        size = sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
        if size > PROTOCOL["limits"]["artifact_bytes"]:
            raise RuntimeError(f"Artifact size cap exceeded: {size}")
        return size

    def seed(self):
        self.check_binding()
        from app import database
        from app.services import item_write, metadata_policy, sources
        from app.services.source_connectors.base import SourceRecord

        database.init_db()
        with database.get_db() as conn:
            conn.execute("INSERT INTO users (id, username, password, display_name, role) "
                         "VALUES (1, 'benchmark-admin', 'not-an-authentication-hash', 'Benchmark', 'admin')")
            for side in ("old", "new"):
                conn.execute("INSERT INTO source_instances (provider, name, base_url, last_status) "
                             "VALUES ('opds', ?, 'https://example.invalid/opds', 'ok')", (side,))
            conn.commit()
            for source_id, side in ((1, "old"), (2, "new")):
                for number in range(1, COUNT + 1):
                    if not conn.in_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    remote = metadata(number)
                    record = sources._record(SourceRecord(
                        f"{side}:{number:06d}", f"Remote {number:06d} · كتاب · 本", "ebook",
                        authors="Ada Reader; ليلى كاتبة; 読書 太郎", isbn=remote["provider_ids"]["Isbn13"],
                        library_id="library-main", format="EPUB",
                        open_url=f"https://example.invalid/read/{side}/{number:06d}", metadata=remote,
                    ), [])
                    fields = {"title": record.title, "authors": record.authors, "description": remote["description"],
                              "publisher": remote["publisher"], "media_type": "ebook", "language": "mul",
                              "page_count": remote["page_count"], "publish_year": 2026}
                    item_id = item_write.insert_item(conn, fields | {"owned": 0, "notes": "Manually retained local note"})
                    assert item_id == (source_id - 1) * COUNT + number
                    sources._snapshot(conn, source_id, item_id, record, "2026-11-01 00:00:00")
                    key = sources.metadata_source_key(source_id, record.external_id)
                    metadata_policy.record_initial(conn, item_id, key, fields)
                    item_write.update_item_fields(conn, item_id, {"title": f"Manual {side} {number:06d} · 手動", "publisher": None})
                    metadata_policy.record_observation(conn, item_id, key, fields | {"isbn": record.isbn})
                    if number % 250 == 0 or number == COUNT:
                        conn.commit()
                        self.check_size()
                        self.check_binding()
                print(f"SEEDED source={source_id} rows={COUNT}", flush=True)
            row = conn.execute("SELECT COUNT(*), MIN(length(CAST(metadata_json AS BLOB))), "
                               "MAX(length(CAST(metadata_json AS BLOB))), SUM(length(CAST(metadata_json AS BLOB))), "
                               "SUM(json_valid(metadata_json)) FROM source_records").fetchone()
            assert tuple(row) == (COUNT * 2, METADATA_BYTES, METADATA_BYTES, COUNT * 2 * METADATA_BYTES, COUNT * 2)
            overrides = conn.execute("SELECT COUNT(*) FROM item_metadata_provenance "
                                     "WHERE json_extract(value_json, '$.overridden') = 1").fetchone()[0]
            assert overrides == COUNT * 4
            self.fixture = {"record_counts": [COUNT, COUNT], "metadata_min_max_bytes": list(row[1:3]),
                            "metadata_total_bytes": row[3], "valid_metadata_rows": row[4],
                            "provenance_rows": conn.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0],
                            "manual_override_rows": overrides,
                            "item_count": conn.execute("SELECT COUNT(*) FROM items_live").fetchone()[0],
                            "database_pragmas": {key: conn.execute("PRAGMA " + key).fetchone()[0]
                                                 for key in ("busy_timeout", "journal_mode", "synchronous", "page_size", "cache_size", "wal_autocheckpoint")}}
            self.timeout_ms = self.fixture["database_pragmas"]["busy_timeout"]
            self.check_integrated_fixture()
            self.baseline = self.protected(conn)
        self.emit({"kind": "fixture", **self.fixture, "artifact_bytes": self.check_size()})

    @staticmethod
    def protected(conn):
        digest = hashlib.sha256()
        for row in conn.execute("SELECT id, title, publisher, notes, isbn, owned, cover_path FROM items_live ORDER BY id"):
            digest.update(dump(tuple(row)).encode())
        return digest.hexdigest()

    def setup_runtime(self):
        self.check_binding()
        # In a dedicated CLI process only; never read or print ambient secrets.
        for name in list(os.environ):
            if name not in {"PATH", "LANG", "LC_ALL", "TZ"}:
                del os.environ[name]
        os.environ.update(DATA_DIR=str(self.path / "data"), SECRET_KEY="benchmark-fixture-only-not-a-real-key")
        sys.path.insert(0, str(ROOT))

        def audit(event, args):
            if event in {"socket.connect", "socket.getaddrinfo"}:
                self.network_attempts.append(event)
                raise RuntimeError("Network is forbidden in the local source-migration benchmark")

        sys.addaudithook(audit)
        self.seed()
        from app.main import app, templates
        from app.routers import source_migration as routes
        from app.services import source_migration as migration
        self.app, self.templates, self.routes, self.migration = app, templates, routes, migration
        assert migration.PAGE_SIZE == 50 and migration.MAX_MAPPINGS == PAIRS
        self.actor = {"id": 1, "username": "benchmark-admin", "role": "admin", "token_version": 1,
                      "expires_at": time.time() + PROTOCOL["limits"]["wall_seconds"] + 3600}
        original = migration._iter_inventory

        def counted(conn, source_id):
            scan = {"source_id": source_id, "rows": 0, "complete": False}
            started = time.perf_counter()
            try:
                for row in original(conn, source_id):
                    scan["rows"] += 1
                    yield row
                scan["complete"] = True
            finally:
                scan["seconds"] = time.perf_counter() - started
                self.scans.append(scan)

        migration._iter_inventory = counted
        self.check_binding()

    def request(self, path, query=b""):
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "path": path, "query_string": query,
                           "headers": [(b"cookie", b"csrf_token=benchmark-csrf")], "scheme": "https",
                           "server": ("benchmark.invalid", 443), "client": ("127.0.0.1", 1), "app": self.app})
        request.state.user = self.actor | {"display_name": "Benchmark", "language": None}
        return request

    def render(self, phase, value):
        if phase == "page":
            query = ("old_source_id=1&new_source_id=2&old_after=" + str(value["old_page"]["after"])
                     + "&new_after=" + str(value["new_page"]["after"]))
            request = self.request("/source-migrations", query.encode())
            value.update(repair_source_id=None, repair_record_id=None)
            self.routes._page_links(request, value)
            name = "source_migration_inventory.html"
        else:
            request, name = self.request("/source-migrations/" + value["id"]), "source_migration_review.html"
        assert not getattr(self, "writer_active", False), "HTML must not run under the writer lock"
        return len(self.templates.TemplateResponse(request, name, value).body)

    def measure(self, conn, mode, cycle, phase, action, *, writer=False, html=False):
        self.check_binding()
        memory = mode == "memory"
        if memory:
            gc.collect()
            tracemalloc.start()
            baseline = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
        self.scans = []
        row = {"kind": "sample", "mode": mode, "cycle": cycle, "phase": phase, "writer": writer, **self.identity}
        rival = Contender(self.path / "data" / "shelf.db", self.timeout_ms) if writer and mode in {"cold", "warm"} else None
        if rival:
            rival.start()
        started, cpu_started = time.perf_counter(), time.process_time()
        try:
            if writer:
                row["begin_call_at"] = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                row["acquired_at"] = time.perf_counter()
                self.writer_active = True
                if rival:
                    rival.proceed_under_lock()
                row["action_start_at"] = time.perf_counter()
                self.migration.authorize_mutation(conn, self.actor)
            else:
                conn.execute("BEGIN")
            value = action(conn)
            if writer:
                self.migration.authorize_mutation(conn, self.actor)
            row["action_end_at"] = time.perf_counter()
            row["commit_call_at"] = time.perf_counter()
            conn.commit()
            row["committed_at"] = time.perf_counter()
            self.writer_active = False
            row["db_context_seconds"] = row["committed_at"] - started
            if html:
                assert not conn.in_transaction
                html_started = time.perf_counter()
                row["html_bytes"] = self.render(phase, value)
                row["html_seconds"] = time.perf_counter() - html_started
            row["seconds"] = time.perf_counter() - started
            row["cpu_seconds"] = time.process_time() - cpu_started
            if writer:
                row["acquisition_wait_seconds"] = row["acquired_at"] - row["begin_call_at"]
                row["writer_lock_seconds"] = row["committed_at"] - row["acquired_at"]
                row["commit_seconds"] = row["committed_at"] - row["commit_call_at"]
                row["barrier_seconds"] = row["action_start_at"] - row["acquired_at"]
                if rival:
                    row["contender"] = rival.finish(row["acquired_at"], row["committed_at"])
            row["scans"] = self.scans
            assert all(s["complete"] and s["rows"] == COUNT for s in self.scans), self.scans
            if phase == "page":
                assert len(self.scans) == 2
                assert [len(value[side + "_records"]) for side in ("old", "new")] == [50, 50]
                assert [value[side + "_page"]["total"] for side in ("old", "new")] == [COUNT, COUNT]
            if phase in {"preview", "receipt_applied", "receipt_retired"}:
                coverage = value.get("coverage", value["plan"].get("coverage"))
                assert coverage == {"old_records": COUNT, "new_records": COUNT, "selected": PAIRS,
                                    "unmapped_old": COUNT - PAIRS, "unmapped_new": COUNT - PAIRS}
                assert len(value["plan"]["mappings"]) == PAIRS
            if memory:
                current, peak = tracemalloc.get_traced_memory()
                row.update(python_baseline_bytes=baseline, python_current_bytes=current,
                           python_peak_bytes=peak, python_peak_delta_bytes=peak - baseline)
                tracemalloc.stop()
            self.check_binding()  # Timing/peak already captured; no hashing under a writer lock.
            row["rss_high_water_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            self.samples.append(row)
            self.emit(row)
            print(f"SAMPLE {mode} {cycle} {phase}: total={row['seconds']:.3f}s "
                  f"lock={row.get('writer_lock_seconds', 0):.3f}s rival={row.get('contender', {}).get('outcome', '-')} "
                  f"scanned={sum(s['rows'] for s in self.scans)}", flush=True)
            return value
        except BaseException as error:
            conn.rollback()
            self.writer_active = False
            row.update(error=repr(error), scans=self.scans)
            self.emit(row)
            raise
        finally:
            if rival:
                rival.released.set()
            if memory:
                tracemalloc.stop()

    def cycle(self, mode, number, warm=None):
        from app.database import get_db

        @contextmanager
        def connection():
            if warm is not None:
                yield warm
            else:
                with get_db() as conn:
                    yield conn

        def perform(phase, action, **kwargs):
            with connection() as conn:
                return self.measure(conn, mode, number, phase, action, **kwargs)

        page_after = (0, COUNT // 2, COUNT - 50)[(number - 1) % 3]
        perform("page", lambda conn: self.migration.workspace(conn, 1, 2, old_after=page_after,
                                                              new_after=COUNT + page_after), html=True)
        preview = perform("preview", lambda conn: self.migration.preview_replacement(
            conn, actor_id=1, old_source_id=1, new_source_id=2, mappings=mappings(), manual_reviewed=True), writer=True)
        applied = perform("apply", lambda conn: self.migration.apply(conn, preview["id"], actor_id=1,
                          fingerprint=preview["fingerprint"], reviewed=True), writer=True)
        assert applied["ok"] and not applied["replayed"]
        with connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_records WHERE item_id != id").fetchone()[0] == PAIRS
        receipt = perform("receipt_applied", lambda conn: self.migration.detail(conn, preview["id"], actor_id=1), html=True)
        retired = perform("retire", lambda conn: self.migration.retire(conn, preview["id"], actor_id=1,
                          fingerprint=receipt["retire_fingerprint"], reviewed=True), writer=True)
        assert retired["ok"] and not retired["replayed"]
        with connection() as conn:
            assert conn.execute("SELECT enabled FROM source_instances WHERE id = 1").fetchone()[0] == 0
        receipt = perform("receipt_retired", lambda conn: self.migration.detail(conn, preview["id"], actor_id=1), html=True)
        rolled = perform("rollback", lambda conn: self.migration.rollback(conn, preview["id"], actor_id=1,
                         fingerprint=receipt["rollback_fingerprint"], reviewed=True), writer=True)
        assert rolled["ok"] and not rolled["replayed"]
        with connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_records WHERE item_id != id").fetchone()[0] == 0
            assert conn.execute("SELECT enabled FROM source_instances WHERE id = 1").fetchone()[0] == 1
            assert self.protected(conn) == self.baseline
        self.check_size()

    def summarize(self):
        groups = defaultdict(list)
        for row in self.samples:
            if row["mode"] in {"cold", "warm"}:
                groups[row["mode"] + "/" + row["phase"]].append(row)
        summary = {**self.identity, "complete": all(len(rows) == SAMPLES for rows in groups.values()) and len(groups) == 14,
                   "groups": {}, "budgets": BUDGETS, "network_attempts": self.network_attempts}
        failures = []
        for name, rows in sorted(groups.items()):
            result = {"n": len(rows), "p95_seconds": percentile95([r["seconds"] for r in rows]),
                      "raw_seconds": [r["seconds"] for r in rows]}
            if rows[0]["writer"]:
                result["lock_p95_seconds"] = percentile95([r["writer_lock_seconds"] for r in rows])
                result["raw_lock_seconds"] = [r["writer_lock_seconds"] for r in rows]
                result["contender_failures"] = sum(not r["contender"]["within_timeout"] for r in rows)
                if result["lock_p95_seconds"] > BUDGETS["writer_lock_p95_seconds"]:
                    failures.append(name + ":writer_lock")
                if result["contender_failures"]:
                    failures.append(name + ":contender_timeout")
            elif rows[0].get("html_bytes") is not None and result["p95_seconds"] > BUDGETS["warm_page_p95_seconds"]:
                failures.append(name + ":page_latency")
            summary["groups"][name] = result
        memory = [r for r in self.samples if r["mode"] == "memory"]
        summary["memory_complete"] = len(memory) == 7
        summary["max_python_peak_delta_bytes"] = max((r["python_peak_delta_bytes"] for r in memory), default=None)
        if memory and summary["max_python_peak_delta_bytes"] > BUDGETS["python_peak_delta_bytes"]:
            failures.append("python_peak")
        summary.update(failed_budgets=failures, elapsed_seconds=time.perf_counter() - self.started,
                       artifact_bytes=sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file()),
                       loadavg_end=os.getloadavg())
        (self.path / "summary.json").write_text(dump(summary) + "\n")
        return summary

    def execute(self):
        self.check_binding()
        from app.database import get_db

        for i in range(1, SAMPLES + 1):
            self.cycle("cold", i)
        with get_db() as conn:
            # Full untruncated warmup, explicitly excluded from p95.
            self.measure(conn, "warmup", 0, "page", lambda c: self.migration.workspace(c, 1, 2), html=True)
            for i in range(1, SAMPLES + 1):
                self.cycle("warm", i, conn)
            self.cycle("memory", 1, conn)
            profiler = cProfile.Profile()
            profiler.enable()
            try:
                self.cycle("profile", 1, conn)
            finally:
                profiler.disable()
                profiler.dump_stats(str(self.path / "profile.pstats"))
        self.check_binding()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-plan", action="store_true")
    group.add_argument("--run", action="store_true")
    parser.add_argument("--artifact-root", type=Path, default=ROOT / ".temp/source-migration-x2-full")
    revisions = parser.add_mutually_exclusive_group()
    revisions.add_argument("--source-revision", default=BASE,
                           help="Isolated source-only commit; only source_migration.py may differ from the baseline app")
    revisions.add_argument("--integrated-revision",
                           help="Explicit frozen HEAD commit for combined app/schema/templates, NOT source-only isolation")
    args = parser.parse_args(argv)
    root = args.artifact_root.resolve()
    if not root.is_relative_to(ROOT / ".temp"):
        parser.error("Artifacts must be inside this worktree's ignored .temp directory")
    root.mkdir(parents=True, exist_ok=True)
    revision = resolve_revision(parser, args.integrated_revision if args.integrated_revision is not None else args.source_revision)
    binding = None
    if args.integrated_revision is not None:
        try:
            binding = IntegratedBinding(revision)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
            parser.error("Cannot declare frozen integrated candidate: " + str(error))
    else:
        changed = subprocess.check_output(["git", "diff", "--name-only", BASE, revision, "--", "app"], cwd=ROOT, text=True).splitlines()
        if set(changed) - {"app/services/source_migration.py"}:
            parser.error("Candidate changes production files outside the assigned source-migration service")
    plan = root / "protocol.json"
    # Preserve the original isolated protocol/whitelist. Integrated declarations
    # are separate immutable plans, never a reinterpretation of old evidence.
    protocol = PROTOCOL | (binding.declaration() if binding is not None else {"source_revision": revision})
    expected = dump(protocol) + "\n"
    if args.write_plan:
        if plan.exists() and plan.read_text() != expected:
            parser.error("Refusing to overwrite a different predeclared protocol")
        plan.write_text(expected)
        print(f"PLAN {plan} sha256={hashlib.sha256(expected.encode()).hexdigest()}")
        return 0
    if not plan.exists() or plan.read_text() != expected:
        parser.error("Write the fixed protocol with --write-plan BEFORE executing measurements")
    if binding is not None:
        binding.protocol_path, binding.protocol_bytes = plan, expected.encode()
        binding.check()
    if "app.config" in sys.modules:
        parser.error("Run this standalone in a fresh interpreter, not in an existing application process")
    if subprocess.check_output(["git", "diff", revision, "--", "app"], cwd=ROOT):
        parser.error("Production app files differ from the frozen source revision; do not measure a moving candidate")
    if shutil.disk_usage(root).free < PROTOCOL["limits"]["artifact_bytes"]:
        parser.error("Insufficient local disk for the declared artifact budget")
    candidate_mode = "integrated-candidate" if binding is not None else "isolated-source"
    run = Run(root, binding=binding, candidate_mode=candidate_mode, candidate_revision=revision)
    shutil.copyfile(plan, run.path / "protocol.json")
    if (root / "PLAN.md").exists():
        shutil.copyfile(root / "PLAN.md", run.path / "PLAN.md")
    manifest = {"source_base": BASE, **run.identity,
                **(binding.declaration() if binding is not None else {"source_revision": revision}),
                "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "python": sys.version, "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
                "cpu_count": os.cpu_count(), "loadavg_start": os.getloadavg(), "protocol": protocol,
                "protocol_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
                    "app/services/source_migration.py", "app/routers/source_migration.py", "app/database.py",
                    "app/services/sources.py", "app/services/metadata_policy.py")},
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (run.path / "manifest.json").write_text(dump(manifest) + "\n")
    print(f"ARTIFACTS {run.path}", flush=True)

    def limit(signum, frame):
        raise TimeoutError("Predeclared 45-minute local wall-clock cap reached")

    signal.signal(signal.SIGALRM, limit)
    signal.alarm(PROTOCOL["limits"]["wall_seconds"])
    try:
        run.setup_runtime()
        run.check_binding()
        run.execute()
        run.check_binding()
        summary = run.summarize()
        run.check_binding()
        after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in manifest["source_sha256"]}
        assert after == manifest["source_sha256"], "Production source changed while measuring"
        if not summary["complete"] or not summary["memory_complete"] or summary["network_attempts"]:
            return 2
        print("SUMMARY " + dump(summary), flush=True)
        return 1 if summary["failed_budgets"] else 0
    except BaseException as error:
        run.emit({"kind": "fatal", "error": repr(error)})
        summary = run.summarize()
        print("INCOMPLETE " + repr(error) + " " + dump(summary), flush=True)
        return 2
    finally:
        signal.alarm(0)
        run.log.close()


if __name__ == "__main__":
    raise SystemExit(main())
