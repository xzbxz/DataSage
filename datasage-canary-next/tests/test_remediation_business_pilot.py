"""Five isolated SQL/evidence pilots. No model, identity binding or live database.

Source records and expected numbers are hand-authored. Contracts are read only
by the implementation being exercised, never to compute expected answers.
"""
from contextlib import closing
from datetime import date, timedelta
import importlib
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'datasage_business_pilot_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / 'plugins/datasage-query')]
sys.modules[PACKAGE] = package
tools = importlib.import_module(f'{PACKAGE}.tools')
security = importlib.import_module(f'{PACKAGE}.db_security')
OBSERVED_ON = date(2026, 9, 2)
RESULTS = []

DDL = '''
CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd(delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT, delivery_time TEXT, customer_dept TEXT);
CREATE TABLE vk_dwd.delivery_return_detail_dwd(return_amount_rmb REAL, status INTEGER, complnt_type INTEGER, channel_type INTEGER, is_inner_cus TEXT, statement_time TEXT, customer_dept TEXT, sales_name TEXT);
CREATE TABLE vk_dwd.receive_bill_detail_dwd(detail_receive_rmb REAL, detail_deal_amount REAL, exchange_rate REAL, bill_status TEXT, bill_time TEXT);
CREATE TABLE vk_dwd.receive_return_bill_detail_dwd(detail_return_rmb REAL, bill_status TEXT, bill_time TEXT);
CREATE TABLE vk_dwd.delivery_target_split_dwd(detail_target_rmb REAL, is_inner_cus TEXT, year_month TEXT, sales_name TEXT);
CREATE TABLE vk_dwd.sale_bill_split_dwd(delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT, delivery_time TEXT, sales_name TEXT);
CREATE TABLE vk_dwd.receivable_bill_detail_dwd(detail_unsettled_amount REAL, exchange_rate REAL, bill_status TEXT, bill_time TEXT, is_inner_cus TEXT, customer_id TEXT, customer_no TEXT, customer_name TEXT, org_name TEXT, currency_no TEXT);
CREATE TABLE vk_dwd.customer_credit_dwd(customer_id TEXT, org_name TEXT, currency_no TEXT, credit_days REAL);
'''


def sqlite_sql(sql):
    """Only adapt parameter/date syntax; no arithmetic or filters are rewritten."""
    sql = re.sub(r'DATE_ADD\((`[^`]+`\.`[^`]+`), INTERVAL (`[^`]+`\.`[^`]+`) DAY\)', r'DATE_ADD_DAY(\1, \2)', sql)
    return sql.replace('%s', '?')


