"""Detail evidence integrity, source arithmetic and continuation boundaries."""
import copy
import importlib
from pathlib import Path
import sys
import types
import unittest

PACKAGE = "datasage_detail_evidence_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins/datasage-query")]
sys.modules[PACKAGE] = package
bridge = importlib.import_module(PACKAGE + ".detail_evidence")

FIELDS = ["target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb"]
CONTRACT = {"id": "target_department_groups", "requires_analysis": True,
            "required_dimensions": ["department"], "summable_fields": FIELDS,
            "default_page_size": 25, "max_page_size": 50, "max_pages": 20, "max_collection": 500}


class DetailEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.claims = [{"dimensions": [{"label": "部门", "value": name}],
                        "facts": dict(zip(FIELDS, [100.0, 50.0, 50.0])),
                        "states": {"analysis_match_state": "match"},
                        "unit": "比例", "currency": "CNY",
                        "fact_units": dict.fromkeys(FIELDS, "人民币元")}
                       for name in ["A", "B"]]
        proof = {"__detail_group_count": 2, "__detail_member_unknown_count": 0}
        for field, value in zip(FIELDS, [200.0, 100.0, 100.0]):
            proof["__detail_known_" + field] = value
            proof["__detail_unknown_" + field] = 0
        self.raw = [dict(proof), dict(proof)]

    def build(self, **overrides):
        args = dict(claims=self.claims, raw_rows=self.raw,
                    detail={"id": CONTRACT["id"], "limit": 1}, contract=CONTRACT,
                    collection_cap=100, collection_truncated=False,
                    binding={"principal": "actor", "sql": "bound compiler"},
                    period={"month": "2026-09"}, scope_fingerprint="scope",
                    projection_fingerprint="projection")
        args.update(overrides)
        return bridge.build_detail(**args)

    def result(self, metadata, claims):
        return {"claim_ledger": claims, "row_count": len(claims),
                "truncated": metadata["full_count"] > len(claims),
                "scope_fingerprint": "scope", "projection_fingerprint": "projection",
                "applied_time_range": {"month": "2026-09"}}

    def test_last_page_still_truncated_but_has_no_later_page(self):
        first = self.build()
        self.assertNotIn("rows", first)
        self.assertEqual("reconciled", first["source_reconciliation"]["status"])
        second = self.build(detail={"id": CONTRACT["id"], "limit": 1, "cursor": first["next_cursor"]})
        self.assertFalse(second["has_more"])
        self.assertTrue(bridge.metadata_is_valid(second, self.result(second, self.claims[1:])))
        self.assertEqual("200.0", second["reconciliation"]["full"]["matched_subtotals"][FIELDS[0]])
        self.assertEqual("100.0", second["reconciliation"]["page"]["matched_subtotals"][FIELDS[0]])

    def test_missing_unequal_source_proof_never_claims_reconciliation(self):
        self.raw[0]["__detail_known_target_amount_rmb"] = 201
        self.assertEqual("not_reconciled", self.build()["source_reconciliation"]["status"])
        self.raw[1].pop("__detail_known_target_amount_rmb")
        self.assertEqual("not_reconciled", self.build()["source_reconciliation"]["status"])
        for row in self.raw:
            row["__detail_known_target_amount_rmb"] = None
        self.assertEqual("not_reconciled", self.build()["source_reconciliation"]["status"])

    def test_unknown_members_have_no_confirmed_matched_total(self):
        for claim in self.claims:
            claim["states"]["analysis_match_state"] = "unknown"
        for row in self.raw:
            row["__detail_member_unknown_count"] = 2
            for field in FIELDS:
                row["__detail_known_" + field] = 0
                row["__detail_unknown_" + field] = 2
        metadata = self.build()
        self.assertNotIn("joint_scope_state", metadata)
        self.assertFalse(metadata["source_reconciliation"]["complete_matched_total_available"])
        self.assertTrue(all(value is None for value in metadata["reconciliation"]["full"]["matched_subtotals"].values()))

    def test_visible_fact_or_period_tampering_invalidates_detail(self):
        metadata = self.build()
        result = self.result(metadata, copy.deepcopy(self.claims[:1]))
        self.assertTrue(bridge.metadata_is_valid(metadata, result))
        result["claim_ledger"][0]["facts"][FIELDS[0]] = 999
        self.assertFalse(bridge.metadata_is_valid(metadata, result))
        result = self.result(metadata, self.claims[:1])
        result["applied_time_range"]["month"] = "2026-08"
        self.assertFalse(bridge.metadata_is_valid(metadata, result))

    def test_partial_read_clock_does_not_erase_business_window(self):
        period = {"window_start": "2026-09-01", "requested_window_end": "2026-10-01",
                  "window_coverage": "partial_to_read", "window_end": "2026-09-11T12:00:00",
                  "read_at": "2026-09-11T12:00:00", "baseline_frozen_at": "2026-09-01T10:00:00"}
        stable = bridge.stable_period(period)
        self.assertNotIn("read_at", stable)
        self.assertNotIn("window_end", stable)
        self.assertEqual(period["baseline_frozen_at"], stable["baseline_frozen_at"])
        self.assertEqual(period["requested_window_end"], stable["requested_window_end"])
