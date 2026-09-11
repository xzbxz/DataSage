"""Independent public-boundary regressions for R-01/R-02/R-03; no real data/model."""
from pathlib import Path
import unittest,json,sys
if 'datasage-canary-next' not in str(Path(__file__).resolve()):
    sys.path.insert(0,'C:/Users/10192/AppData/Local/hermes/profiles/datasage-canary-next/tests')
import test_slow_baseline_comparison as baseline
import test_slow_baseline_net_outbound as net
import test_registered_slow_pool as pool
import test_profit_contract as profit
import test_pattern_matching as pattern
from test_remediation_remaining_cases import metric,facts

def dimensions(row):return {d['label']:d['value'] for d in row['dimensions']}

class P1PublicRegressions(unittest.TestCase):
    def fixture(self,cls):
        h=cls();h.setUp();self.addCleanup(h.doCleanups);return h
    def seed_baseline(self,h):
        h.baseline([(1,1,1,11,'m',30,2,'2026-09-08T09:00:00'),(2,2,1,12,'kg',40,3,'2026-09-08T09:00:00')])
    def test_baseline_detail_default_empty_and_explicit_keep_full_business_tuple(self):
        h=self.fixture(baseline.BaselineComparisonTests);self.seed_baseline(h)
        for kw in [{},{'dimensions':[]},{'dimensions':['product','pool_sku','warehouse_department','unit']}]:
            r=h.result(h.compare(**kw));observed=set()
            for row in r['rows']:
                d=dimensions(row);f=row['facts']
                observed.add((d.get('产品'),d.get('登记规格标识'),d.get('仓库部门'),d.get('登记库存单位'),f['opening_quantity'],row['states']['pool_movement_state']))
            self.assertEqual({('Product','11','A','m',30,'Exited'),('Product','12','A','kg',40,'Exited')},observed)
    def test_baseline_summary_default_empty_and_explicit_preserve_unit_values(self):
        h=self.fixture(baseline.BaselineComparisonTests);self.seed_baseline(h)
        for kw in [{},{'dimensions':[]},{'dimensions':['unit']}]:
            r=h.result(h.compare('summary',**kw))
            self.assertEqual({('m',30,1),('kg',40,1)},{(dimensions(row).get('登记库存单位'),row['facts']['opening_quantity'],row['facts']['exited_group_count']) for row in r['rows']})
    def test_net_default_empty_explicit_units_and_signed_state_match(self):
        h=self.fixture(net.BaselineNetTests)
        h.baseline([(2,2,2,22,'kg',40,3,'2026-09-08T09:00:00')]);h.outgoing(5);h.returning(12)
        for kw in [{},{'dimensions':[]},{'dimensions':['unit']}]:
            r=h.result(h.run_net(**kw))
            self.assertEqual({('m',-7,'both_sides_recorded'),('kg',0,'no_recorded_flow')},{(dimensions(row).get('登记库存单位'),row['facts']['metric_value'],row['states']['net_flow_state']) for row in r['rows']})
    def test_same_long_product_name_different_skus_remain_distinct(self):
        h=self.fixture(pool.SlowPoolTests)
        h.add([(1,'A',1,11,'m','m',20,2,'handing',12345,'n'),(2,'A',2,12,'m','m',30,3,'handing',12345,'n')])
        h.conn.execute('UPDATE vk_ods.slow_moving_goods_ods SET goods_name=?',('相同长产品名称'*30,))
        r=h.result(h.query(h.req('rolls',dimensions=['product','pool_sku'])))
        self.assertEqual({('11',2),('12',3)},{(dimensions(row).get('登记规格标识'),row['facts']['metric_value']) for row in r['rows']})
        self.assertEqual(1,len({dimensions(row)['产品'] for row in r['rows']}))
        selected=h.result(h.query(h.req('rolls',metric_filters={'pool_sku':'12'})))
        self.assertEqual(3,facts(selected)[0]['metric_value'])
        for text in ['goods_sku_id','goods_id','promotion_price','12345','vk_ods.']:self.assertNotIn(text,json.dumps(r))
    def test_same_amount_different_orders_and_negative_profit_keep_numbers(self):
        h=self.fixture(profit.ProfitContractTests)
        h.insert('vk_ads.delivery_bill_profit_ads','sale_bill_id,bill_no,goods_id,delivery_time,gross_profit_rmb',[('1','ORDER-A','1','2026-08-01',50),('2','ORDER-B','1','2026-08-01',50),('3','ORDER-C','1','2026-08-01',-10)])
        r=h.result(h.query(metric('order_lifetime_gross_profit','profit',dimensions=['order'])))
        self.assertEqual({('ORDER-A',50),('ORDER-B',50),('ORDER-C',-10)},{(dimensions(row).get('出库单号'),row['facts']['metric_value']) for row in r['rows']})
        self.assertNotIn('sale_bill_id',json.dumps(r));self.assertNotIn('bill_no',json.dumps(r))
        selected=h.result(h.query(metric('order_lifetime_gross_profit','profit',metric_filters={'order':'ORDER-C'})))
        self.assertEqual(-10,facts(selected)[0]['metric_value'])
        overall=h.result(h.query(metric('order_lifetime_gross_profit','profit',dimensions=[])))
        self.assertEqual(90,facts(overall)[0]['metric_value']);self.assertEqual([],overall['rows'][0]['dimensions'])
    def test_long_identifier_display_is_distinct_and_not_a_filter_token(self):
        h=self.fixture(profit.ProfitContractTests)
        labels=['ORDER-'+'A'*100+'-1','ORDER-'+'A'*100+'-2']
        h.insert('vk_ads.delivery_bill_profit_ads','sale_bill_id,bill_no,goods_id,delivery_time,gross_profit_rmb',[(str(i),label,'1','2026-08-01',50) for i,label in enumerate(labels,1)])
        r=h.result(h.query(metric('order_lifetime_gross_profit','profit',dimensions=['order'])))
        self.assertTrue(all(row['dimensions'] for row in r['rows']))
        output=[row['dimensions'][0] for row in r['rows']]
        self.assertEqual(2,len({d['value'] for d in output}));self.assertTrue(all(d.get('display_only') is True for d in output))
    def test_truncated_baseline_keeps_identifier_and_prelimit_population(self):
        h=self.fixture(baseline.BaselineComparisonTests);self.seed_baseline(h)
        r=h.result(h.compare(limit=1));self.assertTrue(r['truncated'])
        self.assertIn(dimensions(r['rows'][0]).get('登记规格标识'),{'11','12'})
        self.assertEqual(2,r['rows'][0]['facts']['population_union_groups'])
    def test_internal_projection_fields_cannot_be_supplied_by_caller(self):
        h=self.fixture(pool.SlowPoolTests)
        for kwargs in [{'effective_dimensions':['pool_sku']},{'public_display_fields':['password']}]:
            before=len(h.sql_trace);response=h.query(h.req('rolls',**kwargs));self.assertEqual('failed',response['status']);self.assertEqual(before,len(h.sql_trace))
    def test_catalog_keeps_display_policy_private(self):
        h=self.fixture(profit.ProfitContractTests)
        r=h.invoke('datasage_catalog',{'requests':[{'domain':'profit','metric':'order_lifetime_gross_profit'},{'domain':'inventory','metric':'registered_slow_pool_rolls'}]})
        self.assertEqual('success',r['status'])
        for value in ['public_display_fields','goods_sku_id','bill_no']:self.assertNotIn(value,json.dumps(r))
    def seed_pattern(self):
        h=self.fixture(pattern.PatternTests);h.sale(1,100);h.row(detail=1,amount=100,currency=None);return h
    def request_variants(self):
        for basis in ['current_observation','task_created','execution_completed','linked_delivery']:
            for currency in [False,True]:
                kwargs={'pattern_time_basis':basis}
                if basis!='current_observation':kwargs['time_range']={'start':'2026-08-01','end':'2026-10-01'}
                if currency:kwargs['metric_filters']={'currency':'VND'}
                yield kwargs
    def test_unlinked_rows_do_not_change_amount_for_any_supported_basis_filter(self):
        h=self.seed_pattern()
        for code in ['linked_delivery_amount','person_attributed_delivery_amount']:
            for kw in self.request_variants():self.assertEqual(100,h.value(code,**kw)['metric_value'])
        h.row(task=2,execute=22,detail=None,amount=None,product=None,found='n',created=None,completed=None,execute_status=1)
        for code in ['linked_delivery_amount','person_attributed_delivery_amount']:
            for kw in self.request_variants():
                r=h.result(h.run_pattern(code,**kw));f=facts(r)[0]
                self.assertEqual(('VND',100,0,'rows'),(dimensions(r['rows'][0]).get('已核验出库币种'),f['metric_value'],f['pattern_scope_unknown_rows'],r['data_state']))
        self.assertEqual(2,h.value()['known_task_count']);self.assertEqual(1,h.value()['tasks_with_recorded_link'])
        h.conn.execute('DELETE FROM vk_dwd.pattern_matching_dwd WHERE task_id=2')
        self.assertEqual(1,h.value()['known_task_count']);self.assertEqual(100,h.value('linked_delivery_amount')['metric_value'])
    def test_suspected_link_missing_identity_stays_unknown(self):
        h=self.seed_pattern();h.row(task=2,execute=22,detail=None,amount=30,product='P')
        h.conn.execute("UPDATE vk_dwd.pattern_matching_dwd SET sale_bill_no='BAD-LINK' WHERE task_id=2")
        for kw in self.request_variants():
            r=h.result(h.run_pattern('linked_delivery_amount',**kw));self.assertTrue(any(row['facts']['metric_value'] is None for row in r['rows']))
            self.assertEqual(100,sum(row['facts']['known_subset_value'] or 0 for row in r['rows']))
    def test_missing_actual_time_amount_and_source_conflict_are_not_removed(self):
        for fault in ['time','amount','currency','duplicate']:
            with self.subTest(fault=fault):
                h=self.seed_pattern();h.sale(2,30);h.row(task=2,execute=22,detail=2,amount=30)
                if fault=='time':h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_time=NULL WHERE goods_detail_id=2')
                elif fault=='amount':h.conn.execute('UPDATE vk_dwd.pattern_matching_dwd SET delivery_amount=NULL WHERE task_id=2')
                elif fault=='currency':h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET currency_no=NULL WHERE goods_detail_id=2')
                else:h.sale(2,35)
                kw={'pattern_time_basis':'linked_delivery','time_range':{'start':'2026-08-01','end':'2026-10-01'},'metric_filters':{'currency':'VND'}}
                r=h.result(h.run_pattern('linked_delivery_amount',**kw));self.assertTrue(any(row['facts']['metric_value'] is None for row in r['rows']))
                self.assertEqual(100,sum(row['facts']['known_subset_value'] or 0 for row in r['rows']))
                h.doCleanups()
    def test_no_link_only_is_empty_amount_but_preserves_task(self):
        h=self.fixture(pattern.PatternTests);h.row(detail=None,amount=None,product=None,found=None,completed=None)
        for kw in self.request_variants():self.assertEqual('empty',h.result(h.run_pattern('linked_delivery_amount',**kw))['data_state'])
        self.assertEqual(1,h.value()['known_task_count'])

if __name__=='__main__':unittest.main()
