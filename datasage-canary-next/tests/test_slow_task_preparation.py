"""Slow-task preparation barriers and phase order; synthetic I/O only.

These tests intentionally enter the existing ``_produce_and_execute`` seam.
They do not start Hermes, open a database, or call a real transport.  The
preparation-failure cases exercise the zero-send barrier and distinguish
preparation previews from the later audit of actual delivery receipts.
"""

from contextlib import ExitStack, contextmanager, nullcontext
from hashlib import sha256
import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_business_contracts as base
from test_legacy_workflow import mapping, source


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
inputs = importlib.import_module(base.TEST_PACKAGE + ".workflow_inputs")
audit = importlib.import_module(base.TEST_PACKAGE + ".workflow_customer_audit")
xlsx = importlib.import_module(base.TEST_PACKAGE + ".legacy_xlsx")
wf = io.wf


class RecordingTransport:
    def __init__(self, rejected_account=None, fail_stage=None, fail_times=None, unknown_stage=None):
        self.rejected_account = rejected_account
        self.fail_stage = fail_stage
        self.fail_times = fail_times
        self.unknown_stage = unknown_stage
        self.preflights = []
        self.sends = []

    def preflight(self, components):
        self.preflights.append([item["stage"] for item in components])
        if self.rejected_account and any(item["account"] == self.rejected_account for item in components):
            raise io.IOErrorBoundary("SYNTHETIC_TARGET_PREFLIGHT")

    def delivery_lock(self, _progress):
        return nullcontext()

    def fingerprint(self, item):
        if item["kind"] == "file":
            payload = Path(item["path"]).read_bytes()
        else:
            payload = str(item["text"]).encode("utf-8")
        return sha256(payload).hexdigest()

    def send(self, item):
        self.sends.append(item["stage"])
        if self.fail_stage == item["stage"] and (self.fail_times is None or self.fail_times > 0):
            if self.fail_times is not None:
                self.fail_times -= 1
            return {"success": False, "raw_response": {"errcode": 1}}
        if self.unknown_stage == item["stage"]:
            return {"success": True}
        return {"success": True, "message_id": "synthetic"}


def _baseline():
    rows = wf.freeze_plan([source()], [], "2026-W38", "2026-W38")["insert_rows"]
    rows[0]["frozen_at"] = "2026-09-15 09:00:00"
    return rows


def _roles():
    return {
        "regions": {
            "HCM": {
                "executors": [{"account": "synthetic-s", "name": "Synthetic Sales"}],
                "managers": [],
            }
        }
    }


def _recipient_cache():
    return {
        "version": 2,
        "week": "2026-W38",
        "regions": {"HCM": [{"account": "synthetic-s", "name": "Synthetic Sales"}]},
    }


def _fake_zip(package, outdir, week):
    path = Path(outdir) / ("sales_synthetic_" + week + ".zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic-zip")
    return path


def _fake_audits(target):
    def dispatch(packages, statuses, recipient_map, week, out, *, preview=False):
        del packages, statuses, recipient_map, preview
        path = Path(out) / ("dispatch_" + week + ".xlsx")
        path.write_bytes(b"synthetic-dispatch")
        return [{"targets": [target], "text": "synthetic dispatch", "path": path, "scope": "synthetic-audit"}]

    return dispatch


@contextmanager
def _isolated_inputs(root, out, transport):
    baseline = _baseline()

    def complete(_db, sql, params=(), limit=10000):
        del params, limit
        if "NOW(6)" in sql:
            return [{"at": "2026-09-15 12:00:00"}]
        return baseline

    def write_xlsx(_sheets, path, **_kwargs):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"synthetic-xlsx")

    with ExitStack() as stack:
        stack.enter_context(patch.object(io, "read_recipients", return_value=_roles()))
        stack.enter_context(
            patch.object(
                io,
                "cached_plan",
                side_effect=lambda _profile, kind, _week: _recipient_cache() if kind == "recipients" else None,
            )
        )
        stack.enter_context(patch.object(io, "persist_plan"))
        stack.enter_context(patch.object(inputs, "complete", side_effect=complete))
        stack.enter_context(patch.object(inputs, "customer_mapping", return_value=mapping()))
        stack.enter_context(patch.object(xlsx, "gen_workbook_xlsx", side_effect=write_xlsx))
        yield baseline


