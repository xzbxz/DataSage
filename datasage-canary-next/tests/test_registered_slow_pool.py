"""Current pool tests via the registered public boundary; no real database."""
import json,unittest
import test_remediation_remaining_cases as public
from test_remediation_remaining_cases import plugin,metric,facts

class SlowPoolTests(unittest.TestCase):
    insert=public.RemainingCaseTests.insert
    execute=public.RemainingCaseTests.execute
    invoke=public.RemainingCaseTests.invoke
    query=public.RemainingCaseTests.query
    result=public.RemainingCaseTests.result
    def setUp(self):
        public.RemainingCaseTests.setUp(self)
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_ods")
        self.conn.execute('''CREATE TABLE vk_ods.slow_moving_goods_ods(
            id INTEGER,whse_dept TEXT,goods_id INTEGER,goods_sku_id INTEGER,
            goods_no TEXT,goods_name TEXT,attr_val TEXT,color_label TEXT,
            source_unit TEXT,unit TEXT,goods_num REAL,piece_num REAL,
            slow_label TEXT,promotion_price REAL,is_whitelist TEXT)''')
    def add(self,rows):
        self.insert('vk_ods.slow_moving_goods_ods','id,whse_dept,goods_id,goods_sku_id,source_unit,unit,goods_num,piece_num,slow_label,promotion_price,is_whitelist',rows)
    def req(self,stat,**kwargs):return metric('registered_slow_pool_'+stat,'inventory',month=None,**kwargs)
    def val(self,stat,**kwargs):return facts(self.result(self.query(self.req(stat,**kwargs))))[0]
    def test_units_are_source_units_not_promotion_units(self):
        self.add([(1,'A',1,11,'m','kg',20,1.5,'handing',None,'n'),(2,'A',2,22,'kg','m',12,2,'handing',None,'n'),(3,'A',3,33,None,'m',50,3,'handing',None,'n')])
        self.assertEqual('failed',self.query(self.req('quantity'))['status']);self.assertFalse(self.sql_trace)
        r=self.result(self.query(self.req('quantity',dimensions=['unit'])))
        vals={(row['dimensions'][0]['value'] if row['dimensions'] else None):row['facts']['metric_value'] for row in r['rows']}
        self.assertEqual(3,len(r['rows']))
        self.assertEqual(20,vals['m']);self.assertEqual(12,vals['kg']);self.assertIn(None,vals.values())
        self.assertEqual(6.5,self.val('rolls')['metric_value'])
        self.assertEqual('failed',self.query(self.req('quantity',metric_filters={'unit':['m','kg']}))['status'])
    def test_counts_and_threshold_do_not_count_rolls_as_quantity(self):
        self.add([(1,'A',1,11,'m','m',11,1.25,'handing',None,'n'),(2,'B',1,11,'m','m',12,2,'handing',None,'n'),(3,'A',2,22,'m','m',10,100,'handing',None,'n'),(4,'A',3,33,'m','m',50,40,'handing',None,'y')])
        self.assertEqual(2,self.val('entries')['metric_value'])
        self.assertEqual(1,self.val('products')['metric_value'])
        self.assertEqual(1,self.val('skus')['metric_value'])
        self.assertEqual(3.25,self.val('rolls')['metric_value'])
    def test_classifications_overlap_without_exposing_prices(self):
        self.add([(1,'A',1,11,'m','m',20,3,'discountable',123456.78,'n'),(2,'A',2,22,'m','m',20,5,'handing',10,'n'),(3,'A',3,33,'m','m',20,7,'discountable',None,'n')])
        vals=[self.val(k)['metric_value'] for k in ['rolls','discount_rolls','priced_rolls','overlap_rolls','unpriced_rolls']]
        self.assertEqual([15,10,8,3,7],vals)
        self.assertNotIn('123456.78',json.dumps(self.calls))
    def test_zero_category_differs_from_empty_pool(self):
        self.add([(1,'A',1,11,'m','m',20,4,'handing',None,'n')])
        self.assertEqual(0,self.val('discount_rolls')['metric_value'])
        empty=self.result(self.query(self.req('rolls',metric_filters={'warehouse_department':'NO-MATCH'})))
        self.assertEqual('empty',empty['data_state'])
    def test_unknown_labels_and_missing_rolls_stay_in_evidence(self):
        self.add([(1,'A',1,11,'m','m',20,3,'discountable',1,'n'),(2,'A',2,22,'m','m',20,5,None,1,'n')])
        f=self.val('discount_rolls');self.assertIsNone(f['metric_value']);self.assertEqual(3,f['known_subset_value']);self.assertEqual(1,f['missing_value_count'])
        self.assertEqual(5,self.val('unknown_label_rolls')['metric_value'])
        self.conn.execute('UPDATE vk_ods.slow_moving_goods_ods SET piece_num=NULL')
        f=self.val('rolls');self.assertIsNone(f['metric_value']);self.assertIsNone(f['known_subset_value'])
    def test_current_only_no_prices_or_write_paths(self):
        for r in [self.req('rolls',time_range={'start':'2026-08-01','end':'2026-09-01'}),self.req('rolls',comparison={'kind':'previous_period'}),self.req('rolls',dimensions=['currency']),self.req('ddp_amount')]:
            before=len(self.sql_trace);self.assertEqual('failed',self.query(r)['status']);self.assertEqual(before,len(self.sql_trace))
    def test_catalog_and_missing_identity_count(self):
        r=self.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_pool_rolls'}]})
        self.assertEqual('success',r['status'],r)
        self.assertNotIn('promotion_price',json.dumps(r))
        self.add([(1,'A',None,None,'m','m',20,3,'handing',None,'n'),(2,'A',1,11,'m','m',20,2,'handing',None,'n')])
        f=self.val('products');self.assertIsNone(f['metric_value']);self.assertEqual(1,f['known_subset_value'])

    def test_classification_bundle_has_one_SQL_and_keeps_known_subset(self):
        self.add([(1,'A',1,11,'m','m',20,3,'discountable',1,'n'),(2,'A',2,22,'m','m',20,5,None,1,'n')])
        f=self.val('classification_rolls')
        self.assertEqual(1,len(self.sql_trace))
        self.assertEqual(8,f['metric_value'])
        self.assertIsNone(f['pool_discountable_rolls'])
        self.assertEqual(3,f['pool_known_discountable_rolls'])
        self.assertEqual(8,f['pool_priced_rolls'])
        self.assertIsNone(f['pool_overlap_rolls'])
        self.assertEqual(3,f['pool_known_overlap_rolls'])
        self.assertEqual(5,f['pool_unknown_label_rolls'])
        self.assertEqual(0,f['pool_unpriced_rolls'])

if __name__=='__main__':unittest.main()