class BusinessPilotTests(unittest.TestCase):
    def setUp(self):
        self.started = time.perf_counter()
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        self.connection.executescript(DDL)
        self.connection.create_function('CURDATE', 0, lambda: OBSERVED_ON.isoformat())
        self.connection.create_function('DATE_ADD_DAY', 2, lambda day, days: None if day is None or days is None else (date.fromisoformat(day[:10]) + timedelta(days=days)).isoformat())
        self.connection.create_function('DATEDIFF', 2, lambda end, start: None if end is None or start is None else (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days)
        self.connection.create_function('GREATEST', -1, lambda *values: None if any(value is None for value in values) else max(values))
        self.trace = []
        for target in ('socket.socket.connect', 'socket.create_connection'):
            blocker = patch(target, side_effect=AssertionError('network forbidden in offline business pilot'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def insert(self, table, rows):
        if rows:
            self.connection.executemany(f'INSERT INTO vk_dwd.{table} VALUES ({",".join("?" for _ in rows[0])})', rows)

    def query(self, metric, domain, *, filters=None, dimensions=None, month='2026-08', **extra):
        raw = dict(request_id=f'pilot_{len(self.trace)+1}', domain=domain, mode='metric', metric=metric,
                   dimensions=dimensions or [], metric_filters=filters or {}, **extra)
        if month: raw['calendar_month'] = month
        normalized, datasets, semantics = tools._validate_request_plan_without_entities(raw, observed_on=OBSERVED_ON)
        prepared = dict(request=normalized, datasets=datasets, semantics=semantics, resolved_entities=[], entity_resolution_db_call_count=0)
        source = dict(schema='datasage-query-source-evidence/v1', identity_sha256='1'*64, connection_verified=True,
                      transport_mode='plaintext', transport_policy_verified=True, grant_policy='strict_object_read_only',
                      grants_verified=True, read_only=True, source_commitment_sha256='2'*64, security_evidence_sha256='')
        source['security_evidence_sha256'] = security._source_evidence_hash(source)
        trace = {'request': raw, 'source_kind': 'synthetic_in_memory_sqlite'}
        def execute(sql, params, limit, *, deadline_at=None):
            adapted = sqlite_sql(sql)
            rows = [dict(row) for row in self.connection.execute(adapted, params)]
            trace.update(sql=sql, sqlite_sql=adapted, params=params, database_rows=rows)
            return rows[:limit], len(rows) > limit, source
        result = tools._run_one(raw, prepared=prepared, execute_query=execute, period_observed_on=OBSERVED_ON)
        self.assertEqual('success', result['status'], result.get('error'))
        tools._seal_claim_ids([result])
        projected = tools._model_wire_result(result, request=normalized)
        self.assertEqual('success', projected['status'], projected.get('error'))
        trace['model_evidence'] = projected
        self.trace.append(trace)
        return result['rows']

    def record(self, case_id, expected, actual, limitation=None):
        self.assertEqual(expected, actual)
        RESULTS.append({'case_id': case_id, 'offline_sql_evidence_status': 'passed', 'expected_synthetic_facts': expected,
            'actual_offline_facts': actual, 'offline_elapsed_ms': round((time.perf_counter()-self.started)*1000, 3),
            'real_data_reconciliation': 'not_run', 'real_model_answer_review': 'not_run', 'channel_delivery': 'not_run',
            'real_latency_ms': None, 'provider_usage': None, 'real_cost': None,
            'limitation': limitation or 'SQL/evidence only; no natural-language planning, entity preflight or authorization exercised.', 'trace': self.trace})

    def test_B01_net_delivery(self):
        self.insert('sale_bill_goods_detail_dwd', [(100000,6,'n','2026-08-15','HCM'), (50000,6,'n','2026-08-16','HCM'),
            (900000,6,'n','2026-08-15','HN'), (800000,6,'n','2026-07-15','HCM'), (700000,6,'y','2026-08-15','HCM'), (600000,1,'n','2026-08-15','HCM')])
        self.insert('delivery_return_detail_dwd', [(10000,4,1,1,'n','2026-08-17','HCM','甲'), (5000,4,1,1,'n','2026-08-18','HCM','乙')])
        # The corresponding pilot prompt explicitly requests gross, returns and net.
        actual = {key:self.query(metric,'delivery',filters={'department':'HCM'},
                  **({'delivery_scope':'explicit_gross'} if metric == 'gross_delivery_amount' else {}))[0]['metric_value'] for key,metric in
                  [('gross_rmb','gross_delivery_amount'),('returns_rmb','return_amount'),('net_rmb','delivery_amount')]}
        self.record('B01', {'gross_rmb':150000,'returns_rmb':15000,'net_rmb':135000}, actual)

    def test_B04_registered_and_settled_receipts(self):
        self.insert('receive_bill_detail_dwd', [(120000,14000,7,'C','2026-08-15'), (800000,800000,1,'A','2026-08-15'), (700000,700000,1,'C','2026-07-15')])
        self.insert('receive_return_bill_detail_dwd', [(20000,'C','2026-08-17')])
        actual = {'net_registered_rmb':self.query('net_receipt_amount','receipt')[0]['metric_value'],
                  'gross_settled_receipts_rmb':self.query('actual_receipt_amount','receipt')[0]['metric_value']}
        self.record('B04', {'net_registered_rmb':100000,'gross_settled_receipts_rmb':98000}, actual,
                    'Actual receipt is gross settled receipts, not net settled cash flow. Model explanation not reviewed.')

    def test_B10_salesperson_target(self):
        self.insert('delivery_target_split_dwd', [(125000,'n','2026-08','甲'), (900000,'n','2026-08','乙')])
        self.insert('sale_bill_split_dwd', [(100000,6,'n','2026-08-15','甲'), (800000,6,'n','2026-08-15','乙')])
        self.insert('delivery_return_detail_dwd', [(20000,4,1,1,'n','2026-08-17','HCM','甲')])
        self.insert('sale_bill_goods_detail_dwd', [(110000,6,'n','2026-08-15','HCM')])
        row = self.query('delivery_target_completion','target',filters={'salesperson':'甲'},attribution_mode='salesperson_allocation')[0]
        actual = {key:row[key] for key in ['target_amount_rmb','actual_amount_rmb','completion_rate','gap_amount_rmb']}
        self.record('B10', {'target_amount_rmb':125000,'actual_amount_rmb':80000,'completion_rate':0.64,'gap_amount_rmb':45000}, actual)

    def test_B07_customer_overdue_risk(self):
        self.insert('receivable_bill_detail_dwd', [(50000,1,'C','2026-07-14','n','A','A','甲','ORG','CNY'),
            (10000,1,'C','2026-06-04','n','B','B','乙','ORG','CNY'), (900000,1,'C','2026-09-01','n','C','C','未到期','ORG','CNY'),
            (800000,1,'A','2026-06-04','n','A','A','甲','ORG','CNY')])
        self.insert('customer_credit_dwd', [('A','ORG','CNY',30),('B','ORG','CNY',30),('C','ORG','CNY',30)])
        amounts = self.query('overdue_receivable_amount','receivable',dimensions=['customer'],month=None)
        days = self.query('overdue_days','receivable',dimensions=['customer'],month=None)
        actual = {'amounts_rmb':{r['customer_name']:r['metric_value'] for r in amounts}, 'overdue_days':{r['customer_name']:r['metric_value'] for r in days}}
        self.record('B07', {'amounts_rmb':{'甲':50000,'乙':10000}, 'overdue_days':{'甲':20,'乙':60}}, actual,
                    'As-of date is frozen at 2026-09-02. This is not a historical 2026-08-31 database snapshot or a model credit-risk judgment.')

    def test_B15_department_and_period_correction(self):
        self.insert('sale_bill_goods_detail_dwd', [(5000000,6,'n','2026-03-15','HN'), (450000,6,'n','2026-08-15','HCM'),
            (990000,6,'n','2026-08-15','HN'), (120000,6,'n','2026-07-15','HCM')])
        self.insert('delivery_return_detail_dwd', [(50000,4,1,1,'n','2026-08-17','HCM','甲')])
        old = self.query('delivery_amount','delivery',filters={'department':'HN'},month=None,time_range={'start':'2026-01-01','end':'2026-07-01'})[0]['metric_value']
        corrected = self.query('delivery_amount','delivery',filters={'department':'HCM'})[0]['metric_value']
        self.assertNotIn('HN', self.trace[-1]['params'])
        self.assertIn('HCM', self.trace[-1]['params'])
        self.assertIn('2026-08-01', self.trace[-1]['params'])
        self.record('B15', {'old_HN_H1_rmb':5000000,'corrected_HCM_Aug_rmb':400000},
                    {'old_HN_H1_rmb':old,'corrected_HCM_Aug_rmb':corrected},
                    'Two explicit structured requests are exercised. Hermes natural-language context correction remains untested.')


if __name__ == '__main__':
    destination = Path(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == '--report' else None
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BusinessPilotTests))
    if destination:
        destination.write_text(json.dumps({'profile_layer': 'real_compiler_SQL_seal_and_model_evidence', 'data': 'isolated_synthetic_fixtures',
            'no_network': True, 'all_passed': result.wasSuccessful(), 'results':RESULTS},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
