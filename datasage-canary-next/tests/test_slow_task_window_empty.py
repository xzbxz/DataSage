"""Targeted slow-task window/cache/empty-source regressions.

These tests use the existing preparation seam and synthetic inputs only.  They
pin the approved policy: a successful customer plan owns its first observation
window; a failed first verification is never cached; an empty freeze source
blocks; a valid current-week freeze can be reused; truncation remains a hard
input error.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
from datetime import datetime
import importlib
from contextlib import redirect_stderr
import io as pyio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_business_contracts as base
from test_legacy_workflow import employees, mapping, recipients, source
import test_slow_task_preparation as preparation


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
inputs = importlib.import_module(base.TEST_PACKAGE + ".workflow_inputs")
audit = importlib.import_module(base.TEST_PACKAGE + ".workflow_customer_audit")
wf = io.wf
legacy_xlsx = importlib.import_module(base.TEST_PACKAGE + ".legacy_xlsx")


def _baseline():
    rows = wf.freeze_plan([source()], [], "2026-W38", "2026-W38")["insert_rows"]
    rows[0]["frozen_at"] = "2026-09-15 09:00:00"
    return rows


def _mapping(clock: str):
    value = mapping()
    value["window"] = {"start": clock.replace("2026", "2025", 1), "end": clock}
    return value


class _SnapshotDB:
    def __init__(self, clock: str, baseline):
        self.clock = clock
        self.baseline = baseline
        self.calls = []

    def execute(self, sql, params=(), limit=10000):
        self.calls.append(str(sql))
        if "NOW(6)" in str(sql):
            return [{"at": self.clock}], False, {"marker": "synthetic-clock"}
        return self.baseline, False, {"marker": "synthetic-clock"}


@contextmanager
def _snapshot(db):
    yield db


class SlowTaskWindowEmptyTests(unittest.TestCase):
    def test_customer_window_is_bound_to_first_successful_mapping(self):
        db = _SnapshotDB("2026-09-18 11:51:58", [])
        value = inputs.customer_mapping(db, [("SYN-1001", "HCM")])
        self.assertEqual("2025-09-18T11:51:58", value["window"]["start"])
        self.assertEqual("2026-09-18T11:51:58", value["window"]["end"])
        relation_sql = next(sql for sql in db.calls if "delivery_bill_barcode_detail_dwd" in sql)
        self.assertIn("delivery_time>=%s AND d.delivery_time<%s", relation_sql)

    def test_verify_failure_does_not_cache_customer_plan(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "synthetic-verify-window"
            persist_calls = []
            binding = {"recipients_file": "local/workflow-roles.json", "customer_mapping_enabled": True, "send_enabled": False}
            with preparation._isolated_inputs(root, out, preparation.RecordingTransport()):
                with ExitStack() as stack:
                    stack.enter_context(patch.object(io, "persist_plan", side_effect=lambda *args: persist_calls.append(args)))
                    stack.enter_context(patch.object(inputs, "customer_mapping", return_value=_mapping("2026-09-18 11:51:58")))
                    stack.enter_context(patch.object(audit, "verify_plan", side_effect=ValueError("SYNTHETIC_VERIFY_FAILURE")))
                    with self.assertRaisesRegex(ValueError, "SYNTHETIC_VERIFY_FAILURE"):
                        io._produce_and_execute(
                            root,
                            "slow_task",
                            binding,
                            out,
                            "2026-W38",
                            "2026-09",
                            progress,
                            lambda: _snapshot(_SnapshotDB("2026-09-18 11:51:58", _baseline())),
                            None,
                            None,
                        )
            self.assertFalse(any(len(call.args) >= 2 and call.args[1] == "customers" for call in persist_calls))
            self.assertFalse((root / "report_runs" / "legacy_execution" / "customers-2026-W38.json").exists())

    def test_cached_customer_plan_reuses_window_across_day_without_mapping_query_and_tamper_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            cache = {"recipients": {"version": 2, "week": "2026-W38", "regions": {"HCM": [{"account": "synthetic-s", "name": "Synthetic Sales"}]}}, "customers": None}
            mapping_calls = []
            baseline = _baseline()

            def cached(_profile, kind, _week):
                return copy.deepcopy(cache[kind])

            def persist(_profile, kind, _week, value):
                cache[kind] = copy.deepcopy(value)

            def mapping_call(_db, _pairs):
                mapping_calls.append(1)
                return _mapping("2026-09-18 11:51:58")

            def fake_xlsx(_sheets, path, **_kwargs):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"synthetic-xlsx")

            def fake_zip(package, folder, week):
                path = Path(folder) / ("synthetic-" + week + ".zip")
                path.write_bytes(b"synthetic-zip")
                return path

            def fake_audit(*_args, **_kwargs):
                return []

            binding = {"recipients_file": "local/workflow-roles.json", "customer_mapping_enabled": True, "send_enabled": False}
            (out / "day1").mkdir()
            (out / "day2").mkdir()
            with ExitStack() as stack:
                stack.enter_context(patch.object(io, "read_recipients", return_value={"regions": recipients()}))
                stack.enter_context(patch.object(io, "cached_plan", side_effect=cached))
                stack.enter_context(patch.object(io, "persist_plan", side_effect=persist))
                stack.enter_context(patch.object(inputs, "complete", side_effect=lambda _db, sql, params=(), limit=10000: ([{"at": "2026-09-18 11:51:58"}] if "NOW(6)" in sql else baseline)))
                stack.enter_context(patch.object(inputs, "customer_mapping", side_effect=mapping_call))
                stack.enter_context(patch.object(legacy_xlsx, "gen_workbook_xlsx", side_effect=fake_xlsx))
                stack.enter_context(patch.object(wf, "customer_zip", side_effect=fake_zip))
                stack.enter_context(patch.object(io, "dispatch_audits", side_effect=fake_audit))
                first = io._produce_and_execute(root, "slow_task", binding, out / "day1", "2026-W38", "2026-09", io.Progress(root, "slow_task", "2026-W38"), lambda: _snapshot(_SnapshotDB("2026-09-18 11:51:58", baseline)), None, None)
                second = io._produce_and_execute(root, "slow_task", binding, out / "day2", "2026-W38", "2026-09", io.Progress(root, "slow_task", "2026-W38"), lambda: _snapshot(_SnapshotDB("2026-09-19 11:51:58", baseline)), None, None)
                first_observation = __import__("json").loads((out / "day1" / "customer-observation.json").read_text(encoding="utf-8"))
                second_observation = __import__("json").loads((out / "day2" / "customer-observation.json").read_text(encoding="utf-8"))
                self.assertEqual(1, len(mapping_calls))
                self.assertTrue(first_observation["observation"]["relationship_window"]["start"].startswith("2025-09-18"))
                self.assertTrue(second_observation["reused_plan"])
                self.assertEqual(first_observation["observation"], second_observation["observation"])
                self.assertIn("observation_evidence", cache["customers"])
                self.assertIn("observation_evidence_digest", cache["customers"])
                cache["customers"]["observation_evidence"]["relationship_window"] = {
                    "start": "2025-09-17T11:51:58",
                    "end": "2026-09-17T11:51:58",
                }
                # Recomputing the evidence digest must not make a mismatched
                # relationship window acceptable; the independent source mapping
                # window remains the binding reference.
                cache["customers"]["observation_evidence_digest"] = wf.digest(cache["customers"]["observation_evidence"])
                with self.assertRaisesRegex(io.IOErrorBoundary, "CACHED_CUSTOMER_OBSERVATION_WINDOW_MISMATCH"):
                    io._produce_and_execute(root, "slow_task", binding, out / "day3", "2026-W38", "2026-09", io.Progress(root, "slow_task", "2026-W38"), lambda: _snapshot(_SnapshotDB("2026-09-19 11:51:58", baseline)), None, None)
            self.assertEqual(1, len(mapping_calls))

    def test_empty_source_blocks_and_valid_existing_freeze_reuses(self):
        empty = wf.freeze_plan([], [], "2026-W38", "2026-W38")
        self.assertEqual("blocked", empty["status"])
        self.assertIn("EMPTY_SOURCE_KEEP_EXISTING", empty["issues"])
        existing = _baseline()
        reused = wf.freeze_plan([], existing, "2026-W38", "2026-W38")
        self.assertEqual("ready", reused["status"])
        self.assertEqual("reuse_existing", reused["action"])

    def test_truncated_mapping_input_is_a_separate_hard_error(self):
        class TruncatedDB:
            def execute(self, _sql, _params, _limit):
                return [], True, {"marker": "synthetic-truncated"}

        with self.assertRaisesRegex(io.IOErrorBoundary, "WORKFLOW_INPUT_TRUNCATED"):
            inputs.complete(TruncatedDB(), "SELECT synthetic", [], limit=1)

    def test_real_freeze_writer_seam_preserves_empty_plan_reason_and_does_no_dml(self):
        class Cursor:
            def __init__(self, owner):
                self.owner = owner
                self.last = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=()):
                self.owner.sql.append(str(sql))
                if "SELECT NOW(6)" in str(sql):
                    self.last = {"read_at": "2026-09-18 11:51:58"}
                elif "GET_LOCK" in str(sql):
                    self.last = {"acquired": 1}
                else:
                    self.last = {"released": 1}

            def fetchone(self):
                return self.last

        class Writer:
            def __init__(self):
                self.sql = []
                self.begin_calls = 0
                self.commit_calls = 0
                self.rollback_calls = 0
                self.close_calls = 0

            def cursor(self):
                return Cursor(self)

            def begin(self):
                self.begin_calls += 1

            def commit(self):
                self.commit_calls += 1

            def rollback(self):
                self.rollback_calls += 1

            def close(self):
                self.close_calls += 1

        class EmptySnapshot:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _sql, _params, _limit):
                return [], False, {"marker": "synthetic-empty-source"}

        class Progress:
            scope = {"period": "2026-W38"}

            def __init__(self):
                self.values = {}

            def status(self, key):
                return self.values.get(key, {}).get("status", "not_attempted")

            def set(self, key, status, **evidence):
                self.values[key] = {"status": status, **evidence}

        writer = Writer()
        progress = Progress()
        with patch.object(io, "require_action", return_value={"freeze_enabled": True}):
            with self.assertRaisesRegex(io.IOErrorBoundary, r"FREEZE_PLAN_BLOCKED:.*EMPTY_SOURCE_KEEP_EXISTING"):
                io.freeze_current_week(lambda: EmptySnapshot(), lambda: writer, progress, enabled=True, refreeze=False)
        self.assertEqual(0, writer.begin_calls)
        self.assertEqual(0, writer.commit_calls)
        self.assertFalse(any("DELETE FROM" in sql or "INSERT INTO" in sql for sql in writer.sql))
        self.assertNotEqual("committed", progress.status("freeze"))

    def test_cli_captures_empty_freeze_reason_and_returns_two(self):
        local_report = importlib.import_module(base.TEST_PACKAGE + ".local_report")
        with TemporaryDirectory() as tmp:
            profile = Path(tmp)
            stderr = pyio.StringIO()
            with patch("hermes_constants.get_hermes_home", return_value=str(profile)):
                with patch.object(local_report, "_assert_local_context"):
                    with patch.object(io, "run_bound", side_effect=io.IOErrorBoundary("FREEZE_PLAN_BLOCKED:EMPTY_SOURCE_KEEP_EXISTING")):
                        with redirect_stderr(stderr):
                            code = local_report.main(profile, ["--legacy-run", "slow_task"])
            self.assertEqual(2, code)
            text = stderr.getvalue()
            self.assertIn("空源待核验：保留现有冻结，未确认为零任务。", text)
            self.assertIn("FREEZE_PLAN_BLOCKED:EMPTY_SOURCE_KEEP_EXISTING", text)


if __name__ == "__main__":
    unittest.main()
