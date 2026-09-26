"""Independent request and metric-boundary oracles using only synthetic data.

Loads pure implementations under a private namespace; this is NOT a Hermes
registration/identity test. SQLite executes only the compatible SELECT subset
of actual generated SQL, with %s placeholders changed to ?. These checks do not
certify MySQL dialects, real business values, or a language model's use of prose.
"""
from __future__ import annotations

from datetime import date
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "datasage_independent_contract_boundaries_20260926"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins" / "datasage-query")]
    sys.modules[PACKAGE] = package


def load(name):
    return importlib.import_module(f"{PACKAGE}.{name}")


class CatalogInputBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load("contracts")
        cls.wire = load("wire")

    def call(self, request):
        raw = self.catalog.datasage_catalog({"requests": [request]})
        return json.loads(self.wire.enforce_tool_result_budget("datasage_catalog", raw))

    def test_container_domain_is_invalid_input_not_internal_error(self):
        for value in ([], {}, ["delivery"], {"name": "delivery"}, [[[]]]):
            with self.subTest(value=value):
                result = self.call({"domain": value})
                self.assertEqual("failed", result["status"])
                self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_container_view_is_invalid_input_not_internal_error(self):
        for value in ([], {}, ["full"], {"name": "full"}, [[[]]]):
            with self.subTest(value=value):
                result = self.call({"domain": "delivery", "view": value})
                self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_other_nonstring_keys_are_invalid_without_coercion(self):
        for key in ("domain", "view"):
            for value in (True, False, 0, 1, 0.5):
                with self.subTest(key=key, value=value):
                    result = self.call({"domain": "delivery", key: value})
                    self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_normal_metric_and_pending_capability_are_distinct(self):
        normal = self.call({"domain": "receivable", "metric": "average_settlement_days"})
        self.assertEqual("success", normal["status"])
        pending = self.call({"domain": "receivable", "metric": "receivable_quantity"})
        self.assertEqual("success", pending["status"])
        self.assertFalse(pending["results"][0]["metric"]["selectable"])
        self.assertEqual("pending_validation", pending["results"][0]["metric"]["status"])

    def test_unknown_metric_does_not_become_pending(self):
        result = self.call({"domain": "receivable", "metric": "synthetic_unknown_metric"})
        self.assertEqual("failed", result["status"])
        self.assertEqual("METRIC_UNAVAILABLE", result["error"]["code"])

    def test_scorecard_with_domain_is_invalid_not_silently_reinterpreted(self):
        result = self.call({"domain": "delivery", "view": "performance_scorecard"})
        self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_duplicate_requests_remain_invalid(self):
        request = {"domain": "delivery", "metric": "delivery_amount"}
        result = json.loads(self.catalog.datasage_catalog({"requests": [request, request]}))
        self.assertEqual("INVALID_INPUT", result["error"]["code"])


class EntityInputBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entities = load("entities")

    def test_domain_containers_are_rejected_before_querying(self):
        from unittest.mock import patch
        for value in ([], {}, ["delivery"], {"name": "delivery"}):
            with self.subTest(value=value), patch.object(
                self.entities.db_runtime, "execute", side_effect=AssertionError("No DB I/O")
            ) as execute:
                result = json.loads(self.entities.datasage_entity_resolve(
                    {"token": "synthetic-entity", "domain": value}
                ))
                self.assertEqual("INVALID_INPUT", result["error"]["code"])
                self.assertTrue(result["must_stop_business_query"])
                execute.assert_not_called()

    def test_optional_domain_and_valid_structured_args_remain_supported(self):
        token, kinds, domain, metric, attribution, limit = self.entities._validate_args(
            {"token": " synthetic ", "domain": None, "limit": 1}
        )
        self.assertEqual("synthetic", token)
        self.assertIsNone(domain)
        self.assertEqual(1, limit)
        self.entities._validate_args({"token": "synthetic", "domain": "delivery",
                                      "metric": "delivery_amount", "entity_types": ["customer"]})

    def test_invalid_scalar_domain_is_not_string_coerced(self):
        for value in (True, False, 1, 0.5):
            with self.subTest(value=value), self.assertRaises(self.entities.EntityFailure) as error:
                self.entities._validate_args({"token": "synthetic", "domain": value})
            self.assertEqual("INVALID_INPUT", error.exception.code)


class MetricSemanticBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = load("tools")

    def build(self, domain, metric, dimensions=None):
        request = {"request_id": "synthetic-independent", "domain": domain,
                   "metric": metric, "dimensions": dimensions or []}
        if domain != "pattern_matching":
            request["time_range"] = {"start": "2026-08-01", "end": "2026-09-01"}
        datasets, semantics = self.tools._contracts(domain)
        request = self.tools._validate_request(request)
        request = self.tools._validate_metric_contract(request, semantics)
        return self.tools._build_metric_query(
            request, datasets, semantics, 100, observed_on=date(2026, 9, 26)
        )

    def test_empty_pattern_amount_dimensions_retain_currency_grain(self):
        for metric in ("linked_delivery_amount", "person_attributed_delivery_amount"):
            with self.subTest(metric=metric):
                _, _, scope = self.build("pattern_matching", metric)
                self.assertEqual(["currency"], scope["effective_dimensions"])
                self.assertIn("currency_no", scope["dimension_outputs"])

    def test_pattern_amount_explicit_group_cannot_remove_currency(self):
        with self.assertRaises(self.tools.QueryFailure) as error:
            self.build("pattern_matching", "linked_delivery_amount", ["customer"])
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", error.exception.code)

    def test_pattern_summary_has_its_own_default_grain(self):
        _, _, scope = self.build("pattern_matching", "task_recorded_summary")
        self.assertEqual([], scope["effective_dimensions"])

    def test_receipt_public_currency_key_not_physical_column(self):
        sql, _, scope = self.build("receipt", "receipt_amount_original", ["currency"])
        self.assertIn("GROUP BY `f`.`currency_no`", sql)
        self.assertEqual(["currency_no"], scope["dimension_outputs"])
        with self.assertRaises(self.tools.QueryFailure) as error:
            self.build("receipt", "receipt_amount_original", ["currency_no"])
        self.assertEqual("UNSUPPORTED_DIMENSION", error.exception.code)

    def test_original_receipt_requires_supported_currency_scope(self):
        with self.assertRaises(self.tools.QueryFailure) as error:
            self.build("receipt", "receipt_amount_original")
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", error.exception.code)

    def test_generated_receipt_sql_keeps_unlike_currencies_separate(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute("CREATE TABLE vk_dwd.receive_bill_detail_dwd "
                           "(currency_no TEXT, detail_receive_amount REAL, bill_status TEXT, bill_time TEXT)")
        connection.executemany("INSERT INTO vk_dwd.receive_bill_detail_dwd VALUES (?,?,?,?)", [
            ("USD", 10, "B", "2026-08-01"), ("USD", 20, "B", "2026-08-31"),
            ("VND", 700, "B", "2026-08-10"), ("USD", 900, "A", "2026-08-10"),
            ("USD", 800, "B", "2026-09-01")])
        sql, params, _ = self.build("receipt", "receipt_amount_original", ["currency"])
        rows = list(connection.execute(sql.replace("%s", "?"), params))
        self.assertEqual({"USD": 30, "VND": 700}, {r["currency_no"]: r["metric_value"] for r in rows})

    def profit_db(self, rows):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_ads")
        connection.execute("CREATE TABLE vk_ads.customer_profit_ads "
                           "(bill_date TEXT, sale_amount_rmb REAL, gross_profit_rmb REAL, qc_amount_rmb REAL)")
        connection.executemany("INSERT INTO vk_ads.customer_profit_ads VALUES (?,?,?,?)", rows)
        return connection

    def profit_result(self, connection, metric):
        sql, params, _ = self.build("profit", metric)
        return dict(connection.execute(sql.replace("%s", "?"), params).fetchone())

    def test_missing_optional_expense_does_not_erase_recorded_margin(self):
        connection = self.profit_db([("2026-08", 100, 30, None)])
        margin = self.profit_result(connection, "customer_month_gross_margin")
        expense = self.profit_result(connection, "customer_month_inspection_expense")
        self.assertAlmostEqual(0.3, margin["metric_value"])
        self.assertEqual("complete", margin["metric_data_state"])
        self.assertIsNone(expense["metric_value"])
        self.assertEqual("missing", expense["metric_data_state"])
        self.assertEqual(1, expense["missing_value_count"])

    def test_margin_requires_its_own_numerator_and_denominator(self):
        for revenue, profit in ((100, None), (None, 30), (0, 30)):
            with self.subTest(revenue=revenue, profit=profit):
                connection = self.profit_db([("2026-08", revenue, profit, 10)])
                value = self.profit_result(connection, "customer_month_gross_margin")
                self.assertIsNone(value["metric_value"])

    def test_margin_is_weighted_and_preserves_signed_values(self):
        connection = self.profit_db([("2026-08", 100, 30, None), ("2026-08", 300, 30, None)])
        self.assertAlmostEqual(0.15, self.profit_result(connection, "customer_month_gross_margin")["metric_value"])
        connection.execute("DELETE FROM vk_ads.customer_profit_ads")
        connection.execute("INSERT INTO vk_ads.customer_profit_ads VALUES ('2026-08',-100,-20,NULL)")
        result = self.profit_result(connection, "customer_month_gross_margin")
        self.assertAlmostEqual(0.2, result["metric_value"])
        self.assertEqual(-20, result["numerator_value"])
        self.assertEqual(-100, result["denominator_value"])


if __name__ == "__main__":
    unittest.main()
