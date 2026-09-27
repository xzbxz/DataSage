"""Guarded public execution probe for the corrected HCM inventory v3 oracle.

The v1 oracle remains available as an audit erratum and is never imported as a
golden for these assertions.  This probe assembles only the corrected two-row
base example, calls the public metric API through the existing SQLite harness,
and checks the fixed 451/437 values plus the <=50% slice.  Boundary, invalid
DDP, zero-return, signed, and threshold holdout inputs are separate extension
fixtures and cannot contaminate the base aggregate.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import unittest

import test_remediation_remaining_cases as _remaining
import test_slow_baseline_net_outbound as _baseline_net


HERE = Path(__file__).resolve().parent / "fixtures"
ORACLE = json.loads((HERE / "phase1-inventory-v3-oracle.json").read_text(encoding="utf-8"))
BASE = ORACLE["base_fixture"]


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AssertionError(f"invalid numeric fact {value!r}") from error
    if not result.is_finite():
        raise AssertionError(f"non-finite numeric fact {value!r}")
    return result


def _facts(row: dict) -> dict:
    facts = row.get("facts")
    return facts if isinstance(facts, dict) else row


def _states(row: dict) -> dict:
    states = row.get("states")
    return states if isinstance(states, dict) else {}


_LABELS = {
    "产品": "product",
    "商品": "product",
    "库存单位": "unit",
    "单位": "unit",
    "仓库部门": "warehouse_department",
    "客户部门": "department",
    "业务员": "salesperson",
    "product": "product",
    "unit": "unit",
    "warehouse_department": "warehouse_department",
    "department": "department",
    "salesperson": "salesperson",
}


def _dimensions(row: dict) -> dict[str, object]:
    result: dict[str, object] = {}
    values = row.get("dimensions")
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            key = item.get("name", item.get("code", item.get("field", item.get("dimension", item.get("label")))))
            if isinstance(key, str):
                result[_LABELS.get(key, key)] = item.get("value")
    return result


def _product(row: dict, id_map: dict[str, str]) -> str | None:
    dims = _dimensions(row)
    value = dims.get("product")
    if value is None:
        value = row.get("product", row.get("goods_id"))
    if value is None:
        return None
    return id_map.get(str(value), str(value))


def _rows(result: dict, id_map: dict[str, str]) -> dict[str, dict]:
    raw = result.get("rows")
    if not isinstance(raw, list):
        raise AssertionError(f"public rows missing: {result!r}")
    output: dict[str, dict] = {}
    for row in raw:
        if isinstance(row, dict) and _product(row, id_map) is not None:
            output[_product(row, id_map)] = row
    return output


def _first(row: dict, *names: str) -> object:
    facts = _facts(row)
    states = _states(row)
    for name in names:
        if name in facts:
            return facts[name]
        if name in row:
            return row[name]
        if name in states:
            return states[name]
    return None


def _fact_values(result: dict, *names: str) -> list[object]:
    values: list[object] = []
    rows = result.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                facts = row.get("facts")
                if isinstance(facts, dict):
                    for name in names:
                        if name in facts:
                            values.append(facts[name])
    claims = result.get("claim_ledger")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict) and isinstance(claim.get("facts"), dict):
                for name in names:
                    if name in claim["facts"]:
                        values.append(claim["facts"][name])
    return values


def _recursive_values(value: object, key: str) -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(_recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_recursive_values(child, key))
    return found


def _result(payload: dict) -> dict:
    if payload.get("status") not in {"success", "partial"}:
        raise AssertionError(f"public inventory query failed: {payload!r}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise AssertionError(f"unexpected public envelope: {payload!r}")
    result = results[0]
    if not isinstance(result, dict) or result.get("status") not in {None, "success", "partial"}:
        raise AssertionError(f"unexpected request result: {result!r}")
    return result


class InventoryV3ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _baseline_net.BaselineNetTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.load_base_fixture()

    def load_base_fixture(self) -> None:
        self.harness.conn.execute("DELETE FROM vk_ai.slow_moving_baseline")
        self.harness.baseline([
            (1, 1, 1, 101, "m", 1000, 450, "2026-09-01T00:00:00"),
            (2, 2, 2, 102, "m", 1000, 14, "2026-09-01T00:00:00"),
        ])
        # Baseline helper defaults goods_name to "Product".  The public
        # product dimension must come from the frozen baseline source fact,
        # never from hidden goods_id or a current master lookup.
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET goods_name='P-HIGH' WHERE goods_id=1")
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET goods_name='P-LOW' WHERE goods_id=2")
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM'")
        self.harness.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM'")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_return_detail_dwd")
        self.harness.outgoing(qty=10, rolls=450, dept="HCM", unit="m", when="2026-09-09T12:00:00", whse=1, goods=1, sku=101, deal_price=80, ddp_price=100)
        self.harness.outgoing(qty=10, rolls=14, dept="HCM", unit="m", when="2026-09-09T12:00:00", whse=1, goods=2, sku=102, deal_price=50, ddp_price=100)
        # The return is sourced under the low product key but has no historical
        # price-band value. Legacy high-net subtracts all qualified returns;
        # the new <=50% slice must keep its net unknown.
        self.harness.returning(qty=10, rolls=13, dept="HCM", unit="m", when="2026-09-10T12:00:00", whse=1, goods=2, sku=102)

    def load_extension_fixture(self) -> dict[str, str]:
        self.harness.conn.execute("DELETE FROM vk_ai.slow_moving_baseline")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_return_detail_dwd")
        rows = ORACLE["extension_fixtures"]["threshold_and_price_validity"]["rows"]
        id_map: dict[str, str] = {}
        baseline = []
        for index, row in enumerate(rows, 1):
            id_map[str(index)] = row["id"]
            baseline.append((index, index, index, 100 + index, "m", 1000, int(row["gross_rolls"]), "2026-09-01T00:00:00"))
        self.harness.baseline(baseline)
        for index, row in enumerate(rows, 1):
            self.harness.conn.execute(
                "UPDATE vk_ai.slow_moving_baseline SET goods_name=? WHERE goods_id=?",
                (row["id"], index),
            )
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM'")
        self.harness.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM'")
        for index, row in enumerate(rows, 1):
            self.harness.outgoing(
                qty=10,
                rolls=int(row["gross_rolls"]),
                dept="HCM",
                unit="m",
                when="2026-09-01T12:00:00",
                whse=1,
                goods=index,
                sku=100 + index,
                deal_price=float(row["deal_price"]),
                ddp_price=None if row.get("ddp_price") is None else float(row["ddp_price"]),
            )
        return id_map

    def load_negative_fixture(self) -> dict[str, str]:
        self.harness.conn.execute("DELETE FROM vk_ai.slow_moving_baseline")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd")
        self.harness.conn.execute("DELETE FROM vk_dwd.delivery_return_detail_dwd")
        self.harness.baseline([(1, 1, 1, 101, "m", 1000, 0, "2026-09-01T00:00:00")])
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET goods_name='E-NEGATIVE' WHERE goods_id=1")
        self.harness.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM'")
        self.harness.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM'")
        self.harness.outgoing(qty=10, rolls=0, dept="HCM", unit="m", when="2026-09-01T12:00:00", whse=1, goods=1, sku=101, deal_price=80, ddp_price=100)
        self.harness.returning(qty=10, rolls=2, dept="HCM", unit="m", when="2026-09-01T12:00:00", whse=1, goods=1, sku=101)
        return {"1": "E-NEGATIVE"}

    def request(self, *, analysis: bool = True, limit: int = 1) -> dict:
        payload = ORACLE["public_requests"]["analysis" if analysis else "legacy"]
        request = _remaining.metric(
            payload["metric"],
            payload["domain"],
            request_id="v3",
            month=None,
            baseline_week=payload["baseline_week"],
            time_range=payload["time_range"],
            dimensions=payload["dimensions"],
            metric_filters={"warehouse_department": "HCM"},
            limit=limit,
        )
        if analysis:
            request["analysis"] = payload["analysis"]
        return request

    def query_result(self, *, analysis: bool = True, limit: int = 1) -> dict:
        return _result(self.harness.query(self.request(analysis=analysis, limit=limit)))

    def test_v3_fixture_is_separate_from_v1_and_extensions(self) -> None:
        self.assertEqual(2, len(BASE["outbound_rows"]))
        self.assertEqual(1, len(BASE["return_rows"]))
        self.assertEqual("451", BASE["legacy_expected"]["ordinary_net_rolls"])
        self.assertEqual("437", BASE["legacy_expected"]["high_net_rolls"])
        self.assertEqual(6, len(ORACLE["extension_fixtures"]["threshold_and_price_validity"]["rows"]))
        self.assertEqual("f0c39fcf78b07d62f8780938652bcf409e88a13b", ORACLE["frozen_basis"]["candidate_baseline_commit"])
        self.assertTrue(ORACLE["errata"]["v1_preserved"])

    def test_catalog_exposes_price_slice_field(self) -> None:
        payload = self.harness.invoke(
            "datasage_catalog",
            {"requests": [{"domain": "inventory", "metric": "registered_slow_pool_baseline_net_outbound"}]},
        )
        self.assertEqual("success", payload.get("status"), payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("price_to_ddp_ratio", serialized)
        self.assertIn("analysis_fields", serialized)
        self.assertNotIn("private_source_column", serialized)
        self.assertNotIn("vk_ai.", serialized)

    def test_legacy_main_is_451_and_437(self) -> None:
        result = self.query_result(analysis=False, limit=10)
        expected = BASE["legacy_expected"]
        ordinary = [_decimal(value) for value in _fact_values(result, "scope_net_rolls", "net_rolls", "metric_value") if value is not None]
        high = [_decimal(value) for value in _fact_values(result, "scope_high_net_rolls", "high_net_rolls") if value is not None]
        self.assertIn(_decimal(expected["ordinary_net_rolls"]), ordinary, result)
        self.assertIn(_decimal(expected["high_net_rolls"]), high, result)
        product_rows = _rows(result, {"1": "P-HIGH", "2": "P-LOW"})
        self.assertEqual({"P-HIGH", "P-LOW"}, set(product_rows))
        # The scope gross is the sum of complete same-unit product groups;
        # scope metadata may repeat a total per row, so never sum scope_*.
        product_gross = [
            _decimal(_first(row, "gross_rolls", "gross_known_rolls"))
            for row in product_rows.values()
        ]
        self.assertTrue(all(value is not None for value in product_gross), product_rows)
        self.assertEqual(_decimal(expected["ordinary_gross_rolls"]), sum(product_gross, Decimal("0")))
        self.assertFalse(result.get("truncated", False))
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn("2026-09-01", serialized)

    def test_analysis_selects_low_gross_and_keeps_net_unknown(self) -> None:
        result = self.query_result(analysis=True, limit=1)
        rows = _rows(result, {"1": "P-HIGH", "2": "P-LOW"})
        self.assertIn("P-LOW", rows, result)
        self.assertNotIn("P-HIGH", rows)
        low = rows["P-LOW"]
        self.assertEqual(_decimal("14"), _decimal(_first(low, "gross_rolls", "gross_known_rolls")))
        self.assertIsNone(_first(low, "net_rolls", "metric_value"))
        state = json.dumps({"facts": _facts(low), "states": _states(low)}, ensure_ascii=False)
        self.assertRegex(state.lower(), "unknown|incomplete|partial|missing")
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        for required in ("price_to_ddp_ratio", "HCM", "2026-09-01", "2026-10-01"):
            self.assertIn(required, serialized)
        sql = "\n".join(str(item.get("sql", "")) for item in self.harness.sql_trace)
        self.assertIn("deal_price", sql)
        self.assertIn("ddp_price", sql)
        self.assertIn("LIMIT", sql.upper())

    def test_leaveout_changes_only_new_slice_and_keeps_legacy_facts(self) -> None:
        base_legacy = self.query_result(analysis=False, limit=10)
        base_slice = self.query_result(analysis=True, limit=10)
        self.assertIn("P-LOW", _rows(base_slice, {"1": "P-HIGH", "2": "P-LOW"}))
        self.harness.conn.execute("UPDATE vk_dwd.delivery_bill_barcode_detail_dwd SET deal_price=60 WHERE goods_id=2")
        changed_slice = self.query_result(analysis=True, limit=10)
        changed_rows = _rows(changed_slice, {"1": "P-HIGH", "2": "P-LOW"})
        # The row no longer matches <=50%, but its product group remains in
        # evidence because the same-scope return has no price-band attribution.
        # The new slice's matched gross is zero while net remains unknown.
        self.assertIn("P-LOW", changed_rows)
        self.assertEqual(_decimal("0"), _decimal(_first(changed_rows["P-LOW"], "gross_rolls", "gross_known_rolls")))
        self.assertIsNone(_first(changed_rows["P-LOW"], "net_rolls", "metric_value"))
        self.assertRegex(json.dumps({"facts": _facts(changed_rows["P-LOW"]), "states": _states(changed_rows["P-LOW"])}, ensure_ascii=False).lower(), "unknown|incomplete|partial|missing")
        changed_legacy = self.query_result(analysis=False, limit=10)
        self.assertEqual(_fact_values(base_legacy, "scope_net_rolls"), _fact_values(changed_legacy, "scope_net_rolls"))
        self.assertEqual(_fact_values(base_legacy, "scope_high_net_rolls"), _fact_values(changed_legacy, "scope_high_net_rolls"))

    def test_extension_boundary_and_invalid_ddp_public_mutations(self) -> None:
        id_map = self.load_extension_fixture()
        result = self.query_result(analysis=True, limit=10)
        rows = _rows(result, id_map)
        self.assertIn("E-RETURN0", rows, result)
        self.assertEqual(_decimal("8"), _decimal(_first(rows["E-RETURN0"], "gross_rolls", "gross_known_rolls")))
        self.assertEqual(_decimal("8"), _decimal(_first(rows["E-RETURN0"], "net_rolls", "metric_value")))
        for excluded in ("E-75", "E-7501"):
            self.assertNotIn(excluded, rows)
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        invalid_ids = ("E-DDP0", "E-DDPNULL", "E-DDPNEG")
        unknown_values = []
        for value in _recursive_values(result, "analysis_unknown_count"):
            try:
                unknown_values.append(int(value))
            except (TypeError, ValueError):
                pass
        self.assertTrue(max(unknown_values or [0]) >= 3 or all(item in serialized for item in invalid_ids), result)
        legacy = self.query_result(analysis=False, limit=10)
        legacy_rows = _rows(legacy, id_map)
        self.assertEqual(_decimal("0"), _decimal(_first(legacy_rows["E-75"], "high_gross_rolls", "high_known_gross_rolls")))
        self.assertEqual(_decimal("20"), _decimal(_first(legacy_rows["E-7501"], "high_gross_rolls", "high_known_gross_rolls")))

    def test_extension_negative_signed_flow_is_public_and_not_clamped(self) -> None:
        id_map = self.load_negative_fixture()
        result = self.query_result(analysis=False, limit=10)
        rows = _rows(result, id_map)
        self.assertIn("E-NEGATIVE", rows)
        self.assertEqual(_decimal("-2"), _decimal(_first(rows["E-NEGATIVE"], "net_rolls", "metric_value")))
        self.assertEqual(_decimal("-2"), _decimal(_first(rows["E-NEGATIVE"], "high_net_rolls", "high_known_net_rolls")))

    def test_invalid_group_filter_is_rejected_before_sql(self) -> None:
        request = self.request(analysis=True)
        request["analysis"] = {"row_filters": [{"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}], "group_filters": [{"field": "metric_value", "op": "gte", "value": "1"}]}
        before = len(self.harness.sql_trace)
        payload = self.harness.invoke("datasage_query", {"requests": [request]})
        self.assertEqual("failed", payload.get("status"), payload)
        self.assertEqual(before, len(self.harness.sql_trace))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InventoryV3ExecutionTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
