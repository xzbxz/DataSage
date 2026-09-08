"""Regression: missing/incomplete target amounts are not numeric zero/partial facts."""
import unittest
import test_remediation_remaining_cases as public_cases


class TargetMissingValueTests(unittest.TestCase):
    def setUp(self):
        self.harness=public_cases.RemainingCaseTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def target(self, *, month='2026-08'):
        h=self.harness
        response=h.query(public_cases.metric('delivery_target_completion','target',month=month,attribution_mode='salesperson_allocation'))
        result=h.result(response)
        return result['rows'][0]

    def actual(self):
        self.harness.insert('vk_dwd.sale_bill_split_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time',[(10000,6,'n','2026-08-15')])

    def test_absent_target_is_null_in_final_public_facts(self):
        self.actual();row=self.target()
        self.assertEqual('missing',row['states']['target_data_state'])
        self.assertIsNone(row['facts']['target_amount_rmb'])
        self.assertEqual(10000,row['facts']['actual_amount_rmb'])
        self.assertIsNone(row['facts']['gap_amount_rmb'])
        self.assertIsNone(row['facts']['completion_rate'])
        self.assertIn('target_status',row['allowed_relations'])

    def test_partial_target_is_not_reported_as_complete_amount(self):
        self.actual()
        self.harness.insert('vk_dwd.delivery_target_split_dwd','detail_target_rmb,is_inner_cus,year_month',[(125000,'n','2026-08'),(None,'n','2026-08')])
        row=self.target()
        self.assertEqual('incomplete',row['states']['target_data_state'])
        self.assertIsNone(row['facts']['target_amount_rmb'])
        self.assertIsNone(row['facts']['completion_rate'])
        self.assertIsNone(row['facts']['gap_amount_rmb'])

    def test_zero_target_is_still_a_real_zero(self):
        self.actual()
        self.harness.insert('vk_dwd.delivery_target_split_dwd','detail_target_rmb,is_inner_cus,year_month',[(0,'n','2026-08')])
        row=self.target()
        self.assertEqual('zero',row['states']['target_data_state'])
        self.assertEqual(0,row['facts']['target_amount_rmb'])
        self.assertEqual(-10000,row['facts']['gap_amount_rmb'])
        self.assertIsNone(row['facts']['completion_rate'])

    def test_future_unset_and_published_target_remain_distinct(self):
        for value,state in [(None,'not_set_for_future'),(125000,'set')]:
            with self.subTest(value=value):
                self.harness.conn.execute('DELETE FROM vk_dwd.delivery_target_split_dwd')
                if value is not None:self.harness.insert('vk_dwd.delivery_target_split_dwd','detail_target_rmb,is_inner_cus,year_month',[(value,'n','2026-10')])
                row=self.target(month='2026-10')
                self.assertEqual(state,row['states']['target_data_state'])
                self.assertEqual(value,row['facts']['target_amount_rmb'])
                self.assertIsNone(row['facts']['actual_amount_rmb'])
                self.assertIn('target_status',row['allowed_relations'])


if __name__=='__main__':unittest.main()
