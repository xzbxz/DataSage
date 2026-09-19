"""CLI contract tests for the planned slow_report entry controls.

These tests are intentionally isolated from the shared implementation.  The
root integration must wire the flags into local_report/run_bound before this
file is expected to pass.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_business_contracts as base


report = importlib.import_module(base.TEST_PACKAGE + ".local_report")
workflow_io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
batch = importlib.import_module(base.TEST_PACKAGE + ".slow_report_batch")
contract_store = importlib.import_module(base.TEST_PACKAGE + ".contract_store")
schedule = importlib.import_module(base.TEST_PACKAGE + ".workflow_schedule")


class SlowReportEntryTests(unittest.TestCase):
    def run_entry(self, args):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            with patch("hermes_constants.get_hermes_home", return_value=profile), patch.object(
                report, "_assert_local_context"
            ), patch.object(report, "configure_runtime"), patch.object(
                workflow_io, "run_bound", return_value={"status": "success"}
            ) as run_bound:
                code = report.main(profile, ["--legacy-run", "slow_report", *args])
                return code, run_bound

    def complete_batch(self, profile):
        root = batch.batch_root(profile)
        store = batch.BatchStore(root, "2026-W38")
        store.ensure_head(month="2026-09", observed_at="2026-09-19T11:00:00+00:00")
        for phase in ("weekly", "monthly"):
            body = {
                "version": 1,
                "week": "2026-W38",
                "month": "2026-09",
                "phase": phase,
                "period": "2026-W38" if phase == "weekly" else "2026-09",
                "documents": [
                    {
                        "region": "HCM",
                        "text": "synthetic sealed report",
                        "accounts": ["acct"],
                        "attachment": f"HCM_{phase}.xlsx",
                        "evidence": {"synthetic": True},
                    }
                ],
            }
            store.prepare_phase(
                phase,
                body=json.dumps(body, ensure_ascii=False),
                attachments={f"HCM_{phase}.xlsx": b"sealed synthetic workbook"},
                logical_accounts=["acct"],
                target_map={"acct": {"platform": "local", "target_id": "acct"}},
                observed_at="2026-09-19T11:00:00+00:00",
                regions=["HCM"],
                evidence={"synthetic": True},
            )
            store.publish_phase(phase)
        return store

    def test_regenerate_report_requires_reason_and_is_only_slow_report(self):
        code, run = self.run_entry(["--regenerate-report", "--replay-reason", "approved new report generation"])
        self.assertEqual(0, code)
        self.assertEqual(1, run.call_count)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["regenerate_report"])
        self.assertFalse(kwargs["force_resend"])
        self.assertFalse(kwargs["refreeze"])
        self.assertEqual("approved new report generation", kwargs["reason"])

    def test_regenerate_cannot_combine_force_or_refreeze(self):
        for extra in (
            ["--force-resend"],
            ["--refreeze"],
        ):
            code, run = self.run_entry(
                ["--regenerate-report", "--replay-reason", "explicit report generation", *extra]
            )
            self.assertEqual(2, code)
            run.assert_not_called()

    def test_regenerate_reason_is_bounded(self):
        for reason in ("no", "x" * 121):
            code, run = self.run_entry(["--regenerate-report", "--replay-reason", reason])
            self.assertEqual(2, code)
            run.assert_not_called()

    def test_force_resend_reuses_sealed_generation_and_does_not_request_regeneration(self):
        code, run = self.run_entry(["--force-resend", "--replay-reason", "replay sealed report"])
        self.assertEqual(0, code)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["force_resend"])
        self.assertFalse(kwargs["refreeze"])
        self.assertFalse(kwargs["regenerate_report"])
        self.assertIsNone(kwargs.get("resume_report_week"))

    def test_resume_report_week_only_loads_existing_week(self):
        code, run = self.run_entry(["--resume-report-week", "2026-W38"])
        self.assertEqual(0, code)
        self.assertEqual("2026-W38", run.call_args.kwargs["resume_report_week"])
        self.assertFalse(run.call_args.kwargs["regenerate_report"])

    def test_resume_report_week_rejects_invalid_or_historical_arbitrary_shape(self):
        for value in ("2026-09", "2026-W00", "not-a-week"):
            code, run = self.run_entry(["--resume-report-week", value])
            self.assertEqual(2, code)
            run.assert_not_called()

    def test_saturday_sequence_is_weekly_then_monthly(self):
        from datetime import datetime

        saturday_19 = datetime.fromisoformat("2026-09-19T19:00:00+08:00")
        self.assertTrue(schedule.due("slow-report", saturday_19))
        self.assertEqual("HCM", schedule.next_report_department(["HCM"], {"HCM": 3}))
        self.assertEqual("HCM", schedule.next_report_department(["HCM"], {"HCM": 4}))

    def test_no_entry_path_can_send_or_collect(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            with patch("hermes_constants.get_hermes_home", return_value=profile), patch.object(
                report, "_assert_local_context"
            ), patch.object(report, "configure_runtime"), patch.object(
                workflow_io, "run_bound", return_value={"status": "success"}
            ) as run_bound:
                code = report.main(
                    profile,
                    ["--legacy-run", "slow_report", "--force-resend", "--replay-reason", "sealed replay"],
                )
                self.assertEqual(0, code)
                run_bound.assert_called_once()
                self.assertFalse((profile / "cron").exists())

    def test_real_run_bound_resume_missing_head_does_not_configure_or_open_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            binding = {"enabled": True, "read_enabled": True, "send_enabled": False}
            snapshot_calls = []

            def forbidden_snapshot():
                snapshot_calls.append(1)
                raise AssertionError("resume missing head must stop before snapshot")

            with patch.object(contract_store, "profile_root", return_value=profile), patch.object(
                workflow_io, "load_activation", return_value=binding
            ), patch.object(report, "configure_runtime") as configure:
                with self.assertRaisesRegex(workflow_io.IOErrorBoundary, "REPORT_RESUME_BATCH_MISSING"):
                    workflow_io.run_bound(
                        profile,
                        "slow_report",
                        snapshot_factory=forbidden_snapshot,
                        resume_report_week="2026-W38",
                    )
            configure.assert_not_called()
            self.assertEqual([], snapshot_calls)

    def test_real_run_bound_resume_complete_batch_never_uses_snapshot_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            self.complete_batch(profile)
            binding = {"enabled": True, "read_enabled": True, "send_enabled": False}
            snapshot_calls = []

            def forbidden_snapshot():
                snapshot_calls.append(1)
                raise AssertionError("sealed resume must not read database clock")

            with patch.object(contract_store, "profile_root", return_value=profile), patch.object(
                workflow_io, "load_activation", return_value=binding
            ), patch.object(report, "configure_runtime"), patch.object(
                workflow_io, "_produce_and_execute", return_value={"status": "success", "prepared_only": True}
            ) as produce:
                result = workflow_io.run_bound(
                    profile,
                    "slow_report",
                    snapshot_factory=forbidden_snapshot,
                    resume_report_week="2026-W38",
                )
            self.assertEqual("success", result["status"])
            produce.assert_called_once()
            self.assertEqual([], snapshot_calls)


if __name__ == "__main__":
    unittest.main()
