"""Static semantic guards for the A04 fact-operand boundary.

These tests use synthetic already-projected claims.  They do not import the
runtime composition root, connect to a database, call a model, or execute a
service.  The integration owner should run them through the existing guarded
test home after wiring ``tools``.
"""

from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
TEST_PACKAGE = "datasage_query_fact_operand_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package
facts = importlib.import_module(f"{TEST_PACKAGE}.fact_calculations")


def _result(
    request_id: str,
    *,
    dimensions: list[dict[str, object]] | None = None,
    group_dimensions: list[str] | None = None,
    facts_payload: dict[str, object] | None = None,
    fact_units: dict[str, str] | None = None,
    metric_unit: str = "卷",
    filter_scope: dict[str, object] | None = None,
    metric_basis: str = "basis_inventory",
    currency: str | None = None,
    states_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    applied_period = {"start": "2026-08-01", "end": "2026-09-01"}
    default_facts = {
        "metric_value": "10",
        "outbound_unknown_rows": "0",
        "returns_unknown_rows": "0",
        "gross_missing_roll_rows": "0",
        "return_missing_roll_rows": "0",
        "high_missing_price_rows": "0",
        "high_missing_roll_rows": "0",
    }
    fact_payload = {**default_facts, **(facts_payload or {})}
    claim: dict[str, object] = {
        "claim_id": f"claim_{request_id}",
        "claim_seal": "sha256_" + request_id * 64,
        "request_id": request_id,
        "metric_ref": "inventory_flow",
        "dimensions": dimensions or [],
        "scope_entities": [],
        "period": applied_period,
        "scope_fingerprint": f"scope_{request_id}",
        "projection_fingerprint": f"projection_{request_id}",
        "unit": metric_unit,
        "currency": currency,
        "currency_state": "identified" if currency else None,
        "facts": fact_payload,
        "fact_units": fact_units or {"net_rolls": "卷", "gross_rolls": "卷", "return_rolls": "卷", "high_net_rolls": "卷"},
        "states": states_payload or {},
        "source_truncated": False,
        "allowed_relations": ["observation"],
    }
    return {
        "request_id": request_id,
        "status": "success",
        "data_state": "rows",
        "claim_ledger": [claim],
        "row_count": 1,
        "truncated": False,
        "_calculation_scope": {
            "version": "governed-calculation-scope/v1",
            "metric_basis_fingerprint": metric_basis,
            "filter_scope": filter_scope or {},
            "group_dimensions": group_dimensions or [],
            "share_partition_dimensions": [],
        },
    }


def _operand(result: dict[str, object], field: str | None = None) -> facts.FactOperand:
    claim = result["claim_ledger"][0]
    return facts.resolve_fact_operand(
        request_id=result["request_id"],
        result=result,
        claim=claim,
        field=field,
    )


class AnalysisFactOperandTests(unittest.TestCase):
    def test_metric_value_remains_the_default_scalar_fact(self) -> None:
        result = _result(
            "scalar",
            facts_payload={"metric_value": "10.50"},
            metric_unit="人民币元",
            fact_units={},
        )
        operand = _operand(result)
        self.assertEqual("metric_value", operand.field)
        self.assertEqual(Decimal("10.50"), operand.value)
        self.assertEqual("人民币元", operand.unit)

    def test_inventory_secondary_facts_are_selectable_from_one_unit_group(self) -> None:
        result = _result(
            "unit_m",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={
                "metric_value": "437",
                "net_rolls": "437",
                "gross_rolls": "450",
                "return_rolls": "13",
                "high_net_rolls": "437",
            },
        )
        gross = _operand(result, "gross_rolls")
        returned = _operand(result, "return_rolls")
        difference = facts.validate_fact_operation("difference", gross, returned)
        self.assertEqual("gross_rolls", difference["left_field"])
        self.assertEqual("return_rolls", difference["right_field"])
        self.assertEqual(Decimal("450"), gross.value)
        self.assertEqual(Decimal("13"), returned.value)

    def test_unknown_or_repeated_inventory_facts_fail_closed(self) -> None:
        result = _result(
            "unit_m",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={"metric_value": None, "net_rolls": None},
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result, "net_rolls")
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(
                _result(
                    "scope",
                    dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
                    group_dimensions=["unit"],
                    facts_payload={"scope_net_rolls": "437"},
                ),
                "scope_net_rolls",
            )
        self.assertEqual("CALCULATION_FACT_FIELD_UNSUPPORTED", error.exception.code)

    def test_unknown_return_blocks_net_but_not_complete_gross_slice(self) -> None:
        result = _result(
            "return-unknown",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={
                "gross_rolls": "450",
                "net_rolls": "437",
                "returns_unknown_rows": "1",
                "return_missing_roll_rows": "1",
                "metric_data_state": "incomplete",
            },
        )
        self.assertEqual(Decimal("450"), _operand(result, "gross_rolls").value)
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result, "net_rolls")
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_production_analysis_net_state_unknown_does_not_block_gross_slice(self) -> None:
        result = _result(
            "analysis-return-unknown",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={
                "gross_rolls": "1",
                "net_rolls": "1",
                "analysis_gross_rolls": "1",
                "analysis_known_gross_rolls": "1",
                "analysis_unknown_rows": "0",
                "analysis_missing_roll_rows": "0",
                "analysis_return_rows": "1",
                # A non-matching parent row may have a missing roll; it does
                # not poison the selected analysis slice's gross coverage.
                "gross_missing_roll_rows": "4",
            },
            states_payload={"analysis_net_state": "unknown_return_attribution"},
        )
        self.assertEqual(Decimal("1"), _operand(result, "gross_rolls").value)
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result, "net_rolls")
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_analysis_quantity_gap_does_not_poison_known_rolls(self) -> None:
        result = _result(
            "analysis-quantity-gap",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={
                "gross_rolls": "1",
                "analysis_unknown_rows": "0",
                "analysis_missing_roll_rows": "0",
                "analysis_missing_quantity_rows": "3",
                "analysis_return_rows": "0",
            },
            states_payload={"analysis_net_state": "known"},
        )
        self.assertEqual(Decimal("1"), _operand(result, "gross_rolls").value)

    def test_none_states_is_legacy_empty_but_typed_fact_requires_state(self) -> None:
        legacy = _result("legacy-none", facts_payload={"metric_value": "1"})
        legacy["claim_ledger"][0]["states"] = None
        self.assertEqual(Decimal("1"), _operand(legacy).value)

        typed = _result(
            "typed-none",
            facts_payload={"target_amount_rmb": "1"},
            fact_units={"target_amount_rmb": "人民币元"},
            metric_unit="比例",
        )
        typed["claim_ledger"][0]["states"] = None
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(typed, "target_amount_rmb")
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_different_unit_groups_cannot_be_combined(self) -> None:
        left = _operand(
            _result(
                "m",
                dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
                group_dimensions=["unit"],
                facts_payload={"gross_rolls": "450"},
            ),
            "gross_rolls",
        )
        right = _operand(
            _result(
                "y",
                dimensions=[{"code": "unit", "label": "库存单位", "value": "y"}],
                group_dimensions=["unit"],
                facts_payload={"return_rolls": "13"},
            ),
            "return_rolls",
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            facts.validate_fact_operation("difference", left, right)
        self.assertEqual("CALCULATION_SCOPE_MISMATCH", error.exception.code)

    def test_high_net_is_never_promoted_to_a_share(self) -> None:
        result = _result(
            "unit_m",
            dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
            group_dimensions=["unit"],
            facts_payload={"high_net_rolls": "437", "net_rolls": "451"},
        )
        high = _operand(result, "high_net_rolls")
        net = _operand(result, "net_rolls")
        with self.assertRaises(facts.FactCalculationError) as error:
            facts.validate_fact_operation("share", high, net)
        self.assertEqual("CALCULATION_SUBSET_NOT_PROVEN", error.exception.code)

    def test_target_fields_require_registered_typed_units(self) -> None:
        result = _result(
            "target",
            group_dimensions=["department"],
            facts_payload={
                "target_amount_rmb": "1000",
                "actual_amount_rmb": "800",
                "gap_amount_rmb": "200",
                "completion_rate": "0.8",
            },
            fact_units={
                "target_amount_rmb": "人民币元",
                "actual_amount_rmb": "人民币元",
                "gap_amount_rmb": "人民币元",
                "completion_rate": "比例",
            },
            metric_unit="比例",
            states_payload={
                "target_data_state": "set",
                "actual_data_state": "reported",
                "source_scope_state": "complete",
            },
        )
        # A grouped target claim must carry its department identity, even when
        # target facts themselves are complete.
        result["claim_ledger"][0]["dimensions"] = [
            {
                "code": "department",
                "label": "部门",
                "value": "HCM",
                "identity_state": "identified",
            }
        ]
        target = _operand(result, "target_amount_rmb")
        actual = _operand(result, "actual_amount_rmb")
        self.assertEqual("rmb", target.unit_family)
        self.assertEqual(
            Decimal("200"),
            facts.resolve_fact_operand(
                request_id="target",
                result=result,
                claim=result["claim_ledger"][0],
                field="gap_amount_rmb",
            ).value,
        )
        self.assertTrue(facts.validate_fact_operation("difference", target, actual)["same_group_identity"])

    def test_target_group_rejects_display_only_identity(self) -> None:
        result = _result(
            "target",
            group_dimensions=["department"],
            dimensions=[
                {
                    "code": "department",
                    "label": "部门",
                    "value": "A",
                    "display_only": True,
                }
            ],
            facts_payload={"target_amount_rmb": "100"},
            fact_units={"target_amount_rmb": "人民币元"},
            states_payload={"target_data_state": "set"},
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result, "target_amount_rmb")
        self.assertEqual("CALCULATION_GROUP_IDENTITY_UNVERIFIED", error.exception.code)

    def test_target_can_survive_incomplete_actual_scope_but_actual_outputs_cannot(self) -> None:
        states = {
            "target_data_state": "set",
            "actual_data_state": "reported",
            "source_scope_state": "source_range_incomplete",
        }
        target_result = _result(
            "target",
            facts_payload={"target_amount_rmb": "100"},
            fact_units={"target_amount_rmb": "人民币元"},
            states_payload=states,
            metric_unit="比例",
        )
        self.assertEqual(Decimal("100"), _operand(target_result, "target_amount_rmb").value)
        for field, value in (
            ("actual_amount_rmb", "80"),
            ("gap_amount_rmb", "20"),
            ("completion_rate", "0.8"),
        ):
            with self.subTest(field=field):
                result = _result(
                    "incomplete",
                    facts_payload={field: value},
                    fact_units={field: "人民币元" if field != "completion_rate" else "比例"},
                    states_payload=states,
                    metric_unit="比例",
                )
                with self.assertRaises(facts.FactCalculationError) as error:
                    _operand(result, field)
                self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_analysis_unknown_members_cannot_be_selected_as_known_facts(self) -> None:
        result = _result(
            "analysis-unknown",
            facts_payload={"metric_value": "10", "analysis_unknown_count": "1"},
            metric_unit="卷",
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result)
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_usd_unit_does_not_satisfy_rmb_fact_registration(self) -> None:
        result = _result(
            "usd",
            facts_payload={"target_amount_rmb": "100"},
            fact_units={"target_amount_rmb": "美元元"},
            states_payload={"target_data_state": "set"},
            metric_unit="比例",
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            _operand(result, "target_amount_rmb")
        self.assertEqual("CALCULATION_FACT_UNIT_MISMATCH", error.exception.code)

    def test_grouped_secondary_facts_do_not_cross_requests(self) -> None:
        left = _operand(
            _result(
                "left",
                dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
                group_dimensions=["unit"],
                facts_payload={"gross_rolls": "450"},
            ),
            "gross_rolls",
        )
        right = _operand(
            _result(
                "right",
                dimensions=[{"code": "unit", "label": "库存单位", "value": "m"}],
                group_dimensions=["unit"],
                facts_payload={"return_rolls": "13"},
            ),
            "return_rolls",
        )
        with self.assertRaises(facts.FactCalculationError) as error:
            facts.validate_fact_operation("difference", left, right)
        self.assertEqual("CALCULATION_SCOPE_MISMATCH", error.exception.code)

    def test_source_exact_identity_preserves_case_in_group_fingerprint(self) -> None:
        upper = _operand(
            _result(
                "upper",
                dimensions=[
                    {
                        "code": "department",
                        "label": "部门",
                        "value": "A",
                        "identity_state": "identified",
                        "entity_ref": "dept:A",
                    }
                ],
                group_dimensions=["department"],
                facts_payload={"target_amount_rmb": "100"},
                fact_units={"target_amount_rmb": "人民币元"},
                states_payload={"target_data_state": "set"},
                metric_unit="比例",
            ),
            "target_amount_rmb",
        )
        lower = _operand(
            _result(
                "lower",
                dimensions=[
                    {
                        "code": "department",
                        "label": "部门",
                        "value": "a",
                        "identity_state": "identified",
                        "entity_ref": "dept:a",
                    }
                ],
                group_dimensions=["department"],
                facts_payload={"target_amount_rmb": "100"},
                fact_units={"target_amount_rmb": "人民币元"},
                states_payload={"target_data_state": "set"},
                metric_unit="比例",
            ),
            "target_amount_rmb",
        )
        self.assertNotEqual(upper.group_fingerprint, lower.group_fingerprint)

    def test_analysis_scope_must_be_kept_by_owner(self) -> None:
        # This helper consumes the already-bound calculation scope; it does not
        # infer or rewrite analysis predicates.  Distinct basis fingerprints
        # remain visible to the existing tools scope check.
        left = _operand(
            _result(
                "left",
                facts_payload={"metric_value": "10"},
                metric_unit="卷",
                metric_basis="basis_analysis_before",
                filter_scope={"analysis": "before"},
            )
        )
        right = _operand(
            _result(
                "right",
                facts_payload={"metric_value": "5"},
                metric_unit="卷",
                metric_basis="basis_analysis_after",
                filter_scope={"analysis": "after"},
            )
        )
        self.assertNotEqual(left.metric_basis_fingerprint, right.metric_basis_fingerprint)
        self.assertNotEqual(left.filter_fingerprint, right.filter_fingerprint)

    def test_visible_fact_rejects_resealed_value_tampering(self) -> None:
        result = _result(
            "visible-target",
            facts_payload={"target_amount_rmb": "100"},
            fact_units={"target_amount_rmb": "人民币元"},
            states_payload={"target_data_state": "set"},
            metric_unit="比例",
        )
        claim = result["claim_ledger"][0]
        operand = _operand(result, "target_amount_rmb").as_mapping()
        operand["value"] = "999"
        with self.assertRaises(facts.FactCalculationError) as error:
            facts.validate_visible_fact(operand, claim, result)
        self.assertEqual("CALCULATION_SOURCE_INTEGRITY_INVALID", error.exception.code)

    def test_visible_fact_rejects_actual_with_incomplete_source_scope(self) -> None:
        result = _result(
            "visible-actual",
            facts_payload={"actual_amount_rmb": "80"},
            fact_units={"actual_amount_rmb": "人民币元"},
            states_payload={
                "actual_data_state": "reported",
                "source_scope_state": "source_scope_unverifiable",
            },
            metric_unit="比例",
        )
        claim = result["claim_ledger"][0]
        operand = {
            "field": "actual_amount_rmb",
            "value": "80",
            "unit": "人民币元",
            "field_unit": "人民币元",
            "group_dimensions": [],
            "group_fingerprint": facts.group_identity_fingerprint(claim, []),
        }
        with self.assertRaises(facts.FactCalculationError) as error:
            facts.validate_visible_fact(operand, claim, result)
        self.assertEqual("CALCULATION_VALUE_UNAVAILABLE", error.exception.code)

    def test_visible_fact_keeps_legacy_metric_operand_shape(self) -> None:
        result = _result("legacy-visible", facts_payload={"metric_value": "10"})
        claim = result["claim_ledger"][0]
        visible = facts.validate_visible_fact(
            {"value": "10", "unit": "卷"},
            claim,
            result,
        )
        self.assertEqual("metric_value", visible["field"])
        self.assertEqual("10", visible["value"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
