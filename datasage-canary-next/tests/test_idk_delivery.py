"""Synthetic IDK runner contract tests; no DB, network, or active state."""

from contextlib import contextmanager, nullcontext
from hashlib import sha256
import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_business_contracts as base


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
wf = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")
runner = importlib.import_module(base.TEST_PACKAGE + ".idk_runner")
app = importlib.import_module(base.TEST_PACKAGE + ".wecom_app_transport")


class FakeTransport:
    def __init__(self, *, outcome="success", fail_on_send=None, unknown_on_send=None):
        self.outcome = outcome
        self.fail_on_send = fail_on_send
        self.unknown_on_send = unknown_on_send
        self.preflights = []
        self.sends = []
        self.texts = []
        self._normalizer = object.__new__(app.AppTransport)

    def preflight(self, components):
        self.preflights.append([(item["account"], item["stage"]) for item in components])

    def normalize(self, components, progress):
        return self._normalizer.normalize(components, progress)

    def delivery_lock(self, _progress):
        return nullcontext()

    def fingerprint(self, item):
        return sha256(str(item.get("text") or "").encode("utf-8")).hexdigest()

    def send(self, item):
        self.sends.append(item["stage"])
        self.texts.append(item.get("text", ""))
        attempt = len(self.sends)
        if self.unknown_on_send == attempt or self.outcome == "unknown":
            raise TimeoutError("synthetic IDK timeout")
        if self.fail_on_send == attempt or self.outcome == "failed":
            return {"success": False, "raw_response": {"errcode": 1}}
        return {"success": True, "message_id": "synthetic"}


def _roles(accounts=("idk-acct",)):
    return {"regions": {"IDK": {"executors": [{"account": account, "name": "Synthetic IDK"} for account in accounts], "managers": []}}}


def _target_map(accounts=("idk-acct",), suffix="a"):
    return {
        account: {
            "platform": "wecom_app_http",
            "app_name": "synthetic-app",
            "corp_id": "synthetic-corp",
            "agent_id": "1000043",
            "target_kind": "user",
            "target_id": f"idk-{suffix}-{index}",
        }
        for index, account in enumerate(accounts)
    }


def _document(*, rows=None, observed_at="2026-09-19T12:00:00", selection_complete=True):
    records = rows if rows is not None else [{"source_ref": 1, "product": "SYN-IDK-1", "product_name": "Synthetic", "color": "Blue", "unit": "m", "quantity": 20, "rolls": 2, "price_state": "null"}]
    return {
        "status": "success",
        "kind": "idk_unpriced",
        "scope_hash": "synthetic-scope",
        "observed_at": observed_at,
        "records": records,
        "source_rows": len(records),
        "selection_complete": selection_complete,
        "window_days": 0,
        "window_start": None,
        "events": [],
        "baseline_id": None,
    }


def _source_row(index=1):
    return {"source_ref": index, "product": f"SYN-IDK-{index}", "product_name": "Synthetic", "color": "Blue", "unit": "m", "quantity": 20, "rolls": 2, "price_state": "null"}


@contextmanager
def _snapshots():
    yield object()


