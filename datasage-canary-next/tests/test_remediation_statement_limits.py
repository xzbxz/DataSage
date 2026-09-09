"""Aggregate evidence must not authorize row reconciliation or invent zero."""
import unittest
import test_remediation_remaining_cases as public


class StatementLimitTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)

    def receipt(self, code='receipt_amount',rid='r'):
        return public.metric(code,'receipt',request_id=rid)

    def test_equal_totals_with_offsetting_detail_differences_are_aggregate_only(self):
        self.h.insert('vk_dwd.receive_bill_detail_dwd',
            'detail_receive_rmb,detail_deal_amount,exchange_rate,bill_status,bill_time',
            [(100,80,1,'C','2026-08-01'),(200,220,1,'C','2026-08-01')])
        response=self.h.query(self.receipt(rid='registered'),self.receipt('actual_receipt_amount','actual'))
        for rid in ('registered','actual'):
            result=self.h.result(response,rid)
            self.assertEqual(300,result['rows'][0]['facts']['metric_value'])
        # Independent rows differ although sums match: no row reconciliation exists.
        for item in response['evidence_bundle']['items']:
            self.assertEqual('not_requested_or_unavailable',item['reconciliation'])
            self.assertNotIn('row_reconciliation',item['supports'])

    def test_empty_explicitly_cannot_establish_zero(self):
        response=self.h.query(self.receipt());result=self.h.result(response)
        self.assertEqual('empty',result['data_state']);self.assertEqual([],result['rows'])
        item=response['evidence_bundle']['items'][0]
        self.assertIn('DATA_STATE_EMPTY',item['limitations'])
        self.assertIn('empty_result_state',item['supports'])
        self.assertNotIn('verified_zero_state',item['supports'])

    def test_real_zero_retains_verified_zero_support(self):
        self.h.insert('vk_dwd.receive_bill_detail_dwd','detail_receive_rmb,bill_status,bill_time',[(0,'C','2026-08-01')])
        response=self.h.query(self.receipt());result=self.h.result(response)
        self.assertEqual('zero',result['data_state']);self.assertEqual(0,result['rows'][0]['facts']['metric_value'])
        item=response['evidence_bundle']['items'][0]
        self.assertIn('verified_zero_state',item['supports'])
        self.assertNotIn('empty_result_state',item['supports'])


if __name__=='__main__':unittest.main()