class SlowTaskPreparationTests(unittest.TestCase):
    def _run(
        self,
        root,
        out,
        progress,
        transport,
        *,
        audit_target="synthetic-s",
        verify=None,
        zip_error=None,
        preview_error=None,
    ):
        kwargs = {
            "recipients_file": "local/workflow-roles.json",
            "customer_mapping_enabled": True,
            "send_enabled": True,
        }
        dispatch = _fake_audits(audit_target)
        if preview_error is not None:
            normal_dispatch = dispatch

            def dispatch(*args, **kwargs):
                if kwargs.get("preview"):
                    raise preview_error
                return normal_dispatch(*args, **kwargs)

        with _isolated_inputs(root, out, transport):
            with patch.object(io, "dispatch_audits", side_effect=dispatch):
                with patch.object(audit, "verify_plan", side_effect=verify) if verify else nullcontext():
                    with patch.object(wf, "customer_zip", side_effect=zip_error) if zip_error else patch.object(wf, "customer_zip", side_effect=_fake_zip):
                        return io._produce_and_execute(
                            root,
                            "slow_task",
                            kwargs,
                            out,
                            "2026-W38",
                            "2026-09",
                            progress,
                            lambda: _snapshot_context(),
                            transport,
                            None,
                        )

    def test_verify_failure_has_zero_send_calls(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "synthetic-verify"
            with self.assertRaisesRegex(ValueError, "SYNTHETIC_VERIFY_FAILURE"):
                self._run(root, out, progress, transport, verify=ValueError("SYNTHETIC_VERIFY_FAILURE"))
            self.assertEqual([], transport.sends)

    def test_zip_failure_has_zero_send_calls_and_no_dispatch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "synthetic-zip"
            with self.assertRaises((wf.WorkflowError, io.IOErrorBoundary)):
                self._run(root, out, progress, transport, zip_error=wf.WorkflowError("SYNTHETIC_ZIP_FAILURE"))
            self.assertEqual([], transport.sends)

    def test_dynamic_audit_target_is_preflighted_before_task_send(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport(rejected_account="synthetic-missing-target")
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "synthetic-target"
            with self.assertRaises(io.IOErrorBoundary):
                self._run(root, out, progress, transport, audit_target="synthetic-missing-target")
            self.assertEqual([], transport.sends)
            self.assertTrue(transport.preflights)

    def test_successful_phase_order_is_task_customer_then_dispatch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "synthetic-order"
            self._run(root, out, progress, transport)
            core = [stage for stage in transport.sends if not stage.startswith("coverage_")]
            self.assertEqual(
                ["task_text", "task_file", "customer_summary", "customer_zip", "audit_text", "audit_file"],
                core,
            )
            coverage = [stage for stage in transport.sends if stage.startswith("coverage_")]
            self.assertEqual(["coverage_text", "coverage_file"], coverage)
            self.assertGreaterEqual(transport.sends.index("coverage_text"), transport.sends.index("audit_file"))

    def test_accepted_components_are_not_repeated_on_ordinary_rerun(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "ordinary-rerun"
            self._run(root, out, progress, transport)
            first = list(transport.sends)
            self._run(root, out, progress, transport)
            self.assertEqual(first, transport.sends)

    def test_partial_customer_delivery_only_retries_missing_zip_component(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport(fail_stage="customer_zip")
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "partial-rerun"
            with patch.object(io.time, "sleep", side_effect=lambda _seconds: None):
                with self.assertRaises(io.IOErrorBoundary):
                    self._run(root, out, progress, transport)
            first = list(transport.sends)
            transport.fail_stage = None
            self._run(root, out, progress, transport)
            self.assertEqual(first.count("task_text"), transport.sends.count("task_text"))
            self.assertEqual(first.count("task_file"), transport.sends.count("task_file"))
            self.assertEqual(first.count("customer_summary"), transport.sends.count("customer_summary"))
            self.assertEqual(first.count("customer_zip") + 1, transport.sends.count("customer_zip"))

    def test_existing_unknown_customer_component_blocks_the_whole_run(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            scope = wf.digest(["2026-W38", "synthetic-s", mapping()["productsByCustomer"]])
            unknown = io.component("synthetic-s", "text", "old customer summary", scope, "customer_summary")
            progress.set(unknown["key"], "unknown", fingerprint="synthetic-old")
            with self.assertRaisesRegex(io.IOErrorBoundary, "DELIVERY_UNKNOWN_REVIEW_REQUIRED"):
                self._run(root, out, progress, transport)
            self.assertEqual([], transport.sends)

    def test_dispatch_preview_failure_has_zero_send_calls(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "preview-failure"
            with self.assertRaisesRegex(wf.WorkflowError, "SYNTHETIC_PREVIEW_FAILURE"):
                self._run(root, out, progress, transport, preview_error=wf.WorkflowError("SYNTHETIC_PREVIEW_FAILURE"))
            self.assertEqual([], transport.sends)

    def test_transient_known_failure_retries_zip_without_resending_accepted_text(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport(fail_stage="customer_zip", fail_times=1)
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "retry-transient"
            sleeps = []
            with patch.object(io.time, "sleep", side_effect=sleeps.append):
                self._run(root, out, progress, transport)
            self.assertEqual(1, transport.sends.count("customer_summary"))
            self.assertEqual(2, transport.sends.count("customer_zip"))
            self.assertEqual([5.0], sleeps)

    def test_unknown_result_is_not_retried_by_slow_task_budget(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport(unknown_stage="customer_zip")
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "retry-unknown"
            sleeps = []
            with patch.object(io.time, "sleep", side_effect=sleeps.append):
                with self.assertRaises(io.IOErrorBoundary):
                    self._run(root, out, progress, transport)
            self.assertEqual(1, transport.sends.count("customer_zip"))
            self.assertEqual([], sleeps)

    def test_retry_budget_exhaustion_preserves_original_component_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            transport = RecordingTransport(fail_stage="customer_zip")
            progress = io.Progress(root, "slow_task", "2026-W38")
            progress.run_id = "retry-exhausted"
            sleeps = []
            with patch.object(io.time, "sleep", side_effect=sleeps.append):
                with self.assertRaisesRegex(io.IOErrorBoundary, "DELIVERY_COMPONENT_FAILED"):
                    self._run(root, out, progress, transport)
            self.assertEqual(3, transport.sends.count("customer_zip"))
            self.assertEqual([5.0, 15.0], sleeps)


@contextmanager
def _snapshot_context():
    # _isolated_inputs patches every query through workflow_inputs.complete;
    # this context only supplies the existing adapter seam.
    yield object()


if __name__ == "__main__":
    unittest.main()
