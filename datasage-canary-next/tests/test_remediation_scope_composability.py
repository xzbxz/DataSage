"""Unseen literal values and role ambiguity at the public typed interface.

These are interface tests, not natural-language understanding tests. They
never infer a department from a sentence or change the entity registry.
"""
import unittest
import test_remediation_overdue_coverage as coverage
import test_remediation_cross_role_entity as roles


class ScopeComposabilityTests(unittest.TestCase):
    def setUp(self):
        self.case=coverage.OverdueCoverageTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_unseen_department_literals_preserve_independent_scope(self):
        values=[('北区·一部',11),('West Region',23),('HCM外部客户群',37),('HCM',41)]
        for i,(name,amount) in enumerate(values):self.case.row(f'SYNTHETIC_{i}',dept=name,amount=amount)
        for name,amount in values:
            with self.subTest(department=name):
                _,result,facts=self.case.query(metric_filters={'customer_department':name})
                self.assertEqual('rows',result['data_state'])
                self.assertEqual(amount*2,facts['metric_value'])
                self.assertEqual(1,facts['scope_row_count'])
                self.assertIn(name,self.case.h.sql_trace[-1]['params'])

    def test_unknown_literal_is_not_routed_to_a_known_prefix(self):
        self.case.row('SYNTHETIC_HCM',dept='HCM')
        before=len(self.case.h.sql_trace)
        _,result,_=self.case.query(metric_filters={'customer_department':'HCM外部'})
        self.assertEqual('empty',result['data_state']);self.assertEqual([],result['rows'])
        self.assertEqual(before+1,len(self.case.h.sql_trace))
        self.assertIn('HCM外部',self.case.h.sql_trace[-1]['params'])

    def test_date_arithmetic_is_not_bound_to_pilot_month_or_credit_days(self):
        # Fixed test clock 2026-09-02; elapsed 3 days, explicit zero-day credit.
        self.case.row('SYNTHETIC_OTHER_DATE',bill='2026-08-30',days=0)
        _,result,facts=self.case.query('overdue_days')
        self.assertEqual('rows',result['data_state']);self.assertEqual(3,facts['metric_value'])
        self.assertEqual(0,facts['unassessable_row_count'])


class RoleChoiceTests(unittest.TestCase):
    def test_unseen_cross_role_name_exposes_candidates_without_selecting_plan(self):
        case=roles.CrossRoleEntityTests();case.setUp()
        self.addCleanup(case.doCleanups)
        case.h.insert('vk_dwd.customer_dwd','customer_id,customer_no,customer_name,is_delete,is_void',
            [('synthetic_customer','SC','North Meridian','n','n')])
        case.h.insert('vk_dwd.employee_dwd','person_id,person_no,person_name,alias',
            [('synthetic_employee','SE','North Meridian','')])
        result=case.resolve('North Meridian',['customer','salesperson'])
        self.assertEqual('ambiguous',result['status'])
        self.assertEqual({'customer','salesperson'},{r['entity_type'] for r in result['candidates']})
        self.assertTrue(result['must_stop_business_query'])
        selected=case.resolve('North Meridian',['salesperson'])
        self.assertEqual('resolved',selected['status'])
        self.assertEqual('synthetic_employee',selected['candidates'][0]['canonical_id'])
        # No business metric SQL or model-generated clarification was executed.
        self.assertFalse(any('receivable_bill_detail_dwd' in r['sql'] for r in case.h.sql_trace))


if __name__=='__main__':unittest.main()
