"""Independent registered-path regressions; no live database or paid model."""
import unittest
from unittest.mock import patch
import json
import test_profit_contract as profit
import test_remediation_remaining_cases as public
metric, plugin = public.metric, public.plugin

UNITS={'metric_value':'比例','completion_rate':'比例','target_amount_rmb':'人民币元','actual_amount_rmb':'人民币元','gap_amount_rmb':'人民币元'}

class PublicInterfaceRegressions(unittest.TestCase):
    def fixture(self,cls):
        h=cls();h.setUp();self.addCleanup(h.doCleanups)
        if not hasattr(self,'fixtures'):self.fixtures=[]
        self.fixtures.append(h);return h

    def profit(self):
        h=self.fixture(profit.ProfitContractTests)
        h.insert('vk_ads.goods_profit_ads','guid,bill_date,goods_id,goods_no,goods_name,dept_name,sale_amount_rmb,gross_profit_rmb',[
            ('a','2026-08','1','G1','Fabric','Alpha',100,30),('b','2026-08','2','G2','Fabric','Beta',200,30),
            ('c','2026-07','1','G1','Fabric','Alpha',100,20),('d','2026-07','2','G2','Fabric','Beta',200,10)])
        return h

    def dept_values(self,result,field='metric_value'):
        return {next(d['value'] for d in row['dimensions'] if d['label']=='产品利润归属部门'):row['facts'][field] for row in result['rows']}

    def test_profit_public_catalog_and_equal_amount_groups(self):
        h=self.profit()
        cat=h.invoke('datasage_catalog',{'requests':[{'domain':'profit','metric':'product_month_gross_profit'}]})
        self.assertIn('产品利润归属部门',json.dumps(cat,ensure_ascii=False))
        self.assertNotIn('dept_name',json.dumps(cat))
        r=h.result(h.query(metric('product_month_gross_profit','profit',dimensions=['department'])))
        self.assertEqual({'Alpha':30,'Beta':30},self.dept_values(r))

    def test_profit_default_and_empty_still_overall_explicit_filter_keeps_department(self):
        h=self.profit()
        for dims in [None,[]]:
            q=metric('product_month_gross_profit','profit')
            if dims is None:q.pop('dimensions')
            r=h.result(h.query(q));self.assertEqual([],r['rows'][0]['dimensions']);self.assertEqual(60,r['rows'][0]['facts']['metric_value'])
        r=h.result(h.query(metric('product_month_gross_profit','profit',dimensions=['department'],metric_filters={'department':'Alpha'})))
        self.assertEqual({'Alpha':30},self.dept_values(r))

    def test_profit_margin_and_comparison_preserve_department_value_pairs(self):
        h=self.profit()
        r=h.result(h.query(metric('product_month_gross_margin','profit',dimensions=['department'])))
        self.assertEqual({'Alpha':.3,'Beta':.15},self.dept_values(r))
        r=h.result(h.query(metric('product_month_gross_profit','profit',dimensions=['department'],comparison={'kind':'previous_period'})))
        self.assertEqual({'Alpha':10,'Beta':20},self.dept_values(r,'delta_value'))

    def test_one_group_is_not_an_overall_calculation_operand(self):
        h=self.profit()
        a=metric('product_month_gross_profit','profit',request_id='a',dimensions=['department'],metric_filters={'department':'Alpha'})
        b=metric('product_month_gross_profit','profit',request_id='b',metric_filters={'department':'Alpha'})
        r=h.invoke('datasage_query',{'requests':[a,b],'calculations':[{'calculation_id':'c','operation':'difference','left_request_id':'a','right_request_id':'b'}]})
        self.assertEqual('failed',r['calculations'][0]['status'])
        self.assertEqual('CALCULATION_REQUIRES_SCALAR',r['calculations'][0]['error']['code'])

    def target(self,ledger='delivery',targets=(100,200),actuals=(60,180)):
        h=self.fixture(public.RemainingCaseTests)
        if ledger=='delivery':
            h.conn.execute('CREATE TABLE vk_dwd.delivery_target_detail_dwd(detail_target_rmb REAL,is_inner_cus TEXT,year_month TEXT,customer_dept TEXT)')
            h.insert('vk_dwd.delivery_target_detail_dwd','detail_target_rmb,is_inner_cus,year_month,customer_dept',[(v,'n','2026-08',d) for v,d in zip(targets,['Alpha','Beta'])])
            h.delivery([(v,6,'n','2026-08-10',d) for v,d in zip(actuals,['Alpha','Beta'])])
        else:
            h.conn.execute('CREATE TABLE vk_dwd.receive_target_dwd(plan_receive_rmb REAL,plan_receive_time TEXT,customer_dept TEXT)')
            for table in ['receive_bill_detail_dwd','receive_return_bill_detail_dwd']:h.conn.execute('ALTER TABLE vk_dwd.'+table+' ADD COLUMN customer_dept TEXT')
            h.insert('vk_dwd.receive_target_dwd','plan_receive_rmb,plan_receive_time,customer_dept',[(v,'2026-08-01',d) for v,d in zip(targets,['Alpha','Beta'])])
            h.insert('vk_dwd.receive_bill_detail_dwd','detail_receive_rmb,bill_status,bill_time,customer_dept',[(v,'C','2026-08-10',d) for v,d in zip(actuals,['Alpha','Beta'])])
        return h

    def gap_request(self,ledger='delivery',**extra):
        q=metric(ledger+'_target_completion','target',attribution_mode='transaction_detail',complete_target_gap_decomposition={'dimension':'department'},**extra)
        q.pop('dimensions',None);return q

    def test_both_target_ledgers_reconcile_and_hide_physical_overall(self):
        for ledger in ['delivery','receipt']:
            with self.subTest(ledger=ledger):
                h=self.target(ledger)
                r=h.query(self.gap_request(ledger))
                self.assertEqual('success',r['status'],r);self.assertEqual(1,len(r['results']))
                result=r['results'][0]
                self.assertEqual(2,len(result['rows']),result)
                rec=result['target_gap_reconciliation'];self.assertEqual('reconciled',rec['status'])
                self.assertEqual([300,240,60],[float(rec[k]) for k in ['overall_target_amount_rmb','overall_actual_amount_rmb','overall_gap_amount_rmb']])
                observed={row['dimensions'][0]['value']:tuple(float(row['facts'][k]) for k in ['target_amount_rmb','actual_amount_rmb','gap_amount_rmb','completion_rate']) for row in result['rows']}
                self.assertEqual({'Alpha':(100,60,40,.6),'Beta':(200,180,20,.9)},observed)
                for row in result['rows']:
                    self.assertEqual('比例',row['unit']);self.assertEqual(UNITS,row['fact_units']);self.assertIsNone(row.get('currency'))
                self.assertEqual(['r'],[x['request_id'] for x in r['results']])

    def test_zero_target_and_negative_gap_have_explainable_units_and_values(self):
        h=self.target(targets=(0,200),actuals=(60,250))
        r=h.query(self.gap_request());result=r['results'][0]
        self.assertEqual('success',r['status'],r)
        rows={row['dimensions'][0]['value']:row for row in result['rows']}
        self.assertIsNone(rows['Alpha']['facts']['completion_rate']);self.assertEqual(-60,rows['Alpha']['facts']['gap_amount_rmb'])
        self.assertEqual(-50,rows['Beta']['facts']['gap_amount_rmb']);self.assertEqual(1.25,rows['Beta']['facts']['completion_rate'])
        self.assertEqual('reconciled',result['target_gap_reconciliation']['status'])

    def test_missing_target_or_actual_never_fabricates_a_gap(self):
        for targets,actuals in [((None,200),(60,180)),((100,200),(None,180))]:
            h=self.target(targets=targets,actuals=actuals)
            r=h.query(self.gap_request());result=r['results'][0]
            rows={row['dimensions'][0]['value']:row for row in result['rows']}
            self.assertIsNone(rows['Alpha']['facts']['gap_amount_rmb']);self.assertEqual(20,rows['Beta']['facts']['gap_amount_rmb'])
            self.assertNotEqual('reconciled',(result.get('target_gap_reconciliation') or {}).get('status'))

    def test_internal_link_and_illegal_ledger_dimension_rejected_before_io(self):
        h=self.target()
        for update in [{'_target_gap_of_request_id':'forged'},{'attribution_mode':'salesperson_allocation'},{'complete_target_gap_decomposition':{'dimension':'product'}}]:
            q=self.gap_request();q.update(update);before=len(h.sql_trace)
            self.assertEqual('failed',h.query(q)['status']);self.assertEqual(before,len(h.sql_trace))
        before=len(h.sql_trace)
        q=metric('delivery_target_completion','target',attribution_mode='transaction_detail',_target_gap_of_request_id='forged')
        self.assertEqual('failed',h.query(q)['status']);self.assertEqual(before,len(h.sql_trace))

    def test_evidence_failure_is_not_success_and_other_branch_survives(self):
        h=self.target();original=plugin.tools._seal_claim_ids
        def damaged(results):
            original(results)
            for result in results:
                if result.get('request_id')=='r':
                    for claim in result.get('claim_ledger',[]):claim['claim_seal']='damaged'
        with patch.object(plugin.tools,'_seal_claim_ids',damaged):
            r=h.query(self.gap_request())
            self.assertEqual('failed',r['status'],r);self.assertEqual('failed',r['results'][0]['status'])
            good=metric('delivery_target_completion','target',request_id='good',attribution_mode='transaction_detail')
            r=h.query(self.gap_request(),good)
            self.assertEqual('partial',r['status'],r)
            self.assertTrue(next(x for x in r['results'] if x['request_id']=='good')['rows'])

    def test_one_damaged_row_retains_independent_valid_row_as_partial(self):
        h=self.target();original=plugin.tools._seal_claim_ids
        def damaged(results):
            original(results)
            for result in results:
                if result.get('request_id')=='r' and result.get('claim_ledger'):result['claim_ledger'][0]['claim_seal']='damaged'
        with patch.object(plugin.tools,'_seal_claim_ids',damaged):r=h.query(self.gap_request())
        self.assertEqual('partial',r['status']);self.assertEqual('partial',r['results'][0]['status'])
        self.assertEqual(1,len(r['results'][0]['rows']));self.assertNotIn('target_gap_reconciliation',r['results'][0])
        self.assertEqual('limited',r['evidence_bundle']['items'][0]['completeness'])

    def test_wrong_component_unit_cannot_be_resealed_into_valid_target_proof(self):
        h=self.target();original=plugin.tools._seal_claim_ids
        def wrong_unit(results):
            original(results)
            for result in results:
                if result.get('request_id')=='r':
                    for claim in result.get('claim_ledger',[]):
                        claim['fact_units']['gap_amount_rmb']='比例'
                        plugin.evidence.seal_claim(claim)
        with patch.object(plugin.tools,'_seal_claim_ids',wrong_unit):r=h.query(self.gap_request())
        self.assertEqual('failed',r['status']);self.assertEqual([],r['results'][0]['rows'])
        self.assertEqual('EVIDENCE_INTEGRITY_INVALID',r['results'][0]['error']['code'])

    def test_truncation_withholds_full_target_partition_proof(self):
        h=self.target()
        with patch.object(plugin.tools,'_bounded_int',wraps=plugin.tools._bounded_int) as bound:
            original=bound._mock_wraps
            bound.side_effect=lambda name,*args:1 if name=='max_rows' else original(name,*args)
            r=h.query(self.gap_request())
        result=r['results'][0];self.assertTrue(result['truncated']);self.assertEqual(1,len(result['rows']))
        self.assertNotEqual('reconciled',(result.get('target_gap_reconciliation') or {}).get('status'))

    def test_snapshot_failure_is_explicit_and_other_request_survives(self):
        h=self.target();base=plugin.tools._ConsistentSnapshotExecutor
        class FailingSnapshot(base):
            def execute(self,*args,**kwargs):
                self.n=getattr(self,'n',0)+1
                if self.n==2:raise plugin.tools.QueryFailure('QUERY_FAILED','isolated snapshot I/O failure')
                return super().execute(*args,**kwargs)
        good=metric('delivery_target_completion','target',request_id='good',attribution_mode='transaction_detail')
        with patch.object(plugin.tools,'_ConsistentSnapshotExecutor',FailingSnapshot):r=h.query(self.gap_request(),good)
        self.assertEqual('partial',r['status'],r)
        self.assertEqual('failed',r['results'][0]['status']);self.assertEqual('success',r['results'][1]['status'])

    def test_snapshot_drift_does_not_authorize_reconciliation(self):
        h=self.target();base=plugin.tools._ConsistentSnapshotExecutor
        class DriftingSnapshot(base):
            def execute(self,*args,**kwargs):
                result=super().execute(*args,**kwargs);self.marker=str(self.marker)+'changed';return result
        with patch.object(plugin.tools,'_ConsistentSnapshotExecutor',DriftingSnapshot):r=h.query(self.gap_request())
        self.assertNotEqual('reconciled',(r['results'][0].get('target_gap_reconciliation') or {}).get('status'))

    def test_profit_entity_binding_and_bad_closed_value_follow_effective_definition(self):
        h=self.profit()
        h.conn.execute('CREATE TABLE vk_dwd.goods_detail_dwd(goods_id TEXT,goods_no TEXT,goods_name TEXT,alias TEXT,is_delete TEXT,is_void TEXT)')
        h.insert('vk_dwd.goods_detail_dwd','goods_id,goods_no,goods_name,is_delete,is_void',[('1','G1','Fabric One','n','n')])
        # Product exact-ID filtering still passes through the registered master preflight.
        r=h.query(metric('product_month_gross_profit','profit',dimensions=['department'],metric_filters={'product':'1'}))
        self.assertEqual({'Alpha':30},self.dept_values(h.result(r)))
        before=len(h.sql_trace)
        r=h.query(metric('product_month_gross_profit','profit',dimensions=['department'],metric_filters={'customized':'not-a-code'}))
        self.assertEqual('failed',r['status']);self.assertEqual(before,len(h.sql_trace))

if __name__=='__main__':unittest.main()
