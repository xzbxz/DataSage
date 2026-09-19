"""Synthetic slow_report preparation, phase gates, and delivery boundaries."""

from contextlib import ExitStack, contextmanager, nullcontext
from hashlib import sha256
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_business_contracts as base


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
inputs = importlib.import_module(base.TEST_PACKAGE + ".workflow_inputs")
evidence = importlib.import_module(base.TEST_PACKAGE + ".report_evidence")
xlsx = importlib.import_module(base.TEST_PACKAGE + ".legacy_xlsx")
wf = io.wf


REGIONS = ("HCM", "HN")


class RecordingTransport:
    def __init__(self, reject_account=None, fail_stage=None, fail_account=None, unknown_stage=None, fail_times=None):
        self.reject_account = reject_account
        self.fail_stage = fail_stage
        self.fail_account = fail_account
        self.unknown_stage = unknown_stage
        self.fail_times = fail_times
        self.preflights = []
        self.sends = []

    def preflight(self, components):
        self.preflights.append([(item["account"], item["stage"]) for item in components])
        if self.reject_account and any(item["account"] == self.reject_account for item in components):
            raise io.IOErrorBoundary("SYNTHETIC_TARGET_PREFLIGHT")

    def delivery_lock(self, _progress):
        return nullcontext()

    def fingerprint(self, item):
        if item["kind"] == "file":
            data = Path(item["path"]).read_bytes()
        else:
            data = str(item["text"]).encode("utf-8")
        return sha256(data).hexdigest()

    def send(self, item):
        self.sends.append((item["stage"], item["account"]))
        if self.unknown_stage == item["stage"]:
            raise TimeoutError("synthetic transport timeout")
        if self.fail_stage == item["stage"] and (self.fail_account is None or self.fail_account == item["account"]) and (self.fail_times is None or self.fail_times > 0):
            if self.fail_times is not None:
                self.fail_times -= 1
            return {"success": False, "raw_response": {"errcode": 1}}
        return {"success": True, "message_id": "synthetic"}


@contextmanager
def _snapshots():
    yield object()


def _roles(shared_account=False):
    hcm_account = "acct-shared" if shared_account else "acct-hcm"
    hn_account = "acct-shared" if shared_account else "acct-hn"
    return {
        "regions": {
            "HCM": {"executors": [{"account": hcm_account, "name": "Synthetic HCM"}], "managers": []},
            "HN": {"executors": [{"account": hn_account, "name": "Synthetic HN"}], "managers": []},
        }
    }


def _adapted(region, period, *, complete=True):
    return {
        "period": period,
        "summary": {"opening_skus": 1, "closing_skus": 1, "opening_rolls": 1, "closing_rolls": 1, "new": 0, "exited": 0, "net_outbound_rolls": 0},
        "sales_rows": [],
        "detail_rows": [],
        "label_coverage": {"complete": complete},
        "completeness": {"frozen_at": "2026-09-15 09:00:00", "as_of_label": "Synthetic snapshot", "observed_to": "2026-09-16 12:00:00"},
        "detail_semantics": {"synthetic": True},
    }


