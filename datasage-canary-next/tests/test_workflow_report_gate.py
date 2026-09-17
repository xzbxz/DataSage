"""Offline workflow send-gate checks; the transport is always mocked."""

from __future__ import annotations

import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import test_business_contracts as base


PACKAGE = base.TEST_PACKAGE
workflow_io = importlib.import_module(PACKAGE + ".workflow_io")
operations = importlib.import_module(PACKAGE + ".operations")


def _result(*, truncated: bool = False, data_state: str = "rows") -> dict[str, object]:
    return {
        "request_id": "0",
        "status": "success",
        "data_state": data_state,
        "rows": [{"dimensions": [], "facts": {"metric_value": 1}}],
        "row_count": 1,
        "truncated": truncated,
        "applied_time_range": {"fabric_read_at": "2026-09-17T10:00:00"},
    }


def _document(*, truncated: bool = False, expected: bool = True) -> dict[str, object]:
    raw = _result(truncated=truncated, data_state="truncated" if truncated else "rows")
    document: dict[str, object] = {
        "status": "partial" if truncated else "success",
        "query_packets": [{"status": "success", "results": [raw]}],
    }
    if expected:
        document["expected_request_ids"] = ["0"]
    return document


class WorkflowReportGateTests(unittest.TestCase):
    def _run(self, document):
        with TemporaryDirectory() as raw:
            profile = Path(raw)
            out = profile / "out"
            out.mkdir()
            progress = workflow_io.Progress(profile, "fabric", "2026-W38")
            binding = {
                "operation": {
                    "kind": "fabric_review",
                    "limit": 1,
                    "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
                    "inventory_scope": "total",
                },
                "send_enabled": True,
                "target_map": {"synthetic-target": {}},
            }
            sent: list[object] = []

            def fake_send(components, *_args, **_kwargs):
                sent.extend(components)

            with patch.object(operations, "execute", return_value=document), \
                patch(PACKAGE + ".fabric_report.export_report", return_value=[str(out / "report.xlsx")]), \
                patch.object(workflow_io, "deliver_components", side_effect=fake_send):
                returned = workflow_io._produce_and_execute(
                    profile,
                    "fabric",
                    binding,
                    out,
                    "2026-W38",
                    "2026-09",
                    progress,
                    None,
                    object(),
                    None,
                )
            return returned, sent

    def test_truncated_success_never_calls_delivery(self):
        returned, sent = self._run(_document(truncated=True))
        self.assertEqual([], sent)
        self.assertEqual("blocked_incomplete_report", returned["delivery"])
        self.assertFalse(returned["report_delivery_gate"]["allowed"])
        self.assertIn("0:SOURCE_TRUNCATED", returned["report_delivery_gate"]["reason_codes"])

    def test_missing_expected_request_set_never_calls_delivery(self):
        returned, sent = self._run(_document(expected=False))
        self.assertEqual([], sent)
        self.assertEqual("partial", returned["status"])
        self.assertEqual("blocked_incomplete_report", returned["delivery"])
        self.assertEqual(
            ["REQUEST_ID_SET_MISMATCH"],
            returned["report_delivery_gate"]["reason_codes"],
        )

    def test_complete_expected_raw_packet_calls_mock_delivery(self):
        returned, sent = self._run(_document())
        self.assertEqual(2, len(sent))
        self.assertEqual("provider_accepted_not_human_read", returned["delivery"])
        self.assertTrue(returned["report_delivery_gate"]["allowed"])


if __name__ == "__main__":
    unittest.main()
