"""Execute compiler SQL against independent fixtures, then inspect sealed evidence."""
from datetime import date
from contextlib import closing
import copy
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ['HERMES_HOME'] = str(ROOT)
PACKAGE = 'datasage_null_integrity_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / 'plugins' / 'datasage-query')]
sys.modules[PACKAGE] = package
tools = importlib.import_module(f'{PACKAGE}.tools')
security = importlib.import_module(f'{PACKAGE}.db_security')


class NullIntegrityTests(unittest.TestCase):
    def run_fixture(self, metric, values):
        request = dict(request_id='null_integrity', domain='receipt' if metric == 'actual_receipt_amount' else 'target',
                       mode='metric', metric=metric, dimensions=[], metric_filters={}, calendar_month='2026-08')
        if request['domain'] == 'target':
            request['attribution_mode'] = 'salesperson_allocation'
        normalized, datasets, semantics = tools._validate_request_plan_without_entities(request, observed_on=date(2026, 8, 18))
        prepared = dict(request=normalized, datasets=datasets, semantics=semantics,
                        resolved_entities=[], entity_resolution_db_call_count=0)
        source = dict(schema='datasage-query-source-evidence/v1', identity_sha256='1'*64,
                      connection_verified=True, transport_mode='plaintext', transport_policy_verified=True,
                      grant_policy='strict_object_read_only', grants_verified=True, read_only=True,
                      source_commitment_sha256='2'*64, security_evidence_sha256='')
        source['security_evidence_sha256'] = security._source_evidence_hash(source)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
            conn.executescript('''
                CREATE TABLE vk_dwd.receive_bill_detail_dwd(detail_deal_amount REAL, exchange_rate REAL, bill_status TEXT, bill_time TEXT);
                CREATE TABLE vk_dwd.sale_bill_split_dwd(delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT, delivery_time TEXT);
                CREATE TABLE vk_dwd.delivery_return_detail_dwd(return_amount_rmb REAL, status INTEGER, complnt_type INTEGER, channel_type INTEGER, is_inner_cus TEXT, statement_time TEXT);
            ''')
            if request['domain'] == 'receipt':
                conn.executemany('INSERT INTO vk_dwd.receive_bill_detail_dwd VALUES(?,?,?,?)',
                                 [(amount, rate, 'C', '2026-08-15') for amount, rate in values])
            else:
                conn.executemany('INSERT INTO vk_dwd.sale_bill_split_dwd VALUES(?,?,?,?)',
                                 [(amount, 6, 'n', '2026-08-15') for amount in values])

            def execute(sql, params, limit, *, deadline_at=None):
                rows = [dict(row) for row in conn.execute(sql.replace('%s', '?'), params)]
                return rows, False, source

            result = tools._run_one(request, prepared=prepared, execute_query=execute,
                                    period_observed_on=date(2026, 8, 18), started_at=0.0)
        tools._seal_claim_ids([result])
        return result, tools._model_wire_result(result, request=normalized)

    def test_receipt_missing_inputs_never_become_complete_amount(self):
        for values, state, missing, known in [([(100, None)], 'undefined', 1, 0),
                ([(None, 7)], 'undefined', 1, 0), ([(10, 7), (100, None)], 'incomplete', 1, 1)]:
            with self.subTest(values=values):
                result, wire = self.run_fixture('actual_receipt_amount', values)
                self.assertEqual(state, result['data_state'])
                self.assertEqual(state, wire['data_state'])
                row = result['rows'][0]
                self.assertIsNone(row['metric_value'])
                self.assertEqual((missing, known), (row['missing_value_count'], row['known_value_count']))
                for claim in wire.get('claim_ledger', []):
                    self.assertIsNone(claim.get('facts', {}).get('metric_value'))

    def test_allocated_missing_inputs_never_become_complete_amount(self):
        for values, state in [([None], 'undefined'), ([70, None], 'incomplete')]:
            with self.subTest(values=values):
                result, wire = self.run_fixture('allocated_net_delivery_amount', values)
                self.assertEqual(state, result['data_state'])
                self.assertEqual(state, wire['data_state'])
                self.assertIsNone(result['rows'][0]['metric_value'])

    def test_empty_zero_and_complete_remain_distinct(self):
        for metric, cases in [('actual_receipt_amount', [([], 'empty', None), ([(0, 7)], 'zero', 0), ([(10, 7)], 'rows', 70)]),
                              ('allocated_net_delivery_amount', [([], 'empty', None), ([0], 'zero', 0), ([70], 'rows', 70)])]:
            for values, state, expected in cases:
                with self.subTest(metric=metric, values=values):
                    result, wire = self.run_fixture(metric, values)
                    self.assertEqual(state, result['data_state'])
                    self.assertEqual(state, wire['data_state'])
                    if state == 'empty':
                        self.assertFalse(result['claim_ledger'])
                    else:
                        self.assertEqual(expected, result['rows'][0]['metric_value'])

    def test_sum_composite_ratio_and_comparison_do_not_reintroduce_zero(self):
        contracts = importlib.import_module(f'{PACKAGE}.contracts')
        datasets, semantics = contracts.execution_contracts('receipt')
        semantics = copy.deepcopy(semantics)
        base = semantics['metrics']['actual_receipt_amount']
        left = {**base, 'aggregation': 'sum', 'measure': 'detail_deal_amount'}
        right = {**base, 'aggregation': 'sum', 'measure': 'exchange_rate'}
        semantics['metrics'].update(left=left, right=right)
        composite = {**base, 'components': [{'metric': 'left', 'sign': 1}, {'metric': 'right', 'sign': -1}]}
        ratio = {**base, 'ratio': {'numerator': 'left', 'denominator': 'right'}}
        raw = dict(request_id='derived_null', domain='receipt', mode='metric', metric='actual_receipt_amount', dimensions=[],
                   metric_filters={}, calendar_month='2026-08')
        request, _, _ = tools._validate_request_plan_without_entities(raw, observed_on=date(2026, 8, 18))
        compiles = {
            'sum': lambda: tools._build_metric_core(request, left, left, datasets, semantics, observed_on=date(2026, 8, 18)),
            'composite': lambda: tools._build_composite_metric_core(request, composite, datasets, semantics, observed_on=date(2026, 8, 18)),
            'ratio': lambda: tools._build_ratio_metric_core(request, ratio, datasets, semantics, observed_on=date(2026, 8, 18)),
            'comparison': lambda: tools._build_comparison_metric_query({**request, 'comparison': {'kind': 'previous_period'}},
                        base, datasets, semantics, 10, observed_on=date(2026, 8, 18)),
        }
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
            conn.execute('CREATE TABLE vk_dwd.receive_bill_detail_dwd(detail_deal_amount REAL, exchange_rate REAL, bill_status TEXT, bill_time TEXT)')
            conn.executemany('INSERT INTO vk_dwd.receive_bill_detail_dwd VALUES(?,?,?,?)',
                             [(None, 7, 'C', '2026-08-15'), (10, 7, 'C', '2026-07-15')])
            for label, compile_query in compiles.items():
                with self.subTest(path=label):
                    sql, params, _ = compile_query()
                    row = dict(conn.execute(sql.replace('%s', '?'), params).fetchone())
                    self.assertIsNone(row['metric_value'])
                    self.assertGreater(row['missing_value_count'], 0)
                    if label == 'comparison':
                        self.assertEqual(70, row['comparison_value'])
                        self.assertIsNone(row['delta_value'])
                        self.assertIsNone(row['change_rate'])


if __name__ == '__main__':
    unittest.main()