class SlowReportDeliveryTests(unittest.TestCase):
    def run_report(
        self,
        root,
        out,
        progress,
        transport,
        *,
        collect_fn=None,
        body_fail=(),
        workbook_fail=(),
        incomplete=(),
        missing_targets=(),
        force=False,
        events=None,
        shared_account=False,
        read_recipients_fn=None,
        regenerate=False,
        reason="synthetic sealed report replay",
        send_enabled=True,
        body_suffix="",
    ):
        events = events if events is not None else []
        collect_fn = collect_fn or (lambda region, period, phase, week, **kwargs: ({"packets": {"pool": {}, "flow": {}}}, []))

        def collect(region, period, phase, week, **kwargs):
            events.append(("collect", phase, region))
            return collect_fn(region, period, phase, week, **kwargs)

        def adapt(_pool, _flow, _labels, region, period, **kwargs):
            return _adapted(region, period, complete=(region, period) not in set(incomplete))

        def draft(region, period, summary, sales_rows, **kwargs):
            events.append(("draft", kwargs.get("monthly", False), region))
            if region in set(body_fail):
                raise wf.WorkflowError("SYNTHETIC_REPORT_BODY_FAILURE")
            return f"synthetic report {region} {period}{body_suffix}"

        def workbook(sheets, path, **kwargs):
            region = next((value for value in REGIONS if value in str(path)), "")
            events.append(("workbook", region, str(path)))
            if region in set(workbook_fail):
                raise wf.WorkflowError("SYNTHETIC_REPORT_XLSX_FAILURE")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"synthetic-xlsx")

        def recipients(_mapping, region):
            events.append(("recipients", region))
            if region in set(missing_targets):
                return []
            return ["acct-shared"] if shared_account else ["acct-" + region.lower()]

        binding = {
            "send_enabled": send_enabled,
            "_force_resend": force,
            "_regenerate_report": regenerate,
            "_replay_reason": reason if (force or regenerate) else None,
            "target_map": {
                "acct-hcm": {"synthetic_target": "hcm"},
                "acct-hn": {"synthetic_target": "hn"},
                "acct-shared": {"synthetic_target": "shared"},
            },
        }
        with ExitStack() as stack:
            if read_recipients_fn is None:
                stack.enter_context(patch.object(io, "read_recipients", return_value=_roles(shared_account)))
            else:
                stack.enter_context(patch.object(io, "read_recipients", side_effect=read_recipients_fn))
            stack.enter_context(patch.object(wf, "policy", return_value={"regions": {region: {} for region in REGIONS}}))
            stack.enter_context(patch.object(evidence, "collect", side_effect=collect))
            stack.enter_context(patch.object(inputs, "legacy_report_packet", side_effect=adapt))
            stack.enter_context(patch.object(wf, "report_draft", side_effect=draft))
            stack.enter_context(patch.object(wf, "report_recipients", side_effect=recipients))
            stack.enter_context(patch.object(xlsx, "gen_workbook_xlsx", side_effect=workbook))
            return io._produce_and_execute(root, "slow_report", binding, out, "2026-W38", "2026-09", progress, _snapshots, transport, None)

    def test_late_department_workbook_failure_has_zero_send_calls(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            with self.assertRaisesRegex(wf.WorkflowError, "SYNTHETIC_REPORT_XLSX_FAILURE"):
                self.run_report(root, out, progress, transport, workbook_fail=("HN",))
            self.assertEqual([], transport.sends)

    def test_all_targets_are_preflighted_before_any_send(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(reject_account="acct-hn")
            progress = io.Progress(root, "slow_report", "2026-W38")
            with self.assertRaisesRegex(io.IOErrorBoundary, "SYNTHETIC_TARGET_PREFLIGHT"):
                self.run_report(root, out, progress, transport)
            self.assertEqual([], transport.sends)

    def test_body_preparation_failure_has_zero_send_calls(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            events = []
            try:
                self.run_report(root, out, progress, transport, body_fail=("HCM",), events=events)
            except Exception:
                pass
            self.assertEqual([], transport.sends)
            self.assertFalse(any(event[0] == "collect" and event[1] == "monthly" for event in events))

    def test_provider_text_failure_skips_its_file_but_other_department_continues(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(fail_stage="weekly_text", fail_account="acct-hcm")
            progress = io.Progress(root, "slow_report", "2026-W38")
            events = []
            try:
                self.run_report(root, out, progress, transport, events=events)
            except Exception:
                pass
            self.assertEqual([("weekly_text", "acct-hcm")], [item for item in transport.sends if item == ("weekly_text", "acct-hcm")])
            self.assertNotIn(("weekly_file", "acct-hcm"), transport.sends)
            self.assertIn(("weekly_text", "acct-hn"), transport.sends)
            self.assertIn(("weekly_file", "acct-hn"), transport.sends)
            self.assertFalse(any(stage.startswith("monthly_") for stage, _account in transport.sends))

    def test_provider_unknown_stops_other_departments_and_monthly(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(unknown_stage="weekly_text")
            progress = io.Progress(root, "slow_report", "2026-W38")
            try:
                self.run_report(root, out, progress, transport)
            except Exception:
                pass
            self.assertEqual([("weekly_text", "acct-hcm")], transport.sends)
            self.assertFalse(any(account == "acct-hn" for _stage, account in transport.sends))
            self.assertFalse(any(stage.startswith("monthly_") for stage, _account in transport.sends))

    def test_existing_unknown_component_has_zero_send_calls(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport()
            progress = io.Progress(root, "slow_report", "2026-W38")
            item = io.component("acct-hcm", "text", "old weekly body", "2026-W38HCM", "weekly_text")
            progress.set(item["key"], "unknown", fingerprint="synthetic-old")
            with self.assertRaisesRegex(io.IOErrorBoundary, "DELIVERY_UNKNOWN_REVIEW_REQUIRED"):
                self.run_report(root, out, progress, transport)
            self.assertEqual([], transport.sends)

    def test_weekly_unknown_blocks_all_sends_and_monthly(self):
        def unknown_collect(region, period, phase, week, **kwargs):
            if phase == "weekly" and region == "HCM":
                raise io.IOErrorBoundary("REPORT_UNKNOWN_REVIEW_REQUIRED")
            return ({"packets": {"pool": {}, "flow": {}}}, [])

        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            with self.assertRaisesRegex(io.IOErrorBoundary, "REPORT_UNKNOWN_REVIEW_REQUIRED"):
                self.run_report(root, out, progress, transport, collect_fn=unknown_collect)
            self.assertEqual([], transport.sends)

    def test_ordinary_rerun_reuses_completed_phase_without_collect_or_send(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            events = []
            self.run_report(root, out, progress, transport, events=events)
            first_sends = list(transport.sends)
            progress.run_id = "ordinary-rerun"
            self.run_report(root, out, progress, transport, collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not collect")), events=events)
            self.assertEqual(first_sends, transport.sends)

    def test_force_resend_reuses_frozen_content_without_collecting_again(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            events = []
            self.run_report(root, out, progress, transport, events=events)
            first_collects = len([event for event in events if event[0] == "collect"])
            first_sends = len(transport.sends)
            progress.run_id = "explicit-force"
            self.run_report(
                root,
                out,
                progress,
                transport,
                force=True,
                collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("force must reuse frozen content")),
                events=events,
            )
            self.assertEqual(first_collects, len([event for event in events if event[0] == "collect"]))
            self.assertGreater(len(transport.sends), first_sends)

            def sealed_bytes():
                root_path = root / "report_runs" / "legacy_execution" / "slow_report_batches" / "2026-W38"
                return {
                    path.relative_to(root_path).as_posix(): path.read_bytes()
                    for path in root_path.rglob("*")
                    if path.is_file() and path.name != "head.json"
                }

            before = sealed_bytes()
            progress.run_id = "explicit-force-bytes"
            self.run_report(
                root,
                out,
                progress,
                transport,
                force=True,
                collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("force must reuse frozen content")),
                events=events,
            )
            self.assertEqual(before, sealed_bytes())

    def test_changed_current_roles_are_not_read_on_ordinary_sealed_rerun(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            self.run_report(root, out, progress, transport)
            progress.run_id = "roles-changed"
            self.run_report(
                root,
                out,
                progress,
                transport,
                collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not collect")),
                read_recipients_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not read current roles")),
            )

    def test_corrupt_or_remove_sealed_attachment_blocks_without_collect(self):
        for remove in (False, True):
            with self.subTest(remove=remove), TemporaryDirectory() as tmp:
                root = Path(tmp); out = root / "out"; out.mkdir()
                transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
                self.run_report(root, out, progress, transport)
                attachment = next((root / "report_runs" / "legacy_execution" / "slow_report_batches" / "2026-W38").rglob("HCM_weekly.xlsx"))
                if remove:
                    attachment.unlink()
                else:
                    attachment.write_bytes(b"tampered")
                progress.run_id = "sealed-corrupt"
                with self.assertRaises(Exception):
                    self.run_report(
                        root,
                        out,
                        progress,
                        transport,
                        collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sealed corruption must stop before collect")),
                    )

    def test_first_failed_text_retry_sends_only_missing_components(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(fail_stage="weekly_text", fail_account="acct-hcm", fail_times=1)
            progress = io.Progress(root, "slow_report", "2026-W38")
            try:
                self.run_report(root, out, progress, transport)
            except Exception:
                pass
            before = list(transport.sends)
            progress.run_id = "retry-missing"

            def collect_without_weekly(region, period, phase, week, **kwargs):
                if phase == "weekly":
                    raise AssertionError("weekly sealed phase must not collect on retry")
                return ({"packets": {"pool": {}, "flow": {}}}, [])

            self.run_report(root, out, progress, transport, collect_fn=collect_without_weekly)
            self.assertEqual(before.count(("weekly_text", "acct-hcm")) + 1, transport.sends.count(("weekly_text", "acct-hcm")))
            self.assertEqual(1, transport.sends.count(("weekly_file", "acct-hcm")))

    def test_same_account_across_two_departments_keeps_two_notifications(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            self.run_report(root, out, progress, transport, shared_account=True)
            self.assertEqual(2, transport.sends.count(("weekly_text", "acct-shared")))
            self.assertEqual(2, transport.sends.count(("weekly_file", "acct-shared")))

    def test_accepted_text_then_failed_file_retries_only_file_and_keeps_monthly_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(fail_stage="weekly_file", fail_account="acct-hcm", fail_times=1)
            progress = io.Progress(root, "slow_report", "2026-W38")
            try:
                self.run_report(root, out, progress, transport)
            except Exception:
                pass
            before = list(transport.sends)
            self.assertFalse(any(stage.startswith("monthly_") for stage, _account in before))
            progress.run_id = "retry-file-only"

            def sealed_weekly_only(region, period, phase, week, **kwargs):
                if phase == "weekly":
                    raise AssertionError("weekly must reuse sealed phase")
                return ({"packets": {"pool": {}, "flow": {}}}, [])

            self.run_report(root, out, progress, transport, collect_fn=sealed_weekly_only)
            self.assertEqual(before.count(("weekly_text", "acct-hcm")), transport.sends.count(("weekly_text", "acct-hcm")))
            self.assertEqual(before.count(("weekly_file", "acct-hcm")) + 1, transport.sends.count(("weekly_file", "acct-hcm")))

    def test_regenerate_creates_new_content_generation_and_preserves_old_bytes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            events = []
            self.run_report(root, out, progress, transport, events=events)
            batch_root = root / "report_runs" / "legacy_execution" / "slow_report_batches" / "2026-W38"
            old_files = {path.relative_to(batch_root).as_posix(): path.read_bytes() for path in (batch_root / "weekly" / "g0").rglob("*") if path.is_file()}
            old_collect_count = len([event for event in events if event[0] == "collect"])
            progress.run_id = "regenerate-content"
            self.run_report(root, out, progress, transport, regenerate=True, reason="approved report regeneration", body_suffix=" regenerated", events=events)
            head = json.loads((batch_root / "head.json").read_text(encoding="utf-8"))
            self.assertEqual(1, head["content_generation"])
            self.assertGreater(len([event for event in events if event[0] == "collect"]), old_collect_count)
            current_old_files = {path.relative_to(batch_root).as_posix(): path.read_bytes() for path in (batch_root / "weekly" / "g0").rglob("*") if path.is_file()}
            self.assertEqual(old_files, current_old_files)
            self.assertTrue((batch_root / "weekly" / "g1" / "body.txt").is_file())
            self.assertIn(b"regenerated", (batch_root / "weekly" / "g1" / "body.txt").read_bytes())

    def test_force_and_regenerate_are_blocked_for_incomplete_or_unknown_batches(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), TemporaryDirectory() as tmp:
                root = Path(tmp); out = root / "out"; out.mkdir()
                transport = RecordingTransport(unknown_stage="weekly_text" if unknown else None, fail_stage="weekly_file" if not unknown else None)
                progress = io.Progress(root, "slow_report", "2026-W38")
                try:
                    self.run_report(root, out, progress, transport)
                except Exception:
                    pass
                batch_root = root / "report_runs" / "legacy_execution" / "slow_report_batches" / "2026-W38"
                head_before = (batch_root / "head.json").read_bytes() if (batch_root / "head.json").exists() else None
                sends_before = list(transport.sends)
                for force, regenerate in ((True, False), (False, True)):
                    with self.assertRaises(Exception):
                        self.run_report(
                            root,
                            out,
                            progress,
                            transport,
                            force=force,
                            regenerate=regenerate,
                            reason="explicit blocked replay",
                            collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("blocked replay must not collect")),
                        )
                    if head_before is not None:
                        self.assertEqual(head_before, (batch_root / "head.json").read_bytes())
                    self.assertEqual(sends_before, transport.sends)

    def test_receipt_binding_or_manifest_tamper_cannot_claim_completed(self):
        for mode in ("phase_binding", "notification_manifest"):
            with self.subTest(mode=mode), TemporaryDirectory() as tmp:
                root = Path(tmp); out = root / "out"; out.mkdir()
                transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
                self.run_report(root, out, progress, transport)
                data = json.loads(progress.path.read_text(encoding="utf-8"))
                if mode == "phase_binding":
                    data.pop("report_phase_bindings", None)
                else:
                    manifests = data.get("notification_manifests") or {}
                    key = next(iter(manifests))
                    manifests[key] = "tampered"
                progress.path.write_text(json.dumps(data), encoding="utf-8")
                sends_before = list(transport.sends)
                reloaded = io.Progress(root, "slow_report", "2026-W38")
                reloaded.run_id = "receipt-tamper-reload"
                expected = "REPORT_PHASE_RECEIPT_BINDING_MISSING" if mode == "phase_binding" else "REPORT_ACCEPTED_MANIFEST_MISMATCH"
                with self.assertRaisesRegex(io.IOErrorBoundary, expected):
                    self.run_report(
                        root,
                        out,
                        reloaded,
                        transport,
                        collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("tampered receipt must stop before collect")),
                    )
                self.assertEqual(sends_before, transport.sends)

    def test_prepared_only_batch_cannot_be_force_or_regenerated_as_completed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            self.run_report(root, out, progress, transport, send_enabled=False)
            self.assertEqual([], transport.sends)
            for force, regenerate in ((True, False), (False, True)):
                with self.assertRaises(Exception):
                    self.run_report(
                        root,
                        out,
                        progress,
                        transport,
                        send_enabled=False,
                        force=force,
                        regenerate=regenerate,
                        reason="prepared-only replay",
                        collect_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prepared-only replay must not collect")),
                    )

    def test_empty_department_recipients_are_rejected_before_send(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            transport = RecordingTransport(); progress = io.Progress(root, "slow_report", "2026-W38")
            with self.assertRaises(io.IOErrorBoundary):
                self.run_report(root, out, progress, transport, missing_targets=("HN",))
            self.assertEqual([], transport.sends)


if __name__ == "__main__":
    unittest.main()
