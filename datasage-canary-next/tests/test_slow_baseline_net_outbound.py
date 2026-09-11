"""Public API tests for fixed-baseline product flows; no live database or models."""
import unittest,json
import test_slow_baseline_comparison as baseline
from test_remediation_remaining_cases import metric,facts

class BaselineNetTests(unittest.TestCase):
    insert=baseline.BaselineComparisonTests.insert
    invoke=baseline.BaselineComparisonTests.invoke
    query=baseline.BaselineComparisonTests.query
    result=baseline.BaselineComparisonTests.result
    add=baseline.BaselineComparisonTests.add
    req=baseline.BaselineComparisonTests.req
    execute=baseline.BaselineComparisonTests.execute
    baseline=baseline.BaselineComparisonTests.baseline
    def setUp(self):
        baseline.BaselineComparisonTests.setUp(self)
        for table in ['delivery_bill_barcode_detail_dwd','delivery_return_detail_dwd','sale_bill_goods_detail_dwd','whse_info_dwd']:
            self.conn.execute('DROP TABLE IF EXISTS vk_dwd.'+table)
        self.conn.executescript('''
        CREATE TABLE vk_dwd.delivery_bill_barcode_detail_dwd(goods_id INTEGER,goods_sku_id INTEGER,whse_dept TEXT,unit TEXT,goods_num REAL,piece_num REAL,delivery_time TEXT,whse_id INTEGER,bill_type TEXT COLLATE NOCASE,is_inner_cus TEXT,sale_bill_goods_id INTEGER,sales_id INTEGER);
        CREATE TABLE vk_dwd.delivery_return_detail_dwd(goods_id INTEGER,goods_sku_id INTEGER,whse_dept TEXT,unit TEXT,return_goods_num REAL,return_piece_num REAL,statement_time TEXT,in_whse_id INTEGER,sale_bill_type TEXT COLLATE NOCASE,is_inner_cus TEXT,status INTEGER,complnt_type INTEGER,channel_type INTEGER,sales_id INTEGER);
        CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd(goods_detail_id INTEGER,bill_status INTEGER);
        CREATE TABLE vk_dwd.whse_info_dwd(whse_id INTEGER,dept_name TEXT);
        INSERT INTO vk_dwd.whse_info_dwd VALUES(1,'A'),(2,'B'),(3,'C-HT');
        INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES(1,6);
        ''')
        self.baseline([(1,1,1,11,'m',30,2,'2026-09-08T09:00:00')])
    def outgoing(self,qty=10,rolls=1,dept='A',unit='m',when='2026-09-09T12:00:00',whse=1,bill='bulk',goods=1,sku=11,sale=1,inner='n'):
        self.insert('vk_dwd.delivery_bill_barcode_detail_dwd','goods_id,goods_sku_id,whse_dept,unit,goods_num,piece_num,delivery_time,whse_id,bill_type,is_inner_cus,sale_bill_goods_id,sales_id',[(goods,sku,dept,unit,qty,rolls,when,whse,bill,inner,sale,100)])
    def returning(self,qty=3,rolls=1,dept='A',unit='m',when='2026-09-10T12:00:00',whse=1,bill='bulk',goods=1,sku=11,status=4,inner='n'):
        self.insert('vk_dwd.delivery_return_detail_dwd','goods_id,goods_sku_id,whse_dept,unit,return_goods_num,return_piece_num,statement_time,in_whse_id,sale_bill_type,is_inner_cus,status,complnt_type,channel_type,sales_id',[(goods,sku,dept,unit,qty,rolls,when,whse,bill,inner,status,1,1,200)])
    def run_net(self,**kw):return self.query(metric('registered_slow_pool_baseline_net_outbound','inventory',month=None,**kw))
    def value(self,**kw):return facts(self.result(self.run_net(**kw)))[0]
    def test_duplicate_business_keys_do_not_multiply_and_negative_net_is_retained(self):
        self.baseline([(2,2,1,11,'m',40,3,'2026-09-08T09:00:00')])
        self.outgoing(5);self.returning(12)
        f=self.value();self.assertEqual(-7,f['metric_value']);self.assertEqual(1,f['baseline_scope_groups']);self.assertEqual(1,f['gross_flow_rows'])
        self.assertEqual(5,f['gross_quantity']);self.assertEqual(12,f['return_quantity']);self.assertEqual(0,f['net_rolls'])
    def test_one_sided_and_empty_are_not_proven_zero(self):
        f=self.value();self.assertIsNone(f['metric_value']);self.assertIsNone(f['recorded_net_quantity'])
        self.outgoing(10)
        r=self.result(self.run_net());f=facts(r)[0]
        self.assertIsNone(f['metric_value']);self.assertEqual(10,f['recorded_net_quantity']);self.assertIsNone(f['return_quantity'])
        self.assertEqual('one_sided_recorded_flow',r['rows'][0]['states']['net_flow_state'])
        self.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd');self.returning(4)
        f=self.value();self.assertEqual(-4,f['recorded_net_quantity']);self.assertIsNone(f['metric_value'])
    def test_actual_return_warehouse_and_own_sales_are_not_inherited(self):
        self.outgoing(10);self.returning(4,dept='B',whse=2)
        f=self.value();self.assertEqual(1,f['returns_unmatched_rows']);self.assertEqual(0,f['return_flow_rows'])
        self.returning(3) # different own salesperson is intentionally included, no original-sale join
        f=self.value();self.assertEqual(7,f['metric_value']);self.assertEqual(1,f['return_flow_rows'])
    def test_unknown_unit_identity_time_and_master_mismatch_are_coverage_gaps(self):
        self.outgoing(10);self.returning(3)
        self.outgoing(2,unit=None);self.outgoing(2,goods=None);self.outgoing(2,when=None);self.returning(1,whse=2)
        f=self.value();self.assertIsNone(f['metric_value']);self.assertEqual(7,f['recorded_net_quantity']);self.assertEqual(3,f['outbound_unknown_rows']);self.assertEqual(1,f['returns_unknown_rows']);self.assertEqual(1,f['outbound_missing_time_rows'])
    def test_legacy_ht_is_unrestricted_non_ht_bulk_only(self):
        self.outgoing(2,bill='sq');self.outgoing(3,bill=None);self.outgoing(4,bill='BULK')
        f=self.value();self.assertEqual(1,f['outbound_excluded_rows']);self.assertEqual(1,f['outbound_unknown_rows']);self.assertEqual(4,f['recorded_net_quantity'])
        self.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='C-HT'")
        self.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        for bill in ['sq','future_kind',None]:self.outgoing(2,dept='C-HT',whse=3,bill=bill)
        f=self.value();self.assertEqual(3,f['outbound_matched_rows']);self.assertEqual(0,f['outbound_unknown_rows']);self.assertEqual(6,f['recorded_net_quantity']);self.assertEqual(2,f['outbound_unknown_document_rows'])
    def test_ht_suffix_case_and_trailing_space_are_source_exact(self):
        for dept,expected in [('XHT',0),('X-ht',1),('X-HT ',0)]:
            self.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
            self.conn.execute('UPDATE vk_ai.slow_moving_baseline SET whse_dept=?',(dept,))
            self.conn.execute('UPDATE vk_dwd.whse_info_dwd SET dept_name=? WHERE whse_id=1',(dept,))
            self.outgoing(2,dept=dept,bill='future_kind')
            self.assertEqual(expected,self.value()['outbound_matched_rows'])

    def test_half_open_window_and_validity_filters(self):
        self.outgoing(3,when='2026-09-08T09:00:00');self.outgoing(100,when='2026-09-11T12:00:00');self.outgoing(100,when='2026-09-08T08:59:59')
        self.returning(1);self.returning(100,status=3);self.outgoing(100,inner='y')
        self.assertEqual(2,self.value()['metric_value'])
    def test_sales_join_missing_or_duplicate_never_amplifies(self):
        self.outgoing(10);self.returning(3)
        self.conn.execute('INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES(1,6)')
        f=self.value();self.assertIsNone(f['metric_value']);self.assertEqual(1,f['outbound_unknown_rows']);self.assertEqual(-3,f['recorded_net_quantity'])
        self.conn.execute('DELETE FROM vk_dwd.sale_bill_goods_detail_dwd')
        self.assertEqual(1,self.value()['outbound_unknown_rows'])
    def test_missing_baseline_keys_fail_and_current_new_is_never_queried(self):
        self.outgoing();self.returning();self.value()
        self.assertFalse(any('`vk_ods`.' in q['sql'] for q in self.sql_trace))
        self.conn.execute('UPDATE vk_ai.slow_moving_baseline SET source_unit=NULL')
        r=self.run_net();self.assertEqual('BASELINE_IDENTITY_INCOMPLETE',r['results'][0]['error']['code'])
    def test_missing_quantity_does_not_become_zero_and_units_do_not_mix(self):
        self.outgoing(None);self.returning(3)
        f=self.value();self.assertIsNone(f['metric_value']);self.assertEqual(1,f['gross_missing_quantity_rows'])
        self.baseline([(2,2,2,22,'kg',40,3,'2026-09-08T09:00:00')])
        self.outgoing(7,goods=2,sku=22,unit='kg');self.returning(2,goods=2,sku=22,unit='kg')
        r=self.result(self.run_net());self.assertEqual(2,len(r['rows']))
        self.assertEqual('failed',self.run_net(dimensions=['product'])['status'])
    def test_catalog_parameters_empty_scope_and_truncation(self):
        r=self.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_pool_baseline_net_outbound'}]})
        self.assertEqual('success',r['status']);self.assertNotIn('vk_ai.',json.dumps(r))
        for kw in [{'movement_state':'New'},{'time_range':{'start':'2026-08-01','end':'2026-09-01'}},{'baseline_week':'2027-W01'},{'dimensions':['salesperson','unit']}]:self.assertEqual('failed',self.run_net(**kw)['status'])
        r=self.result(self.run_net(metric_filters={'warehouse_department':'NO-MATCH'}));self.assertEqual('empty',r['data_state']);self.assertIn('frozen_at',r['applied_time_range'])
        self.baseline([(2,2,2,22,'kg',40,3,'2026-09-08T09:00:00')]);self.outgoing();self.returning()
        r=self.result(self.run_net(limit=1));self.assertTrue(r['truncated']);self.assertEqual(1,facts(r)[0]['outbound_matched_rows'])

if __name__=='__main__':unittest.main()
