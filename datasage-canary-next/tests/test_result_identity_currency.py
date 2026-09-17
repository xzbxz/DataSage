"""B convergence checks for public entity identity and original-currency evidence.

The tests exercise the registered public handlers with isolated SQLite data.  No
live connector, model call, network request, or profile state is used.
"""

from hashlib import sha256
import json
from unittest.mock import patch
import unittest

import test_profit_contract as profit_fixture
import test_remediation_remaining_cases as public_fixture
from test_remediation_remaining_cases import facts, metric, plugin


class ResultIdentityCurrencyTests(unittest.TestCase):
    def _profit_fixture(self):
        harness = profit_fixture.ProfitContractTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def _inventory_fixture(self):
        harness = public_fixture.RemainingCaseTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.conn.execute(
            "ALTER TABLE vk_dw.inventory_barcode_detail_dw "
            "ADD COLUMN ddp_amount REAL"
        )
        harness.conn.execute(
            "ALTER TABLE vk_dw.inventory_barcode_detail_dw "
            "ADD COLUMN currency_no TEXT"
        )
        return harness

    def test_same_name_entities_keep_distinct_stable_public_refs(self):
        harness = self._profit_fixture()
        harness.customer_rows(
            [
                ("row-a", "2026-08", "customer-a", "同名客户", 100, 30, 0),
                ("row-b", "2026-08", "customer-b", "同名客户", 100, 30, 0),
            ]
        )
        result = harness.result(
            harness.query(
                metric(
                    "customer_month_sales_revenue",
                    "profit",
                    dimensions=["customer"],
                )
            )
        )
        rows = result["rows"]
        self.assertEqual(2, len(rows), result)
        refs = [
            next(
                dimension["entity_ref"]
                for dimension in row["dimensions"]
                if dimension["label"] == "利润报表客户"
            )
            for row in rows
        ]
        self.assertEqual(2, len(set(refs)), result)
        self.assertTrue(all(ref.startswith("entity_ref_v1_") for ref in refs))
        self.assertTrue(
            all(
                dimension["entity_ref_kind"] == "opaque_reference_non_filter_token"
                for row in rows
                for dimension in row["dimensions"]
                if dimension["label"] == "利润报表客户"
            )
        )
        public_json = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("customer_id", public_json)
        self.assertNotIn("customer_name", public_json)
        self.assertNotIn("customer-a", public_json)
        self.assertNotIn("customer-b", public_json)

        # Repeating the same canonical source identity produces the same ref,
        # independently of the amount or the display name.
        self.assertEqual(refs[0], plugin.entities.opaque_entity_ref("customer", "customer-a"))
        self.assertNotEqual(refs[0], plugin.entities.opaque_entity_ref("customer", "customer-b"))

    def test_missing_entity_identity_is_explicit_in_public_claim(self):
        harness = self._profit_fixture()
        harness.customer_rows(
            [("row-missing", "2026-08", None, "客户缺少稳定身份", 80, 20, 0)]
        )
        result = harness.result(
            harness.query(
                metric(
                    "customer_month_sales_revenue",
                    "profit",
                    dimensions=["customer"],
                )
            )
        )
        customer_dimension = next(
            dimension
            for dimension in result["rows"][0]["dimensions"]
            if dimension["label"] == "利润报表客户"
        )
        self.assertEqual("identity_missing", customer_dimension["identity_state"])
        self.assertNotIn("entity_ref", customer_dimension)
        self.assertEqual(80, facts(result)[0]["metric_value"])

    def test_missing_currency_is_visible_and_cannot_be_complete(self):
        harness = self._inventory_fixture()
        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,currency_no,status",
            [(100, "USD", 1), (50, None, 1)],
        )
        result = harness.result(
            harness.query(
                metric(
                    "current_inventory_amount_original",
                    "inventory",
                    month=None,
                    dimensions=["currency"],
                    inventory_scope="total",
                )
            )
        )
        self.assertEqual("incomplete", result["data_state"], result)
        self.assertEqual({"mode": "grouped"}, result["currency_scope"])
        by_currency = {
            row["dimensions"][0]["value"]: row for row in result["rows"]
        }
        self.assertEqual(100, by_currency["USD"]["facts"]["metric_value"])
        unknown = by_currency["未知币种"]
        self.assertEqual("missing", unknown["states"]["metric_data_state"])
        self.assertIsNone(unknown["facts"]["metric_value"])
        self.assertEqual(1, unknown["facts"]["currency_missing_rows"])
        self.assertEqual(50, unknown["facts"]["unclassified_source_amount"])
        self.assertEqual(50, unknown["facts"]["unclassified_source_amount_min"])
        self.assertEqual(50, unknown["facts"]["unclassified_source_amount_max"])
        self.assertEqual(1, unknown["facts"]["unclassified_source_row_count"])
        self.assertEqual(
            "currency_unknown_source_value_not_comparable",
            unknown["states"]["unclassified_amount_state"],
        )
        self.assertIn(
            "不可比较",
            unknown["fact_units"]["unclassified_source_amount"],
        )
        self.assertEqual("unknown", unknown["dimensions"][0]["identity_state"])
        self.assertTrue(unknown["dimensions"][0]["display_only"])
        self.assertTrue(unknown["dimensions"][0]["display_name_missing"])
        self.assertIsNone(unknown["currency"])

    def test_single_currency_filter_is_carried_through_final_wire(self):
        harness = self._inventory_fixture()
        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,currency_no,status",
            [(100, "USD", 1), (50, "EUR", 1)],
        )
        result = harness.result(
            harness.query(
                metric(
                    "current_inventory_amount_original",
                    "inventory",
                    month=None,
                    metric_filters={"currency": "USD"},
                    inventory_scope="total",
                )
            )
        )
        self.assertEqual(
            {"mode": "filtered", "value": "USD"}, result["currency_scope"]
        )
        row = result["rows"][0]
        self.assertEqual("USD", row["currency"])
        self.assertEqual("identified", row["currency_state"])
        self.assertEqual(100, row["facts"]["metric_value"])
        self.assertEqual(0, row["facts"]["currency_missing_rows"])

    def test_multiple_unknown_currency_rows_expose_range_without_a_total(self):
        harness = self._inventory_fixture()
        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,currency_no,status",
            [(100, "USD", 1), (50, None, 1), (30, None, 1)],
        )
        result = harness.result(
            harness.query(
                metric(
                    "current_inventory_amount_original",
                    "inventory",
                    month=None,
                    dimensions=["currency"],
                    inventory_scope="total",
                )
            )
        )
        unknown_rows = [
            row
            for row in result["rows"]
            if row["dimensions"][0]["value"] == "未知币种"
        ]
        self.assertEqual(1, len(unknown_rows), result)
        unknown = unknown_rows[0]
        self.assertIsNone(unknown["facts"]["metric_value"])
        self.assertIsNone(unknown["facts"]["unclassified_source_amount"])
        self.assertEqual(30, unknown["facts"]["unclassified_source_amount_min"])
        self.assertEqual(50, unknown["facts"]["unclassified_source_amount_max"])
        self.assertEqual(2, unknown["facts"]["unclassified_source_row_count"])
        self.assertEqual(
            "currency_unknown_source_range_not_comparable",
            unknown["states"]["unclassified_amount_state"],
        )

    def test_unknown_currency_with_missing_numeric_input_stays_unknown(self):
        harness = self._inventory_fixture()
        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,currency_no,status",
            [(None, None, 1)],
        )
        result = harness.result(
            harness.query(
                metric(
                    "current_inventory_amount_original",
                    "inventory",
                    month=None,
                    dimensions=["currency"],
                    inventory_scope="total",
                )
            )
        )
        unknown = result["rows"][0]
        self.assertIsNone(unknown["facts"]["metric_value"])
        self.assertIsNone(unknown["facts"]["unclassified_source_amount"])
        self.assertIsNone(unknown["facts"]["unclassified_source_amount_min"])
        self.assertIsNone(unknown["facts"]["unclassified_source_amount_max"])
        self.assertEqual(1, unknown["facts"]["unclassified_source_row_count"])
        self.assertEqual(
            "currency_unknown_source_value_not_comparable",
            unknown["states"]["unclassified_amount_state"],
        )

    def test_public_ref_resolves_then_reenters_existing_metric_filter_path(self):
        harness = self._profit_fixture()
        harness.conn.create_function(
            "SHA2",
            2,
            lambda value, _algorithm: None
            if value is None
            else sha256(str(value).encode("utf-8")).hexdigest(),
        )
        harness.insert(
            "vk_dwd.customer_dwd",
            "customer_id,customer_no,customer_name,is_delete,is_void",
            [("customer-a", "CUS-A", "可回查客户", "n", "n")],
        )
        harness.customer_rows(
            [("row-a", "2026-08", "customer-a", "可回查客户", 123, 30, 0)]
        )
        ref = plugin.entities.opaque_entity_ref("customer", "customer-a")
        resolver_args = {
            "token": ref,
            "entity_types": ["customer"],
            "domain": "profit",
            "metric": "customer_month_sales_revenue",
        }
        def resolver_execute(sql, params, limit, *, deadline_at=None):
            rows, truncated, _source = harness.execute(
                sql,
                params,
                limit,
                deadline_at=deadline_at,
            )
            return rows, truncated

        with patch.object(plugin.entities.db_runtime, "execute", resolver_execute):
            resolved = harness.invoke("datasage_entity_resolve", resolver_args)
            self.assertEqual("resolved", resolved["status"], resolved)
            self.assertEqual("opaque_reference_exact", resolved["resolution_path"])
            self.assertEqual("customer-a", resolved["candidates"][0]["canonical_id"])

            # The existing governed filter path receives the resolver's
            # approved canonical value and emits the identical public ref.
            result = harness.result(
                harness.query(
                    metric(
                        "customer_month_sales_revenue",
                        "profit",
                        dimensions=["customer"],
                        metric_filters={"customer": "customer-a"},
                    )
                )
            )
        dimension = next(
            dimension
            for dimension in result["rows"][0]["dimensions"]
            if dimension["label"] == "利润报表客户"
        )
        self.assertEqual(ref, dimension["entity_ref"])
        self.assertEqual(123, facts(result)[0]["metric_value"])

        # The opaque comparison reference is deliberately not a raw metric
        # filter token; it must first pass through the resolver above.
        direct = harness.query(
            metric(
                "customer_month_sales_revenue",
                "profit",
                metric_filters={"customer": ref},
            )
        )
        direct_result = direct["results"][0]
        self.assertEqual("failed", direct_result["status"], direct)
        self.assertEqual("ENTITY_NOT_FOUND", direct_result["error"]["code"])

    def test_ref_lookup_uses_logical_role_when_it_differs_from_entity_type(self):
        harness = public_fixture.RemainingCaseTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.conn.execute(
            "CREATE TABLE vk_dwd.supplier_info_dwd("
            "supplier_id TEXT,supplier_no TEXT,supplier_name TEXT)"
        )
        harness.conn.create_function(
            "SHA2",
            2,
            lambda value, _algorithm: None
            if value is None
            else sha256(str(value).encode("utf-8")).hexdigest(),
        )
        harness.insert(
            "vk_dwd.supplier_info_dwd",
            "supplier_id,supplier_no,supplier_name",
            [("supplier-a", "SUP-A", "供应商甲")],
        )
        ref = plugin.entities.opaque_entity_ref("product_supplier", "supplier-a")

        def resolver_execute(sql, params, limit, *, deadline_at=None):
            rows, truncated, _source = harness.execute(
                sql,
                params,
                limit,
                deadline_at=deadline_at,
            )
            return rows, truncated

        with patch.object(plugin.entities.db_runtime, "execute", resolver_execute):
            resolved = harness.invoke(
                "datasage_entity_resolve",
                {
                    "token": ref,
                    "entity_types": ["supplier"],
                    "domain": "inventory",
                },
            )
        self.assertEqual("resolved", resolved["status"], resolved)
        self.assertEqual("opaque_reference_exact", resolved["resolution_path"])
        self.assertEqual("product_supplier", resolved["candidates"][0]["filter_role"])


if __name__ == "__main__":
    unittest.main()
