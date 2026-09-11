"""Sales identity, empty set and reconciliation tests via registered catalog/query tools."""
import unittest,json
import test_slow_baseline_net_outbound as net
from test_remediation_remaining_cases import facts

class BaselineSalesTests(unittest.TestCase):
    setUp=net.BaselineNetTests.setUp
    insert=net.BaselineNetTests.insert
    invoke=net.BaselineNetTests.invoke
    query=net.BaselineNetTests.query
    result=net.BaselineNetTests.result
    add=net.BaselineNetTests.add
    req=net.BaselineNetTests.req
    execute=net.BaselineNetTests.execute
    baseline=net.BaselineNetTests.baseline
    outgoing=net.BaselineNetTests.outgoing
    returning=net.BaselineNetTests.returning
    run_net=net.BaselineNetTests.run_net
    value=net.BaselineNetTests.value
    def sales(self,**kw):return self.result(self.run_net(dimensions=['salesperson','unit'],**kw))
    def employees(self):
        self.conn.execute('CREATE TABLE vk_dwd.employee_dwd(person_id INTEGER,person_no TEXT,person_name TEXT,alias TEXT)')
        self.insert('vk_dwd.employee_dwd','person_id,person_no,person_name,alias',[(100,'E100','Twin',None),(200,'E200','Twin',None),(300,'E300','Current Name',None)])
    def test_same_name_different_ids_and_own_return_sales_do_not_merge(self):
        self.outgoing(10,sales_id=100,sales_name='Twin');self.returning(3,sales_id=200,sales_name='Twin')
        r=self.sales();f=facts(r)
        self.assertEqual(2,len(f));self.assertEqual([-3,10],sorted(x['metric_value'] for x in f))
        self.assertEqual(2,len({x['sales_identity_ref'] for x in f}))
        self.assertTrue(all(x['unit_net_quantity']==7 for x in f));self.assertTrue(all(x['missing_value_count']==0 for x in f))
        self.assertEqual(self.value()['metric_value'],sum(x['metric_value'] for x in f))
    def test_unknown_sales_is_included_in_unfiltered_total(self):
        self.outgoing(10,sales_id=None,sales_name=None);self.returning(4)
        r=self.sales();f=facts(r)
        unknown=next(x for x in f if x['sales_identity_ref']=='unattributed')
        self.assertEqual(10,unknown['metric_value']);self.assertEqual(1,unknown['outbound_unattributed_sales_rows'])
        self.assertEqual(6,sum(x['metric_value'] for x in f));self.assertEqual(6,self.value()['metric_value'])
    def test_name_changes_same_id_preserve_identity_without_current_employee_join(self):
        self.outgoing(10,sales_id=300,sales_name='Old Name');self.returning(3,sales_id=300,sales_name='New Name')
        r=self.sales();f=facts(r)
        self.assertEqual(1,len(f));self.assertEqual(7,f[0]['metric_value']);self.assertEqual(2,f[0]['sales_name_variant_count'])
        self.assertNotIn('employee_dwd',self.sql_trace[-1]['sql'])
        self.assertTrue(f[0]['sales_identity_ref'].startswith('sales_'))
    def test_empty_sales_has_no_roster_rows_and_zero_unit_flow(self):
        self.assertEqual('empty',self.sales()['data_state'])
        f=self.value();self.assertEqual(0,f['metric_value']);self.assertEqual(0,f['missing_value_count'])
    def test_null_quantity_and_unknown_candidate_are_not_empty_sets(self):
        self.outgoing(None,sales_id=100);self.returning(4,sales_id=100)
        f=facts(self.sales())[0];self.assertIsNone(f['metric_value']);self.assertEqual(1,f['gross_missing_quantity_rows']);self.assertEqual(-4,f['known_subset_value'])
        self.outgoing(9,unit=None,sales_id=200)
        f=facts(self.sales())[0];self.assertIsNone(f['metric_value']);self.assertEqual(1,f['outbound_unknown_rows'])
    def test_same_product_many_sales_does_not_multiply_baseline_or_events(self):
        self.baseline([(2,2,1,11,'m',40,3,'2026-09-08T09:00:00')])
        self.outgoing(10,sales_id=100);self.outgoing(20,sales_id=200);self.returning(4,sales_id=200)
        f=facts(self.sales());self.assertEqual(26,sum(x['metric_value'] for x in f));self.assertEqual(2,sum(x['gross_flow_rows'] for x in f))
        self.assertTrue(all(x['unit_baseline_scope_groups']==1 for x in f));self.assertTrue(all(x['baseline_scope_groups'] is None for x in f))
        self.assertEqual(2,sum(x['matched_product_groups'] for x in f)) # overlapping flow groups, not two baseline assignments
    def test_truncation_retains_full_unit_total_and_group_count(self):
        for i in range(5):self.outgoing(i+1,sales_id=100+i,sales_name='Same')
        r=self.sales(limit=2);f=facts(r)
        self.assertTrue(r['truncated']);self.assertEqual(2,len(f));self.assertTrue(all(x['unit_net_quantity']==15 and x['unit_display_groups']==5 and x['population_display_groups']==5 for x in f))
        self.assertLess(sum(x['metric_value'] for x in f),15)
    def test_only_unknown_scope_candidates_fail_instead_of_empty_sales(self):
        self.outgoing(10,unit=None)
        response=self.run_net(dimensions=['salesperson','unit'])
        self.assertEqual('failed',response['status'])
        self.assertEqual('FLOW_SCOPE_UNASSESSABLE',response['results'][0]['error']['code'])

    def test_catalog_resolver_and_query_sales_filter_use_existing_identity_binding(self):
        self.employees();self.outgoing(10,sales_id=100);self.outgoing(20,sales_id=200);self.returning(3,sales_id=100)
        r=self.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_pool_baseline_net_outbound'}]})
        self.assertEqual('success',r['status']);self.assertIn('salesperson',json.dumps(r))
        r=self.invoke('datasage_entity_resolve',{'token':'Twin','entity_types':['salesperson'],'domain':'inventory','metric':'registered_slow_pool_baseline_net_outbound'})
        self.assertEqual('ambiguous',r['status']);self.assertTrue(r['must_stop_business_query'])
        f=facts(self.result(self.run_net(metric_filters={'salesperson':'E100'})))[0]
        self.assertEqual(7,f['metric_value']);self.assertEqual(1,f['gross_flow_rows'])
        self.outgoing(9,sales_id=None)
        f=facts(self.result(self.run_net(metric_filters={'salesperson':'E100'})))[0]
        self.assertIsNone(f['metric_value']);self.assertEqual(7,f['known_subset_value']);self.assertEqual(1,f['outbound_unknown_rows'])

if __name__=='__main__':unittest.main()
