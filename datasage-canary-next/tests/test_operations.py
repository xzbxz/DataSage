"""Synthetic migration checks; never contacts a database, model or recipient."""
from copy import deepcopy
from datetime import date,datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib,json,sqlite3,unittest
import test_business_contracts as base

ops=importlib.import_module(base.TEST_PACKAGE+'.operations')

class PriceOperationsTests(unittest.TestCase):
    def row(self,**change):
        return {'goods_id':1,'goods_no':'SYNTHETIC','goods_name':'Synthetic','dept':'IDK','customer_grade':'A','color_label':'black','ddp_price':'10.00','currency_no':'IDR','unit':'m','unit_cuur':'m','is_inclue_tax':'n','effective_date':'2026-01-01','expiration_date':'2026-12-31','detail_id':1,'supplier_no':'SYNTHETIC-S','tax_inclue_price':'11','tax_exclue_price':'10',**change}
    def classify(self,side='sales',**change):return ops.classify_prices(side,[self.row(**change)],date(2026,9,15))
    def test_exact_decimal_and_new_changed_unchanged(self):
        a=self.classify();b=self.classify(ddp_price='10.0000')
        self.assertEqual('new_baseline_candidate',ops.compare(None,a)[0]['event'])
        self.assertEqual('unchanged',ops.compare(a,b)[0]['event'])
        self.assertEqual('price_changed',ops.compare(a,self.classify(ddp_price='10.0000001'))[0]['event'])
    def test_unknown_basis_expiry_duplicates_and_absence_are_not_price_changes(self):
        a=self.classify()
        for values,state in [({'ddp_price':None},'missing_price'),({'ddp_price':'-1'},'negative_price'),({'unit':None},'unknown_basis'),({'expiration_date':'2026-01-02'},'expired'),({'effective_date':'2027-01-01'},'invalid_validity'),({'effective_date':None},'unknown_validity')]:
            with self.subTest(values=values):
                b=self.classify(**values);self.assertEqual(state,b[0]['state']);self.assertEqual('unresolved',ops.compare(a,b)[0]['event'])
        self.assertEqual('basis_changed',ops.compare(a,self.classify(currency_no='USD'))[0]['event'])
        self.assertEqual('validity_changed',ops.compare(a,self.classify(expiration_date='2026-11-30'))[0]['event'])
        self.assertEqual('absent_from_selection',ops.compare(a,[])[0]['event'])
        duplicate=ops.classify_prices('sales',[self.row(),self.row(ddp_price='20')],date(2026,9,15))[0]
        self.assertEqual('ambiguous_latest',duplicate['state']);self.assertEqual({},duplicate['prices'])
    def test_purchase_separate_tax_identity_and_units(self):
        a=self.classify('purchase')
        self.assertEqual('price_changed',ops.compare(a,self.classify('purchase',tax_inclue_price='12'))[0]['event'])
        self.assertEqual('basis_changed',ops.compare(a,self.classify('purchase',unit_cuur='kg'))[0]['event'])
        events=ops.compare(a,self.classify('purchase',supplier_no='OTHER'))
        self.assertEqual({'absent_from_selection','new_baseline_candidate'},{r['event'] for r in events})
    def test_validity_keeps_source_timestamp_precision(self):
        row=self.row(expiration_date='2026-09-15 10:00:00')
        self.assertEqual('expired',ops.classify_prices('sales',[row],datetime(2026,9,15,12))[0]['state'])
        self.assertEqual('validity_boundary',ops.classify_prices('sales',[row],datetime(2026,9,15,10))[0]['state'])
    def test_idk_null_zero_negative_and_no_identity_expansion(self):
        rows=[self.row(id=i,promotion_price=p,source_unit='m') for i,p in enumerate([None,'0','-1'])]
        result=ops.classify_idk(rows)
        self.assertEqual(['null','zero','negative'],[r['price_state'] for r in result])
        self.assertNotIn('ddp_price',result[0]);self.assertNotIn('supplier_no',result[0])
    def test_snapshot_accept_is_explicit_idempotent_and_conflict_checked(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            doc={'status':'success','kind':'sales_prices','scope_hash':'scope','baseline_id':None,'records':self.classify()}
            path=ops.save_observation(root,'sales',doc)
            self.assertIsNone(ops.load_baseline(root,'sales','scope'))
            self.assertEqual(path,ops.save_observation(root,'sales',doc))
            ops.accept_snapshot(root,'sales',path.stem);ops.accept_snapshot(root,'sales',path.stem)
            self.assertEqual(path.stem,ops.load_baseline(root,'sales','scope')['observation_id'])
            stale={**doc,'records':self.classify(ddp_price='20')}
            new=ops.save_observation(root,'sales',stale)
            with self.assertRaisesRegex(ops.OperationError,'BASELINE_ADVANCED'):ops.accept_snapshot(root,'sales',new.stem)
            with self.assertRaisesRegex(ops.OperationError,'SCOPE_CHANGED'):ops.load_baseline(root,'sales','other')
    def test_failed_or_partial_never_becomes_baseline_and_html_escapes(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);doc={'status':'partial','kind':'sales_prices','scope_hash':'s','records':[]}
            path=ops.save_observation(root,'r',doc)
            with self.assertRaisesRegex(ops.OperationError,'NOT_ACCEPTABLE'):ops.accept_snapshot(root,'r',path.stem)
            self.assertNotIn('<script>',ops.render({'x':'<script>alert(1)</script>'}))
    def test_source_limit_stops_before_any_state_or_artifact(self):
        binding={'kind':'sales_prices','regions':['IDK'],'limit':1}
        with TemporaryDirectory() as tmp,patch.object(base.tools,'_execute_with_source',return_value=([self.row()],True,{})),patch.object(importlib.import_module(base.TEST_PACKAGE+'.local_report'),'_assert_local_context'):
            with self.assertRaisesRegex(ops.OperationError,'TRUNCATED'):ops.execute(Path(tmp),'r',binding)
            self.assertFalse((Path(tmp)/'report_runs').exists())
    def test_binding_rejects_sql_recipients_regions_and_unapproved_customer_scope(self):
        for binding in ({'kind':'sales_prices','regions':['IDK'],'limit':10,'sql':'SELECT 1'},{'kind':'sales_prices','regions':['NOPE'],'limit':10},{'kind':'idk_unpriced','limit':10,'recipient':'someone'},{'kind':'slow_assignment','limit':10}):
            with self.assertRaises((ops.OperationError,ValueError)):ops.validate_binding(binding)

    def test_governed_fabric_plan_uses_existing_queries_and_checks_results(self):
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        wire=importlib.import_module(base.TEST_PACKAGE+'.wire')
        calls=[]
        def handler(payload):
            request=payload['requests'][0];calls.append(request)
            datasets,semantics=base.contracts.execution_contracts(request['domain'])
            base.tools._build_metric_query(request,datasets,semantics,10,observed_on=date(2026,9,15))
            return json.dumps({'status':'success','results':[{'status':'success','request_id':request['request_id'],'rows':[],'truncated':False}]})
        binding={'kind':'fabric_review','limit':10,'time_range':{'start':'2026-08-01','end':'2026-09-01'},'inventory_scope':'total'}
        with patch.object(wire,'bounded_json_handler',return_value=handler):
            result=ops.execute_governed(Path('.'),'fabric',binding,'scope')
        self.assertEqual('success',result['status']);self.assertEqual(6,len(calls))
        self.assertEqual({'delivery','inventory'},{r['domain'] for r in calls})
        with patch.object(wire,'bounded_json_handler',return_value=lambda p:json.dumps({'status':'success','results':[{'status':'success','request_id':'wrong'}]})):
            self.assertEqual('partial',ops.execute_governed(Path('.'),'fabric',binding,'scope')['status'])

    def test_new_local_cli_only_generates_artifact_and_keeps_snapshot_unaccepted(self):
        import hermes_constants
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        with TemporaryDirectory() as tmp:
            root=Path(tmp);binding={'kind':'idk_unpriced','limit':10}
            (root/'local-report-bindings.json').write_text(json.dumps({'version':1,'default_report':'idk','reports':{'idk':binding}}),encoding='utf-8')
            raw={'id':1,'goods_no':'SYNTHETIC','goods_name':'Synthetic','goods_sku_id':11,'attr_val':'red','source_unit':'m','goods_num':20,'piece_num':2,'promotion_price':None,'observed_at':'2026-09-15 12:00:00'}
            with patch.object(hermes_constants,'get_hermes_home',return_value=root),patch.object(local,'_assert_local_context'),patch.object(local,'configure_runtime'),patch.object(base.tools,'_execute_with_source',return_value=([raw],False,{})):
                self.assertEqual(0,local.main(root,[]))
            artifacts=list((root/'report_runs'/'operations'/'idk').glob('*.json'))
            self.assertEqual(1,len(artifacts));self.assertFalse((artifacts[0].parent/'accepted.json').exists())
            self.assertEqual('not_requested',json.loads(artifacts[0].read_text(encoding='utf-8'))['delivery_state'])
            self.assertTrue(artifacts[0].with_suffix('.html').is_file())
            (root/'local-report-bindings.json').write_text(json.dumps({'version':2,'default_report':'idk','reports':{'idk':binding}}),encoding='utf-8')
            with patch.object(hermes_constants,'get_hermes_home',return_value=root),patch.object(local,'_assert_local_context'),patch.object(local,'configure_runtime') as configure:
                self.assertEqual(2,local.main(root,[]));configure.assert_not_called()

class SourceSQLTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        for schema in ('vk_dwd','vk_ods','data_assistant'):self.db.execute("ATTACH DATABASE ':memory:' AS "+schema)
        self.db.create_function('NOW',1,lambda n:'2026-09-15 12:00:00')
        self.db.executescript('''
        CREATE TABLE vk_dwd.ready_goods_dwd(goods_id INTEGER,goods_no TEXT,dept TEXT,type TEXT);
        CREATE TABLE data_assistant.push_goods_report(goods_no TEXT,promotion_area TEXT,promotion_start_time TEXT,promotion_end_time TEXT,default_end_time TEXT);
        CREATE TABLE vk_dwd.sale_price_bill_detail_dwd(goods_id INTEGER,goods_no TEXT,goods_name TEXT,structure_name TEXT,is_void TEXT,customer_grade TEXT,color_label TEXT,ddp_price TEXT,currency_no TEXT,unit TEXT,unit_cuur TEXT,is_inclue_tax TEXT,effective_date TEXT,expiration_date TEXT,gmt_modified TEXT,detail_id INTEGER);
        CREATE TABLE vk_dwd.purchase_price_bill_detail_dwd(goods_no TEXT,goods_name TEXT,supplier_no TEXT,supplier_name TEXT,color_label TEXT,tax_inclue_price TEXT,tax_exclue_price TEXT,currency_no TEXT,unit_cuur TEXT,effective_date TEXT,expiration_date TEXT,gmt_modified TEXT,detail_id INTEGER,parent_org_ids TEXT);
        INSERT INTO vk_dwd.ready_goods_dwd VALUES(1,'SYN','IDK','备货'),(1,'SYN','IDK','备货'),(2,'CANCEL','IDK','取消备货');
        ''')
    def query(self,kind):
        sql,params=ops.build_observation({'kind':kind,'regions':['IDK'],'limit':100})
        base.tools.db_executor._validate_read_only_sql(sql) if hasattr(base.tools.db_executor,'_validate_read_only_sql') else None
        return [dict(r) for r in self.db.execute(sql.replace('%s','?').replace('CURRENT_DATE()',"'2026-09-15'"),params)]
    def sale(self,detail,structure,price,modified='2026-09-01'):
        self.db.execute('INSERT INTO vk_dwd.sale_price_bill_detail_dwd VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(1,'SYN','Synthetic',structure,'n','A','black',price,'IDR','m','m','n','2026-01-01','2026-12-31',modified,detail))
    def test_region_precedence_latest_ties_and_duplicate_pool(self):
        self.sale(1,'Indonesia Kim Company','9','2026-09-15');self.sale(2,'IDK','10')
        rows=self.query('sales_prices');self.assertEqual(1,len(rows));self.assertEqual('10',rows[0]['ddp_price'])
        self.sale(3,'IDK','11');rows=self.query('sales_prices');self.assertEqual(2,len(rows))
        self.assertEqual('ambiguous_latest',ops.classify_prices('sales',rows,date(2026,9,15))[0]['state'])
    def test_missing_quote_and_purchase_tax_source(self):
        rows=self.query('sales_prices');self.assertEqual(1,len(rows));self.assertIsNone(rows[0]['ddp_price'])
        self.db.execute("INSERT INTO vk_dwd.purchase_price_bill_detail_dwd VALUES('SYN','Synthetic','S','Supplier','black','11','10','CNY','m','2026-01-01','2026-12-31','2026-09-01',1,'/0/2172/2225/')")
        rows=self.query('purchase_prices');self.assertEqual(1,len(rows));self.assertEqual('11',rows[0]['tax_inclue_price']);self.assertEqual('10',rows[0]['tax_exclue_price'])
    def test_empty_pool_and_promotion_date_boundary(self):
        self.db.execute('DELETE FROM vk_dwd.ready_goods_dwd');self.assertEqual([],self.query('sales_prices'))
        self.sale(1,'IDK','10')
        self.db.execute("INSERT INTO data_assistant.push_goods_report VALUES('SYN','IDK','2026-09-15','2026-09-15',NULL)")
        self.assertEqual(1,len(self.query('sales_prices')))
        self.db.execute("UPDATE data_assistant.push_goods_report SET promotion_start_time='2026-09-16'")
        self.assertEqual([],self.query('sales_prices'))

class IDKPublicTests(unittest.TestCase):
    def test_registered_query_separates_null_zero_negative_and_keeps_existing_metric(self):
        from test_registered_slow_pool import SlowPoolTests
        from test_remediation_remaining_cases import metric,facts
        h=SlowPoolTests();h.setUp();self.addCleanup(h.doCleanups)
        h.conn.create_function('NOW',1,lambda n:'2026-09-15 12:00:00')
        h.add([(1,'IDK',1,11,'m','m',20,1,'handing',None,'n'),(2,'IDK',1,12,'m','m',30,2,'handing',0,'n'),(3,'IDK',2,22,'kg','kg',40,None,'handing',-1,'n'),(4,'IDK',3,33,'m','m',10,8,'handing',None,'n'),(5,'IDK',4,44,'m','m',50,8,'handing',10,'n')])
        response=h.query(metric('idk_unpriced_pool','inventory',month=None))
        result=h.result(response);rows=facts(result)
        self.assertEqual(3,sum(r['metric_value'] for r in rows))
        self.assertEqual(1,sum(r['idk_null_price_rows'] for r in rows));self.assertEqual(1,sum(r['idk_zero_price_rows'] for r in rows));self.assertEqual(1,sum(r['idk_negative_price_rows'] for r in rows))
        self.assertEqual({3},{r['idk_scope_rows'] for r in rows})
        self.assertTrue(any(r['idk_rolls'] is None for r in rows))
        self.assertTrue(all('promotion_price' not in r and 'ddp_price' not in r for r in rows))
        detail=json.loads(base.contracts.datasage_catalog({'requests':[{'domain':'inventory','metric':'idk_unpriced_pool'}]}))
        self.assertEqual('success',detail['status']);self.assertIn('result_fields',detail['results'][0]['metric'])
    def test_historical_or_other_region_not_accepted(self):
        datasets,semantics=base.contracts.execution_contracts('inventory')
        from test_remediation_remaining_cases import metric
        for request in (metric('idk_unpriced_pool','inventory'),metric('idk_unpriced_pool','inventory',month=None,metric_filters={'warehouse_department':'HCM'})):
            with self.assertRaises(base.tools.QueryFailure):base.tools._build_metric_query(request,datasets,semantics,10,observed_on=date(2026,9,15))
