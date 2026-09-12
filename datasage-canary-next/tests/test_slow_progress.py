"""On-demand periods over one existing cohort: registered tool counterexamples."""
import unittest
import test_slow_baseline_net_outbound as baseline_net
from test_remediation_remaining_cases import facts


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.h=baseline_net.BaselineNetTests();self.h.setUp();self.addCleanup(self.h.doCleanups)

    def result(self,**kw):
        return self.h.result(self.h.run_net(baseline_week='2026-W37',**kw))

    def test_explicit_half_open_period_and_own_sales(self):
        h=self.h
        h.outgoing(100,when='2026-09-08T12:00:00')
        h.outgoing(10,when='2026-09-09T00:00:00');h.outgoing(100,when='2026-09-10T00:00:00')
        h.returning(3,when='2026-09-09T01:00:00')
        r=self.result(time_range={'start':'2026-09-09','end':'2026-09-10'})
        self.assertEqual(7,facts(r)[0]['metric_value'])
        self.assertEqual('2026-09-10',r['applied_time_range']['window_end'])
        self.assertEqual('requested_window_observed',r['applied_time_range']['window_coverage'])
        sales=self.result(dimensions=['salesperson','unit'],time_range={'start':'2026-09-09','end':'2026-09-10'})
        self.assertEqual([-3,10],sorted(f['metric_value'] for f in facts(sales)))

    def test_in_progress_window_is_capped_at_fixed_read_clock(self):
        self.h.outgoing(10);self.h.outgoing(100,when='2026-09-12T00:00:00')
        r=self.result(time_range={'start':'2026-09-09','end':'2026-10-01'})
        self.assertEqual(10,facts(r)[0]['metric_value'])
        self.assertEqual('partial_to_read',r['applied_time_range']['window_coverage'])
        self.assertEqual('2026-09-11T12:00:00',r['applied_time_range']['window_end'])

    def test_future_and_pre_freeze_windows_are_not_zero_results(self):
        for window,code in [({'start':'2026-09-12','end':'2026-09-13'},'FLOW_WINDOW_NOT_OBSERVED'),
                            ({'start':'2026-09-08','end':'2026-09-09'},'FLOW_WINDOW_PRECEDES_BASELINE')]:
            r=self.h.run_net(baseline_week='2026-W37',time_range=window)
            self.assertEqual('failed',r['status']);self.assertEqual(code,r['results'][0]['error']['code'])
            self.assertEqual([],r['results'][0]['rows'])

    def test_explicit_period_requires_one_baseline_not_latest_or_list(self):
        for extra in [{},{'baseline_week':['2026-W36','2026-W37']}]:
            r=self.h.run_net(time_range={'start':'2026-09-09','end':'2026-09-10'},**extra)
            self.assertEqual('failed',r['status'])
        self.assertEqual([],self.h.sql_trace)

    def test_month_with_earlier_cohort_is_observed_flow_not_month_start_stock(self):
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET week_label='2026-W35',frozen_at='2026-08-25T09:00:00'")
        self.h.outgoing(10,when='2026-09-01T00:00:00');self.h.outgoing(100,when='2026-08-31T23:59:59')
        r=self.h.result(self.h.run_net(baseline_week='2026-W35',calendar_month='2026-09'))
        self.assertEqual(10,facts(r)[0]['metric_value']);self.assertEqual('2026-09-01',r['applied_time_range']['window_start'])
        self.assertEqual('partial_to_read',r['applied_time_range']['window_coverage'])
        self.assertNotIn('closing_quantity',facts(r)[0])

    def test_closed_month_cross_year_and_end_boundary(self):
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET week_label='2025-W48',frozen_at='2025-11-25T09:00:00'")
        self.h.outgoing(12,when='2025-12-31T23:59:59');self.h.outgoing(100,when='2026-01-01T00:00:00')
        r=self.h.result(self.h.run_net(baseline_week='2025-W48',calendar_month='2025-12'))
        self.assertEqual(12,facts(r)[0]['metric_value']);self.assertEqual('requested_window_observed',r['applied_time_range']['window_coverage'])

    def test_duplicate_weekly_members_not_added_to_selected_cohort(self):
        self.h.conn.execute("INSERT INTO vk_ai.slow_moving_baseline SELECT 2,'2026-W36',2,'2026-09-01T09:00:00',baseline_version,source_table,goods_id,goods_sku_id,goods_name,whse_dept,source_unit,total_qty,total_piece,slow_label FROM vk_ai.slow_moving_baseline")
        self.h.outgoing(10)
        r=self.result(time_range={'start':'2026-09-09','end':'2026-09-10'})
        self.assertEqual(10,facts(r)[0]['metric_value']);self.assertEqual(1,facts(r)[0]['baseline_scope_groups'])

    def test_no_flow_zero_but_missing_quantity_and_time_remain_unknown(self):
        window={'start':'2026-09-09','end':'2026-09-10'}
        self.assertEqual(0,facts(self.result(time_range=window))[0]['metric_value'])
        self.h.outgoing(None);self.h.returning(4,when='2026-09-09T13:00:00')
        f=facts(self.result(time_range=window))[0];self.assertIsNone(f['metric_value']);self.assertEqual(-4,f['known_subset_value'])
        self.h.outgoing(10,when=None)
        self.assertEqual(1,facts(self.result(time_range=window))[0]['outbound_missing_time_rows'])

    def test_sales_truncation_preserves_unit_total_and_unknown_identity(self):
        for person in [100,101,None]:self.h.outgoing(10,sales_id=person,sales_name='Same')
        r=self.result(time_range={'start':'2026-09-09','end':'2026-09-10'},dimensions=['salesperson','unit'],limit=1)
        self.assertTrue(r['truncated']);self.assertEqual(30,facts(r)[0]['unit_net_quantity'])
        self.assertEqual(3,facts(r)[0]['unit_display_groups'])

    def test_current_comparison_does_not_accept_historical_period(self):
        from test_remediation_remaining_cases import metric
        r=self.h.query(metric('registered_slow_pool_baseline_summary','inventory',month=None,baseline_week='2026-W37',time_range={'start':'2026-09-09','end':'2026-09-10'}))
        self.assertEqual('failed',r['status']);self.assertEqual([],self.h.sql_trace)


if __name__=='__main__':unittest.main()