class IDKDeliveryTests(unittest.TestCase):
    def run_idk(self, root, out, progress, transport, *, document=None, roles=None, target_map=None, send_enabled=True, force=False, reason=None, operation=None, execute_log=None, week="2026-W38", roles_fn=None):
        raw = document or _document()
        execute_log = execute_log if execute_log is not None else []

        def execute(_profile, _report_id, binding):
            execute_log.append(dict(binding))
            return raw

        binding = {
            "send_enabled": send_enabled,
            "recipients_file": "local/workflow-roles.json",
            "target_map": target_map or _target_map(),
            "operation": operation or {"kind": "idk_unpriced", "limit": 10000},
            "_force_resend": force,
            "_replay_reason": reason,
        }
        role_patch = patch.object(io, "read_recipients", side_effect=roles_fn) if roles_fn is not None else patch.object(io, "read_recipients", return_value=roles or _roles())
        with role_patch, patch.object(io.operations, "execute", side_effect=execute):
            return io._produce_and_execute(root, "idk", binding, out, week, "2026-09", progress, _snapshots, transport, None)

    def test_zero_rows_with_executor_is_explainable_message(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(); progress = io.Progress(root, "idk", "2026-W38")
            result = self.run_idk(root, out, progress, transport, document=_document(rows=[]))
            self.assertEqual("success", result["status"])
            self.assertEqual(0, len(transport.sends))
            self.assertEqual("empty_no_task", result["delivery_state"])

    def test_empty_executor_plan_fails_before_observation_or_send(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(); progress = io.Progress(root, "idk", "2026-W38"); executed = []
            with self.assertRaisesRegex(Exception, "EMPTY|RECIPIENT|EXECUTOR|TARGET"):
                self.run_idk(root, out, progress, transport, roles=_roles(()), execute_log=executed)
            self.assertEqual([], executed)
            self.assertEqual([], transport.sends)

    def test_window_days_is_forwarded_to_observation_operation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(); progress = io.Progress(root, "idk", "2026-W38"); executed = []
            window_document = _document()
            window_document["window_days"] = 7
            window_document["window_start"] = "2026-09-12T12:00:00"
            self.run_idk(root, out, progress, transport, document=window_document, send_enabled=False, operation={"kind": "idk_unpriced", "limit": 10000, "window_days": 7}, execute_log=executed)
            self.assertEqual(7, executed[0]["window_days"])
            self.assertEqual([], transport.sends)

    def test_unknown_provider_result_stops_and_does_not_retry(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(outcome="unknown"); progress = io.Progress(root, "idk", "2026-W38")
            with self.assertRaisesRegex(Exception, "UNKNOWN"):
                self.run_idk(root, out, progress, transport)
            self.assertEqual(1, len(transport.sends))
            with self.assertRaisesRegex(Exception, "UNKNOWN"):
                self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), transport)
            self.assertEqual(1, len(transport.sends))

    def test_partial_failure_blocks_force_resend(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            failed = FakeTransport(outcome="failed"); progress = io.Progress(root, "idk", "2026-W38")
            with self.assertRaisesRegex(Exception, "FAILED|INCOMPLETE|REVIEW"):
                self.run_idk(root, out, progress, failed)
            before = len(failed.sends)
            with self.assertRaisesRegex(Exception, "FAILED|INCOMPLETE|REVIEW"):
                self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), FakeTransport(), force=True, reason="explicit IDK resend")
            self.assertEqual(before, len(failed.sends))

    def test_full_acceptance_can_force_resend_without_reobserving(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(); progress = io.Progress(root, "idk", "2026-W38"); executed = []
            self.run_idk(root, out, progress, transport, execute_log=executed)
            first_observations = len(executed); first_sends = len(transport.sends)
            self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), transport, force=True, reason="explicit IDK resend", execute_log=executed)
            self.assertEqual(first_observations, len(executed))
            self.assertGreater(len(transport.sends), first_sends)

    def test_target_change_is_blocked_before_reobservation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            transport = FakeTransport(); progress = io.Progress(root, "idk", "2026-W38"); executed = []
            self.run_idk(root, out, progress, transport, target_map=_target_map(suffix="old"), execute_log=executed)
            count = len(executed)
            with self.assertRaisesRegex(Exception, "TARGET|BINDING|CONTENT|CONFIGURATION"):
                self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), transport, target_map=_target_map(suffix="new"), execute_log=executed)
            self.assertEqual(count, len(executed))

    def test_no_send_preview_is_not_a_formal_week_seal(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            preview = FakeTransport(); executed = []
            self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), preview, send_enabled=False, execute_log=executed)
            self.assertEqual([], preview.sends)
            real = FakeTransport()
            result = self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), real, send_enabled=True, execute_log=executed)
            self.assertEqual("success", result["status"])
            self.assertEqual(1, len(real.sends))
            self.assertEqual(2, len(executed))

    def test_zero_result_reuses_sealed_week_without_roles_or_reobservation_and_new_week_reads(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); out = root / "out"; out.mkdir()
            executed = []
            first = FakeTransport()
            self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), first, document=_document(rows=[]), execute_log=executed)
            self.assertEqual([], first.sends)
            observations = len(executed)
            second = FakeTransport()
            self.run_idk(
                root,
                out,
                io.Progress(root, "idk", "2026-W38"),
                second,
                document=_document(rows=[_source_row()]),
                execute_log=executed,
                roles_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sealed zero result must not read roles")),
            )
            self.assertEqual(observations, len(executed))
            self.assertEqual([], second.sends)
            third = FakeTransport()
            self.run_idk(root, out, io.Progress(root, "idk", "2026-W39"), third, document=_document(rows=[]), execute_log=executed, week="2026-W39")
            self.assertEqual(observations + 1, len(executed))

    def test_source_shape_and_truncation_signals_are_rejected_before_send(self):
        for bad in (
            _document(rows=[_source_row()]),
            _document(rows=[_source_row()], selection_complete=False),
            _document(rows=[{**_source_row(),'source_ref':'   '}],selection_complete=True),
            _document(rows=[{**_source_row(),'product':'   '}],selection_complete=True),
        ):
            if bad['records'][0].get('source_ref')==1 and bad['records'][0].get('product')=='SYN-IDK-1' and bad["selection_complete"]:
                bad["source_rows"] = 2
            with self.subTest(bad=bad):
                with TemporaryDirectory() as temp:
                    root = Path(temp); out = root / "out"; out.mkdir()
                    transport = FakeTransport(); executed = []
                    with self.assertRaisesRegex(Exception, "SOURCE|OBSERVATION|SELECTION|INCOMPLETE|TRUNCAT"):
                        self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), transport, document=bad, execute_log=executed)
                    self.assertEqual([], transport.sends)

    def test_second_utf8_segment_known_failure_and_unknown_stop_followup_segments(self):
        rows = [
            {"source_ref": index, "product": f"SYN-IDK-{index}", "product_name": "Synthetic", "color": "Blue", "unit": "m", "quantity": 20, "rolls": 2, "price_state": "null"}
            for index in range(1, 1501)
        ]
        for outcome_name, transport in (("failed", FakeTransport(fail_on_send=2)), ("unknown", FakeTransport(unknown_on_send=2))):
            with self.subTest(outcome=outcome_name), TemporaryDirectory() as temp:
                root = Path(temp); out = root / "out"; out.mkdir()
                with self.assertRaisesRegex(Exception, "FAILED|UNKNOWN"):
                    self.run_idk(root, out, io.Progress(root, "idk", "2026-W38"), transport, document=_document(rows=rows))
                self.assertEqual(2, len(transport.sends))
                self.assertGreaterEqual(max(len(group) for group in transport.preflights), 2)
                recovery=FakeTransport();executed=[]
                if outcome_name=='failed':
                    result=self.run_idk(root,out,io.Progress(root,'idk','2026-W38'),recovery,document=_document(rows=[]),execute_log=executed)
                    self.assertEqual(result['components']-1,len(recovery.sends))
                    self.assertNotEqual(transport.texts[0],recovery.texts[0])
                    self.assertEqual(transport.texts[1],recovery.texts[0])
                else:
                    with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):
                        self.run_idk(root,out,io.Progress(root,'idk','2026-W38'),recovery,execute_log=executed)
                    self.assertEqual([],recovery.sends)
                self.assertEqual([],executed)

    def test_utf8_markdown_split_is_byte_safe_and_bound_to_one_notice(self):
        class NotAttempted:
            def status(self, _key):
                return "not_attempted"

        splitter = object.__new__(app.AppTransport)
        body = "**IDK**\n" + ("中" * 1800)
        item = io.component("synthetic-idk", "text", body, "2026-W38", "idk"); item["message_format"] = "markdown"
        parts = splitter.normalize([item], NotAttempted())
        self.assertGreater(len(parts), 1)
        self.assertLessEqual(max(len(part["text"].encode("utf-8")) for part in parts), 4096)
        self.assertEqual(body, "".join(part["text"] for part in parts))
        self.assertEqual(1, len({part["notification_key"] for part in parts}))


if __name__ == "__main__":
    unittest.main()
