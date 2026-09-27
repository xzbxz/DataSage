"""Independent finite-analysis counterexamples using hand-authored source rows.

This is an in-memory SQLite-compatible SQL/projection test, not MySQL, native
Hermes registration, authentication, model behavior, or real HCM business data.
The prices/rolls are intentionally unrelated to the incident's 451/437 values.
"""
from datetime import date, datetime
from pathlib import Path
from collections import Counter
import importlib
import json
import os
import re
import sqlite3
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKG = 'datasage_free_analysis_independent_tests'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'plugins/datasage-query')]
sys.modules[PKG] = pkg
ct = importlib.import_module(PKG + '.contracts')
aq = importlib.import_module(PKG + '.analytical_queries')
tools = importlib.import_module(PKG + '.tools')
security = importlib.import_module(PKG + '.db_security')
analysis_contract = importlib.import_module(PKG + '.analysis_contract')
analysis_queries = importlib.import_module(PKG + '.analysis_queries')
wire = importlib.import_module(PKG + '.wire')

def conn():
 c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
 for s in ('vk_dw','vk_dwd','vk_ods','vk_ai'):c.execute(f"ATTACH DATABASE ':memory:' AS {s}")
 c.executescript('''
 CREATE TABLE vk_dw.inventory_barcode_detail_bymonth_dw(id INTEGER,month_date TEXT,goods_id INTEGER,goods_sku_id INTEGER,goods_name TEXT,unit TEXT,whse_dept TEXT,goods_num REAL,piece_num REAL,whse_org TEXT,whse_type TEXT,is_handing_sales TEXT,is_discountable TEXT);
 CREATE TABLE vk_ods.slow_moving_goods_ods(id INTEGER,goods_id INTEGER,goods_sku_id INTEGER,goods_name TEXT,source_unit TEXT,whse_dept TEXT,goods_num REAL,piece_num REAL,is_whitelist TEXT,slow_label TEXT);
 CREATE TABLE vk_dwd.delivery_bill_barcode_detail_dwd(goods_id INTEGER,goods_sku_id INTEGER,whse_dept TEXT,unit TEXT,goods_num REAL,piece_num REAL,delivery_time TEXT,whse_id INTEGER,bill_type TEXT COLLATE NOCASE,is_inner_cus TEXT,sale_bill_goods_id INTEGER,sales_id INTEGER,sales_name TEXT,deal_price REAL,ddp_price REAL);
 CREATE TABLE vk_dwd.delivery_return_detail_dwd(goods_id INTEGER,goods_sku_id INTEGER,whse_dept TEXT,unit TEXT,return_goods_num REAL,return_piece_num REAL,statement_time TEXT,in_whse_id INTEGER,sale_bill_type TEXT COLLATE NOCASE,is_inner_cus TEXT,status INTEGER,complnt_type INTEGER,channel_type INTEGER,sales_id INTEGER,sales_name TEXT);
 CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd(goods_detail_id INTEGER,bill_status INTEGER);
 CREATE TABLE vk_dwd.whse_info_dwd(whse_id INTEGER,dept_name TEXT);
 ''')
 c.create_collation('utf8mb4_bin',lambda x,y:(x>y)-(x<y))
 c.create_function('CONVERT',1,lambda x:x)
 c.create_function('NOW',1,lambda _:'2026-09-26 15:35:00')
 c.create_function('UTC_TIMESTAMP',1,lambda _:'2026-09-26 07:35:00')
 c.create_function('DATE_FORMAT',2,lambda x,f:datetime.fromisoformat(str(x)).strftime(f) if x else None)
 c.create_function('GREATEST',-1,lambda *a:None if None in a else max(a))
 c.execute("INSERT INTO vk_dw.inventory_barcode_detail_bymonth_dw VALUES (1,'2026-08',1,11,'fixture','m','HCM',30,3,'fixture','普通仓','y','n')")
 c.execute("INSERT INTO vk_ods.slow_moving_goods_ods VALUES (1,1,11,'fixture','m','HCM',30,3,'n','处理')")
 c.execute("INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (1,6)")
 c.execute("INSERT INTO vk_dwd.whse_info_dwd VALUES (1,'HCM')")
 return c

