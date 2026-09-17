"""Profit SQL and public evidence checks; only database I/O is replaced by SQLite."""
import unittest
import json
import yaml
import test_remediation_remaining_cases as public
from test_remediation_remaining_cases import plugin, metric, facts


class ProfitContractTests(unittest.TestCase):
    insert = public.RemainingCaseTests.insert
    execute = public.RemainingCaseTests.execute
    invoke = public.RemainingCaseTests.invoke
    query = public.RemainingCaseTests.query
    result = public.RemainingCaseTests.result

    def setUp(self):
        public.RemainingCaseTests.setUp(self)
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_ads")
        ds = plugin.contract_store.read_yaml('plugins/datasage-query/contracts/datasets.yaml')['datasets']
        for table in ('customer_profit_ads', 'delivery_bill_profit_ads', 'dept_profit_ads', 'goods_profit_ads'):
            columns = ds['vk_ads.'+table]['allowed_columns']
            decl = ','.join('"'+c+'" '+('REAL' if c.endswith('_rmb') else 'TEXT') for c in columns)
            self.conn.execute('CREATE TABLE vk_ads.'+table+' ('+decl+')')

    def customer_rows(self, rows):
        self.insert('vk_ads.customer_profit_ads','guid,bill_date,customer_id,customer_name,sale_amount_rmb,gross_profit_rmb,qc_amount_rmb',rows)

    def test_public_catalog_registers_four_ledgers_without_physical_fields(self):
        data=self.invoke('datasage_catalog',{'requests':[{'domain':'profit','view':'expert_index'}]})
        self.assertEqual('success',data['status'],data)
        self.assertEqual(70,len(data['results'][0]['metrics']),data)
        self.assertFalse(self.sql_trace)
        self.assertNotIn("vk_ads.", json.dumps(data))

    def test_profit_disclosures_bound_cross_ledger_causality_and_missing_values(self):
        semantics=plugin.contract_store.read_yaml('plugins/datasage-query/contracts/profit-semantics.yaml')
        defaults={item['id']:item['text'] for item in semantics['default_disclosures']}
        self.assertIn('不能直接互相替代',defaults['profit.scope'])
        self.assertIn('不能单独证明当次差额的原因或方向',defaults['profit.scope'])
        self.assertIn('既不能解释为0，也不能据此断言为非0',defaults['profit.missing-fees'])

    def test_weighted_margin_sums_before_dividing_and_keeps_month_grain(self):
        self.customer_rows([('1','2026-08','1','A',100,30,2),('2','2026-08','1','A',300,30,3),('3','2026-09','1','A',900,900,4)])
        data=self.result(self.query(metric('customer_month_gross_margin','profit')))
        self.assertAlmostEqual(.15,facts(data)[0]['metric_value'])
        self.assertTrue(any('2026-08' in q['params'] and '2026-09' in q['params'] for q in self.sql_trace))

    def test_negative_denominator_is_signed_not_clipped(self):
        self.customer_rows([('1','2026-08','1','A',-100,-20,0)])
        data=self.result(self.query(metric('customer_month_gross_margin','profit')))
        self.assertAlmostEqual(.2,facts(data)[0]['metric_value'])
        self.assertEqual(-20,facts(data)[0]['numerator_value'])
        self.assertEqual(-100,facts(data)[0]['denominator_value'])

    def test_zero_denominator_and_missing_numerator_stay_undefined(self):
        for rows in [ [('1','2026-08','1','A',100,20,0),('2','2026-08','2','B',-100,5,0)], [('1','2026-08','1','A',100,None,0)] ]:
            self.conn.execute('DELETE FROM vk_ads.customer_profit_ads')
            self.customer_rows(rows)
            data=self.result(self.query(metric('customer_month_gross_margin','profit')))
            self.assertIsNone(facts(data)[0]['metric_value'],data)

    def test_missing_expense_is_not_zero_or_a_complete_total(self):
        self.customer_rows([('1','2026-08','1','A',100,30,10),('2','2026-08','2','B',200,50,None)])
        data=self.result(self.query(metric('customer_month_inspection_expense','profit')))
        f=facts(data)[0]
        self.assertIsNone(f['metric_value'],data)
        self.assertEqual(1,f['missing_value_count'],data)
        self.assertEqual(10,f['known_subset_value'],data)

    def test_monthly_trend_and_partial_month_rejection(self):
        self.customer_rows([('1','2026-07','1','A',100,30,0),('2','2026-08','1','A',200,40,0)])
        r=metric('customer_month_gross_profit','profit',month=None,time_range={'start':'2026-07-01','end':'2026-09-01'},time_bucket='month')
        data=self.result(self.query(r))
        self.assertEqual([30,40],sorted(f['metric_value'] for f in facts(data)))
        before=len(self.sql_trace)
        r['time_range']={'start':'2026-07-15','end':'2026-09-01'}
        self.assertEqual('failed',self.query(r)['status'])
        self.assertEqual(before,len(self.sql_trace))

    def test_unsupported_customer_product_and_private_price_rejected_before_io(self):
        for r in [metric('customer_month_gross_profit','profit',dimensions=['product']),metric('order_lifetime_purchase_cost_price','profit')]:
            before=len(self.sql_trace)
            self.assertEqual('failed',self.query(r)['status'])
            self.assertEqual(before,len(self.sql_trace))

    def test_order_lifetime_product_grouping(self):
        self.insert('vk_ads.delivery_bill_profit_ads','sale_bill_id,goods_id,goods_no,goods_name,delivery_time,gross_profit_rmb', [('1','1','G1','P1','2026-08-01',50),('2','1','G1','P1','2026-08-15',-10),('3','2','G2','P2','2026-08-15',20)])
        data=self.result(self.query(metric('order_lifetime_gross_profit','profit',dimensions=['product'])))
        self.assertEqual([20,40],sorted(f['metric_value'] for f in facts(data)))

    def test_existing_return_ratio_still_rejects_negative_base(self):
        self.insert('vk_dwd.sale_bill_goods_detail_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time',[(-100,6,'n','2026-08-01')])
        self.insert('vk_dwd.delivery_return_detail_dwd','return_amount_rmb,status,complnt_type,channel_type,is_inner_cus,statement_time',[(10,4,1,1,'n','2026-08-01')])
        data=self.result(self.query(metric('return_amount_rate','delivery')))
        self.assertIsNone(facts(data)[0]['metric_value'])

    def test_monthly_comparison_uses_previous_complete_month(self):
        self.customer_rows([('1','2026-07','1','A',100,30,0),('2','2026-08','1','A',200,60,0)])
        data=self.result(self.query(metric('customer_month_gross_profit','profit',comparison={'kind':'previous_period'})))
        f=facts(data)[0]
        self.assertEqual(60,f['metric_value'])
        self.assertEqual(30,f['comparison_value'])
        self.assertEqual(30,f['delta_value'])

    def test_all_missing_expenses_have_no_known_subtotal(self):
        self.customer_rows([('1','2026-08','1','A',100,30,None)])
        data=self.result(self.query(metric('customer_month_inspection_expense','profit')))
        f=facts(data)[0]
        self.assertIsNone(f['metric_value'])
        self.assertIsNone(f['known_subset_value'])
        self.assertEqual(0,f['known_value_count'])

    def test_four_ledgers_keep_distinct_reported_values(self):
        self.customer_rows([('c','2026-08','1','A',1000,100,None)])
        self.insert('vk_ads.dept_profit_ads','guid,bill_date,customer_dept,sale_amount_rmb,gross_profit_rmb',[('d','2026-08','HCM',1000,200)])
        self.insert('vk_ads.goods_profit_ads','guid,bill_date,goods_id,goods_no,goods_name,dept_name,sale_amount_rmb,gross_profit_rmb',[('p','2026-08','1','G1','P1','HCM',1000,300)])
        self.insert('vk_ads.delivery_bill_profit_ads','sale_bill_id,goods_id,delivery_time,sale_amount_rmb,gross_profit_rmb',[('o','1','2026-08-10',1000,400)])
        codes=['customer_month','department_month','product_month','order_lifetime']
        data=self.query(*(metric(code+'_gross_profit','profit',request_id=code) for code in codes))
        for code,value in zip(codes,[100,200,300,400]):
            self.assertEqual(value,facts(self.result(data,code))[0]['metric_value'])

    def test_product_department_mapping_and_margin(self):
        self.insert('vk_ads.goods_profit_ads','guid,bill_date,goods_id,goods_no,goods_name,dept_name,sale_amount_rmb,gross_profit_rmb',[('1','2026-08','1','G1','P1','HCM',100,30),('2','2026-08','2','G2','P2','HCM',300,30),('3','2026-08','3','G3','P3','BKK',500,200)])
        r=metric('product_month_gross_margin','profit',dimensions=['department'],metric_filters={'department':'HCM'})
        data=self.result(self.query(r))
        self.assertEqual(1,len(data['rows']))
        self.assertAlmostEqual(.15,facts(data)[0]['metric_value'])
        self.assertEqual(60,facts(data)[0]['numerator_value'])
        self.assertEqual(400,facts(data)[0]['denominator_value'])
        self.assertTrue(any('`dept_name`' in q['sql'] for q in self.sql_trace))

    def test_unallocated_product_commission_changes_after_allocation(self):
        self.insert('vk_ads.goods_profit_ads','guid,bill_date,sales_commission_rmb',[('1','2026-08',None)])
        r=metric('product_month_sales_commission','profit')
        before=self.result(self.query(r));f=facts(before)[0]
        self.assertIsNone(f['metric_value']);self.assertIsNone(f['known_subset_value'])
        self.assertEqual(1,f['missing_value_count'])
        self.conn.execute('UPDATE vk_ads.goods_profit_ads SET sales_commission_rmb=12')
        after=self.result(self.query(r));f=facts(after)[0]
        self.assertEqual(12,f['metric_value']);self.assertEqual(0,f['missing_value_count'])

    def test_department_and_product_reject_absent_dimensions(self):
        for code,dim in [('department_month_gross_profit','customer'),('department_month_gross_profit','product'),('product_month_gross_profit','salesperson'),('product_month_gross_profit','internal_customer')]:
            before=len(self.sql_trace)
            self.assertEqual('failed',self.query(metric(code,'profit',dimensions=[dim]))['status'])
            self.assertEqual(before,len(self.sql_trace))

if __name__=='__main__': unittest.main()
