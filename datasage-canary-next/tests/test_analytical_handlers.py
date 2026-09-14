"""Integration invariants for registered shapes; no real database or model."""
from copy import deepcopy
from datetime import date
import unittest

from test_remediation_remaining_cases import plugin, metric


class AnalyticalHandlerTests(unittest.TestCase):
    def setUp(self):
        self.datasets = plugin.contract_store.read_yaml('plugins/datasage-query/contracts/datasets.yaml')
        self.semantics = plugin.contract_store.read_yaml('plugins/datasage-query/contracts/inventory-semantics.yaml')
        self.code = 'registered_slow_historical_customers'
        self.request = metric(self.code, 'inventory', month=None, baseline_week='2026-W37')

    def compile(self, semantics):
        return plugin.tools._build_metric_query(
            self.request, self.datasets, semantics, 50, observed_on=date(2026, 9, 14),
        )

    def test_history_depends_on_pool_definition_not_other_public_metric(self):
        expected = self.compile(self.semantics)
        changed = deepcopy(self.semantics)
        del changed['metrics']['registered_slow_pool_baseline_net_outbound']
        self.assertEqual(expected, self.compile(changed))

    def test_missing_or_inconsistent_pool_contract_is_typed_failure(self):
        for pool in (None, {}, {'table': 'unregistered.table', 'baseline_source_table': 'unregistered.source', 'flow_sources': {}}):
            with self.subTest(pool=pool):
                changed = deepcopy(self.semantics)
                changed['metrics'][self.code]['pool_definition'] = pool
                with self.assertRaises(plugin.tools.QueryFailure) as failure:
                    self.compile(changed)
                self.assertEqual('CONTRACT_UNAVAILABLE', failure.exception.code)

    def test_units_cannot_grant_public_fact_permission(self):
        handler = plugin.tools.analytical_handlers.get_handler('slow_customer_history')
        allowed = plugin.tools.analytical_handlers.public_fact_fields()
        self.assertTrue(frozenset(handler.resolve('public_fields')) <= allowed)
        ledger = plugin.tools._claim_ledger(
            'r', self.code, '历史关系', '条', [], {}, 'scope', 'projection', False,
            [{'history_customer_ref': 'customer_safe', 'private_source_column': 'secret'}],
            fact_units={'history_customer_ref': '客户引用', 'private_source_column': '元'},
        )
        self.assertEqual({'history_customer_ref': 'customer_safe'}, ledger[0]['facts'])
        self.assertEqual({'history_customer_ref': '客户引用'}, ledger[0]['fact_units'])
        self.assertNotIn('private_source_column', plugin.tools.analytical_handlers.public_fact_fields())
        self.assertNotIn('private_source_column', plugin.tools._PUBLIC_FACT_FIELDS)
        self.assertIn('history_customer_ref', allowed)

    def test_registered_time_projection_preserves_clock_and_hides_internal_metadata(self):
        value = {'source': 'fixed_12_calendar_month_customer_history',
                 'history_start': '2025-09-14T12:00:00', 'history_end': '2026-09-14T12:00:00',
                 'observed_db_utc_offset_seconds': 28800, 'private_source_column': 'hidden'}
        public = plugin.tools._public_time_range(value)
        self.assertEqual(28800, public['observed_db_utc_offset_seconds'])
        self.assertNotIn('private_source_column', public)
        handler = plugin.tools.analytical_handlers.handler_for_time(public['source'])
        self.assertIn('28800', handler.resolve('time_description')(public)[0])

    def test_high_price_factor_is_read_from_numeric_contract_not_sql_literal(self):
        code = 'registered_slow_pool_baseline_net_outbound'
        request = metric(code, 'inventory', month=None, baseline_week='2026-W37')
        changed = deepcopy(self.semantics)
        changed['metrics'][code]['high_price_policy']['factor'] = 0.5
        sql, _, _ = plugin.tools._build_metric_query(request, self.datasets, changed, 50, observed_on=date(2026, 9, 14))
        self.assertIn('f.deal_price>0.5*f.ddp_price', sql)
        self.assertNotIn('f.deal_price>0.75*f.ddp_price', sql)
        for invalid in (True, '0.75; SELECT 1', float('nan'), float('inf'), 0, -1, 1.1):
            with self.subTest(invalid=invalid):
                changed['metrics'][code]['high_price_policy']['factor'] = invalid
                with self.assertRaises(plugin.tools.QueryFailure) as failure:
                    plugin.tools._build_metric_query(request, self.datasets, changed, 50, observed_on=date(2026, 9, 14))
                self.assertEqual('CONTRACT_UNAVAILABLE', failure.exception.code)


if __name__ == '__main__':
    unittest.main()
