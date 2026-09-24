"""Offline checks for the shared result-consumer completeness boundary."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

import test_business_contracts as base


PACKAGE = base.TEST_PACKAGE
fabric = importlib.import_module(PACKAGE + ".fabric_report")
operations = importlib.import_module(PACKAGE + ".operations")
local_report = importlib.import_module(PACKAGE + ".local_report")
completeness = importlib.import_module(PACKAGE + ".result_completeness")
wire = importlib.import_module(PACKAGE + ".wire")


def result(
    request_id: str = "table",
    *,
    status: str = "success",
    data_state: str = "rows",
    truncated: object = False,
    row_count: int = 2,
    population: int | None = 3,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "metric_value": "12",
        "fabric_population_groups": population,
        "fabric_scope_rows": 8,
        "fabric_unknown_tags": 1,
        "fabric_read_at": "2026-09-17T10:00:00",
    }
    rows = [
        {
            "dimensions": [
                {
                    "code": "product",
                    "label": "产品",
                    "value": "SYN-1",
                    "entity_ref": "product_abc123",
                    "identity_state": "resolved",
                }
            ],
            "facts": facts,
        }
        for _ in range(row_count)
    ]
    return {
        "request_id": request_id,
        "status": status,
        "data_state": data_state,
        "rows": rows,
        "row_count": row_count,
        "truncated": truncated,
        "applied_time_range": {"read_at": "2026-09-17T10:00:00"},
    }


class ResultCompletenessTests(unittest.TestCase):
    def test_summary_uses_existing_result_fields_and_keeps_unknowns(self):
        item = completeness.summarize_result(result())
        self.assertEqual("success", item["status"])
        self.assertEqual("rows", item["data_state"])
        self.assertFalse(item["truncated"])
        self.assertEqual(2, item["returned_group_count"])
        self.assertEqual(3, item["population_group_count"])
        self.assertEqual(8, item["population_row_count"])
        self.assertEqual("2026-09-17T10:00:00", item["observed_at"])
        self.assertIn("rows[0].facts.fabric_unknown_tags", item["unknown_items"])
        self.assertTrue(item["report_complete"])

    def test_missing_truncation_flag_cannot_pass_report_gate(self):
        missing = result()
        missing.pop("truncated")
        item = completeness.summarize_result(missing)
        self.assertFalse(item["report_complete"])
        gate = completeness.report_delivery_gate([missing])
        self.assertFalse(gate["allowed"])
        self.assertIn("table:TRUNCATION_EVIDENCE_MISSING", gate["reason_codes"])

    def test_successful_truncated_result_is_bounded_and_not_complete(self):
        bounded = result(truncated=True, data_state="truncated", row_count=2, population=5)
        item = completeness.summarize_result(bounded)
        self.assertEqual("truncated", item["completeness"])
        self.assertFalse(item["report_complete"])
        gate = completeness.report_delivery_gate([bounded])
        self.assertFalse(gate["allowed"])
        self.assertIn("table:SOURCE_TRUNCATED", gate["reason_codes"])

    def test_document_gate_accounts_for_expected_ids_and_missing_packets(self):
        first = completeness.summarize_result(result("first", population=1, row_count=1))
        document = {
            "expected_request_ids": ["first", "second"],
            "query_packets": [{"status": "success", "results": [result("first", population=1, row_count=1)]}],
            "coverage": [first],
        }
        gate = completeness.gate_for_document(document)
        self.assertFalse(gate["allowed"])
        self.assertIn("REQUEST_ID_SET_MISMATCH", gate["reason_codes"])
        packet_gate = completeness.gate_for_document(
            {
                "expected_request_ids": ["only"],
                "query_packets": [{"status": "success", "results": []}],
            }
        )
        self.assertFalse(packet_gate["allowed"])
        self.assertTrue(any("MISSING_RESULT" in code for code in packet_gate["reason_codes"]))
        no_expected = completeness.gate_for_document(
            {"query_packets": [{"status": "success", "results": [result("only", population=1, row_count=1)]}]}
        )
        self.assertFalse(no_expected["allowed"])
        self.assertEqual(["REQUEST_ID_SET_MISMATCH"], no_expected["reason_codes"])

    def test_document_gate_does_not_trust_complete_cached_coverage_over_raw_truncation(self):
        raw = result("only", truncated=True, data_state="truncated", row_count=2, population=5)
        cached = completeness.summarize_result(
            result("only", truncated=False, data_state="rows", row_count=2, population=5)
        )
        gate = completeness.gate_for_document(
            {
                "status": "success",
                "expected_request_ids": ["only"],
                "query_packets": [{"status": "success", "results": [raw]}],
                "coverage": [cached],
            }
        )
        self.assertFalse(gate["allowed"])
        self.assertIn("only:SOURCE_TRUNCATED", gate["reason_codes"])
        self.assertIn("COVERAGE_RESULT_MISMATCH", gate["reason_codes"])

    def test_fabric_export_recomputes_raw_coverage_instead_of_using_cached_summary(self):
        raw = result("0", truncated=True, data_state="truncated", row_count=2, population=5)
        cached = completeness.summarize_result(
            result("0", truncated=False, data_state="rows", row_count=2, population=5)
        )
        observations = fabric.observations(
            {
                "expected_request_ids": ["0"],
                "query_packets": [{"status": "success", "results": [raw]}],
                "coverage": [cached],
            }
        )
        self.assertTrue(observations[0]["coverage"]["truncated"])
        self.assertEqual("truncated", observations[0]["coverage"]["completeness"])

    def test_fabric_export_keeps_missing_result_truncation_evidence_missing(self):
        raw = result("0")
        raw.pop("truncated")
        observations = fabric.observations(
            {
                "expected_request_ids": ["0"],
                "query_packets": [{"status": "success", "results": [raw]}],
            }
        )
        self.assertIsNone(observations[0]["coverage"]["truncated"])
        self.assertFalse(observations[0]["coverage"]["report_complete"])

    def test_fabric_export_does_not_default_missing_result_status_or_data_state(self):
        raw = result("0")
        raw.pop("status")
        raw.pop("data_state")
        raw.pop("truncated")
        observation = fabric.observations(
            {
                "expected_request_ids": ["0"],
                "query_packets": [{"status": "success", "results": [raw]}],
            }
        )[0]
        self.assertEqual("missing_result_status", observation["coverage"]["status"])
        self.assertIsNone(observation["coverage"]["data_state"])
        self.assertIsNone(observation["coverage"]["truncated"])
        self.assertFalse(observation["coverage"]["report_complete"])

    def test_fabric_export_keeps_packet_failure_status(self):
        raw = result("0")
        observations = fabric.observations(
            {
                "expected_request_ids": ["0"],
                "query_packets": [{"status": "failed", "results": [raw]}],
            }
        )
        self.assertEqual("failed", observations[0]["coverage"]["status"])
        self.assertEqual("failed", observations[0]["_result"]["status"])

    def test_governed_operations_bind_ids_and_truncated_success_becomes_partial(self):
        calls: list[str] = []

        def handler(payload):
            request_id = payload["requests"][0]["request_id"]
            calls.append(request_id)
            return json.dumps(
                {
                    "status": "success",
                    "results": [
                        result(
                            request_id,
                            truncated=request_id == "2",
                            data_state="truncated" if request_id == "2" else "rows",
                            row_count=1,
                            population=2,
                        )
                    ],
                }
            )

        binding = {
            "kind": "fabric_review",
            "limit": 1,
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "inventory_scope": "total",
        }
        with patch.object(wire, "bounded_json_handler", return_value=handler):
            document = operations.execute_governed(Path("."), "fabric", binding, "scope")
        self.assertEqual([str(i) for i in range(6)], calls)
        self.assertEqual("partial", document["status"])
        self.assertEqual([str(i) for i in range(6)], document["expected_request_ids"])
        self.assertFalse(document["report_complete"])
        self.assertFalse(document["delivery_allowed"])
        self.assertIn("2:SOURCE_TRUNCATED", document["delivery_gate"]["reason_codes"])

    def test_complete_governed_document_is_the_only_delivery_allowed_shape(self):
        def handler(payload):
            request_id = payload["requests"][0]["request_id"]
            return json.dumps({"status": "success", "results": [result(request_id, row_count=1, population=1)]})

        binding = {
            "kind": "fabric_review",
            "limit": 1,
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "inventory_scope": "total",
        }
        with patch.object(wire, "bounded_json_handler", return_value=handler):
            document = operations.execute_governed(Path("."), "fabric", binding, "scope")
        self.assertEqual("success", document["status"])
        self.assertTrue(document["report_complete"])
        self.assertTrue(completeness.gate_for_document(document)["allowed"])
        document["status"] = "partial"
        self.assertFalse(completeness.gate_for_document(document)["allowed"])
        self.assertIn("DOCUMENT_STATUS_PARTIAL", completeness.gate_for_document(document)["reason_codes"])

    def test_governed_operations_reject_wrong_id_without_rebinding_it(self):
        def handler(payload):
            request_id = payload["requests"][0]["request_id"]
            returned_id = "wrong" if request_id == "0" else request_id
            return json.dumps({"status": "success", "results": [result(returned_id, row_count=1)]})

        binding = {
            "kind": "fabric_review",
            "limit": 1,
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "inventory_scope": "total",
        }
        with patch.object(wire, "bounded_json_handler", return_value=handler):
            document = operations.execute_governed(Path("."), "fabric", binding, "scope")
        self.assertEqual("partial", document["status"])
        self.assertEqual("missing_result", document["coverage"][0]["status"])
        self.assertFalse(completeness.gate_for_document(document)["allowed"])

    def test_excel_and_text_consumers_keep_same_coverage_and_identity(self):
        doc = {
            "status": "success",
            "query_packets": [
                {"status": "success", "results": [result("0", truncated=True, data_state="truncated")]},
                {"status": "success", "results": [result("1", row_count=1, population=1)]},
            ],
        }
        with TemporaryDirectory() as tmp:
            paths = fabric.export_report(doc, Path(tmp))
            workbook = Path(paths[0])
            with zipfile.ZipFile(workbook) as archive:
                content = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
            self.assertIn("completeness", content.decode("utf-8"))
            self.assertIn("product_abc123", content.decode("utf-8"))
        text = local_report.render_text({"query": {"results": [result("0", truncated=True, data_state="truncated")]}})
        self.assertIn("完整报告门槛：未通过", text)
        self.assertIn("返回分组：2", text)
        self.assertIn("总体分组：3", text)
        self.assertIn("2026-09-17T10:00:00", text)
        self.assertIn("product_abc123", text)
        failed_text = local_report.render_text(
            {"query": {"status": "partial", "results": [result("0")]}}
        )
        self.assertIn("完整报告门槛：未通过", failed_text)
        self.assertIn("DOCUMENT_STATUS_PARTIAL", failed_text)

    def test_each_observation_sheet_retains_its_own_truncation_marker(self):
        doc = {
            "observations": [
                {
                    "name": "出库总览",
                    "status": "success",
                    "data_state": "rows",
                    "truncated": True,
                    "observed_at": "2026-09-17T10:00:00",
                    "rows": [{"group": "A", "facts": {"metric_value": 1}}],
                }
            ]
        }
        with TemporaryDirectory() as tmp:
            workbook = Path(fabric.export_report(doc, Path(tmp))[0])
            with zipfile.ZipFile(workbook) as archive:
                sheet_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                all_xml = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".xml")).decode("utf-8")
            self.assertIn("truncated", sheet_xml)
            self.assertIn("1", sheet_xml)
            self.assertIn("RAW_QUERY_EVIDENCE_MISSING", all_xml)


    def test_declared_row_count_cannot_replace_delivered_rows(self):
        for count in (0, 1, 3, True, -1, "invalid"):
            with self.subTest(declared=count):
                raw = result()
                raw["row_count"] = count
                summary = completeness.summarize_result(raw)
                self.assertEqual(2, summary["returned_group_count"])
                self.assertFalse(summary["report_complete"])
                self.assertFalse(completeness.report_delivery_gate([raw])["allowed"])

    def test_raw_rows_are_required_and_malformed_containers_do_not_raise(self):
        for missing in (True, False):
            for raw_value in (None, 123, {}, "not rows", [{}, "broken"]):
                with self.subTest(missing=missing, raw=raw_value):
                    raw = result()
                    if missing:
                        raw.pop("rows")
                        raw["claim_ledger"] = raw_value
                    else:
                        raw["rows"] = raw_value
                    self.assertFalse(completeness.report_delivery_gate([raw])["allowed"])

    def test_raw_ledger_and_compact_wire_have_same_valid_coverage(self):
        raw = result()
        raw["claim_ledger"] = raw.pop("rows")
        self.assertTrue(completeness.report_delivery_gate([raw])["allowed"])
        no_count = result()
        no_count.pop("row_count")
        self.assertTrue(completeness.report_delivery_gate([no_count])["allowed"])
        string_count = result()
        string_count["row_count"] = "2"
        self.assertTrue(completeness.report_delivery_gate([string_count])["allowed"])

    def test_empty_state_cannot_hide_delivered_rows(self):
        raw = result(data_state="empty")
        self.assertFalse(completeness.report_delivery_gate([raw])["allowed"])
        raw.update(rows=[], row_count=0)
        self.assertNotIn("EMPTY_STATE_HAS_ROWS", completeness.summarize_result(raw).get("integrity_errors", []))
        # Existing policy still treats empty evidence as limited, not a complete report.
        self.assertFalse(completeness.report_delivery_gate([raw])["allowed"])

    def test_ranking_population_never_invents_a_source_row_count(self):
        raw = result()
        for row in raw["rows"]:
            row["facts"].pop("fabric_population_groups", None)
            row["facts"].pop("fabric_scope_rows", None)
        raw["ranking_evidence"] = {"population_count": 10}
        summary = completeness.summarize_result(raw)
        self.assertEqual(10, summary["population_group_count"])
        self.assertIsNone(summary["population_row_count"])
        raw["population_row_count"] = 200
        self.assertEqual(200, completeness.summarize_result(raw)["population_row_count"])

    def test_embedded_error_and_stale_packet_success_both_block_delivery(self):
        raw = result()
        raw["error"] = {"code": "SYNTHETIC_FAILURE"}
        self.assertFalse(completeness.report_delivery_gate([raw])["allowed"])
        doc = {
            "expected_request_ids": ["table"],
            "query_packets": [{"status": "success", "error": raw["error"], "results": [result()]}],
        }
        self.assertFalse(completeness.gate_for_document(doc)["allowed"])

    def test_blank_expected_id_is_not_a_valid_evidence_binding(self):
        doc = {
            "expected_request_ids": [" "],
            "query_packets": [{"status": "success", "results": [result(" ")]}],
        }
        self.assertFalse(completeness.gate_for_document(doc)["allowed"])


    def test_report_projects_raw_claim_ledger_without_erasing_rows(self):
        raw = result("only", row_count=1, population=1)
        raw["claim_ledger"] = raw.pop("rows")
        document = {"status": "success", "expected_request_ids": ["only"],
                    "query_packets": [{"status": "success", "results": [raw]}]}
        self.assertTrue(completeness.gate_for_document(document)["allowed"])
        item = fabric.observations(document)[0]
        self.assertEqual(1, len(item["rows"]))
        self.assertEqual("12", item["rows"][0]["facts"]["metric_value"])
        self.assertEqual(1, item["coverage"]["returned_group_count"])
        self.assertTrue(item["coverage"]["report_complete"])
        self.assertNotIn("rows", raw)  # projection never mutates upstream evidence

    def test_report_keeps_packet_error_and_missing_row_evidence(self):
        raw = result("only", row_count=1, population=1)
        document = {"expected_request_ids": ["only"], "query_packets": [
            {"status": "success", "error": {"code": "SYNTHETIC_FAILURE"}, "results": [raw]}]}
        self.assertFalse(fabric.observations(document)[0]["coverage"]["report_complete"])
        document["query_packets"][0].pop("error")
        raw.pop("rows")
        raw["row_count"] = 0
        item = fabric.observations(document)[0]
        self.assertIn("ROW_EVIDENCE_MISSING", item["coverage"]["integrity_errors"])
        self.assertFalse(item["coverage"]["report_complete"])


if __name__ == "__main__":
    unittest.main()
