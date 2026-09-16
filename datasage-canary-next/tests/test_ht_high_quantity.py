import unittest,importlib
import test_slow_baseline_net_outbound as net
from test_remediation_remaining_cases import facts,plugin

class HTHighQuantityTests(unittest.TestCase):
    def setUp(self):
        self.h=net.BaselineNetTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM-HT'")
        self.h.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM-HT' WHERE whse_id=1")
    def out(self,**kw):self.h.outgoing(**{'dept':'HCM-HT','bill':'sq',**kw})
    def ret(self,**kw):self.h.returning(**{'dept':'HCM-HT','bill':'sq',**kw})
    def result(self,**kw):return self.h.result(self.h.run_net(**kw))
    def test_high_net_quantity_keeps_units_and_negative_values_independent_of_rolls(self):
        self.h.baseline([(2,2,2,22,'kg',40,3,'2026-09-08T09:00:00')])
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM-HT'")
        self.out(qty=100,rolls=1,deal_price=80);self.out(qty=50,rolls=1,deal_price=70);self.ret(qty=120,rolls=1)
        self.out(goods=2,sku=22,unit='kg',qty=10,rolls=1,deal_price=80);self.out(goods=2,sku=22,unit='kg',qty=7,rolls=1,deal_price=70);self.ret(goods=2,sku=22,unit='kg',qty=2,rolls=1)
        rows=self.result()['rows'];by_unit={r['dimensions'][0]['value']:r['facts'] for r in rows}
        self.assertEqual(-20,by_unit['m']['high_net_quantity']);self.assertEqual(8,by_unit['kg']['high_net_quantity'])
        self.assertEqual(30,by_unit['m']['metric_value']);self.assertEqual(15,by_unit['kg']['metric_value'])
        self.assertEqual(0,by_unit['m']['high_net_rolls'])
        for row in rows:self.assertIn('不跨单位相加',row['fact_units']['high_net_quantity'])
    def test_exact_75_not_high_and_returns_deduct_without_new_filters(self):
        self.out(qty=10,deal_price=75);self.out(qty=20,deal_price=75.01);self.ret(qty=30)
        f=facts(self.result())[0];self.assertEqual(-10,f['high_net_quantity']);self.assertEqual(20,f['high_gross_quantity'])
    def test_missing_low_price_quantity_does_not_erase_high_quantity(self):
        self.out(qty=None,deal_price=50);self.out(qty=20,deal_price=80);self.ret(qty=3)
        f=facts(self.result())[0];self.assertIsNone(f['metric_value']);self.assertEqual(17,f['high_net_quantity'])
        self.assertEqual(0,f['high_missing_quantity_rows'])
    def test_missing_high_quantity_price_or_return_quantity_stays_unknown(self):
        for kind in ('high_quantity','price','return_quantity'):
            self.h.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd');self.h.conn.execute('DELETE FROM vk_dwd.delivery_return_detail_dwd')
            self.out(qty=None if kind=='high_quantity' else 10,deal_price=None if kind=='price' else 80)
            self.ret(qty=None if kind=='return_quantity' else 3)
            self.assertIsNone(facts(self.result())[0]['high_net_quantity'])
    def test_missing_rolls_do_not_erase_quantity(self):
        self.out(qty=10,rolls=None,deal_price=80);self.ret(qty=3,rolls=1)
        f=facts(self.result())[0];self.assertEqual(7,f['high_net_quantity']);self.assertIsNone(f['high_net_rolls'])
    def test_sales_unit_total_survives_display_truncation_without_cross_unit_sum(self):
        self.out(qty=10,sales_id=1);self.out(qty=20,sales_id=2);self.ret(qty=3,sales_id=2)
        result=self.result(dimensions=['salesperson','unit'],limit=1)
        self.assertTrue(result['truncated']);self.assertEqual(27,result['rows'][0]['facts']['unit_high_net_quantity'])
    def test_monthly_source_reuses_same_quantity_formula(self):
        import test_monthly_slow_pool as monthly
        case=monthly.MonthlyTests();case.setUp()
        try:
            case.snap(dept='HCM-HT');case.current(dept='HCM-HT')
            case.h.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM-HT' WHERE whse_id=1")
            case.out(dept='HCM-HT',bill='sq',qty=12,rolls=1,deal_price=80);case.ret(dept='HCM-HT',bill='sq',qty=15,rolls=1)
            self.assertEqual(-3,case.f('net_outbound')['high_net_quantity'])
        finally:case.doCleanups()
if __name__=='__main__':unittest.main()
