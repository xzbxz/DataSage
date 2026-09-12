"""Monthly independent cohorts and two achievement measures, registered/offline."""
import unittest
import test_slow_baseline_net_outbound as weekly
from test_remediation_remaining_cases import metric,facts

class MonthlyTests(unittest.TestCase):
    def setUp(self):
        self.h=weekly.BaselineNetTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM' WHERE whse_id=1")
        self.h.conn.execute('''CREATE TABLE vk_dw.inventory_barcode_detail_bymonth_dw(id INTEGER,month_date TEXT,
          goods_id INTEGER,goods_sku_id INTEGER,goods_name TEXT,whse_dept TEXT,unit TEXT,goods_num REAL,piece_num REAL,
          whse_org TEXT,whse_type TEXT,is_handing_sales TEXT,is_discountable TEXT)''')
        self.seq=0
        self.h.conn.create_function('CONVERT',1,lambda x:x)
        original=self.h.execute
        self.h.execute=lambda sql,*a,**kw:original(sql.replace(' USING utf8mb4',''),*a,**kw)
    def snap(self,month='2026-08',goods=1,sku=11,dept='HCM',unit='m',qty=30,rolls=3,handing='y',discount='n',org='Synthetic',kind='普通仓'):
        self.seq+=1
        self.h.insert('vk_dw.inventory_barcode_detail_bymonth_dw','id,month_date,goods_id,goods_sku_id,goods_name,whse_dept,unit,goods_num,piece_num,whse_org,whse_type,is_handing_sales,is_discountable',[(self.seq,month,goods,sku,'Synthetic',dept,unit,qty,rolls,org,kind,handing,discount)])
    def current(self,id=1,goods=1,sku=11,qty=20,rolls=2,white='n',unit='m',promotion_unit='m',dept='HCM'):
        self.h.add([(id,dept,goods,sku,unit,promotion_unit,qty,rolls,'handing',None,white)])
    def out(self,**kw):self.h.outgoing(**{'dept':'HCM',**kw})
    def ret(self,**kw):self.h.returning(**{'dept':'HCM',**kw})
    def query(self,view='summary',month='2026-09',**kw):
        return self.h.query(metric('registered_slow_monthly_'+view,'inventory',month=month,**kw))
    def result(self,view='summary',**kw):return self.h.result(self.query(view,**kw))
    def f(self,view='summary',**kw):return facts(self.result(view,**kw))[0]

    def test_current_month_new_changes_but_not_month_start_achievement(self):
        self.snap();self.current();self.current(id=2,goods=2,sku=22,qty=40,rolls=4)
        self.out(qty=10,rolls=4);self.ret(qty=3,rolls=1)
        self.out(qty=50,rolls=5,goods=2,sku=22)
        f=self.f();self.assertEqual(1,f['opening_group_count']);self.assertEqual(2,f['closing_group_count']);self.assertEqual(1,f['new_group_count'])
        f=self.f('net_outbound');self.assertEqual(3,f['net_rolls']);self.assertEqual(3,f['high_net_rolls'])
        self.assertEqual(1,f['gross_flow_rows'])

    def test_one_current_white_combination_excludes_both_and_all_same_key_rows(self):
        self.snap();self.snap(goods=2,sku=22,qty=40,rolls=4)
        self.current();self.current(id=2,white='y',promotion_unit='kg')
        self.current(id=3,goods=2,sku=22,qty=40,rolls=4)
        self.out(rolls=10);self.out(goods=2,sku=22,rolls=2)
        f=self.f();self.assertEqual(4,f['opening_rolls']);self.assertEqual(4,f['closing_rolls'])
        self.assertEqual(1,f['monthly_whitelist_rows']);self.assertEqual(0,f['exited_group_count'])
        self.assertEqual(2,self.f('net_outbound')['net_rolls'])

    def test_physical_group_threshold_differs_from_current_ods_row_threshold(self):
        self.snap(qty=6,rolls=1);self.snap(qty=6,rolls=1)
        self.current(qty=6,rolls=1);self.current(id=2,qty=6,rolls=1)
        f=self.f();self.assertEqual(1,f['opening_group_count']);self.assertEqual(0,f['closing_group_count']);self.assertEqual(1,f['exited_group_count'])
        self.assertEqual(2,f['opening_source_rows']);self.assertEqual(2,f['opening_rolls'])

    def test_historical_closing_uses_target_physical_and_same_white(self):
        self.snap(month='2026-07',qty=30,rolls=3);self.snap(month='2026-08',qty=20,rolls=2)
        self.current(qty=999,rolls=99)
        f=self.f(month='2026-08');self.assertEqual(3,f['opening_rolls']);self.assertEqual(2,f['closing_rolls'])
        self.current(id=2,white='y')
        r=self.result(month='2026-08');self.assertEqual([],r['rows'])

    def test_all_returns_deducted_zero_net_negative_high_sales_retained(self):
        self.snap();self.current()
        self.out(qty=40,rolls=4,deal_price=80,sales_id=100);self.out(qty=60,rolls=6,deal_price=75,sales_id=100)
        self.ret(qty=100,rolls=10,sales_id=100,sales_name='Sales A')
        f=self.f('net_outbound');self.assertEqual(0,f['metric_value']);self.assertEqual(0,f['net_rolls']);self.assertEqual(-6,f['high_net_rolls'])
        rows=self.result('net_outbound',dimensions=['salesperson','unit'])['rows']
        self.assertEqual(1,len(rows));self.assertEqual(-6,rows[0]['facts']['high_net_rolls'])

    def test_strict_75_nonpositive_ddp_no_new_exclusion_and_fraction(self):
        self.snap();self.current()
        self.out(rolls=1,deal_price=75,ddp_price=100)
        self.out(rolls=1.4,deal_price=75.01,ddp_price=100)
        self.out(rolls=2,deal_price=1,ddp_price=0)
        self.out(rolls=3,deal_price=-1,ddp_price=-2)
        f=self.f('net_outbound');self.assertAlmostEqual(6.4,f['high_net_rolls']);self.assertEqual(2,f['high_price_anomaly_rows'])

    def test_price_null_does_not_erase_complete_ordinary_rolls(self):
        self.snap();self.out(rolls=2,deal_price=None);self.ret(rolls=1)
        f=self.f('net_outbound');self.assertEqual(1,f['net_rolls']);self.assertIsNone(f['high_net_rolls'])
        self.assertEqual(-1,f['high_known_net_rolls']);self.assertEqual(1,f['high_missing_price_rows'])

    def test_missing_quantity_does_not_erase_complete_roll_achievement(self):
        self.snap();self.out(qty=None,rolls=2);self.ret(rolls=1)
        f=self.f('net_outbound');self.assertIsNone(f['metric_value']);self.assertEqual(1,f['net_rolls']);self.assertEqual(1,f['high_net_rolls'])

    def test_missing_opening_snapshot_is_failure_but_missing_closing_is_not_zero_stock(self):
        self.current()
        r=self.query('net_outbound');self.assertEqual('failed',r['status']);self.assertEqual('MONTHLY_OPENING_SNAPSHOT_MISSING',r['results'][0]['error']['code'])
        self.snap(month='2026-06');self.out(when='2026-07-12T12:00:00',rolls=2)
        r=self.result(month='2026-07');self.assertEqual(1,facts(r)[0]['unassessable_group_count']);self.assertEqual(0,facts(r)[0]['exited_group_count'])
        self.assertIsNone(facts(r)[0]['closing_group_count']);self.assertIsNone(facts(r)[0]['closing_rolls'])
        self.assertEqual(2,self.f('net_outbound',month='2026-07')['net_rolls'])

    def test_sales_ids_own_return_unit_total_and_truncation(self):
        self.snap();self.out(rolls=5,sales_id=1,sales_name='Same');self.out(rolls=4,sales_id=2,sales_name='Same')
        self.ret(rolls=2,sales_id=2,sales_name='Same')
        full=self.result('net_outbound',dimensions=['salesperson','unit'])
        self.assertEqual([2,5],sorted(f['net_rolls'] for f in facts(full)))
        self.assertEqual(7,sum(f['high_net_rolls'] for f in facts(full)))
        small=self.result('net_outbound',dimensions=['salesperson','unit'],limit=1)
        self.assertTrue(small['truncated']);self.assertEqual(7,facts(small)[0]['unit_high_net_rolls'])

    def test_unknown_white_unit_withholds_complete_flow(self):
        self.snap();self.current(id=2,white='y',unit=None);self.out(rolls=2)
        f=self.f('net_outbound');self.assertEqual(1,f['monthly_whitelist_unknown_rows']);self.assertIsNone(f['net_rolls']);self.assertEqual(2,f['known_net_rolls'])

    def test_no_extra_status_or_deleted_order_filter_and_explicit_department_scope(self):
        self.snap();self.out(rolls=2)
        self.f('net_outbound')
        sql=self.h.sql_trace[-1]['sql']
        self.assertNotIn('is_delete',sql);self.assertNotIn('is_void',sql)
        r=self.query(metric_filters={'warehouse_department':'outside'});self.assertEqual('failed',r['status'])

    def test_weekly_formula_also_uses_high_fields(self):
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='HCM'")
        self.out(rolls=1.4,deal_price=80);self.ret(rolls=2)
        r=self.h.result(self.h.run_net())
        self.assertAlmostEqual(-.6,facts(r)[0]['net_rolls']);self.assertAlmostEqual(-.6,facts(r)[0]['high_net_rolls'])

    def test_cross_unit_roll_totals_and_one_sales_identity(self):
        self.snap();self.snap(goods=2,sku=22,unit='kg')
        self.out(rolls=2,sales_id=1);self.out(goods=2,sku=22,unit='kg',rolls=3,deal_price=50,sales_id=1)
        self.ret(goods=2,sku=22,unit='kg',rolls=1,sales_id=1,sales_name='Sales A')
        rows=facts(self.result('net_outbound',dimensions=['salesperson','unit']))
        self.assertEqual(2,len(rows))
        for f in rows:
            self.assertEqual(4,f['scope_net_rolls']);self.assertEqual(1,f['scope_high_net_rolls'])
            self.assertEqual(4,f['sales_net_rolls']);self.assertEqual(1,f['sales_high_net_rolls'])
            self.assertEqual(1,f['population_sales_groups'])

    def test_low_price_missing_roll_does_not_erase_high_roll_result(self):
        self.snap();self.out(rolls=None,deal_price=50);self.ret(rolls=1)
        f=self.f('net_outbound');self.assertIsNone(f['net_rolls']);self.assertEqual(-1,f['high_net_rolls']);self.assertEqual(0,f['high_missing_roll_rows'])

    def test_monthly_report_uses_result_totals_without_week_binding(self):
        import importlib
        from test_remediation_remaining_cases import plugin
        from gateway.session_context import set_session_vars,clear_session_vars
        report=importlib.import_module(plugin.__name__+'.local_report')
        self.snap();self.current()
        self.out(qty=40,rolls=4,deal_price=80,sales_id=1);self.out(qty=60,rolls=6,deal_price=75,sales_id=1)
        self.ret(qty=100,rolls=10,sales_id=1,sales_name='Sales A')
        binding={'version':1,'default_report':'synthetic','reports':{'synthetic':{'department':'HCM','calendar_month':'2026-09','views':['monthly_pool_summary','monthly_flow_sales'],'limit':20}}}
        tokens=set_session_vars()
        try:value=report.execute_report(binding)
        finally:clear_session_vars(tokens)
        self.assertEqual('success',value['status'])
        text=report.render_text(value)
        self.assertIn('普通净出库总卷数：0',text);self.assertIn('高折净出库总卷数：-6',text)
        self.assertIn('该销售在当前筛选范围的高折净卷：-6',text)

    def test_catalog_grouping_and_no_general_raw_tag_expansion(self):
        catalog=self.h.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_pool_baseline_summary'}]})
        self.assertEqual(['unit','warehouse_department'],catalog['results'][0]['metric']['grouping']['allowed'])
        request=metric('registered_slow_pool_baseline_summary','inventory',month=None,dimensions=['product','unit'])
        self.assertEqual('failed',self.h.query(request)['status'])
        self.assertEqual('failed',self.h.query(metric('current_inventory_roll_count','inventory',month=None,dimensions=['is_discountable']))['status'])
        self.assertEqual([],self.h.sql_trace)

    def test_present_snapshot_with_empty_eligible_pool_is_not_missing_snapshot(self):
        self.snap(qty=10)
        result=self.result('net_outbound')
        self.assertEqual([],result['rows']);self.assertGreater(result['applied_time_range']['monthly_opening_snapshot_rows'],0)


if __name__=='__main__':unittest.main()
