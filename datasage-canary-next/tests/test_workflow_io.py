"""Real adapter code, synthetic I/O only. Never enables production actions."""
import importlib,json,unittest,zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import contextmanager
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import test_business_contracts as base
from test_legacy_workflow import source

io=importlib.import_module(base.TEST_PACKAGE+'.workflow_io');wf=importlib.import_module(base.TEST_PACKAGE+'.legacy_workflow');inputs=importlib.import_module(base.TEST_PACKAGE+'.workflow_inputs');fabric=importlib.import_module(base.TEST_PACKAGE+'.fabric_report')

class Cursor:
    def __init__(self,db):self.db=db;self.result=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,params=()):
        self.db.sql.append(sql)
        if 'NOW(6)' in sql:self.result=[{'read_at':'2026-09-15 09:00:00'}]
        elif 'GET_LOCK' in sql:self.result=[{'acquired':1}]
        elif sql.startswith('DELETE'):self.db.rows=[]
        elif sql.startswith('INSERT'):
            if self.db.fail_insert:raise RuntimeError('synthetic insert failure')
            size=len(wf.BASELINE_COLUMNS)
            self.db.rows=[{**dict(zip(wf.BASELINE_COLUMNS,params[i:i+size])),'frozen_at':'2026-09-15 09:00:00'} for i in range(0,len(params),size)]
        elif 'FROM vk_ai.slow_moving_baseline' in sql:self.result=self.db.rows
        else:self.result=[]
    def fetchone(self):return self.result[0]
    def fetchall(self):return self.result

class Writer:
    def __init__(self,fail_insert=False,fail_commit=False):self.rows=[];self.sql=[];self.fail_insert=fail_insert;self.fail_commit=fail_commit;self.committed=False;self.closed=False
    def cursor(self):return Cursor(self)
    def begin(self):self.before=list(self.rows)
    def commit(self):
        self.committed=True
        if self.fail_commit:raise RuntimeError('synthetic acknowledgement lost')
    def rollback(self):
        if not self.committed and hasattr(self,'before'):self.rows=self.before
    def close(self):self.closed=True

@contextmanager
def snapshot():
    class DB:
        def execute(self,sql,params,limit):return ([source()] if 'FROM vk_ods.' in sql else []),False,{}
    yield DB()

