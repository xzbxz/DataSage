"""Remaining pilot cases through registered public handlers, offline only.

Only readiness and SQL I/O/snapshot transport are replaced. The actual schemas,
entitlement guards, compiler, calculations, seals and final JSON wire execute.
Synthetic session bindings never reach a live database; sockets are blocked.
"""
from datetime import date, timedelta
from decimal import Decimal
import importlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

from test_remediation_business_pilot import DDL, sqlite_sql
from gateway.session_context import set_session_vars, clear_session_vars

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / 'plugins/datasage-query'
spec = importlib.util.spec_from_file_location('datasage_remaining_public_tests', PLUGIN_ROOT/'__init__.py', submodule_search_locations=[str(PLUGIN_ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
health = importlib.import_module(f'{spec.name}.runtime_health')
OBSERVED = date(2026, 9, 2)
RECORDS = []


class Context:
    def __init__(self): self.handlers = {}
    def get_config(self, name, default=None): return 1 if name == 'max_concurrent_queries' else default
    def register_tool(self, **entry): self.handlers[entry['name']] = entry['handler']


def metric(code, domain, *, request_id='r', month='2026-08', **extra):
    request = {'request_id':request_id, 'domain':domain, 'mode':'metric', 'metric':code, **extra}
    if 'complete_change_decomposition' not in extra: request.setdefault('dimensions', [])
    if month: request['calendar_month'] = month
    return request


def facts(result):
    return [row.get('facts', {}) for row in result.get('rows', [])]


def first_of_month_add(value, months):
    if value is None: return None
    day=date.fromisoformat(value[:10])
    if day.day != 1: raise ValueError("adapter only supports compiled first-of-month arithmetic")
    ordinal=day.year*12+day.month-1+months
    return date(ordinal//12,ordinal%12+1,1).isoformat()


def first_of_month_diff(start, end):
    if start is None or end is None: return None
    a,b=date.fromisoformat(start[:10]),date.fromisoformat(end[:10])
    if a.day != 1 or b.day != 1: raise ValueError("adapter only supports first-of-month differences")
    return (b.year-a.year)*12+b.month-a.month


class RemainingCaseTests(unittest.TestCase):
    def setUp(self):
        self.started = time.perf_counter()
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_dw")
        self.conn.executescript(DDL + '''
            ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN sorted_goods_num REAL;
            ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN unit TEXT;
            ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN return_goods_num REAL;
            ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN unit TEXT;
            ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN sales_id TEXT;
            ALTER TABLE vk_dwd.sale_bill_split_dwd ADD COLUMN sales_id TEXT;
            ALTER TABLE vk_dwd.delivery_target_split_dwd ADD COLUMN sales_id TEXT;
            CREATE TABLE vk_dw.goods_turnover_basic_data_dw(cost_amount_rmb REAL,ddp_amount_rmb REAL,pur_delivery_rmb REAL,bill_date TEXT);
            CREATE TABLE vk_dw.customer_debt_bymonth_dw(debt_amount_rmb REAL,bill_date TEXT,is_inner_cus TEXT);
            CREATE TABLE vk_dwd.inventory_cost_dwd(cost_amount_rmb REAL,bill_date TEXT);
            CREATE TABLE vk_dw.inventory_barcode_detail_dw(ddp_amount_rmb REAL,status INTEGER);
            CREATE TABLE vk_dwd.customer_dwd(customer_id TEXT,customer_no TEXT,customer_name TEXT,is_delete TEXT,is_void TEXT);
        ''')
        self.conn.create_function('CURDATE', 0, lambda: OBSERVED.isoformat())
        self.conn.create_function('DATE_FORMAT', 2, lambda day, fmt: None if day is None else date.fromisoformat(day[:10]).strftime(fmt))
        self.conn.create_function('DATE_ADD_DAY', 2, lambda day, days: None if day is None or days is None else (date.fromisoformat(day[:10])+timedelta(days=days)).isoformat())
        self.conn.create_function('DATEDIFF', 2, lambda end,start: None if end is None or start is None else (date.fromisoformat(end[:10])-date.fromisoformat(start[:10])).days)
        self.conn.create_function('GREATEST', -1, lambda *v: None if None in v else max(v))
        self.conn.create_function('CONCAT', -1, lambda *v: None if None in v else ''.join(map(str,v)))
        self.conn.create_function('STR_TO_DATE', 2, lambda value,fmt: None if value is None else date.fromisoformat(value).isoformat())
        self.conn.create_function('DATE_ADD_MONTH', 2, first_of_month_add)
        self.conn.create_function('DATE_SUB_MONTH', 2, lambda value,n: first_of_month_add(value,-n))
        self.conn.create_function('MONTH_DIFF', 2, first_of_month_diff)
        self.conn.create_function('CHAR_LENGTH', 1, lambda v: len(v) if v is not None else None)
        self.conn.create_function('LEFT_TEXT', 2, lambda value,n: value[:n])
        self.conn.create_function('LOCATE', 2, lambda needle,value: value.find(needle)+1)
        self.sql_trace, self.calls = [], []
        self.fail_receipts = False
        self.ctx = Context()
        plugin.register(self.ctx)
        self.source = dict(schema='datasage-query-source-evidence/v1',identity_sha256='1'*64,connection_verified=True,
            transport_mode='plaintext',transport_policy_verified=True,grant_policy='strict_object_read_only',grants_verified=True,
            read_only=True,source_commitment_sha256='2'*64,security_evidence_sha256='')
        self.source['security_evidence_sha256'] = plugin.tools.db_security._source_evidence_hash(self.source) if hasattr(plugin.tools,'db_security') else importlib.import_module(f'{spec.name}.db_security')._source_evidence_hash(self.source)
        owner = self
        class Snapshot:
            marker = 'isolated-sqlite-snapshot'
            source_evidence_ref = owner.source
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args, **kwargs): return owner.execute(*args, **kwargs)
        for target, kwargs in [
            ('socket.socket.connect', {'side_effect':AssertionError('offline network tripwire')}),
            ('socket.socket.connect_ex', {'side_effect':AssertionError('offline network tripwire')}),
            ('socket.create_connection', {'side_effect':AssertionError('offline network tripwire')}),
        ]:
            p = patch(target, **kwargs); p.start(); self.addCleanup(p.stop)
        for target, attribute, value in [(health,'query_readiness_status',lambda: {'ready':True}),
            (plugin.tools,'_execute_with_source',self.execute),(plugin.tools,'_ConsistentSnapshotExecutor',Snapshot),
            (plugin.tools,'_business_today',lambda: OBSERVED),
            (plugin.tools.db_runtime,'connect',lambda **kw: (_ for _ in ()).throw(AssertionError('live DB forbidden')))]:
            p=patch.object(target,attribute,value);p.start();self.addCleanup(p.stop)

    def insert(self, table, columns, rows):
        self.conn.executemany(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in columns.split(","))})',rows)

    def execute(self, sql, params, limit, *, deadline_at=None):
        trace = {'sql':sql,'params':list(params),'limit':limit}
        self.sql_trace.append(trace)
        if self.fail_receipts and '`receive_bill_detail_dwd`' in sql:
            trace['injected_io_error']='BATCH_DEADLINE_EXCEEDED'
            raise plugin.tools.QueryFailure('BATCH_DEADLINE_EXCEEDED','Synthetic receipt I/O timeout',timeout=True,stage='business_sql')
        adapted = sqlite_sql(sql).replace('<=>','IS').replace('%%','%').replace('LEFT(', 'LEFT_TEXT(')
        adapted=re.sub(r'TIMESTAMPDIFF\(\s*MONTH\s*,','MONTH_DIFF(',adapted)
        adapted=adapted.replace('DATE_ADD(', 'DATE_ADD_MONTH(').replace('DATE_SUB(', 'DATE_SUB_MONTH(')
        adapted=re.sub(r',\s*INTERVAL\s+(\d+)\s+MONTH\)',r', \1)',adapted)
        trace['sqlite_sql'] = adapted
        rows=[dict(row) for row in self.conn.execute(adapted, params)]
        trace['database_rows']=rows
        return rows[:limit],len(rows)>limit,self.source

    def invoke(self, name, args, *, bound=True):
        tokens = set_session_vars(platform='wecom',source='wecom',user_id='OFFLINE-ONLY-SYNTHETIC' if bound else '',chat_id='OFFLINE-ONLY',chat_type='dm')
        try: result=json.loads(self.ctx.handlers[name](args))
        finally: clear_session_vars(tokens)
        self.calls.append({'tool':name,'args':args,'final_json':result})
        return result

    def query(self, *requests): return self.invoke('datasage_query',{'requests':list(requests)})

    def result(self, payload, rid='r'):
        self.assertIn(payload.get('status'),{'success','partial'},payload)
        return next(r for r in payload['results'] if r['request_id']==rid)

    def save(self, case_id, layer, actual, unverified):
        RECORDS.append({'case_id':case_id,'verified_layer':layer,'actual':actual,'unverified':unverified,
            'offline_elapsed_ms':round((time.perf_counter()-self.started)*1000,3),'calls':self.calls,'sql':self.sql_trace,
            'readiness_replaced':True,'identity':'synthetic offline ContextVars only','real_data':'not_run',
            'model_answer_review':'not_run','channel_delivery':'not_run','real_latency_ms':None,'real_cost':None})

    def delivery(self, rows):
        self.insert('vk_dwd.sale_bill_goods_detail_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept',rows)

    def returns(self, values):
        self.insert('vk_dwd.delivery_return_detail_dwd','return_amount_rmb,status,complnt_type,channel_type,is_inner_cus,statement_time,customer_dept',[(v,4,1,1,'n','2026-08-15','A') for v in values])

    def test_B02_return_rate(self):
        self.delivery([(200000,6,'n','2026-08-15','A')]);self.returns([20000])
        row=facts(self.result(self.query(metric('return_amount_rate','delivery'))))[0]
        self.assertEqual(0.1,row['metric_value'])
        self.save('B02','public_SQL_final_evidence',{'return_rate':row['metric_value']},['Model choice of denominator/explanation and causal restraint'])

    def test_B03_unit_boundary(self):
        self.insert('vk_dwd.sale_bill_goods_detail_dwd','sorted_goods_num,unit,bill_status,is_inner_cus,delivery_time',[(100,'m',6,'n','2026-08-15'),(20,'Pcs',6,'n','2026-08-15'),(3,'tao',6,'n','2026-08-15')])
        denied=self.query(metric('delivery_quantity','delivery'))
        self.assertEqual('failed',denied['status']);self.assertEqual(0,len(self.sql_trace))
        output=self.result(self.query(metric('delivery_quantity','delivery',dimensions=['unit'])))
        self.assertEqual({'m':100,'Pcs':20,'tao':3}, {r['dimensions'][0]['value']:r['facts']['metric_value'] for r in output['rows']})
        self.assertEqual(3,len(output['rows']))
        self.save('B03','public_rejection_and_grouped_SQL',{'unscoped_error':denied.get('error'),'separate_facts':facts(output)},['Model unit wording, unknown tao interpretation, unnecessary clarification'])

    def test_B05_null_empty_zero_controls(self):
        outcomes=[]
        for values,expected_state,expected in [([(10,7),(100,None)],'incomplete',None), ([(100,None)],'undefined',None), ([], 'empty',None), ([(0,7)],'zero',0)]:
            self.conn.execute('DELETE FROM vk_dwd.receive_bill_detail_dwd')
            self.insert('vk_dwd.receive_bill_detail_dwd','detail_deal_amount,exchange_rate,bill_status,bill_time',[(a,r,'C','2026-08-15') for a,r in values])
            result=self.result(self.query(metric('actual_receipt_amount','receipt')))
            self.assertEqual(expected_state,result['data_state'])
            if expected_state=='empty': self.assertFalse(result.get('rows'))
            else:self.assertEqual(expected,facts(result)[0].get('metric_value'))
            outcomes.append({'input':values,'state':result['data_state'],'facts':facts(result)})
        self.save('B05','public_SQL_final_evidence_positive_negative',outcomes,['Model disclosure of missing rate and avoidance of unsupported totals'])

    def test_B06_net_and_positive_debt(self):
        self.insert('vk_dw.customer_debt_bymonth_dw','debt_amount_rmb,bill_date,is_inner_cus',[(100000,'2026-08','n'),(-20000,'2026-08','n'),(50000,'2026-08','n'),(900000,'2026-07','n')])
        actual={k:facts(self.result(self.query(metric(m,'receivable',month=None))))[0]['metric_value'] for k,m in [('net', 'current_debt_amount'),('positive','positive_debt_amount')]}
        self.assertEqual({'net':130000,'positive':150000},actual)
        self.save('B06','public_SQL_final_evidence',actual,['Model interpretation of negative balances and collection risk'])

    def test_B08_inventory_null_coverage(self):
        self.insert('vk_dwd.inventory_cost_dwd','cost_amount_rmb,bill_date',[(100000,'2026-08'),(None,'2026-08'),(800000,'2026-07')])
        result=self.result(self.query(metric('month_end_inventory_cost_rmb','inventory',month=None)))
        self.assertEqual('incomplete',result['data_state']);self.assertIsNone(facts(result)[0]['metric_value'])
        self.assertEqual(1,facts(result)[0]['missing_value_count'])
        self.save('B08','public_SQL_final_evidence',facts(result),['Model coverage explanation; quantity cannot fill missing cost'])

    def test_B09_current_balance_only(self):
        self.insert('vk_dw.inventory_barcode_detail_dw','ddp_amount_rmb,status',[(100000,1),(50000,2),(900000,9)])
        result=self.result(self.query(metric('current_inventory_amount_rmb','inventory',month=None)))
        self.assertEqual(150000,facts(result)[0]['metric_value'])
        unavailable=[]
        for partial_cost_row in (False,True):
            if partial_cost_row:self.insert('vk_dw.goods_turnover_basic_data_dw','cost_amount_rmb,ddp_amount_rmb,pur_delivery_rmb,bill_date',[(None,150000,None,'2026-08')])
            turnover=self.result(self.query(metric('inventory_turnover_days','inventory',month=None)))
            self.assertIn(turnover['data_state'], {'empty','undefined'})
            self.assertTrue(all(row.get('metric_value') is None for row in facts(turnover)))
            unavailable.append({'partial_cost_row':partial_cost_row,'result':turnover})
        self.save('B09','public_SQL_current_balance_and_missing_turnover_inputs',{'current_inventory_rmb':150000,'turnover_controls':unavailable},['Model refusal to grade efficiency from current balance alone; no full positive turnover-formula acceptance or real MySQL calendar/DECIMAL validation'])

    def test_B11_missing_and_zero_target(self):
        self.insert('vk_dwd.delivery_target_split_dwd','detail_target_rmb,is_inner_cus,year_month,sales_name,sales_id',[(0,'n','2026-08','乙','B')])
        self.insert('vk_dwd.sale_bill_split_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,sales_name,sales_id',[(10000,6,'n','2026-08-15','甲','A'),(10000,6,'n','2026-08-15','乙','B')])
        result=self.result(self.query(metric('delivery_target_completion','target',dimensions=['salesperson'],attribution_mode='salesperson_allocation')))
        self.assertEqual(2,len(result['rows']))
        states={row['states']['target_data_state'] for row in result['rows']}
        self.assertEqual({'missing','zero'},states)
        by_name={row['dimensions'][0]['value']:row for row in result['rows']}
        self.assertIsNone(by_name['甲']['facts']['target_amount_rmb'])
        self.assertEqual(0,by_name['乙']['facts']['target_amount_rmb'])
        self.assertTrue(all(row['facts'].get('completion_rate') is None for row in result['rows']))
        self.save('B11','public_SQL_final_evidence',result['rows'],['Model distinction between unset and zero target; no unjustified performance grade'])

    def seed_overdue(self):
        values=[50000,30000,10000,6000,4000,3000,2000]
        self.insert('vk_dwd.receivable_bill_detail_dwd','detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no',[(v,1,'C','2026-01-01','n',str(i),str(i),f'C{i}','O','CNY') for i,v in enumerate(values)])
        self.insert('vk_dwd.customer_credit_dwd','customer_id,org_name,currency_no,credit_days',[(str(i),'O','CNY',30) for i in range(7)])

    def test_B12_top5_and_denominator(self):
        self.seed_overdue()
        top=self.result(self.query(metric('overdue_receivable_amount','receivable',month=None,dimensions=['customer'],limit=5)))
        self.assertEqual('truncated',top['data_state']);self.assertEqual(100000,sum(r['metric_value'] for r in facts(top)))
        self.assertFalse(any('company_share' in row for row in facts(top)))
        whole=self.result(self.query(metric('overdue_receivable_amount','receivable',month=None)))
        self.assertEqual(105000,facts(whole)[0]['metric_value'])
        self.save('B12','public_SQL_truncation_and_full_denominator',{'top5':100000,'top5_state':top['data_state'],'whole':105000},['Model must not invent missing denominator or call top5 the whole company; percentage calculation/wording unreviewed'])

    def test_B13_scorecard_availability_only(self):
        payload=self.invoke('datasage_catalog',{'requests':[{'view':'performance_scorecard'}]})
        lens=next(v for v in payload['results'][0]['candidate_lenses'] if v['lens']=='profitability')
        self.assertEqual('not_available',lens['status']);self.assertFalse(lens['candidates']);self.assertFalse(self.sql_trace)
        self.save('B13','catalog_availability_only',lens,['All cross-domain synthesis, evidence omission, actionability and company-health judgment require actual model review; no business numerical scenario executed'])

    def test_B14_comparison_and_decomposition(self):
        self.delivery([(600000,6,'n','2026-07-15','A'),(400000,6,'n','2026-07-15','B'),(900000,6,'n','2026-08-15','A'),(300000,6,'n','2026-08-15','B')])
        result=self.query(metric('delivery_amount','delivery',comparison={'kind':'previous_period'},complete_change_decomposition={'dimension':'department'}))
        self.assertEqual('success',result['status'],result)
        all_facts=[f for r in result['results'] for f in facts(r)]
        reconciliation=result['results'][0]['change_reconciliation']
        self.assertEqual(Decimal('200000'),Decimal(reconciliation['overall_delta']))
        self.assertTrue(any(f.get('delta_value')==300000 for f in all_facts),all_facts)
        self.assertTrue(any(f.get('delta_value')==-100000 for f in all_facts),all_facts)
        rates=[Decimal(f['net_change_contribution_rate']) for f in all_facts if 'net_change_contribution_rate' in f]
        self.assertIn(Decimal('1.5'),rates);self.assertIn(Decimal('-0.5'),rates)
        before=len(self.sql_trace)
        self.delivery([(None,6,'n','2026-08-15','A')])
        incomplete=self.query(metric('delivery_amount','delivery',comparison={'kind':'previous_period'},complete_change_decomposition={'dimension':'department'}))
        self.assertGreater(len(self.sql_trace),before)
        for r in incomplete.get('results',[]):
            for row in r.get('rows',[]):
                self.assertNotIn('structural_contribution',row.get('allowed_relations',[]))
                self.assertIsNone(row.get('facts',{}).get('net_change_contribution_rate'))
        self.save('B14','public_SQL_decomposition_and_missing_input_negative',{'complete_facts':all_facts,'overall_delta':reconciliation['overall_delta'],'missing_input_response':incomplete},['Natural-language causal discipline and explanation of >100%/negative contribution'])

    def test_B16_partial_failure(self):
        self.delivery([(150000,6,'n','2026-08-15','A')]);self.returns([15000])
        self.insert('vk_dwd.sale_bill_split_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,sales_name',[(150000,6,'n','2026-08-15','甲')])
        self.insert('vk_dwd.delivery_target_split_dwd','detail_target_rmb,is_inner_cus,year_month,sales_name',[(180000,'n','2026-08','甲')])
        self.fail_receipts=True
        payload=self.query(metric('delivery_amount','delivery',request_id='delivery'),metric('net_receipt_amount','receipt',request_id='receipt'),metric('delivery_target_completion','target',request_id='target',attribution_mode='salesperson_allocation'))
        self.assertEqual('partial',payload['status'])
        actual={r['request_id']:r['status'] for r in payload['results']}
        self.assertEqual({'delivery':'success','receipt':'timeout','target':'success'},actual)
        self.assertEqual(135000,facts(self.result(payload,'delivery'))[0]['metric_value'])
        self.assertEqual(0.75,facts(self.result(payload,'target'))[0]['completion_rate'])
        self.assertEqual('BATCH_DEADLINE_EXCEEDED',self.result(payload,'receipt')['error']['code'])
        self.save('B16','public_partial_failure_with_SQL_successful_branches',actual,['Model retains valid facts, explains receipt failure and avoids a complete overall conclusion; real timeout cancellation is covered by earlier tests, not this injected I/O failure'])

    def test_B17_same_name_customer_ambiguity(self):
        self.insert('vk_dwd.customer_dwd','customer_id,customer_no,customer_name,is_delete,is_void',[('A','A','pilot-same-name','n','n'),('B','B','pilot-same-name','n','n')])
        args={'token':'pilot-same-name','entity_types':['customer'],'domain':'delivery','metric':'delivery_amount'}
        ambiguous=self.invoke('datasage_entity_resolve',args)
        self.assertEqual('ambiguous',ambiguous['status'],ambiguous);self.assertTrue(ambiguous['must_stop_business_query'])
        self.conn.execute("DELETE FROM vk_dwd.customer_dwd WHERE customer_id='B'")
        unique=self.invoke('datasage_entity_resolve',args)
        self.assertEqual('resolved',unique['status'],unique)
        self.save('B17','partial_public_customer_ambiguity_SQL',{'two_customers':ambiguous,'one_customer':unique},['Original department-vs-customer Thai Kim ambiguity and user confirmation need model review plus an approved cross-role fixture. This test does not mutate the authoritative alias registry.'])

    def test_B18_action_rejection_only(self):
        self.assertEqual({'datasage_catalog','datasage_entity_resolve','datasage_query'},set(self.ctx.handlers))
        payload=self.query({'request_id':'freeze','domain':'customer_risk','mode':'action','metric':'freeze_customer'})
        self.assertEqual('failed',payload['status']);self.assertFalse(self.sql_trace)
        self.save('B18','public_unsupported_action_rejected',payload,['Model refusal to claim execution, human approval, credit advice, causal caution and material-risk discussion'])

    def test_calendar_adapter_is_limited_and_checked(self):
        self.assertEqual('2025-12-01',first_of_month_add('2026-01-01',-1))
        self.assertEqual('2024-03-01',first_of_month_add('2024-02-01',1))
        self.assertEqual(11,first_of_month_diff('2025-09-01','2026-08-01'))
        self.assertIsNone(first_of_month_add(None,1))
        with self.assertRaises(ValueError): first_of_month_add('2026-01-31',1)

    def test_unbound_caller_cannot_reach_sql(self):
        payload=self.invoke('datasage_query',{'requests':[metric('actual_receipt_amount','receipt')]},bound=False)
        self.assertEqual('DATA_ENTITLEMENT_DENIED',payload['error']['code']);self.assertFalse(self.sql_trace)


if __name__=='__main__':
    destination=Path(sys.argv[2]) if len(sys.argv)==3 and sys.argv[1]=='--report' else None
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RemainingCaseTests))
    if destination:destination.write_text(json.dumps({'all_tests_passed':result.wasSuccessful(),'records':RECORDS},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
