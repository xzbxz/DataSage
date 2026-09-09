"""Independent synthetic credit fixtures through the registered public handler.

No customer/employee data or live I/O. Expected values are hand calculated,
never derived from the semantic YAML. SQLite adapts MySQL date syntax only.
"""
import unittest
import copy
from unittest.mock import patch
import test_remediation_remaining_cases as public


class OverdueCoverageTests(unittest.TestCase):
    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.conn.execute('ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN customer_dept TEXT')

    def row(self, key, *, days=30, bill='2026-07-14', amount=100, fx=2,
            dept='HCM', internal='n', status='C', credit=True):
        self.h.insert('vk_dwd.receivable_bill_detail_dwd',
            'detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,org_name,currency_no,customer_dept',
            [(amount,fx,status,bill,internal,key,'SYNTHETIC_ORG','CNY',dept)])
        if credit:
            self.h.insert('vk_dwd.customer_credit_dwd','customer_id,org_name,currency_no,credit_days',
                [(key,'SYNTHETIC_ORG','CNY',days)])

    def query(self, metric='overdue_receivable_amount', **kw):
        response=self.h.query(public.metric(metric,'receivable',month=None,
            metric_filters=kw.pop('metric_filters',{'customer_department':'HCM'}), **kw))
        result=self.h.result(response)
        return response,result,(result['rows'][0]['facts'] if result['rows'] else {})

    def test_mixed_credit_null_has_partial_value_not_full_total(self):
        self.row('known');self.row('unknown',days=None)
        response,result,facts=self.query()
        self.assertEqual('incomplete',result['data_state'])
        self.assertIsNone(facts['metric_value'])
        self.assertEqual(200,facts['known_subset_value'])
        self.assertEqual(2,facts['scope_row_count'])
        self.assertEqual(1,facts['unassessable_row_count'])
        self.assertEqual(1,facts['missing_credit_days_count'])
        self.assertEqual(.5,facts['eligibility_coverage_rate'])
        # Contribution-candidate coverage includes unknown eligibility, not just known overdue inputs.
        self.assertEqual(.5,facts['value_coverage_rate'])
        self.assertEqual('limited',response['evidence_bundle']['items'][0]['completeness'])
        raw=self.h.sql_trace[-1]['database_rows'][0]
        self.assertIsNone(raw['metric_value'])
        self.assertEqual(1,raw['unassessable_row_count'])

    def test_missing_credit_record_or_all_null_credit_is_undefined(self):
        for credit in (False,True):
            with self.subTest(credit=credit):
                self.h.conn.execute('DELETE FROM vk_dwd.receivable_bill_detail_dwd')
                self.h.conn.execute('DELETE FROM vk_dwd.customer_credit_dwd')
                self.row('unknown',days=None,credit=credit)
                _,result,facts=self.query()
                self.assertEqual('undefined',result['data_state'])
                self.assertIsNone(facts['metric_value']);self.assertIsNone(facts['known_subset_value'])
                self.assertEqual(1,facts['scope_row_count']);self.assertEqual(1,facts['missing_credit_days_count'])

    def test_date_null_and_both_null_count_once_per_unknown_row(self):
        self.row('known');self.row('date_missing',bill=None);self.row('both_missing',bill=None,days=None)
        for code in ('overdue_receivable_amount','overdue_days','overdue_customer_count'):
            with self.subTest(metric=code):
                _,result,facts=self.query(code)
                self.assertEqual('incomplete',result['data_state']);self.assertIsNone(facts['metric_value'])
                self.assertEqual(3,facts['scope_row_count']);self.assertEqual(2,facts['unassessable_row_count'])
                self.assertEqual(2,facts['missing_bill_time_count']);self.assertEqual(1,facts['missing_credit_days_count'])

    def test_amount_missing_fx_does_not_poison_days_or_customer_count(self):
        self.row('known');self.row('missing_fx',fx=None)
        _,result,facts=self.query()
        self.assertEqual('incomplete',result['data_state']);self.assertIsNone(facts['metric_value'])
        self.assertEqual(200,facts['known_subset_value']);self.assertEqual(1,facts['missing_value_count'])
        self.assertEqual(0,facts['unassessable_row_count'])
        for metric,value in [('overdue_days',20),('overdue_customer_count',2),('overdue_receivable_amount_original',200)]:
            kwargs={'dimensions':['currency']} if metric.endswith('_original') else {}
            _,result,facts=self.query(metric,**kwargs)
            self.assertEqual('rows',result['data_state']);self.assertEqual(value,facts['metric_value'])

    def test_zero_empty_and_known_not_overdue_remain_distinct(self):
        _,result,_=self.query();self.assertEqual('empty',result['data_state']);self.assertEqual([],result['rows'])
        self.row('not_due',bill='2026-09-01')
        _,result,_=self.query();self.assertEqual('empty',result['data_state'])
        self.row('zero_converted',fx=0)
        _,result,facts=self.query();self.assertEqual('zero',result['data_state']);self.assertEqual(0,facts['metric_value'])
        self.assertEqual(2,facts['scope_row_count']);self.assertEqual(1,facts['eligible_row_count'])

    def test_scope_filters_exclude_other_department_internal_void_nonpositive(self):
        self.row('known')
        self.row('other',dept='HN',days=None);self.row('internal',internal='y',days=None)
        self.row('void',status='A',days=None);self.row('negative',amount=-10,days=None)
        _,result,facts=self.query()
        self.assertEqual('rows',result['data_state']);self.assertEqual(200,facts['metric_value'])
        self.assertEqual(1,facts['scope_row_count']);self.assertEqual(0,facts['unassessable_row_count'])

    def test_missing_fx_on_not_due_row_is_not_a_missing_overdue_amount(self):
        self.row('known');self.row('not_due',bill='2026-09-01',fx=None)
        _,result,facts=self.query()
        self.assertEqual('rows',result['data_state']);self.assertEqual(200,facts['metric_value'])
        self.assertEqual(0,facts['missing_value_count']);self.assertEqual(2,facts['scope_row_count'])


    def test_scope_and_contribution_coverage_have_different_denominators(self):
        self.row('due');self.row('unknown',days=None);self.row('not_due',bill='2026-09-01')
        _,result,facts=self.query()
        self.assertEqual('incomplete',result['data_state'])
        self.assertAlmostEqual(2/3,facts['eligibility_coverage_rate'])
        self.assertEqual(.5,facts['value_coverage_rate'])
        self.assertEqual(1,facts['eligible_row_count'])
        self.assertEqual(200,facts['known_subset_value'])

    def test_days_subset_and_missing_customer_join_key_remain_unknown(self):
        self.row('known');self.row('no_date',bill=None)
        _,result,facts=self.query('overdue_days')
        self.assertEqual('incomplete',result['data_state'])
        self.assertEqual(20,facts['known_subset_value']);self.assertIsNone(facts['metric_value'])
        self.h.conn.execute('DELETE FROM vk_dwd.receivable_bill_detail_dwd')
        self.row(None)
        _,result,facts=self.query('overdue_customer_count')
        self.assertEqual('undefined',result['data_state'])
        self.assertIsNone(facts['metric_value']);self.assertIsNone(facts['known_subset_value'])
        self.assertEqual(1,facts['unassessable_row_count'])

    def test_group_without_eligible_or_unknown_rows_does_not_become_zero(self):
        self.row('due',dept='HCM');self.row('not_due',dept='HN',bill='2026-09-01')
        self.row('unknown',dept='SYNTHETIC_UNKNOWN',days=None)
        response,result,_=self.query(dimensions=['customer_department'],metric_filters={})
        self.assertEqual('incomplete',result['data_state'])
        self.assertEqual(2,len(result['rows']))
        self.assertEqual({200,None},{r['facts']['metric_value'] for r in result['rows']})
        self.assertNotIn('HN',{d['value'] for r in result['rows'] for d in r['dimensions']})
        self.assertNotIn('verified_zero_state',response['evidence_bundle']['items'][0]['supports'])

    def test_new_coverage_fields_are_sealed_and_survive_model_projection(self):
        self.row('known');self.row('unknown',days=None)
        captured=[];original=public.plugin.tools._run_one
        def capture(*args,**kwargs):
            result=original(*args,**kwargs);captured.append(result);return result
        with patch.object(public.plugin.tools,'_run_one',side_effect=capture):
            _,result,facts=self.query()
        internal=captured[-1];claim=internal['claim_ledger'][0]
        self.assertTrue(public.plugin.tools.evidence._claim_is_validly_sealed(claim))
        self.assertTrue(public.plugin.tools._disclosure_ledger_has_valid_seal(
            internal['disclosure_ledger'],internal['disclosure_ledger_seal'],
            request_id=claim['request_id'],metric_ref=claim['metric_ref'],
            scope_fingerprint=claim['scope_fingerprint'],projection_fingerprint=claim['projection_fingerprint'],
            ledger_contract_version=internal['disclosure_contract_version']))
        request=self.h.calls[-1]['args']['requests'][0]
        projected=public.plugin.tools._model_wire_result(internal,request=request)
        for field in ('known_subset_value','unassessable_row_count','eligibility_coverage_rate','value_coverage_rate'):
            self.assertEqual(facts[field],claim['facts'][field])
            self.assertEqual(facts[field],projected['claim_ledger'][0]['facts'][field])
        tampered=copy.deepcopy(claim);tampered['facts']['unassessable_row_count']=0
        self.assertFalse(public.plugin.tools.evidence._claim_is_validly_sealed(tampered))


if __name__=='__main__':unittest.main()