class WorkflowIOTests(unittest.TestCase):
    def test_disabled_blocks_before_any_factory_or_state(self):
        with TemporaryDirectory() as tmp,patch.object(io,'load_activation',side_effect=io.IOErrorBoundary('WORKFLOW_EXECUTION_NOT_ENABLED')),patch.object(importlib.import_module(base.TEST_PACKAGE+'.contract_store'),'profile_root',return_value=Path(tmp)):
            with self.assertRaisesRegex(io.IOErrorBoundary,'NOT_ENABLED'):io.run_bound(Path(tmp),'slow_task',writer_factory=lambda:self.fail('writer called'))
            self.assertFalse((Path(tmp)/'report_runs').exists())
        with self.assertRaisesRegex(io.IOErrorBoundary,'NOT_ENABLED'):io.open_freeze_writer()
        with self.assertRaisesRegex(io.IOErrorBoundary,'NOT_ENABLED'):io.create_paused_official_job('slow_task')

    def test_transport_preflight_precedes_freeze_or_input_reads(self):
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        from unittest.mock import Mock
        writer=Mock();reader=Mock();binding={'enabled':True,'read_enabled':True,'customer_mapping_enabled':True,'freeze_enabled':True,'send_enabled':True,'target_map':{'s':{'platform':'wecom_callback','chat_id':'corp:s','app_name':'app'}}}
        class Reject:
            def preflight(self,items):raise io.IOErrorBoundary('OFFICIAL_PLATFORM_FILE_UNSUPPORTED')
        with patch.object(io,'load_activation',return_value=binding),patch.object(local,'configure_runtime'):
            with self.assertRaisesRegex(io.IOErrorBoundary,'FILE_UNSUPPORTED'):io.run_bound(base.PROFILE_ROOT,'slow_task',transport=Reject(),writer_factory=writer,snapshot_factory=reader)
        writer.assert_not_called();reader.assert_not_called()

    def test_freeze_transaction_real_sql_and_failure_unknown(self):
        with TemporaryDirectory() as tmp,patch.object(io,'require_action',return_value={'freeze_enabled':True}):
            for fail_insert,fail_commit,expected in [(False,False,'committed'),(True,False,'failed'),(False,True,'unknown')]:
                writer=Writer(fail_insert,fail_commit);p=io.Progress(Path(tmp),'case'+str(fail_insert)+str(fail_commit),'2026-W38')
                if expected=='committed':self.assertEqual(1,len(io.freeze_current_week(snapshot,lambda:writer,p,enabled=True)))
                else:
                    with self.assertRaises(RuntimeError):io.freeze_current_week(snapshot,lambda:writer,p,enabled=True)
                self.assertEqual(expected,p.status('freeze'));self.assertTrue(writer.closed)
                self.assertTrue(any('RELEASE_LOCK' in s for s in writer.sql))
                self.assertTrue(all('vk_ai.slow_moving_baseline' in s for s in writer.sql if s.startswith(('DELETE','INSERT'))))
                if expected=='unknown':
                    with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):io.freeze_current_week(snapshot,lambda:self.fail('must not reopen'),p,enabled=True)

    def test_send_progress_known_partial_and_unknown(self):
        class Transport:
            def __init__(self):self.calls=[];self.fail_file=True
            def preflight(self,c):pass
            def send(self,item):
                self.calls.append(item['kind'])
                if item['kind']=='file' and self.fail_file:return {'success':False,'raw_response':{'errcode':1}}
                return {'success':True,'message_id':'synthetic'}
        with TemporaryDirectory() as tmp:
            p=io.Progress(Path(tmp),'send','2026-W38');t=Transport();parts=[io.component('a','text','hello','scope','text'),io.component('a','file','synthetic.xlsx','scope','file')]
            with self.assertRaisesRegex(io.IOErrorBoundary,'COMPONENT_FAILED'):io.deliver_components(parts,t,p,enabled=True)
            t.fail_file=False;io.deliver_components(parts,t,p,enabled=True)
            self.assertEqual(['text','file','file'],t.calls)
            p.set(parts[1]['key'],'in_flight')
            with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):io.deliver_components(parts,t,p,enabled=True,force=True)
            self.assertEqual(3,len(t.calls))

    def test_official_live_adapter_bridge_without_network(self):
        from gateway.platforms.base import SendResult
        import gateway.run as gateway
        class Adapter:
            _group_chat_ids=set()
            def __init__(self):self.calls=[]
            async def send(self,**kwargs):self.calls.append(('text',kwargs));return SendResult(success=True,message_id='synthetic-text')
            async def send_document(self,**kwargs):self.calls.append(('file',kwargs));return SendResult(success=True,message_id='synthetic-file')
        adapter=Adapter();runner=SimpleNamespace(adapters={'wecom':adapter});binding={'send_enabled':True,'target_map':{'a':{'platform':'wecom','chat_id':'approved-chat'}}}
        with patch.object(io,'require_action',return_value=binding),patch.object(gateway,'_gateway_runner_ref',return_value=runner):
            transport=io.OfficialTransport('slow_task');parts=[io.component('a','text','hello','s','text'),io.component('a','file','synthetic.xlsx','s','file')];transport.preflight(parts)
            self.assertTrue(transport.send(parts[0]).success);self.assertTrue(transport.send(parts[1]).success)
            self.assertEqual(['text','file'],[c[0] for c in adapter.calls])
        with patch.object(io,'require_action',return_value=binding),patch.object(gateway,'_gateway_runner_ref',return_value=None):
            with self.assertRaisesRegex(io.IOErrorBoundary,'NO_STANDALONE'):io.OfficialTransport('slow_task').preflight(parts)

    def test_official_callback_file_support_is_not_assumed(self):
        import gateway.run as gateway
        from gateway.platforms.base import BasePlatformAdapter
        class Callback:
            send_document=BasePlatformAdapter.send_document
            def _resolve_app_for_chat(self,chat):return {'name':'app','corp_id':'corp'}
        b={'send_enabled':True,'target_map':{'a':{'platform':'wecom_callback','chat_id':'corp:a','app_name':'app'}}}
        with patch.object(io,'require_action',return_value=b),patch.object(gateway,'_gateway_runner_ref',return_value=SimpleNamespace(adapters={'wecom_callback':Callback()})):
            with self.assertRaisesRegex(io.IOErrorBoundary,'FILE_UNSUPPORTED'):io.OfficialTransport('slow_task').preflight([io.component('a','file','x.xlsx','s','f')])

    def test_official_paused_registration_no_duplicate(self):
        import tools.cronjob_tools as cron
        import cron.jobs as jobs
        calls=[];existing=[]
        def fake(**kwargs):
            calls.append(kwargs)
            if kwargs['action']=='list':return json.dumps({'jobs':existing})
            self.assertTrue(kwargs['paused']);existing.append({'job_id':'synthetic','name':kwargs['name'],'script':kwargs['script'],'enabled':False,'state':'paused','schedule':{'expr':kwargs['schedule']},'no_agent':True,'deliver':'local'});return json.dumps({'success':True})
        with patch.object(io,'require_action',return_value={'register_schedule_enabled':True}),patch.object(cron,'cronjob',side_effect=fake),patch.object(jobs,'get_job',side_effect=lambda job_id:existing[0]),patch.object(jobs,'_hermes_now',return_value=datetime(2026,9,15,tzinfo=timezone(timedelta(hours=8)))):
            io.create_paused_official_job('slow_task',enabled=True);io.create_paused_official_job('slow_task',enabled=True)
        self.assertEqual(1,sum(c['action']=='create' for c in calls))

    def test_source_inputs_are_parameterized_and_missing_identity_fails(self):
        class DB:
            def __init__(self):self.calls=[]
            def execute(self,sql,params,limit):
                self.calls.append((sql,params))
                if sql.startswith('SELECT NOW'):return [{'at':'2026-09-15 12:00:00'}],False,{}
                return [],False,{}
        db=DB();m=inputs.customer_mapping(db,[("SYN'",'HCM')]);self.assertEqual({},m['productsByCustomer'])
        self.assertNotIn("SYN'",db.calls[1][0]);self.assertIn("SYN'",db.calls[1][1]);self.assertIn('delivery_time<%s',db.calls[1][0])

    def test_real_writer_factory_requires_separate_credentials_before_connect(self):
        from agent import secret_scope
        runtime=importlib.import_module(base.TEST_PACKAGE+'.db_runtime')
        with patch.object(io,'require_action',return_value={'freeze_enabled':True}),patch.object(secret_scope,'get_secret',return_value=''),patch.object(runtime,'load_pymysql') as load:
            with self.assertRaisesRegex(io.IOErrorBoundary,'SEPARATE_FREEZE_CREDENTIALS'):io.open_freeze_writer(enabled=True)
            load.assert_not_called()

    def test_writer_factory_verifies_same_server_and_dedicated_account(self):
        from agent import secret_scope
        runtime=importlib.import_module(base.TEST_PACKAGE+'.db_runtime');security=importlib.import_module(base.TEST_PACKAGE+'.db_security')
        class C:
            closed=False
            def cursor(self):return self
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,*args):pass
            def fetchone(self):return {'server_uuid':'synthetic-server','database_name':'vk_ai','account':'writer@localhost'}
            def close(self):self.closed=True
        c=C();secrets={'DATASAGE_FREEZE_MYSQL_HOST':'same-host','DATASAGE_FREEZE_MYSQL_USER':'writer','DATASAGE_FREEZE_MYSQL_PASSWORD':'synthetic','DATA_QUERY_MYSQL_HOST':'same-host','DATA_QUERY_MYSQL_USER':'readonly'}
        from unittest.mock import Mock
        driver=SimpleNamespace(connect=Mock(return_value=c),cursors=SimpleNamespace(DictCursor=object))
        with patch.object(io,'require_action',return_value={'freeze_enabled':True}),patch.object(secret_scope,'get_secret',side_effect=lambda k,d='':secrets.get(k,d)),patch.object(runtime,'load_pymysql',return_value=driver),patch.object(runtime,'connection_port',return_value=3306),patch.object(security,'mysql_tls_policy',return_value={}),patch.object(security,'mysql_tls_kwargs',return_value={}),patch.object(security,'verify_mysql_tls'),patch.object(base.tools,'_execute_with_source',return_value=([{'server_uuid':'synthetic-server'}],False,{})):
            self.assertIs(c,io.open_freeze_writer(enabled=True));self.assertEqual('writer',driver.connect.call_args.kwargs['user']);self.assertEqual('vk_ai',driver.connect.call_args.kwargs['database'])

    def test_readonly_idk_production_input_does_not_need_manual_input_or_recipients(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'run';out.mkdir();progress=io.Progress(root,'idk','2026-W38')
            row={'id':1,'goods_no':'SYN','goods_name':'Synthetic','goods_sku_id':11,'attr_val':'Red','source_unit':'m','goods_num':20,'piece_num':2,'promotion_price':None,'observed_at':'2026-09-15 12:00:00'}
            with patch.object(base.tools,'_execute_with_source',return_value=([row],False,{})):
                result=io._produce_and_execute(root,'idk',{'enabled':True,'read_enabled':True},out,'2026-W38','2026-09',progress,None,None,None)
            self.assertEqual('success',result['status']);self.assertEqual('not_requested',result['delivery']);self.assertFalse(result['baseline_accepted'])
            self.assertFalse((root/'report_inputs').exists());self.assertFalse((root/'legacy-recipients.json').exists())

    def test_customer_mapping_deduplicates_and_keeps_calendar_window(self):
        class DB:
            def execute(self,sql,params,limit):
                if sql.startswith('SELECT NOW'):rows=[{'at':'2024-02-29 12:00:00'}]
                elif 'FROM vk_dwd.delivery_' in sql:
                    self.start=params[-3];rows=[{'customer_id':1,'goods_no':'SYN','whse_dept':'HCM'}]*2
                elif 'FROM vk_dwd.customer_dwd' in sql:rows=[{'customer_id':1,'customer_no':'SYN-C','customer_name':'Synthetic','sales_name':'Sales'}]
                else:rows=[{'person_name':'Sales','wecom_account':'synthetic-sales','region':'HCM','main_dept':'HCM Sales','position':'Sales'}]
                return rows,False,{}
        db=DB();result=inputs.customer_mapping(db,[('SYN','HCM')])
        self.assertEqual(1,len(result['productsByCustomer']['1']));self.assertEqual(28,db.start.day);self.assertEqual(2023,db.start.year)

    def test_fabric_workbook_has_real_embedded_charts_and_unknowns(self):
        doc={'observations':[{'name':'出库总览','rows':[{'group':'总体','facts':{'metric_value':10,'fabric_rolls':20}}]},{'name':'出库渠道','rows':[{'group':'合成渠道','facts':{'fabric_known_tagged_rolls':5,'fabric_tag_rate':'0.25','fabric_tag_contribution':None}}]}]}
        with TemporaryDirectory() as tmp:
            paths=fabric.export_report(doc,Path(tmp))
            self.assertEqual(3,len(paths))
            with zipfile.ZipFile(paths[0]) as z:
                self.assertIn('xl/drawings/drawing1.xml',z.namelist());self.assertIn('xl/media/chart1.png',z.namelist())
                self.assertTrue(z.read('xl/media/chart1.png').startswith(b'\x89PNG'))

    def test_governed_rows_adapt_to_legacy_detail_without_using_names_as_ids(self):
        _,sem=base.contracts.execution_contracts('inventory')
        def dims(metric,sales=False):
            definitions=base.capability_contract.effective_dimension_definitions(sem,metric)
            values={'product':'Display only','pool_sku':'11','warehouse_department':'HCM','unit':'m'}
            if sales:values['salesperson']='Synthetic Sales'
            return [{'label':definitions[k]['label'],'value':v} for k,v in values.items()]
        pool={'results':[{'rows':[{'dimensions':dims('registered_slow_pool_baseline_groups'),'facts':{'opening_quantity':20,'closing_quantity':15,'opening_rolls':2,'closing_rolls':1.5},'states':{'pool_movement_state':'Reduced'}}]}]}
        flow={'results':[{'rows':[{'dimensions':dims('registered_slow_pool_baseline_net_outbound',True),'facts':{'net_rolls':4,'sales_net_rolls':4,'sales_identity_ref':'s','scope_net_rolls':4,'scope_high_net_rolls':2,'unit_net_quantity':10}}]}]}
        result=inputs.legacy_report_packet(pool,flow,[{'goods_sku_id':11,'whse_dept':'HCM','goods_no':'SYN','attr_val':'Red'}],'HCM','2026-W38')
        self.assertEqual('SYN',result['detail_rows'][0][0]);self.assertEqual(4,result['detail_rows'][0][10]);self.assertEqual(1,result['summary']['opening_skus'])
        unknown=inputs.legacy_report_packet(pool,flow,[],'HCM','2026-W38')
        self.assertEqual('Unknown',unknown['detail_rows'][0][0]);self.assertEqual(1,unknown['summary']['opening_skus']);self.assertFalse(unknown['detail_complete'])

    def test_dynamic_task_chain_and_failed_zip_audit_use_real_adapters_with_fake_io(self):
        baseline=wf.freeze_plan([source()],[],'2026-W38','2026-W38')['insert_rows']
        baseline[0]['frozen_at']='2026-09-15 09:00:00'
        class DB:
            def execute(self,sql,params,limit):
                if sql.startswith('SELECT NOW'):rows=[{'at':'2026-09-15 12:00:00'}]
                elif 'FROM vk_ai.slow_moving_baseline' in sql:rows=baseline
                elif 'FROM vk_dwd.delivery_' in sql:rows=[{'customer_id':1,'goods_no':'SYN-1001','whse_dept':'HCM'}]
                elif 'FROM vk_dwd.customer_dwd' in sql:rows=[{'customer_id':1,'customer_no':'SYN-C','customer_name':'Synthetic Customer','sales_name':'Sales'}]
                else:rows=[{'region':'HCM','main_dept':'HCM Sales','is_delete':'n','wecom_status':'payroll','wecom_account':'s','person_name':'Sales','position':'Sales'}]
                return rows,False,{}
        @contextmanager
        def snapshots():yield DB()
        class Transport:
            def __init__(self):self.calls=[]
            def preflight(self,components):pass
            def send(self,item):
                self.calls.append(item)
                return {'success':False,'raw_response':{'errcode':1}} if item['stage']=='customer_zip' else {'success':True,'message_id':'synthetic'}
        with TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'run';out.mkdir()
            (root/'legacy-recipients.json').write_text(json.dumps({'regions':{'HCM':{'executors':[{'account':'e','name':'Executor'}],'managers':['m'],'dynamic_sales_departments':['HCM Sales']}}}),encoding='utf-8')
            binding={'recipients_file':'legacy-recipients.json','customer_mapping_enabled':True,'send_enabled':True}
            t=Transport();p=io.Progress(root,'slow_task','2026-W38')
            with self.assertRaisesRegex(io.IOErrorBoundary,'CUSTOMER_DELIVERY_PARTIAL'):io._produce_and_execute(root,'slow_task',binding,out,'2026-W38','2026-09',p,snapshots,t,None)
            self.assertTrue(any(r['account']=='m' and r['stage']=='audit_file' for r in t.calls))
            self.assertEqual('not_attempted',p.status('freeze'))
            self.assertTrue(io.cached_plan(root,'recipients','2026-W38'))
            self.assertFalse((root/'report_inputs').exists())
