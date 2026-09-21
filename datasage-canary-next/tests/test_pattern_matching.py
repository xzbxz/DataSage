"""Registered finding-task catalog/entity/query tests with independent SQLite records."""
import unittest,json
from datetime import datetime
import test_remediation_remaining_cases as public
from test_remediation_remaining_cases import metric,facts

class PatternTests(unittest.TestCase):
    insert=public.RemainingCaseTests.insert
    invoke=public.RemainingCaseTests.invoke
    query=public.RemainingCaseTests.query
    result=public.RemainingCaseTests.result
    execute=public.RemainingCaseTests.execute
    def setUp(self):
        public.RemainingCaseTests.setUp(self)
        self.conn.create_function('NOW',-1,lambda *args:'2026-09-11T13:00:00')
        self.conn.create_function('UTC_TIMESTAMP',-1,lambda *args:'2026-09-11T05:00:00')
        import re
        self.conn.create_function('REGEXP',2,lambda pattern,value:False if value is None else re.search(pattern,value) is not None)
        self.conn.executescript('''
          CREATE TABLE vk_dwd.pattern_matching_dwd(task_id INTEGER,task_no TEXT,task_type TEXT,customer_id INTEGER,customer_name TEXT,sales_id INTEGER,sales_name TEXT,task_region TEXT,task_status INTEGER,task_create_time TEXT,task_modified_time TEXT,execute_id INTEGER,executor_id INTEGER,executor_erp_id INTEGER,executor_name TEXT,execute_status INTEGER,execute_modified_time TEXT,complete_time TEXT,is_find TEXT,is_suitable TEXT,is_receive TEXT,final_goods_no TEXT,sale_bill_no TEXT,sale_goods_detail_id INTEGER,delivery_amount REAL,currency_no TEXT);
          DROP TABLE vk_dwd.sale_bill_goods_detail_dwd;
          CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd(goods_detail_id INTEGER,sale_bill_id INTEGER,goods_no TEXT,sales_id INTEGER,delivery_time TEXT,bill_status INTEGER,delivery_amount REAL,currency_no TEXT);
          CREATE TABLE vk_dwd.employee_dwd(person_id INTEGER,person_no TEXT,person_name TEXT,alias TEXT);
          INSERT INTO vk_dwd.employee_dwd VALUES(100,'E100','Sales',NULL),(201,'E201','Executor A',NULL),(202,'E202','Executor B',NULL);
        ''')
    def row(self,task=1,execute=11,person=1,product='P',detail=None,amount=None,currency='VND',found='y',suitable=None,received=None,status=3,execute_status=3,created='2026-08-01T10:00:00',completed='2026-08-02T10:00:00',task_no=None):
        values=(task,task_no or f'T{task}','picture',1,'Customer',100,'Sales','GZH',status,created,created,execute,person,None if person is None else 200+person,f'Executor {person}',execute_status,completed,completed,found,suitable,received,product,None if detail is None else f'B{detail}',detail,amount,currency)
        self.insert('vk_dwd.pattern_matching_dwd','task_id,task_no,task_type,customer_id,customer_name,sales_id,sales_name,task_region,task_status,task_create_time,task_modified_time,execute_id,executor_id,executor_erp_id,executor_name,execute_status,execute_modified_time,complete_time,is_find,is_suitable,is_receive,final_goods_no,sale_bill_no,sale_goods_detail_id,delivery_amount,currency_no',[values])
    def sale(self,detail,amount=100,currency='VND',bill=1,product='P',when='2026-09-01T10:00:00'):
        self.insert('vk_dwd.sale_bill_goods_detail_dwd','goods_detail_id,sale_bill_id,goods_no,sales_id,delivery_time,bill_status,delivery_amount,currency_no',[(detail,bill,product,100,when,6,amount,currency)])
    def run_pattern(self,code='task_recorded_summary',**kw):return self.query(metric(code,'pattern_matching',month=None,**kw))
    def value(self,code='task_recorded_summary',**kw):return facts(self.result(self.run_pattern(code,**kw)))[0]
    def test_task_execution_person_and_detail_grains_and_owner_attribution(self):
        for detail in [101,102,103]:self.sale(detail,bill=1 if detail<103 else 2)
        self.row(detail=101,amount=100);self.row(execute=12,detail=101,amount=100)
        self.row(execute=13,person=2,detail=101,amount=100);self.row(task=2,execute=21,detail=101,amount=100)
        self.row(detail=102,amount=100);self.row(detail=103,amount=100)
        f=self.value();self.assertEqual(2,f['metric_value']);self.assertEqual(4,f['known_execution_count']);self.assertEqual(2,f['known_executor_count']);self.assertEqual(3,f['recorded_linked_detail_count']);self.assertEqual(2,f['verified_linked_bill_count'])
        self.assertEqual(300,self.value('linked_delivery_amount')['metric_value'])
        self.assertEqual(500,self.value('person_attributed_delivery_amount')['metric_value'])
    def test_equal_amount_different_details_not_sum_distinct_or_bill_dedup(self):
        for d in [1,2]:self.sale(d,50,bill=10);self.row(detail=d,amount=50)
        self.assertEqual(100,self.value('linked_delivery_amount')['metric_value'])
    def test_conflicting_amounts_poison_detail_but_keep_other_known_part(self):
        self.sale(1,100);self.sale(2,50)
        self.row(detail=1,amount=100);self.row(task=2,execute=21,detail=1,amount=90);self.row(detail=2,amount=50)
        f=self.value('linked_delivery_amount');self.assertIsNone(f['metric_value']);self.assertEqual(50,f['known_subset_value']);self.assertGreater(f['unresolved_amount_rows'],0)
        grouped=self.result(self.run_pattern('linked_delivery_amount',dimensions=['task','currency']))
        self.assertEqual(2,len(grouped['rows']))
        self.assertTrue(all(f['metric_value'] is None for f in facts(grouped)))
        self.assertEqual([0,50],sorted(f['known_subset_value'] for f in facts(grouped)))
        self.assertTrue(all(f['currency_known_amount']==50 for f in facts(grouped)))
        self.assertTrue(all(f['unresolved_amount_rows']>0 for f in facts(grouped)))
        # Confirmed source currency is metadata, not proof that the raw amount passed.
        self.assertEqual({'VND'},{d['value'] for row in grouped['rows'] for d in row['dimensions'] if d['label']=='已核验出库币种'})
    def test_transaction_currency_not_requirement_currency_and_no_fx(self):
        self.sale(1,10,'VND');self.sale(2,20,'THB');self.sale(3,30,None)
        self.row(detail=1,amount=10,currency=None);self.row(detail=2,amount=20,currency='VND');self.row(detail=3,amount=30,currency='VND')
        r=self.result(self.run_pattern('linked_delivery_amount'));self.assertEqual(3,len(r['rows']));self.assertEqual({'VND','THB','未知币种'},{d['value'] for row in r['rows'] for d in row['dimensions'] if d['label']=='已核验出库币种'})
        self.assertEqual([10,20],sorted(x['facts']['metric_value'] for x in r['rows'] if x['facts']['metric_value'] is not None))
        missing_rows=[x for x in r['rows'] if x['facts']['metric_value'] is None]
        self.assertEqual(1,len(missing_rows))
        missing_currency=next(d for d in missing_rows[0]['dimensions'] if d['label']=='已核验出库币种')
        self.assertEqual('未知币种',missing_currency['value'])
        self.assertTrue(missing_currency['display_only'])
        self.assertTrue(missing_currency['display_name_missing'])
        self.assertEqual('failed',self.run_pattern('linked_delivery_amount',dimensions=['task'])['status'])
    def test_duplicate_source_currency_or_missing_amount_is_unresolved(self):
        self.sale(1,100,'VND');self.sale(1,100,'THB');self.row(detail=1,amount=100)
        self.assertIsNone(self.value('linked_delivery_amount')['metric_value'])
        self.conn.execute('DELETE FROM vk_dwd.sale_bill_goods_detail_dwd');self.sale(1,100)
        self.conn.execute('UPDATE vk_dwd.pattern_matching_dwd SET delivery_amount=NULL')
        self.assertIsNone(self.value('linked_delivery_amount')['metric_value'])

    def test_identity_currency_and_internal_agreement_do_not_replace_sales_amount_check(self):
        self.sale(1,30,'VND');self.sale(2,50,'VND')
        self.row(detail=1,amount=20);self.row(task=2,execute=21,detail=1,amount=20)
        self.row(task=3,execute=31,detail=2,amount=50)
        f=self.value('linked_delivery_amount')
        self.assertIsNone(f['metric_value'])
        self.assertEqual(50,f['known_subset_value'])
        self.assertEqual(2,f['unresolved_amount_rows'])

    def test_conflicting_states_are_not_final_status_and_presence_counts_overlap(self):
        self.row(found='y',suitable='y',status=2)
        self.row(execute=12,found='n',suitable=None,status=3,execute_status=0)
        f=self.value();self.assertEqual(1,f['metric_value']);self.assertEqual(1,f['task_status_conflict_count']);self.assertEqual(1,f['tasks_with_found_record']);self.assertEqual(1,f['tasks_with_not_found_record']);self.assertEqual(1,f['tasks_with_unfilled_feedback']);self.assertEqual(1,f['execute_status_unknown_rows'])
    def test_unfilled_status_and_undefined_code_remain_distinct(self):
        self.row(task=1,execute=11,execute_status=None)
        self.row(task=2,execute=22,execute_status=0)
        f=self.value();self.assertEqual(1,f['execute_status_unfilled_rows']);self.assertEqual(1,f['execute_status_unknown_rows'])
        r=self.result(self.run_pattern(dimensions=['execute_status']));self.assertEqual(2,len(r['rows']))

    def test_status_filter_does_not_coerce_text_to_zero_or_change_attribution_mode(self):
        self.row(execute_status=0)
        self.assertEqual(1,self.value(metric_filters={'execute_status':'0'})['metric_value'])
        self.assertEqual('failed',self.run_pattern(metric_filters={'execute_status':'已完成'})['status'])
        self.assertEqual('failed',self.run_pattern(attribution_mode='salesperson_allocation')['status'])

    def test_no_link_is_distinct_from_unresolved_link_identity(self):
        self.row(detail=None,amount=None,product=None,found=None)
        f=self.value();self.assertEqual(0,f['tasks_with_recorded_link']);self.assertEqual(1,f['tasks_with_unfilled_found'])
        self.assertEqual('empty',self.result(self.run_pattern('linked_delivery_amount'))['data_state'])
        self.conn.execute("UPDATE vk_dwd.pattern_matching_dwd SET sale_bill_no='SOME-LINK',delivery_amount=10")
        f=self.value();self.assertEqual(1,f['recorded_unverified_link_rows']);self.assertIsNone(self.value('linked_delivery_amount')['metric_value'])
    def test_creation_cohort_can_gain_later_link_without_claiming_historical_snapshot(self):
        self.row(found='n')
        window={'start':'2026-08-01','end':'2026-09-01'}
        f=self.value(pattern_time_basis='task_created',time_range=window);self.assertEqual(0,f['tasks_with_recorded_link'])
        self.sale(1,100);self.conn.execute("UPDATE vk_dwd.pattern_matching_dwd SET sale_goods_detail_id=1,sale_bill_no='B1',delivery_amount=100")
        f=self.value(pattern_time_basis='task_created',time_range=window);self.assertEqual(1,f['tasks_with_recorded_link'])
        self.assertEqual(100,self.value('linked_delivery_amount',pattern_time_basis='linked_delivery',time_range={'start':'2026-09-01','end':'2026-10-01'})['metric_value'])
        self.assertEqual('failed',self.run_pattern(time_range=window)['status'])
        self.assertEqual('failed',self.run_pattern(pattern_time_basis='linked_delivery',time_range=window)['status'])
        r=self.result(self.run_pattern(pattern_time_basis='task_created',time_range=window,time_bucket='month'));self.assertIn('2026-08',json.dumps(r))
    def test_missing_and_conflicting_task_identity_not_filled_from_names(self):
        self.row();self.row(task=None,execute=22)
        f=self.value();self.assertIsNone(f['metric_value']);self.assertEqual(1,f['known_subset_value']);self.assertEqual(1,f['task_identity_unknown_rows'])
        self.conn.execute('DELETE FROM vk_dwd.pattern_matching_dwd');self.row(task=1,task_no='SAME');self.row(task=2,execute=22,task_no='SAME')
        self.assertIsNone(self.value()['metric_value'])
    def test_topn_retains_currency_dedup_total_and_not_all_groups(self):
        self.sale(1,100);self.row(detail=1,amount=100);self.row(task=2,execute=21,detail=1,amount=100)
        r=self.result(self.run_pattern('linked_delivery_amount',dimensions=['task','currency'],limit=1));self.assertTrue(r['truncated'])
        f=facts(r)[0];self.assertEqual(2,f['pattern_display_groups']);self.assertEqual(100,f['currency_known_amount'])
        r=self.result(self.run_pattern('linked_delivery_amount',dimensions=['task','currency']));self.assertEqual(200,sum(f['metric_value'] for f in facts(r)))
    def test_registered_catalog_entity_binding_and_orphan_executor_group(self):
        r=self.invoke('datasage_catalog',{'requests':[{'domain':'pattern_matching'}]});self.assertEqual('success',r['status']);self.assertNotIn('vk_dwd.',json.dumps(r))
        r=self.invoke('datasage_catalog',{'requests':[{'domain':d} for d in ['delivery','receipt','receivable','target','inventory','profit','pattern_matching']]});self.assertEqual('success',r['status'])
        self.row(person=1);self.row(task=2,execute=22,person=99)
        r=self.result(self.run_pattern(dimensions=['executor']));self.assertEqual(2,len(r['rows']))
        self.assertEqual(1,self.value(metric_filters={'executor':'E201'})['metric_value'])
        self.assertFalse(any('JOIN `vk_dwd`.`employee_dwd`' in x['sql'] for x in self.sql_trace if x['sql'].startswith('WITH')))

    def test_executor_filter_ref_does_not_replace_source_executor_group(self):
        self.row(task=1,execute=11,person=1)
        self.row(task=2,execute=22,person=2)
        # Two source executor IDs can share one ERP salesperson identity.
        self.conn.execute('UPDATE vk_dwd.pattern_matching_dwd SET executor_erp_id=999')
        result=self.result(self.run_pattern(dimensions=['executor']))
        self.assertEqual(2,len(result['rows']))
        dimensions=[next(d for d in row['dimensions'] if d['label']=='执行人') for row in result['rows']]
        self.assertEqual(1,len({d['entity_ref'] for d in dimensions}))
        self.assertTrue(all(d['entity_ref_scope']=='filter_identity' for d in dimensions))
        self.assertTrue(all(d['source_group_identity']=='source_executor_id' for d in dimensions))
        self.assertTrue(all(d['source_group_ref_field']=='pattern_executor_ref' for d in dimensions))
        self.assertEqual(2,len({row['facts']['pattern_executor_ref'] for row in result['rows']}))
        self.assertEqual({1}, {row['facts']['metric_value'] for row in result['rows']})
        self.assertNotIn('executor_filter_identity',json.dumps(result,ensure_ascii=False))
    def test_missing_time_and_nonatomic_product_preserve_unknown(self):
        self.row(created=None,product='P1,P2')
        f=self.value();self.assertEqual(0,f['known_candidate_product_count']);self.assertEqual(1,f['candidate_product_nonatomic_rows'])
        f=self.value(pattern_time_basis='task_created',time_range={'start':'2026-08-01','end':'2026-09-01'});self.assertIsNone(f['metric_value']);self.assertEqual(1,f['pattern_scope_unknown_rows'])

if __name__=='__main__':unittest.main()
