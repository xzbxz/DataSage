from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
sys.path.insert(0, str(PROFILE_ROOT / "tests"))

import test_business_contracts as tb  # noqa: E402


def _attach_valid_disclosure(result: dict[str, object]) -> None:
    disclosure = {
        "disclosure_id": "remediation.scope",
        "contract_version": "metric-disclosure/v1",
        "request_id": result["request_id"],
        "metric_ref": result["business_metric_ref"],
        "scope_fingerprint": result["scope_fingerprint"],
        "projection_fingerprint": result["projection_fingerprint"],
        "mode": "required_always",
        "order": 0,
        "text": "governed remediation scope",
        "applies": True,
    }
    item = json.dumps(
        disclosure,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    disclosure["disclosure_seal"] = "sha256_" + hashlib.sha256(item).hexdigest()
    result["disclosure_contract_version"] = "metric-disclosure-ledger/v1"
    result["disclosure_ledger"] = [disclosure]
    ledger = json.dumps(
        {
            "contract_version": result["disclosure_contract_version"],
            "request_id": result["request_id"],
            "metric_ref": result["business_metric_ref"],
            "scope_fingerprint": result["scope_fingerprint"],
            "projection_fingerprint": result["projection_fingerprint"],
            "ledger": result["disclosure_ledger"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    result["disclosure_ledger_seal"] = "sha256_" + hashlib.sha256(ledger).hexdigest()


class EvidenceIntegrityRemediationTests(unittest.TestCase):
    def _target_gap(self):
        case = tb.BusinessContractTests()
        contexts, results, operations = case._synthetic_target_gap_inputs()
        tb.tools._finalize_target_gap_decompositions(contexts, results, operations)
        _attach_valid_disclosure(results[0])
        _attach_valid_disclosure(results[1])
        return contexts, results

    def test_valid_target_gap_requires_full_context_and_survives_projection(self):
        contexts, results = self._target_gap()
        overall, partition = results
        self.assertFalse(
            tb.tools.evidence._target_gap_reconciliation_is_valid(partition)
        )
        self.assertTrue(
            tb.tools.evidence._target_gap_reconciliation_is_valid(
                partition,
                request=contexts[1]["request"],
                overall_result=overall,
            )
        )
        projected = tb.tools._model_wire_result(
            partition,
            request=contexts[1]["request"],
            overall_result=overall,
        )
        self.assertIsNone(projected.get("error"))
        self.assertIn("target_gap_reconciliation", projected)

    def test_target_gap_binding_mutations_fail_closed_on_model_wire(self):
        contexts, results = self._target_gap()
        overall, base = results
        mutations = {
            "partition_request_id": "other-partition",
            "overall_request_id": "other-overall",
            "population_fingerprint": "other-population",
            "partition_projection_fingerprint": "other-projection",
            "dimension": "other-dimension",
            "snapshot_consistency": "different-snapshot-proof",
            "proof_mode": "different-proof-mode",
            "full_partition_row_count": 999,
            "completion_rate_basis": "different-completion-basis",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                partition = copy.deepcopy(base)
                receipt = partition["target_gap_reconciliation"]
                receipt[field] = value
                receipt["reconciliation_id"] = (
                    tb.tools.evidence._canonical_reconciliation_id(receipt)
                )
                projected = tb.tools._model_wire_result(
                    partition,
                    request=contexts[1]["request"],
                    overall_result=overall,
                )
                self.assertNotIn("target_gap_reconciliation", projected)
                self.assertEqual([], projected["claim_ledger"])
                self.assertEqual(
                    "EVIDENCE_INTEGRITY_INVALID",
                    projected["error"]["code"],
                )

    def test_target_gap_result_scope_bindings_fail_closed(self):
        contexts, results = self._target_gap()
        overall, base = results
        mutations = {
            "business_metric_ref": "metric_other",
            "business_metric_unit": "other-unit",
            "applied_time_range": {
                "start": "2026-07-01",
                "end": "2026-08-01",
            },
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                partition = copy.deepcopy(base)
                partition[field] = value
                projected = tb.tools._model_wire_result(
                    partition,
                    request=contexts[1]["request"],
                    overall_result=overall,
                )
                self.assertNotIn("target_gap_reconciliation", projected)
                self.assertEqual(
                    "EVIDENCE_INTEGRITY_INVALID",
                    projected["error"]["code"],
                )

    def test_target_gap_resealed_overall_scope_mutations_fail_closed(self):
        contexts, results = self._target_gap()
        overall, partition = results
        mutations = {
            "business_metric_ref": "metric_other",
            "business_metric_unit": "other-unit",
            "applied_time_range": {
                "start": "2026-07-01",
                "end": "2026-08-01",
            },
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                mutated_overall = copy.deepcopy(overall)
                mutated_overall[field] = value
                claim = mutated_overall["claim_ledger"][0]
                if field == "business_metric_ref":
                    claim["metric_ref"] = value
                elif field == "applied_time_range":
                    claim["period"] = value
                tb.tools.evidence.seal_claim(claim)
                self.assertTrue(
                    tb.tools.evidence.claim_is_valid_for_result(
                        claim,
                        mutated_overall,
                    )
                )
                self.assertFalse(
                    tb.tools.evidence._target_gap_reconciliation_is_valid(
                        partition,
                        request=contexts[1]["request"],
                        overall_result=mutated_overall,
                    )
                )

    def test_zero_target_allows_undefined_metric_only_with_complete_receipt(self):
        case = tb.BusinessContractTests()
        contexts, results, operations = case._synthetic_target_gap_inputs()
        for result in results:
            result["data_state"] = "undefined"
            for claim in result["claim_ledger"]:
                facts = claim["facts"]
                facts.update(
                    {
                        "target_amount_rmb": "0",
                        "actual_amount_rmb": "0",
                        "gap_amount_rmb": "0",
                        "completion_rate": None,
                        "metric_value": None,
                    }
                )
                claim["states"]["target_data_state"] = "zero"
                tb.tools.evidence.seal_claim(claim)
            result["row_count"] = len(result["claim_ledger"])
        tb.tools._finalize_target_gap_decompositions(contexts, results, operations)
        overall, partition = results
        _attach_valid_disclosure(overall)
        _attach_valid_disclosure(partition)
        self.assertEqual("reconciled", partition["target_gap_reconciliation"]["status"])
        self.assertTrue(
            tb.tools.evidence._target_gap_reconciliation_is_valid(
                partition,
                request=contexts[1]["request"],
                overall_result=overall,
            )
        )
        projected = tb.tools._model_wire_result(
            partition,
            request=contexts[1]["request"],
            overall_result=overall,
        )
        self.assertIsNone(projected.get("error"))
        self.assertIn("target_gap_reconciliation", projected)

    def test_ordinary_undefined_or_incomplete_target_gap_stays_fail_closed(self):
        contexts, results = self._target_gap()
        overall, base = results
        for data_state in ("undefined", "incomplete"):
            with self.subTest(data_state=data_state):
                partition = copy.deepcopy(base)
                partition["data_state"] = data_state
                projected = tb.tools._model_wire_result(
                    partition,
                    request=contexts[1]["request"],
                    overall_result=overall,
                )
                self.assertNotIn("target_gap_reconciliation", projected)
                self.assertEqual(
                    "EVIDENCE_INTEGRITY_INVALID",
                    projected["error"]["code"],
                )

    def test_snapshot_claim_period_must_equal_outer_period(self):
        _payload, raw, _calls = tb.BusinessContractTests._run_snapshot_change_operation()
        result = copy.deepcopy(raw["inventory_snapshot_partition"])
        claim = result["claim_ledger"][0]
        claim["period"] = {
            "current": {"source": "latest_snapshot", "snapshot_month": "1900-01"},
            "comparison": {
                "source": "latest_snapshot_offset",
                "months_before": 1,
                "snapshot_month": "1899-12",
            },
            "comparison_compatibility": {"status": "compatible", "reason_codes": []},
        }
        tb.tools.evidence.seal_claim(claim)
        self.assertFalse(tb.tools.evidence.claim_is_valid_for_result(claim, result))

    def test_formal_dso_validator_recomputes_formula(self):
        facts = {
            "metric_value": "42.00",
            "average_net_debt_rmb": "100.00",
            "same_period_gross_delivery_rmb": "900.00",
            "period_natural_days": 365,
            "snapshot_month_count": 13,
            "effective_month_count": "12",
        }
        self.assertFalse(
            tb.tools._formal_dso_attested_components_are_valid(
                facts,
                facts=facts,
                applied_time_range={"start": "2025-08-01", "end": "2026-08-01"},
            )
        )
        facts["metric_value"] = "40.5555555556"
        self.assertTrue(
            tb.tools._formal_dso_attested_components_are_valid(
                facts,
                facts=facts,
                applied_time_range={"start": "2025-08-01", "end": "2026-08-01"},
            )
        )

    def test_missing_comparison_states_do_not_authorize_comparison(self):
        facts = {
            "metric_value": "100",
            "comparison_value": "80",
            "delta_value": "20",
        }
        self.assertFalse(
            tb.tools._comparison_is_complete(
                facts,
                {},
                require_state_evidence=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
