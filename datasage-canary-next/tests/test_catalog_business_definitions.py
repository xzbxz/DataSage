"""Targeted read-only checks for the consolidated semantic catalog.

Run from the Profile tests directory with ``python -B``.  The test imports the
plugin package under an isolated name and only compiles the local contracts;
there is no database, gateway, model, or network access.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types
import unittest

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_ROOT = PLUGIN_ROOT / "contracts"
PACKAGE = "datasage_query_catalog_business_tests"

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PACKAGE] = package
contracts = importlib.import_module(f"{PACKAGE}.contracts")
wire = importlib.import_module(f"{PACKAGE}.wire")


def catalog(request: dict) -> dict:
    return json.loads(contracts.datasage_catalog(request))


class ResultFieldSemanticsTests(unittest.TestCase):
    def setUp(self):
        from test_remediation_remaining_cases import plugin
        self.plugin = plugin
        self.datasets, self.semantics = plugin.contracts.execution_contracts('inventory')
        self.codes = ['registered_slow_'+prefix+suffix for prefix in ('pool_baseline_', 'monthly_') for suffix in ('summary', 'groups', 'net_outbound')]

    def test_exact_wire_fields_are_permitted_and_indexes_stay_small(self):
        for code in self.codes:
            detail = wire.compact_catalog_payload(catalog({'requests':[{'domain':'inventory','metric':code}]}))['results'][0]['metric']
            fields = detail['result_fields']
            self.assertEqual(set(self.semantics['metrics'][code]['result_fields']), set(fields))
            self.assertLessEqual(set(fields), self.plugin.tools._PUBLIC_FACT_FIELDS)
            self.assertEqual('main', fields['metric_value']['role'])
            self.assertEqual(detail['unit'], fields['metric_value']['unit'])
            if code.endswith('net_outbound'):
                self.assertEqual('卷', fields['net_rolls']['unit'])
                self.assertNotEqual(fields['net_rolls']['unit'], fields['metric_value']['unit'])
            else:
                self.assertNotEqual(fields['comparable_quantity_delta']['unit'], fields['metric_value']['unit'])
        for view in ('summary', 'expert_index'):
            request={'domain':'inventory'}
            if view!='summary':request['view']=view
            payload=wire.compact_catalog_payload(catalog({'requests':[request]}))
            self.assertNotIn('result_fields', json.dumps(payload))

    def test_semantics_and_units_cannot_grant_fact_permission(self):
        from copy import deepcopy
        definition=deepcopy(self.semantics['metrics'][self.codes[0]])
        before=set(self.plugin.tools._PUBLIC_FACT_FIELDS)
        for name in ('goods_id', 'ddp_price', 'private_secret'):
            changed=deepcopy(definition)
            changed['result_fields'][name]={'unit':'元','meaning':'Unapproved field'}
            changed['result_fact_units']={name:'元'}
            with self.assertRaises(contracts.ContractFailure):
                contracts._result_fields_projection(changed,set())
            ledger=self.plugin.tools._claim_ledger('r','x','x','组',[],{},'s','p',False,[{'metric_value':1,name:123}],fact_units=changed['result_fact_units'])
            self.assertNotIn(name,ledger[0]['facts'])
        self.assertEqual(before,self.plugin.tools._PUBLIC_FACT_FIELDS)

    def test_semantic_metadata_does_not_change_six_query_plans(self):
        from copy import deepcopy
        from datetime import date
        from test_remediation_remaining_cases import metric
        stripped=deepcopy(self.semantics)
        for definition in stripped['metrics'].values():definition.pop('result_fields',None)
        for code in self.codes:
            request=metric(code,'inventory',month='2026-08' if 'monthly_' in code else None,**({} if 'monthly_' in code else {'baseline_week':'2026-W37'}))
            args=(request,self.datasets)
            actual=self.plugin.tools._build_metric_query(*args,self.semantics,19,observed_on=date(2026,9,15))
            old=self.plugin.tools._build_metric_query(*args,stripped,19,observed_on=date(2026,9,15))
            self.assertEqual(old,actual,code)

    def test_synthetic_registered_results_have_distinct_counts_quantities_and_signed_delta(self):
        from test_monthly_slow_pool import MonthlyTests
        h=MonthlyTests();h.setUp();self.addCleanup(h.doCleanups)
        h.snap(qty=30,rolls=3);h.current(qty=20,rolls=2)
        h.current(id=2,goods=2,sku=22,qty=40,rolls=4)
        h.out(qty=10,rolls=2);h.ret(qty=3,rolls=1)
        summary=h.f();flow=h.f('net_outbound')
        self.assertEqual(2,summary['metric_value'])
        self.assertEqual(30,summary['opening_quantity'])
        self.assertEqual(60,summary['closing_quantity'])
        self.assertEqual(-10,summary['comparable_quantity_delta'])
        self.assertEqual(7,flow['metric_value']);self.assertEqual(1,flow['net_rolls'])
        new=next(row for row in h.result('groups')['rows'] if row['states']['pool_movement_state']=='New')
        self.assertIsNone(new['facts']['comparable_quantity_delta'])
        self.assertIsNone(new['facts']['opening_quantity'])
        # Logical equality example on actual synthetic handler output: no flow,
        # no inventory movement. Equal zero does not make their definitions equal.
        h.h.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        h.h.conn.execute('DELETE FROM vk_dwd.delivery_return_detail_dwd')
        h.h.conn.execute('UPDATE vk_ods.slow_moving_goods_ods SET goods_num=30 WHERE goods_id=1')
        self.assertEqual(0,h.f()['comparable_quantity_delta'])
        self.assertEqual(0,h.f('net_outbound')['metric_value'])


class CatalogBusinessDefinitionTests(unittest.TestCase):
    def test_exact_inventory_detail_exposes_existing_boundaries(self) -> None:
        raw = catalog(
            {
                "requests": [
                    {
                        "domain": "inventory",
                        "metric": "registered_slow_monthly_net_outbound",
                    }
                ]
            }
        )
        self.assertEqual("success", raw["status"])
        result = raw["results"][0]
        metric = result["metric"]
        boundary = metric.get("answer_boundary_summary")
        self.assertIsInstance(boundary, list)
        text = "\n".join(boundary)
        semantics = yaml.safe_load((CONTRACT_ROOT / 'inventory-semantics.yaml').read_text(encoding='utf-8'))
        declared = semantics['metrics']['registered_slow_monthly_net_outbound']['disclosures']
        # Check the maintained business definitions survive the public path;
        # do not maintain another copy of their wording in this test.
        for disclosure in declared:
            if disclosure['mode'] == 'required_always':
                self.assertIn(disclosure['text'], boundary)
        self.assertNotIn("vk_", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("high_price_policy", metric)
        self.assertNotIn("disclosures", metric)

        compact = wire.compact_catalog_payload(raw)
        compact_metric = compact["results"][0]["metric"]
        self.assertEqual(boundary, compact_metric["answer_boundary_summary"])

    def test_summary_and_expert_index_do_not_expand_with_boundary_prose(self) -> None:
        for view in (None, "expert_index"):
            request = {"domain": "inventory"}
            if view is not None:
                request["view"] = view
            raw = catalog({"requests": [request]})
            self.assertEqual("success", raw["status"])
            metrics = raw["results"][0]["metrics"]
            self.assertTrue(metrics)
            for metric in metrics:
                self.assertNotIn("answer_boundary_summary", metric)

    def test_profit_detail_reuses_domain_default_boundaries(self) -> None:
        raw = catalog(
            {
                "requests": [
                    {
                        "domain": "profit",
                        "metric": "customer_month_sales_commission",
                    }
                ]
            }
        )
        self.assertEqual("success", raw["status"])
        text = "\n".join(raw["results"][0]["metric"]["answer_boundary_summary"])
        self.assertIn("费用空值表示尚未分配到", text)
        self.assertIn("客户及订单利润默认不排除内部客户", text)
        self.assertIn("优先取提成表", text)

    def test_history_pool_definition_uses_weekly_source_aliases(self) -> None:
        semantics = yaml.safe_load(
            (CONTRACT_ROOT / "inventory-semantics.yaml").read_text(encoding="utf-8")
        )
        metrics = semantics["metrics"]
        weekly = metrics["registered_slow_pool_baseline_net_outbound"]
        history = metrics["registered_slow_historical_customers"]
        self.assertNotIn("pool_metric", history)
        pool = history["pool_definition"]
        self.assertEqual(
            {"table", "baseline_source_table", "flow_sources"}, set(pool)
        )
        self.assertEqual(weekly["table"], history["table"])
        self.assertEqual(weekly["table"], pool["table"])
        self.assertEqual(weekly["baseline_source_table"], pool["baseline_source_table"])
        self.assertEqual(weekly["flow_sources"], pool["flow_sources"])
        self.assertEqual(
            {"outbound", "returns", "sales", "warehouses"},
            set(pool["flow_sources"]),
        )

    def test_registered_slow_pool_common_blocks_parse_equivalently(self) -> None:
        semantics = yaml.safe_load(
            (CONTRACT_ROOT / "inventory-semantics.yaml").read_text(encoding="utf-8")
        )
        metrics = semantics["metrics"]
        names = [code for code, definition in metrics.items() if definition.get('query_kind') == 'registered_slow_pool']
        common = (
            "query_kind",
            "table",
            "time_policy",
            "max_group_dimensions",
            "required_filters",
            "allowed_dimensions",
            "dimension_overrides",
            "classification_policy",
            "disclosures",
        )
        first = metrics[names[0]]
        for name in names[1:]:
            with self.subTest(metric=name):
                for key in common:
                    self.assertEqual(first[key], metrics[name][key], key)

    def test_removed_route_metadata_retains_domain_navigation(self) -> None:
        for filename in ("delivery-semantics.yaml", "receipt-semantics.yaml"):
            semantics = yaml.safe_load(
                (CONTRACT_ROOT / filename).read_text(encoding="utf-8")
            )

            def walk(value):
                if isinstance(value, dict):
                    self.assertNotIn("triggers", value, filename)
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(semantics)

        receivable = yaml.safe_load(
            (CONTRACT_ROOT / "receivable-semantics.yaml").read_text(encoding="utf-8")
        )
        refs = receivable.get("related_metric_refs", [])
        self.assertIn(
            {"domain": "receivable", "metric": "formal_receivable_turnover_days"},
            refs,
        )

    def test_all_domain_catalogs_still_compile(self) -> None:
        for domain in (
            "delivery",
            "receipt",
            "receivable",
            "target",
            "inventory",
            "pattern_matching",
            "profit",
        ):
            with self.subTest(domain=domain):
                raw = catalog({"requests": [{"domain": domain, "view": "expert_index"}]})
                self.assertEqual("success", raw["status"])


if __name__ == "__main__":
    unittest.main()
