"""Original B17 role collision, using unchanged registry and independent SQL rows."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
import test_remediation_remaining_cases as public_cases

ROOT=Path(__file__).resolve().parents[1]
OBSERVATIONS=[]


class CrossRoleEntityTests(unittest.TestCase):
    def setUp(self):
        self.h=public_cases.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.registry_before=hashlib.sha256((ROOT/'plugins/datasage-query/contracts/entity-registry.yaml').read_bytes()).hexdigest()
        self.h.conn.executescript('''
          CREATE TABLE vk_dwd.goods_detail_dwd(goods_id TEXT,goods_no TEXT,goods_name TEXT,alias TEXT,is_delete TEXT,is_void TEXT);
          CREATE TABLE vk_dwd.employee_dwd(person_id TEXT,person_no TEXT,person_name TEXT,alias TEXT);
          CREATE TABLE vk_dwd.whse_info_dwd(whse_id TEXT,whse_name TEXT,whse_full_name TEXT);
          CREATE TABLE vk_dwd.supplier_info_dwd(supplier_id TEXT,supplier_no TEXT,supplier_name TEXT,supplier_full_name TEXT);
          ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN customer_id TEXT;
          ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN customer_no TEXT;
          ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN customer_name TEXT;
          ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN customer_id TEXT;
          ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN customer_no TEXT;
          ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN customer_name TEXT;
        ''')
        self.h.insert('vk_dwd.customer_dwd','customer_id,customer_no,customer_name,is_delete,is_void',[
            ('customer-thai','TK','Thai Kim','n','n'),('customer-other','OTHER','Other customer','n','n')])
        self.h.insert('vk_dwd.sale_bill_goods_detail_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept,customer_id,customer_no,customer_name',[
            (100000,6,'n','2026-08-15','Thai Kim','customer-other','OTHER','Other customer'),
            (900000,6,'n','2026-08-15','HCM','customer-thai','TK','Thai Kim')])

    def tearDown(self):
        self.assertEqual(self.registry_before,hashlib.sha256((ROOT/'plugins/datasage-query/contracts/entity-registry.yaml').read_bytes()).hexdigest())
        OBSERVATIONS.append({'test':self._testMethodName,'calls':self.h.calls,'sql':self.h.sql_trace,'registry_unchanged':True})

    def resolve(self, token='Thai Kim', types=None):
        args={'token':token,'domain':'delivery','metric':'delivery_amount'}
        if types is not None:args['entity_types']=types
        return self.h.invoke('datasage_entity_resolve',args)

    def amount(self, role, value):
        result=self.h.result(self.h.query(public_cases.metric('delivery_amount','delivery',metric_filters={role:value})))
        return result,public_cases.facts(result)

    def test_neutral_original_name_does_not_claim_global_unique_role(self):
        result=self.resolve()
        self.assertEqual('ambiguous',result['status'],result)
        self.assertTrue(result['must_clarify']);self.assertTrue(result['must_stop_business_query'])
        self.assertEqual({'customer'},{c['entity_type'] for c in result['candidates']})
        self.assertIn('department',result['resolution_scope']['unsearched_entity_types'])
        self.assertTrue(result['candidate_count_is_lower_bound'])
        self.assertFalse(any('sale_bill_goods_detail_dwd' in entry['sql'] for entry in self.h.sql_trace))

    def test_explicit_multiple_types_preserve_unsearched_department_uncertainty(self):
        result=self.resolve(types=['department','customer'])
        self.assertEqual('ambiguous',result['status'],result)
        self.assertTrue(result['must_stop_business_query'])

    def test_user_selected_department_queries_exact_fact_role_without_customer_bleed(self):
        first=self.resolve()
        self.assertTrue(first['must_stop_business_query'])
        result,rows=self.amount('department','Thai Kim')
        self.assertEqual(100000,rows[0]['metric_value'])
        self.assertIn('`customer_dept`',self.h.sql_trace[-1]['sql'])
        self.assertNotIn('`customer_id`',self.h.sql_trace[-1]['sql'])
        self.assertIn('Thai Kim',self.h.sql_trace[-1]['params'])
        near,rows=self.amount('department','Thai Ki')
        self.assertEqual('empty',near['data_state']);self.assertFalse(rows)
        wrong_id,rows=self.amount('department','customer-thai')
        self.assertEqual('empty',wrong_id['data_state']);self.assertFalse(rows)

    def test_explicit_customer_remains_unique_and_uses_customer_id(self):
        result=self.resolve(types=['customer'])
        self.assertEqual('resolved',result['status'],result)
        self.assertFalse(result['must_stop_business_query'])
        _,rows=self.amount('customer','Thai Kim')
        self.assertEqual(900000,rows[0]['metric_value'])
        self.assertIn('`customer_id`',self.h.sql_trace[-1]['sql'])
        self.assertIn('customer-thai',self.h.sql_trace[-1]['params'])

    def test_registered_alias_multiple_types_must_search_other_requested_type(self):
        self.h.insert('vk_dwd.customer_dwd','customer_id,customer_no,customer_name,is_delete,is_void',[('customer-hcm','CH','HCM','n','n')])
        result=self.resolve('HCM',['department','customer'])
        self.assertEqual('ambiguous',result['status'],result)
        self.assertEqual({'department','customer'},{c['entity_type'] for c in result['candidates']})
        self.assertTrue(self.h.sql_trace)
        self.assertTrue(result['must_stop_business_query'])

    def test_unique_registered_department_preserves_fast_path_and_neutral_control(self):
        result=self.resolve('HCM',['department'])
        self.assertEqual('resolved',result['status']);self.assertFalse(self.h.sql_trace)
        neutral=self.resolve('HCM')
        self.assertEqual('resolved',neutral['status'],neutral)
        self.assertEqual({'department'},{c['entity_type'] for c in neutral['candidates']})

    def test_unregistered_department_is_not_discoverable_but_is_valid_explicit_fact_value(self):
        result=self.resolve(types=['department'])
        self.assertEqual('not_found',result['status'])
        self.assertEqual('no_candidate_source',result['resolution_path'])
        self.assertFalse(self.h.sql_trace)
        _,rows=self.amount('department','Thai Kim')
        self.assertEqual(100000,rows[0]['metric_value'])

    def test_unsupported_role_cannot_be_guessed_by_query(self):
        payload=self.h.query(public_cases.metric('delivery_amount','delivery',metric_filters={'entity':'Thai Kim'}))
        self.assertEqual('failed',payload['status']);self.assertFalse(self.h.sql_trace)

    def test_structured_role_is_not_a_user_confirmation_receipt(self):
        first=self.resolve()
        self.assertTrue(first['must_stop_business_query'])
        # Deliberately demonstrate the stateless boundary: the tool sees the
        # supplied role, not whether a human actually confirmed it in chat.
        _,rows=self.amount('customer','Thai Kim')
        self.assertEqual(900000,rows[0]['metric_value'])


if __name__=='__main__':
    destination=Path(sys.argv[2]) if len(sys.argv)==3 and sys.argv[1]=='--report' else None
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CrossRoleEntityTests))
    if destination:destination.write_text(json.dumps({'checks_passed':result.wasSuccessful(),'scenario':'same department/customer name Thai Kim','data':'independent isolated SQLite rows','model_or_live_channel_tested':False,'observations':OBSERVATIONS},ensure_ascii=False,indent=2),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