def addout(c,price,rolls,*,ddp=100,qty='auto',goods=1,day='2026-09-20 00:00:00',inner='n'):
 if qty=='auto':qty=rolls*10
 c.execute('INSERT INTO vk_dwd.delivery_bill_barcode_detail_dwd VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(goods,11,'HCM','m',qty,rolls,day,1,'bulk',inner,1,7,'fixture',price,ddp))
def addret(c,rolls):
 c.execute('INSERT INTO vk_dwd.delivery_return_detail_dwd VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(1,11,'HCM','m',rolls*10,rolls,'2026-09-21',1,'bulk','n',4,1,1,7,'fixture'))
def adapt(sql):
 return sql.replace('%s','?').replace('%%','%').replace(' USING utf8mb4','').replace('<=>','IS')

def request(*, op='lte', value='0.5', analysis=True, dimensions=None):
    raw = dict(request_id='free_probe', domain='inventory', mode='metric',
               metric='registered_slow_monthly_net_outbound',
               dimensions=dimensions or ['unit'],
               metric_filters={'warehouse_department': 'HCM'},
               calendar_month='2026-09')
    if analysis:
        raw['analysis'] = {'row_filters': [{'field': 'price_to_ddp_ratio', 'op': op, 'value': value}]}
    return raw


def compiled(c, raw):
    normalized, datasets, semantics = tools._validate_request_plan_without_entities(raw, observed_on=date(2026,9,26))
    sql, params, scope = aq.build_analytical_metric_query(
        normalized, semantics['metrics'][normalized['metric']], datasets, semantics, 100,
        observed_on=date(2026,9,26))
    cursor = c.execute(adapt(sql), params)
    return [dict(r) for r in cursor], [d[0] for d in cursor.description], scope, sql, params


def projected(c, raw):
    normalized, datasets, semantics = tools._validate_request_plan_without_entities(raw, observed_on=date(2026,9,26))
    prepared = dict(request=normalized, datasets=datasets, semantics=semantics,
                    resolved_entities=[], entity_resolution_db_call_count=0)
    # Explicit synthetic test double for the existing execute_query boundary.
    source = dict(schema='datasage-query-source-evidence/v1',identity_sha256='1'*64,
                  connection_verified=True, transport_mode='plaintext',transport_policy_verified=True,
                  grant_policy='strict_object_read_only',grants_verified=True,read_only=True,
                  source_commitment_sha256='2'*64,security_evidence_sha256='')
    source['security_evidence_sha256'] = security._source_evidence_hash(source)
    def execute(sql, params, limit, **kwargs):
        rows = [dict(row) for row in c.execute(adapt(sql),params)]
        return rows[:limit], len(rows)>limit, source
    result = tools._run_one(raw,prepared=prepared,execute_query=execute,period_observed_on=date(2026,9,26))
    if result.get('status') != 'success':
        raise AssertionError(result.get('error'))
    tools._seal_claim_ids([result])
    model = tools._model_wire_result(result,request=normalized)
    return model


class FreeAnalysisIndependentTests(unittest.TestCase):
    def setUp(self):
        self.c=conn()
        self.addCleanup(self.c.close)

    def fixture(self):
        for price, rolls in ((49,3),(50,2),(60,4),(80,5)):
            addout(self.c,price,rolls)

    def row(self, **kwargs):
        return compiled(self.c,request(**kwargs))[0][0]

    def test_half_inclusive_is_executable_without_a_new_kpi(self):
        self.fixture();r=self.row()
        self.assertEqual(5,r['gross_rolls']);self.assertEqual(5,r['net_rolls'])
        self.assertEqual(2,r['analysis_match_count'])

    def test_strict_and_inclusive_boundary_are_distinct(self):
        self.fixture();self.assertEqual(3,self.row(op='lt')['gross_rolls'])
        self.assertEqual(5,self.row(op='lte')['gross_rolls'])

    def test_holdout_thresholds_are_parameters_not_hardcoded_half(self):
        prices=[0,10,25,49,50,62.5,75,80,99,120]
        for i,price in enumerate(prices):addout(self.c,price,i+1)
        for cut in ('0','0.25','0.625','0.75','0.91','1.25'):
            with self.subTest(cut=cut):
                expected=sum(i+1 for i,p in enumerate(prices) if p<=float(cut)*100)
                self.assertEqual(expected,self.row(value=cut)['gross_rolls'])

    def test_all_registered_comparisons_use_the_user_threshold(self):
        self.fixture()
        for op,expected in [('eq',2),('gt',9),('gte',11),('lt',3),('lte',5)]:
            with self.subTest(op=op):self.assertEqual(expected,self.row(op=op)['gross_rolls'])

    def test_interval_and_order_of_conditions_are_equivalent(self):
        self.fixture();raw=request()
        clauses=[{'field':'price_to_ddp_ratio','op':'gte','value':'0.5'},
                 {'field':'price_to_ddp_ratio','op':'lt','value':'0.8'}]
        raw['analysis']['row_filters']=clauses
        a=compiled(self.c,raw)[0][0]
        raw['analysis']['row_filters']=list(reversed(clauses));b=compiled(self.c,raw)[0][0]
        self.assertEqual(6,a['gross_rolls']);self.assertEqual(a,b)

    def test_contradictory_valid_bounds_return_complete_empty_not_unknown(self):
        self.fixture();raw=request();raw['analysis']['row_filters'] += [
            {'field':'price_to_ddp_ratio','op':'gt','value':'0.8'}]
        r=compiled(self.c,raw)[0][0]
        self.assertEqual(0,r['gross_rolls']);self.assertEqual('complete',r['metric_data_state'])

    def test_unattributed_return_preserves_gross_not_fabricated_net(self):
        self.fixture();addret(self.c,1);r=self.row()
        self.assertEqual(5,r['gross_rolls']);self.assertIsNone(r['net_rolls'])
        self.assertIsNone(r['return_rolls']);self.assertEqual('incomplete',r['metric_data_state'])
        self.assertEqual(1,r['analysis_unattributed_return_rolls'])

    def test_empty_slice_with_return_does_not_claim_zero_net(self):
        addout(self.c,80,5);addret(self.c,1);r=self.row()
        self.assertEqual(0,r['gross_rolls']);self.assertIsNone(r['net_rolls'])

    def test_price_unknown_has_subset_not_complete_gross(self):
        addout(self.c,49,3);addout(self.c,None,4);r=self.row()
        self.assertIsNone(r['gross_rolls']);self.assertIsNone(r['analysis_gross_rolls'])
        self.assertEqual(3,r['analysis_known_gross_rolls'])
        self.assertEqual(1,r['analysis_unknown_count'])

    def test_invalid_ddp_cannot_be_treated_as_excluded_price(self):
        addout(self.c,49,3)
        for ddp in (None,0,-100):addout(self.c,5,1,ddp=ddp)
        r=self.row();self.assertEqual(3,r['analysis_unknown_count'])
        self.assertIsNone(r['analysis_gross_rolls']);self.assertEqual('incomplete',r['metric_data_state'])

    def test_no_current_promotion_price_needed_for_historical_slice(self):
        # The ODS fixture deliberately has no promotion-price column at all.
        addout(self.c,25,4)
        self.assertEqual(4,self.row(value='0.3')['gross_rolls'])

    def test_out_of_slice_missing_quantity_does_not_poison_slice(self):
        addout(self.c,49,3);addout(self.c,80,4,qty=None);r=self.row()
        self.assertEqual(30,r['metric_value']);self.assertEqual('complete',r['metric_data_state'])
        self.assertEqual(0,r['missing_value_count'])
        # Same parent population without a price slice is still incomplete.
        self.assertEqual('incomplete',self.row(analysis=False)['metric_data_state'])

    def test_selected_missing_quantity_keeps_roll_fact_but_not_scalar(self):
        addout(self.c,49,3,qty=None);r=self.row()
        self.assertEqual(3,r['gross_rolls']);self.assertIsNone(r['metric_value'])
        self.assertIsNone(r['analysis_gross_quantity']);self.assertEqual('incomplete',r['metric_data_state'])

    def test_analysis_result_has_no_duplicate_fact_column_names(self):
        self.fixture()
        for dimensions in (['unit'],['product','unit'],['salesperson','unit']):
            with self.subTest(dimensions=dimensions):
                _,names,_,_,_=compiled(self.c,request(dimensions=dimensions))
                dup={k:v for k,v in Counter(names).items() if v>1}
                self.assertEqual({},dup)

    def test_model_wire_preserves_unknown_and_known_subset_separately(self):
        addout(self.c,49,3);addout(self.c,None,4)
        m=projected(self.c,request());f=m['claim_ledger'][0]['facts']
        self.assertEqual('incomplete',m['data_state'])
        self.assertIsNone(f['analysis_gross_rolls']);self.assertEqual(3,f['analysis_known_gross_rolls'])
        self.assertNotIn('high_net_rolls',f)

    def test_model_wire_keeps_gross_with_unattributed_return(self):
        self.fixture();addret(self.c,1);m=projected(self.c,request())
        f=m['claim_ledger'][0]['facts']
        self.assertEqual(5,f['gross_rolls']);self.assertIsNone(f['net_rolls'])
        self.assertEqual('incomplete',m['data_state'])
        self.assertEqual('inventory_flow',m['analysis_context']['operation'])

    def test_model_wire_selected_complete_not_parent_incomplete(self):
        addout(self.c,49,3);addout(self.c,80,4,qty=None)
        m=projected(self.c,request());self.assertEqual('rows',m['data_state'])
        self.assertEqual(30,m['claim_ledger'][0]['facts']['metric_value'])

    def test_parent_high_kpi_is_unchanged_and_not_a_lower_band(self):
        self.fixture();addret(self.c,1);r=self.row(analysis=False)
        self.assertEqual(13,r['net_rolls']);self.assertEqual(4,r['high_net_rolls'])
        self.assertEqual(5,self.row()['gross_rolls'])
        self.assertNotEqual(r['net_rolls']-r['high_net_rolls'],self.row()['gross_rolls'])

    def test_parent_aggregates_do_not_identify_low_band(self):
        self.fixture();addret(self.c,1);a=self.row(analysis=False);low=self.row()['gross_rolls']
        self.c.execute('UPDATE vk_dwd.delivery_bill_barcode_detail_dwd SET deal_price=65 WHERE deal_price<=50')
        b=self.row(analysis=False)
        self.assertEqual(a['net_rolls'],b['net_rolls']);self.assertEqual(a['high_net_rolls'],b['high_net_rolls'])
        self.assertEqual(5,low);self.assertEqual(0,self.row()['gross_rolls'])

    def test_outside_opening_pool_period_and_invalid_customer_are_excluded(self):
        addout(self.c,49,3);addout(self.c,10,100,goods=2)
        addout(self.c,10,100,day='2026-10-01');addout(self.c,10,100,inner='y')
        self.assertEqual(3,self.row()['gross_rolls'])

    def test_current_month_observation_time_is_explicit(self):
        self.fixture();r=self.row();self.assertEqual('2026-09-26 15:35:00',r['monthly_window_end'])
        self.c.create_function('NOW',1,lambda _:'2026-09-26 15:40:00')
        self.assertEqual('2026-09-26 15:40:00',self.row()['monthly_window_end'])

    def test_daily_cutoff_is_not_silently_accepted_as_full_calendar_month(self):
        raw=request();raw.pop('calendar_month');raw['time_range']={'start':'2026-09-01','end':'2026-09-26'}
        with self.assertRaises(Exception):compiled(self.c,raw)

    def test_catalog_publishes_the_actual_price_predicate(self):
        payload=json.loads(ct.datasage_catalog({'requests':[{'domain':'inventory','metric':'registered_slow_monthly_net_outbound'}]}))
        text=json.dumps(payload,ensure_ascii=False)
        self.assertIn('price_to_ddp_ratio',text);self.assertIn('analysis_fields',text)
        self.assertNotIn('"status": "failed"',text)

    def test_documented_examples_are_valid_for_their_named_metric(self):
        text=(ROOT/'skills/business-analytics/datasage/references/query-rules.md').read_text(encoding='utf-8')
        examples=[json.loads(x) for x in re.findall(r'```json\s*(.*?)```',text,re.S)
                  if '"analysis"' in x and '"detail"' not in x]
        self.assertGreaterEqual(len(examples),2)
        for domain,metric,example in [('inventory','registered_slow_monthly_net_outbound',examples[0]),
                                      ('receivable','open_receivable_amount',examples[1])]:
            _,sem=ct.execution_contracts(domain)
            self.assertTrue(analysis_contract.validate_analysis_for_metric(example['analysis'],sem['metrics'][metric],metric_code=metric))

    def test_cross_domain_field_mix_remains_rejected(self):
        raw=request();raw['analysis']['group_filters']=[{'field':'metric_value','op':'gte','value':'10000'}]
        with self.assertRaises(Exception):compiled(self.c,raw)

    def test_source_documents_do_not_promote_one_example_to_user_defaults(self):
        soul=(ROOT/'SOUL.md').read_text(encoding='utf-8')
        self.assertNotIn('The intended range is RMB 10,000',soul)
        self.assertNotIn('For target groups, `completion_rate <0.8`',soul)
        self.assertIn("current user's metric and thresholds",soul)

    def test_original_receivable_analysis_has_visible_execution_restriction(self):
        payload=json.loads(ct.datasage_catalog({'requests':[{'domain':'receivable','metric':'open_receivable_amount_original'}]}))
        text=json.dumps(payload,ensure_ascii=False)
        self.assertIn('execution_requirements',text)
        self.assertIn('"exact_metric_analysis_supported": false',text)
        self.assertIn('"effective_metric": "open_receivable_amount"',text)
        raw=dict(request_id='orig',mode='metric',domain='receivable',metric='open_receivable_amount_original',
                 dimensions=['customer'],analysis={'group_filters':[{'field':'metric_value','op':'gte','value':'12000'}]})
        with self.assertRaises(Exception) as caught:
            tools._validate_request_plan_without_entities(raw,observed_on=date(2026,9,26))
        self.assertEqual('ANALYSIS_UNSUPPORTED_COMBINATION',caught.exception.code)

    def test_rmb_counterpart_is_explicit_resolution_not_silent_original_sum(self):
        raw=dict(request_id='rmb',mode='metric',domain='receivable',metric='open_receivable_amount_original',
                 currency_basis='rmb',dimensions=['customer'],
                 analysis={'group_filters':[{'field':'metric_value','op':'gte','value':'12000'}]})
        normalized,_,_=tools._validate_request_plan_without_entities(raw,observed_on=date(2026,9,26))
        self.assertEqual('open_receivable_amount',normalized['metric'])
        self.assertEqual('rmb',analysis_contract.required_analysis_currency_basis(normalized['metric']))

if __name__ == '__main__':unittest.main()
