"""Selected-period inventory turnover through the registered offline SQL path."""
import unittest
import test_remediation_remaining_cases as public

class InventoryTurnoverTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.conn.execute('ALTER TABLE vk_dw.goods_turnover_basic_data_dw ADD COLUMN dept_name TEXT')
        self.h.conn.execute('ALTER TABLE vk_dw.goods_turnover_basic_data_dw ADD COLUMN org_name TEXT')
    def seed(self,rows):
        self.h.conn.execute('DELETE FROM vk_dw.goods_turnover_basic_data_dw')
        other=[(10,20,1,m,'OTHER','ORG') for m in ['2026-05','2026-06','2026-07']]
        self.h.insert('vk_dw.goods_turnover_basic_data_dw','cost_amount_rmb,ddp_amount_rmb,pur_delivery_rmb,bill_date,dept_name,org_name',[(*r,'ORG') for r in rows]+other)
    def query(self,**kw):
        return self.h.query(public.metric('inventory_turnover_days','inventory',month=None,
            time_range={'start':'2026-06-01','end':'2026-08-01'},**kw))
    def facts(self,response):return self.h.result(response)['rows'][0]['facts']
    def base(self):return [(100,200,0,'2026-05','A'),(100,200,50,'2026-06','A'),(100,200,50,'2026-07','A')]
    def test_complete_control_and_two_dimension_groups(self):
        self.seed(self.base())
        f=self.facts(self.query(metric_filters={'department':'A'},dimensions=['department','organization']))
        self.assertEqual(61,f['cost_turnover_days']);self.assertEqual(122,f['ddp_turnover_days'])
        self.assertEqual(3,f['expected_snapshot_count']);self.assertEqual(3,f['actual_snapshot_count'])
    def test_null_cost_keeps_independent_ddp(self):
        self.seed(self.base()+[(None,50,20,'2026-06','A')])
        response=self.query(metric_filters={'department':'A'});f=self.facts(response)
        self.assertIsNone(f['avg_inventory_cost_rmb']);self.assertIsNone(f['cost_turnover_days'])
        self.assertEqual(114.375,f['ddp_turnover_days']);self.assertEqual(1,f['cost_missing_value_count'])
    def test_null_ddp_keeps_cost_and_null_net_invalidates_both(self):
        self.seed(self.base()+[(20,None,20,'2026-06','A')])
        f=self.facts(self.query(metric_filters={'department':'A'}))
        self.assertAlmostEqual(110*61/120,f['cost_turnover_days']);self.assertIsNone(f['ddp_turnover_days'])
        self.seed(self.base()+[(20,40,None,'2026-06','A')])
        f=self.facts(self.query(metric_filters={'department':'A'}))
        self.assertIsNone(f['net_delivery_rmb']);self.assertIsNone(f['cost_turnover_days']);self.assertIsNone(f['ddp_turnover_days'])
    def test_missing_entity_opening_not_proven_by_other_entity(self):
        self.seed(self.base()[1:]);f=self.facts(self.query(metric_filters={'department':'A'}))
        self.assertIsNone(f['avg_inventory_cost_rmb']);self.assertIsNone(f['cost_turnover_days'])
        self.assertEqual(2,f['actual_snapshot_count']);self.assertEqual(3,f['expected_snapshot_count'])
        self.assertEqual(100,f['net_delivery_rmb'])
    def test_explicit_zero_months_do_not_shorten_period(self):
        self.seed([(0,0,0,'2026-05','A'),(0,0,0,'2026-06','A'),(100,200,50,'2026-07','A')])
        response=self.query(metric_filters={'department':'A'});f=self.facts(response)
        self.assertEqual(61,f['period_natural_days']);self.assertEqual(2,f['effective_operating_months'])
        self.assertEqual(25,f['avg_inventory_cost_rmb']);self.assertEqual(30.5,f['cost_turnover_days'])
    def test_signed_zero_values_and_zero_denominator(self):
        for inventory,net,expected in [(100,50,61),(-100,50,-61),(0,50,0),(100,-50,-61),(-100,-50,61),(100,0,None)]:
            with self.subTest(inventory=inventory,net=net):
                self.seed([(inventory,inventory*2,0,'2026-05','A'),(inventory,inventory*2,net,'2026-06','A'),(inventory,inventory*2,net,'2026-07','A')])
                f=self.facts(self.query(metric_filters={'department':'A'}))
                self.assertEqual(expected,f['cost_turnover_days']);self.assertEqual(inventory,f['avg_inventory_cost_rmb'])
    def test_default_dates_keep_nonzero_snapshot_candidate(self):
        rows=[(100,200,50,f'2025-{m:02d}','A') for m in range(7,13)]+[(100,200,50,f'2026-{m:02d}','A') for m in range(1,8)]+[(0,0,50,'2026-08','A')]
        self.seed(rows)
        response=self.h.query(public.metric('inventory_turnover_days','inventory',month=None,metric_filters={'department':'A'}))
        r=self.h.result(response)
        self.assertEqual('2025-08-01',r['applied_time_range']['start']);self.assertEqual('2026-08-01',r['applied_time_range']['end'])
        self.assertEqual(365,r['rows'][0]['facts']['period_natural_days'])
    def test_group_failure_does_not_hide_valid_group(self):
        self.seed(self.base()+[(100,200,50,'2026-07','MISSING')])
        result=self.h.result(self.query(dimensions=['department']))
        by_name={r['dimensions'][0]['value']:r['facts'] for r in result['rows']}
        self.assertEqual(61,by_name['A']['cost_turnover_days']);self.assertIsNone(by_name['MISSING']['cost_turnover_days'])

    def test_global_zero_cost_blocks_both_even_with_nonzero_ddp(self):
        self.seed(self.base())
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=0 WHERE bill_date='2026-07'")
        response=self.query(metric_filters={'department':'A'});f=self.facts(response)
        self.assertIsNone(f['cost_turnover_days']);self.assertIsNone(f['ddp_turnover_days'])
        self.assertIsNone(f['avg_inventory_cost_rmb']);self.assertIsNone(f['avg_inventory_ddp_rmb'])
        self.assertEqual(1,f['unready_accounting_month_count'])
        self.assertEqual('2026-07',f['unready_accounting_months'])
        self.assertEqual(61,f['period_natural_days'])
        self.assertEqual('2026-08-01',self.h.result(response)['applied_time_range']['end'])
        # Readiness is global: another group's nonzero cost permits A's true zero.
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=10 WHERE bill_date='2026-07' AND dept_name='OTHER'")
        f=self.facts(self.query(metric_filters={'department':'A'}))
        self.assertEqual(0,f['unready_accounting_month_count'])
        self.assertEqual(45.75,f['cost_turnover_days']);self.assertEqual(122,f['ddp_turnover_days'])

    def test_default_cannot_use_ddp_to_bypass_cost_accounting(self):
        rows=[(100,200,50,f'2025-{m:02d}','A') for m in range(7,13)]+[(100,200,50,f'2026-{m:02d}','A') for m in range(1,8)]+[(0,999,50,'2026-08','A')]
        self.seed(rows)
        response=self.h.query(public.metric('inventory_turnover_days','inventory',month=None,metric_filters={'department':'A'}))
        r=self.h.result(response)
        self.assertEqual('2025-08-01',r['applied_time_range']['start'])
        self.assertEqual('2026-08-01',r['applied_time_range']['end'])
        self.assertEqual(0,r['rows'][0]['facts']['unready_accounting_month_count'])

    def test_opening_or_middle_unaccounted_month_is_not_skipped(self):
        for month in ('2026-05','2026-06'):
            with self.subTest(month=month):
                self.seed(self.base())
                self.h.conn.execute('UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=0 WHERE bill_date=?',(month,))
                f=self.facts(self.query(metric_filters={'department':'A'}))
                self.assertIsNone(f['cost_turnover_days']);self.assertIsNone(f['ddp_turnover_days'])
                self.assertEqual(month,f['unready_accounting_months'])
                self.assertEqual(2,f['effective_operating_months'])

    def test_global_net_zero_is_not_all_rows_zero(self):
        self.seed(self.base())
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=-100 WHERE dept_name='OTHER'")
        f=self.facts(self.query(metric_filters={'department':'A'}))
        self.assertEqual(0,f['unready_accounting_month_count'])
        self.assertEqual(61,f['cost_turnover_days'])

    def seed_original(self):
        self.seed(self.base())
        columns={row[1] for row in self.h.conn.execute('PRAGMA vk_dw.table_info(goods_turnover_basic_data_dw)')}
        for name,kind in [('currency_no','TEXT'),('cost_amount','REAL'),('ddp_amount','REAL'),('pur_delivery_amount','REAL')]:
            if name not in columns:
                self.h.conn.execute(f'ALTER TABLE vk_dw.goods_turnover_basic_data_dw ADD COLUMN {name} {kind}')
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET currency_no=CASE WHEN dept_name='A' THEN 'VND' ELSE 'THB' END,cost_amount=cost_amount_rmb*10,ddp_amount=ddp_amount_rmb*10,pur_delivery_amount=pur_delivery_rmb*5")

    def original_query(self,**kw):
        return self.h.query(public.metric('inventory_turnover_days_original','inventory',month=None,
            time_range={'start':'2026-06-01','end':'2026-08-01'},**kw))

    def test_original_uses_original_operands_not_rmb_turnover_value(self):
        self.seed_original()
        facts=self.facts(self.original_query(metric_filters={'department':'A','currency':'VND'}))
        # Three snapshots of 1000; June/July flows are 250 each, over 61 days.
        self.assertEqual(1000,facts['avg_inventory_cost_original'])
        self.assertEqual(500,facts['net_delivery_original'])
        self.assertEqual(122,facts['cost_turnover_days'])
        self.assertEqual(244,facts['ddp_turnover_days'])
        self.assertNotIn('avg_inventory_cost_rmb',facts)

    def test_original_keeps_global_rmb_accounting_readiness(self):
        self.seed_original()
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=0 WHERE bill_date='2026-07'")
        facts=self.facts(self.original_query(metric_filters={'department':'A','currency':'VND'}))
        self.assertIsNone(facts['cost_turnover_days'])
        self.assertIsNone(facts['ddp_turnover_days'])
        self.assertEqual(1,facts['unready_accounting_month_count'])
        # A different currency's nonzero RMB cost establishes global readiness.
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET cost_amount_rmb=10 WHERE bill_date='2026-07' AND dept_name='OTHER'")
        facts=self.facts(self.original_query(metric_filters={'department':'A','currency':'VND'}))
        self.assertEqual(0,facts['unready_accounting_month_count'])
        self.assertEqual(122,facts['cost_turnover_days'])

    def test_original_unknown_currency_keeps_source_rows_without_amount_or_ratio(self):
        self.seed_original()
        self.h.conn.execute("UPDATE vk_dw.goods_turnover_basic_data_dw SET currency_no=NULL WHERE dept_name='A'")
        facts=self.facts(self.original_query(metric_filters={'department':'A'},dimensions=['currency']))
        self.assertEqual(3,facts['currency_missing_value_count'])
        for field in ('metric_value','cost_turnover_days','ddp_turnover_days','avg_inventory_cost_original','avg_inventory_ddp_original','net_delivery_original'):
            self.assertIsNone(facts[field],field)

    def test_auto_probe_includes_currency_present_only_in_opening_snapshot(self):
        self.seed_original()
        self.h.conn.execute("DELETE FROM vk_dw.goods_turnover_basic_data_dw WHERE dept_name='OTHER' AND bill_date<>'2026-05'")
        facts=self.facts(self.query(currency_basis='auto'))
        # Opening has 110 RMB, middle/closing 100: weighted average 102.5.
        self.assertEqual(102.5,facts['avg_inventory_cost_rmb'])
        self.assertAlmostEqual(62.525,facts['cost_turnover_days'])
        self.assertNotIn('avg_inventory_cost_original',facts)

if __name__=='__main__':unittest.main()
