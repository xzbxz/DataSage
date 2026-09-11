"""Public frozen/current comparison tests, using independent small SQLite records."""
import unittest,json
from datetime import datetime,date
from unittest.mock import patch
import test_registered_slow_pool as pool
from test_remediation_remaining_cases import plugin,metric,facts

class BaselineComparisonTests(unittest.TestCase):
    insert=pool.SlowPoolTests.insert
    invoke=pool.SlowPoolTests.invoke
    query=pool.SlowPoolTests.query
    result=pool.SlowPoolTests.result
    add=pool.SlowPoolTests.add
    req=pool.SlowPoolTests.req
    def setUp(self):
        pool.SlowPoolTests.setUp(self)
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_ai")
        self.conn.execute('''CREATE TABLE vk_ai.slow_moving_baseline(id INTEGER,week_label TEXT,source_row_id INTEGER,
            frozen_at TEXT,baseline_version INTEGER,source_table TEXT,goods_id INTEGER,goods_sku_id INTEGER,
            goods_name TEXT,whse_dept TEXT,source_unit TEXT,total_qty REAL,total_piece REAL,slow_label TEXT)''')
        self.conn.create_collation('utf8mb4_bin',lambda a,b:(a>b)-(a<b))
        self.conn.create_function('NOW',-1,lambda *a:'2026-09-11T12:00:00')
        self.conn.create_function('UTC_TIMESTAMP',-1,lambda *a:'2026-09-11T04:00:00')
        self.conn.create_function('SECOND_DIFF',2,lambda a,b:int((datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()))
        import re
        self.conn.create_function('REGEXP',2,lambda pattern,v:False if v is None else re.search(pattern,v) is not None)
        p=patch.object(plugin.tools,'_business_today',lambda:date(2026,9,11));p.start();self.addCleanup(p.stop)
    def execute(self,sql,params,limit,**kwargs):
        sql=sql.replace('TIMESTAMPDIFF(SECOND,','SECOND_DIFF(')
        return pool.SlowPoolTests.execute(self,sql,params,limit,**kwargs)
    def baseline(self,rows):
        self.insert('vk_ai.slow_moving_baseline','id,week_label,source_row_id,frozen_at,baseline_version,source_table,goods_id,goods_sku_id,goods_name,whse_dept,source_unit,total_qty,total_piece,slow_label',[(i,'2026-W37',sid,t,2,'vk_ods.slow_moving_goods_ods',g,sku,'Product','A',u,q,r,'handing') for i,sid,g,sku,u,q,r,t in rows])
    def current(self,rows):
        self.add([(sid,'A',g,sku,u,'m',q,r,'handing',None,w) for sid,g,sku,u,q,r,w in rows])
    def compare(self,view='groups',**kwargs):return self.query(metric('registered_slow_pool_baseline_'+view,'inventory',month=None,**kwargs))
    def test_union_states_presence_precision_and_renumbering(self):
        t='2026-09-08T09:00:00'
        self.baseline([(1,1,1,1,'m',20,2,t),(2,2,2,2,'m',30,3,t),(3,3,3,3,'m',40,4,t),(4,4,4,4,'m',0,0,t),(5,5,5,5,'m',10.0001,1,t),(6,6,6,6,'m',12,1,t),(7,7,6,6,'m',13,1,t)])
        self.current([(101,1,1,'m',20,2,'n'),(2,2,2,'m',25,3,'n'),(4,4,4,'m',11,1,'n'),(5,5,5,'m',10.0002,1,'n'),(6,6,6,'m',25,2,'n'),(8,8,8,'m',20,2,'n')])
        r=self.result(self.compare());states=[x['states']['pool_movement_state'] for x in r['rows']]
        self.assertEqual(2,states.count('No Change'));self.assertEqual(2,states.count('Increased'))
        self.assertEqual(1,states.count('Reduced'));self.assertEqual(1,states.count('Exited'));self.assertEqual(1,states.count('New'))
        exited=next(x for x in r['rows'] if x['states']['pool_movement_state']=='Exited')
        self.assertIsNone(exited['facts']['closing_quantity']);self.assertIsNone(exited['facts']['comparable_quantity_delta'])
        summary=self.result(self.compare('summary'));f=facts(summary)[0]
        self.assertEqual(7,f['metric_value']);self.assertEqual(7,f['opening_source_rows']);self.assertEqual(6,f['closing_source_rows'])
        self.assertEqual(6,f['opening_product_id_count'])
    def test_unknown_membership_not_exited_and_unknown_units_retained(self):
        t='2026-09-08T09:00:00'
        self.baseline([(1,1,1,1,'m',20,2,t),(2,2,2,2,None,30,3,t)])
        self.current([(1,1,1,'m',None,2,'n'),(2,2,2,'m',30,3,'n')])
        r=self.result(self.compare());self.assertTrue(all(x['states']['pool_movement_state']=='Unassessable' for x in r['rows']))
        self.assertTrue(any(x['facts']['closing_uncertain_membership_rows'] for x in r['rows']))
        self.assertTrue(any(x['facts']['opening_known_rolls']==3 for x in r['rows']))
    def test_zero_opening_is_not_new_and_missing_quantity_is_not_zero(self):
        t='2026-09-08T09:00:00'
        self.baseline([(1,1,1,1,'m',None,2,t)])
        self.current([(1,1,1,'m',20,None,'n')])
        r=self.result(self.compare());x=r['rows'][0]
        self.assertEqual('Unassessable',x['states']['pool_movement_state'])
        self.assertIsNone(x['facts']['opening_quantity']);self.assertIsNone(x['facts']['closing_rolls'])
    def test_empty_and_multiple_freeze_times_fail_without_freezing(self):
        self.current([(1,1,1,'m',20,2,'n')])
        r=self.compare();self.assertEqual('BASELINE_NOT_FOUND',r['results'][0]['error']['code'])
        self.baseline([(1,1,1,1,'m',20,2,'2026-09-08T09:00:00'),(2,2,2,2,'m',20,2,'2026-09-08T10:00:00')])
        r=self.compare();self.assertEqual('BASELINE_TIME_AMBIGUOUS',r['results'][0]['error']['code'])
        self.assertTrue(all(q['sql'].startswith('WITH') for q in self.sql_trace))
    def test_duplicate_source_rows_fail(self):
        t='2026-09-08T09:00:00';self.baseline([(1,1,1,1,'m',20,2,t),(2,1,1,1,'m',20,2,t)])
        r=self.compare();self.assertEqual('BASELINE_INCOMPLETE',r['results'][0]['error']['code'])
    def test_week_selection_time_metadata_state_filter_and_truncation(self):
        t='2026-09-08T09:00:00';self.baseline([(1,1,1,1,'m',20,2,t),(2,2,2,2,'m',20,2,t)])
        self.current([(1,1,1,'m',15,2,'n'),(3,3,3,'m',20,2,'n')])
        r=self.result(self.compare(movement_state='Reduced',limit=1))
        self.assertEqual('2026-W37',r['applied_time_range']['baseline_week'])
        self.assertEqual(t,r['applied_time_range']['frozen_at'])
        self.assertEqual(3,facts(r)[0]['population_union_groups'])
        self.assertEqual(1,facts(r)[0]['population_exited_group_count'])
        self.assertEqual('Reduced',r['rows'][0]['states']['pool_movement_state'])
        self.assertEqual('failed',self.compare(baseline_week='2026-W53')['status'])
        self.assertEqual('failed',self.compare(time_range={'start':'2026-08-01','end':'2026-09-01'})['status'])
        plain=self.query(self.req('rolls',baseline_week='2026-W37'));self.assertEqual('failed',plain['status'])

    def test_unknown_whitelist_and_classification_are_distinct(self):
        t='2026-09-08T09:00:00'
        self.baseline([(1,1,1,1,'m',20,2,t),(2,2,2,2,'m',20,2,t)])
        self.current([(1,1,1,'m',20,2,None),(2,2,2,'m',20,2,'n')])
        self.conn.execute("UPDATE vk_ods.slow_moving_goods_ods SET slow_label=NULL WHERE id=2")
        r=self.result(self.compare())
        unknown=next(x for x in r['rows'] if x['states']['pool_movement_state']=='Unassessable')
        self.assertEqual(1,unknown['facts']['closing_unknown_whitelist_rows'])
        self.assertEqual(0,unknown['facts']['closing_missing_quantity_rows'])
        unchanged=next(x for x in r['rows'] if x['states']['pool_movement_state']=='No Change')
        self.assertEqual(1,unchanged['facts']['closing_unknown_class_rows'])

    def test_filtered_empty_has_real_time_and_state_counts_survive_limit(self):
        t='2026-09-08T09:00:00'
        self.baseline([(1,1,1,1,'m',20,2,t),(2,2,2,2,'kg',20,2,t)])
        self.current([(1,1,1,'m',21,2,'n'),(2,2,2,'kg',19,2,'n'),(3,3,3,'m',25,2,'n')])
        r=self.result(self.compare(limit=1))
        self.assertTrue(r['truncated']);self.assertEqual(3,facts(r)[0]['population_union_groups'])
        r=self.result(self.compare(metric_filters={'warehouse_department':'NO-MATCH'}))
        self.assertEqual('empty',r['data_state'])
        self.assertEqual(t,r['applied_time_range']['frozen_at'])
        r=self.result(self.compare('summary'))
        self.assertEqual(2,len(r['rows']))

    def test_known_exclusion_is_not_unknown_membership(self):
        t='2026-09-08T09:00:00';self.baseline([(1,1,1,1,'m',20,2,t)])
        self.current([(1,1,1,'m',None,2,'y')])
        r=self.result(self.compare())
        self.assertEqual('Exited',r['rows'][0]['states']['pool_movement_state'])
        self.assertIsNone(facts(r)[0]['closing_quantity'])

    def test_catalog_exposes_existing_baseline_views_without_private_tables(self):
        for view in ('groups','summary'):
            r=self.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'registered_slow_pool_baseline_'+view}]})
            self.assertEqual('success',r['status'],r)
            self.assertNotIn('vk_ai.',json.dumps(r))
            self.assertNotIn('source_row_id',json.dumps(r))
        self.assertFalse(self.sql_trace)

if __name__=='__main__':unittest.main()
