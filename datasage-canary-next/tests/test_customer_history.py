"""Registered history queries against isolated SQLite fixtures; no live customers or models."""
import unittest,json
from datetime import datetime,date
from calendar import monthrange
from unittest.mock import patch
import test_slow_baseline_net_outbound as weekly
from test_remediation_remaining_cases import metric,facts,plugin

class CustomerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h=weekly.BaselineNetTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.clock='2026-09-11T12:00:00'
        self.h.conn.create_function('NOW',-1,lambda *a:self.clock)
        self.h.conn.create_function('UTC_TIMESTAMP',-1,lambda *a:self.clock.replace('T12:','T04:'))
        def lower(value):
            t=datetime.fromisoformat(value)
            return t.replace(year=t.year-1,day=min(t.day,monthrange(t.year-1,t.month)[1])).isoformat()
        self.h.conn.create_function('HISTORY_START',1,lower)
        # SQLite preserves value semantics here, not MySQL charset validation.
        self.h.conn.create_function('CONVERT',1,lambda value:value)
        original=self.h.execute
        self.h.execute=lambda sql,*a,**kw:original(sql.replace('DATE_SUB(closing_read_at,INTERVAL 12 MONTH)','HISTORY_START(closing_read_at)').replace(' USING utf8mb4',''),*a,**kw)
        self.h.conn.executescript('''
        ALTER TABLE vk_dwd.delivery_bill_barcode_detail_dwd ADD COLUMN barcode_detail_id INTEGER;
        ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN barcode_detail_id INTEGER;
        ALTER TABLE vk_dwd.delivery_return_detail_dwd ADD COLUMN customer_id TEXT;
        ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN sale_bill_id INTEGER;
        ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN customer_id TEXT;
        ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN customer_name TEXT;
        ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd ADD COLUMN goods_id INTEGER;
        DELETE FROM vk_dwd.sale_bill_goods_detail_dwd;
        DROP TABLE IF EXISTS vk_dwd.customer_dwd;
        CREATE TABLE vk_dwd.customer_dwd(customer_id TEXT,customer_no TEXT,customer_name TEXT,sales_name TEXT,is_delete TEXT,is_void TEXT);
        INSERT INTO vk_dwd.customer_dwd VALUES(1,'C1','Same Name','Current Owner','n','n'),(2,'C2','Same Name','Other Owner','n','n');
        DROP TABLE IF EXISTS vk_dwd.goods_detail_dwd;
        CREATE TABLE vk_dwd.goods_detail_dwd(goods_id INTEGER,goods_name TEXT,goods_no TEXT,is_delete TEXT,is_void TEXT);
        INSERT INTO vk_dwd.goods_detail_dwd VALUES(1,'Product One','P1','n','n');
        ALTER TABLE vk_dwd.goods_detail_dwd ADD COLUMN alias TEXT;
        ''')
        self.seq=0
    def out(self,buyer=1,order=None,**kwargs):
        self.seq+=1
        self.h.conn.execute('INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES(?,?,?,?,?,?)',(self.seq,6,order or self.seq,buyer,'Historical Name',kwargs.get('goods',1)))
        self.h.outgoing(sale=self.seq,**kwargs)
        self.h.conn.execute('UPDATE vk_dwd.delivery_bill_barcode_detail_dwd SET barcode_detail_id=rowid WHERE barcode_detail_id IS NULL')
    def ret(self,buyer=1,**kwargs):
        self.h.returning(**kwargs)
        self.h.conn.execute('UPDATE vk_dwd.delivery_return_detail_dwd SET barcode_detail_id=rowid,customer_id=? WHERE barcode_detail_id IS NULL',(buyer,))
    def query(self,week='2026-W37',**kwargs):
        return self.h.query(metric('registered_slow_historical_customers','inventory',month=None,baseline_week=week,**kwargs))
    def result(self,**kwargs):
        raw=self.query(**kwargs)
        self.assertEqual('success',raw['results'][0]['status'],raw)
        return raw['results'][0]
    def test_same_product_other_sku_full_return_and_return_only(self):
        self.out(sku=99,qty=10,rolls=1);self.ret(sku=88,qty=10,rolls=1)
        self.ret(buyer=2,qty=20)
        r=self.result();f=facts(r)[0]
        self.assertEqual(1,f['history_scope_customers']);self.assertEqual(1,f['history_scope_relations'])
        self.assertEqual(10,f['history_outbound_quantity']);self.assertEqual(10,f['history_return_quantity'])
        self.assertEqual('2025-09-11T12:00:00',r['applied_time_range']['history_start'])
        self.assertEqual('UTC+08:00',r['applied_time_range']['history_business_timezone_at_read'])
    def test_fixed_window_boundaries_and_no_custom_period(self):
        self.out(when='2025-09-11T11:59:59');self.out(when='2025-09-11T12:00:00')
        self.out(when='2026-09-11T12:00:00');self.out(when='2026-09-12T12:00:00')
        f=facts(self.result())[0];self.assertEqual(1,f['history_outbound_rows'])
        for kwargs in ({'time_range':{'start':'2026-01-01','end':'2026-02-01'}},{'calendar_month':'2026-09'}):
            before=len(self.h.sql_trace);r=self.query(**kwargs)
            self.assertEqual('failed',r['results'][0]['status']);self.assertEqual(before,len(self.h.sql_trace))
    def test_cross_department_invalid_and_inner_excluded(self):
        self.out(dept='B',whse=2,buyer=2);self.out(inner='y',buyer=2);self.out(bill='sq',buyer=2)
        self.out();self.h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET bill_status=1 WHERE goods_detail_id=1')
        self.assertEqual(1,facts(self.result())[0]['history_scope_customers'])
    def test_same_name_missing_master_and_duplicate_master_no_amplification(self):
        self.out(buyer=1);self.out(buyer=2)
        r=self.result();fs=facts(r);self.assertEqual(2,len(fs))
        self.assertEqual(2,len({f['history_customer_ref'] for f in fs}))
        self.h.conn.execute('DELETE FROM vk_dwd.customer_dwd WHERE customer_id=1')
        self.h.conn.execute("INSERT INTO vk_dwd.customer_dwd VALUES(2,'C2','Same Name','Duplicate','n','n')")
        fs=facts(self.result());self.assertEqual(2,len(fs));self.assertTrue(all(f['history_customer_details_missing']==1 for f in fs))
    def test_units_relation_order_dedup_and_truncation_totals(self):
        self.out(order=100,unit='m');self.out(order=100,unit='kg');self.out(buyer=2,order=200)
        f=facts(self.result(limit=1))[0]
        self.assertEqual(2,f['history_scope_relations']);self.assertEqual(2,f['history_scope_customers']);self.assertEqual(3,f['history_display_groups'])
        self.assertEqual(1,f['history_order_count'])
    def test_duplicate_source_and_parent_do_not_amplify(self):
        self.out();self.h.conn.execute('INSERT INTO vk_dwd.delivery_bill_barcode_detail_dwd SELECT * FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        self.assertEqual('HISTORY_SOURCE_DUPLICATE',self.query()['results'][0]['error']['code'])
    def test_missing_pool_no_freeze_and_first_last_dates(self):
        self.out(when='2025-10-01T12:00:00');self.out(when='2026-08-01T12:00:00')
        f=facts(self.result())[0]
        self.assertEqual('2025-10-01T12:00:00',f['history_first_outbound_at']);self.assertEqual('2026-08-01T12:00:00',f['history_last_outbound_at'])
        r=self.query(week='2026-W36');self.assertEqual('BASELINE_NOT_FOUND',r['results'][0]['error']['code'])
        self.assertEqual(1,self.h.conn.execute('SELECT COUNT(*) FROM vk_ai.slow_moving_baseline').fetchone()[0])
    def test_leap_day_uses_calendar_year_not_365_days(self):
        self.clock='2024-02-29T12:00:00'
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET week_label='2024-W08',frozen_at='2024-02-20T09:00:00'")
        with patch.object(plugin.tools,'_business_today',lambda:date(2024,2,29)):
            self.out(when='2023-02-28T12:00:00')
            r=self.result(week='2024-W08');self.assertEqual('2023-02-28T12:00:00',r['applied_time_range']['history_start'])
    def test_unknown_time_keeps_known_subset_not_false_complete_dates(self):
        self.out();self.out(when=None)
        f=facts(self.result())[0]
        self.assertIsNone(f['history_scope_customers']);self.assertEqual(1,f['history_known_customers'])
        self.assertIsNone(f['history_first_outbound_at']);self.assertIsNotNone(f['history_known_first_outbound_at'])
    def test_unknown_only_is_not_empty_purchase_list(self):
        self.out(when=None)
        self.assertEqual('HISTORY_SCOPE_UNASSESSABLE',self.query()['results'][0]['error']['code'])
    def test_duplicate_parent_does_not_choose_one_customer(self):
        self.out()
        self.h.conn.execute('INSERT INTO vk_dwd.sale_bill_goods_detail_dwd SELECT * FROM vk_dwd.sale_bill_goods_detail_dwd')
        self.assertEqual('HISTORY_SCOPE_UNASSESSABLE',self.query()['results'][0]['error']['code'])
    def test_missing_quantities_keep_relation_and_independent_rolls(self):
        self.out(qty=None,rolls=2);self.ret(qty=3,rolls=None)
        f=facts(self.result())[0]
        self.assertEqual(1,f['history_scope_relations']);self.assertIsNone(f['history_outbound_quantity'])
        self.assertEqual(2,f['history_outbound_rolls']);self.assertEqual(3,f['history_return_quantity']);self.assertIsNone(f['history_return_rolls'])
    def test_missing_unit_keeps_purchase_identity_but_not_quantity(self):
        self.out(unit=None)
        f=facts(self.result())[0]
        self.assertEqual(1,f['history_scope_customers']);self.assertIsNone(f['history_outbound_quantity'])
    def test_zero_quantity_is_not_deleted_by_net_positive_rule(self):
        self.out(qty=0,rolls=0)
        self.assertEqual(1,facts(self.result())[0]['history_scope_relations'])
    def test_ht_rule_and_invalid_completed_state(self):
        self.h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET whse_dept='C-HT'")
        self.out(dept='C-HT',whse=3,bill='sq');self.out(dept='C-HT',whse=3,bill=None,buyer=2)
        self.assertEqual(2,facts(self.result())[0]['history_scope_customers'])
        self.h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET bill_status=1 WHERE customer_id=2')
        self.assertEqual(1,facts(self.result())[0]['history_scope_customers'])
    def test_empty_existing_pool_history_is_zero_and_no_default_pool(self):
        r=self.result();self.assertEqual([],r['rows'])
        self.assertEqual(0,r['applied_time_range']['history_scope_relations'])
        self.assertEqual('failed',self.query(week=None)['results'][0]['status'])
    def test_blank_master_fields_are_missing_not_false_owner(self):
        self.out();self.h.conn.execute("UPDATE vk_dwd.customer_dwd SET customer_name='',sales_name=' ' WHERE customer_id=1")
        f=facts(self.result())[0]
        self.assertEqual(1,f['history_customer_details_missing']);self.assertIsNone(f['history_current_owner'])
        self.assertEqual('C1',f['history_customer_no'])
    def test_unit_and_department_filters_keep_separate_record_scope(self):
        self.out(unit='m');self.out(unit='kg',buyer=2)
        r=self.result(metric_filters={'unit':'m','warehouse_department':'A'})
        self.assertEqual(1,len(r['rows']));self.assertEqual(1,facts(r)[0]['history_scope_customers'])
    def test_catalog_and_failures_expose_registered_bounds(self):
        r=self.h.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_historical_customers'}]})
        self.assertEqual(['customer','product','warehouse_department','unit'],r['results'][0]['metric']['grouping']['required'])
        self.assertEqual([],r['results'][0]['metric']['allowed_time_buckets'])
        self.assertEqual([],r['results'][0]['metric']['ordering']['fields'])
        before=len(self.h.sql_trace)
        denied=self.h.invoke('datasage_query',{'requests':[metric('registered_slow_historical_customers','inventory',month=None,baseline_week='2026-W37')]},bound=False)
        self.assertEqual('failed',denied['status'])
        self.assertEqual('DATA_ENTITLEMENT_DENIED',denied['error']['code'])
        self.assertEqual(before,len(self.h.sql_trace))
    def test_existing_customer_and_product_entity_binding_is_reused(self):
        self.out(buyer=1);self.out(buyer=2)
        r=self.result(metric_filters={'customer':'C1','product':'P1'})
        self.assertEqual(1,len(r['rows']));self.assertEqual('C1',facts(r)[0]['history_customer_no'])
    def test_multiple_pool_skus_never_multiply_history(self):
        self.h.baseline([(2,2,1,22,'kg',40,3,'2026-09-08T09:00:00')])
        self.out(qty=10,rolls=1,sku=99)
        f=facts(self.result())[0]
        self.assertEqual(1,f['history_outbound_rows']);self.assertEqual(10,f['history_outbound_quantity'])
        self.assertEqual(1,f['history_scope_relations'])
    def test_fractional_measures_and_unsupported_sort(self):
        self.out(qty=1.125,rolls=.25);self.ret(qty=1.125,rolls=.25)
        f=facts(self.result())[0]
        self.assertEqual(.25,f['history_outbound_rolls']);self.assertEqual(1.125,f['history_return_quantity'])
        before=len(self.h.sql_trace)
        r=self.query(order_by={'field':'metric_value','direction':'desc'})
        self.assertEqual('failed',r['results'][0]['status']);self.assertEqual(before,len(self.h.sql_trace))
        self.assertEqual('order_by',r['results'][0]['error']['path'])
        self.assertEqual('HISTORY_PARAMETER_UNSUPPORTED',r['results'][0]['error']['code'])
    def test_missing_order_key_is_unknown_count_not_zero_purchase(self):
        self.out();self.h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET sale_bill_id=NULL')
        f=facts(self.result())[0]
        self.assertEqual(1,f['history_scope_relations']);self.assertIsNone(f['history_order_count'])
        self.assertEqual(1,f['history_missing_order_rows'])
    def test_parent_product_mismatch_cannot_assign_a_customer(self):
        self.out();self.h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET goods_id=999')
        self.assertEqual('HISTORY_SCOPE_UNASSESSABLE',self.query()['results'][0]['error']['code'])

    def test_missing_and_unicode_customer_names_preserve_returns_and_nulls(self):
        self.out();self.ret(qty=1)
        self.h.conn.execute('UPDATE vk_dwd.customer_dwd SET customer_name=NULL')
        self.h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET customer_name=NULL')
        result=self.result()
        self.assertEqual(1,facts(result)[0]['history_customer_details_missing'])
        self.assertEqual(1,facts(result)[0]['history_return_quantity'])
        customer_dimension=next(d for d in result['rows'][0]['dimensions'] if d['label']=='客户')
        # A missing display name is represented explicitly.  The stable
        # source identity may still be known, so ``未知`` is never treated as
        # the customer's real name or as a resolver filter token.
        self.assertEqual('未知',customer_dimension.get('value'))
        self.assertTrue(customer_dimension.get('display_only'))
        self.assertTrue(customer_dimension.get('display_name_missing'))
        self.assertIn(customer_dimension.get('identity_state'),{'identified','identity_missing'})
        self.assertNotEqual('客户甲',customer_dimension.get('value'))
        self.h.conn.execute("UPDATE vk_dwd.customer_dwd SET customer_name=' 客户甲😀 ' WHERE customer_id=1")
        result=self.result()
        self.assertTrue(any(d['label']=='客户' and d['value']=='客户甲😀' for d in result['rows'][0]['dimensions']))
        self.assertEqual(1,facts(result)[0]['history_return_quantity'])
