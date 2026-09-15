"""Legacy workflow behavior on synthetic records and in-memory SQLite only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib,json,sqlite3,subprocess,sys,unittest,zipfile
import test_business_contracts as base
from datetime import datetime,timezone

wf=importlib.import_module(base.TEST_PACKAGE+'.legacy_workflow')

def source(i=1,**kw):
    return {'id':i,'goods_id':1,'goods_sku_id':11,'goods_no':'SYN-1001','goods_name':'Synthetic fabric','attr_val':'Red','color_label':'A','whse_dept':'HCM','source_unit':'m','unit':'m','goods_num':20,'piece_num':2,'is_whitelist':'n','slow_label':'handing','promotion_price':None,**kw}

def recipients():return {'HCM':{'executors':[{'account':'synthetic-e','name':'Executor'}],'managers':['synthetic-m'],'dynamic_sales_departments':['HCM Sales']}}
def employees():return [{'region':'HCM','main_dept':'HCM Sales','is_delete':'n','wecom_status':'payroll','wecom_account':'synthetic-s','person_name':'Sales'}]
def mapping():return {'productsByCustomer':{'c1':[{'goods_no':'SYN-1001','whse_dept':'HCM'},{'goods_no':'SYN-1001','whse_dept':'HCM'}]},'customerInfo':{'c1':{'customer_no':'SYN-C1','name':'Synthetic Customer','sales':'Sales'}},'wecomBySales':{'Sales':'synthetic-s'},'employeeBySales':{'Sales':{'wecom_account':'synthetic-s','region':'HCM'}}}

class LegacyWorkflowTests(unittest.TestCase):
    def test_old_timezone_week_boundary_and_complete_report_reconciliation(self):
        periods=wf.legacy_periods(datetime(2026,9,20,16,tzinfo=timezone.utc))
        self.assertEqual('2026-W39',periods['week']);self.assertIn('09:00:00+08:00',periods['planned_start'])
        packet={'detail_complete':True,'detail_rows':[['SYN','Red','HCM','Reduced',20,'m',15,2,1.5,-0.5,1,'Sales']],'summary':{'opening_skus':1,'closing_skus':1,'new':0,'exited':0,'opening_rolls':2,'closing_rolls':1.5,'net_outbound_rolls':1}}
        wf.validate_complete_detail(packet)
        packet['summary']['net_outbound_rolls']=2
        with self.assertRaisesRegex(wf.WorkflowError,'MISMATCH'):wf.validate_complete_detail(packet)
    def test_freeze_grain_threshold_repeat_and_current_policy_conflicts(self):
        plan=wf.freeze_plan([source(),source(2),source(3,goods_num=10),source(4,is_whitelist='y')],[],'2026-W38','2026-W38')
        self.assertEqual('ready',plan['status']);self.assertEqual(2,plan['insert_count'])
        self.assertEqual('M',plan['insert_rows'][0]['unit'])
        reused=wf.freeze_plan([],plan['insert_rows'],'2026-W38','2026-W38',refreeze=False)
        self.assertEqual('reuse_existing',reused['action'])
        repeated=wf.freeze_plan([source(5)],plan['insert_rows'],'2026-W38','2026-W38')
        self.assertEqual('replace_requested_week',repeated['action'])
        for rows in ([source(),source()],[source(source_unit=None)],[source(piece_num=None)],[]):
            self.assertEqual('blocked',wf.freeze_plan(rows,plan['insert_rows'],'2026-W38','2026-W38')['status'])
        self.assertEqual('blocked',wf.freeze_plan([source()],[],'2026-W37','2026-W38')['status'])
        specs=wf.source_query_specs('2026-W38')
        self.assertEqual(['freeze_source','existing_week','dynamic_recipients'],[s['role'] for s in specs])
        self.assertTrue(all(s['sql'].startswith('SELECT ') for s in specs))
        self.assertEqual(['2026-W38'],specs[1]['params'])

    def test_isolated_freeze_transaction_rolls_back_and_does_not_touch_other_week(self):
        conn=sqlite3.connect(':memory:');self.addCleanup(conn.close);conn.execute("ATTACH DATABASE ':memory:' AS vk_ai")
        columns=','.join(k+(' INTEGER' if k=='baseline_version' else ' TEXT') for k in wf.BASELINE_COLUMNS)
        conn.execute('CREATE TABLE vk_ai.slow_moving_baseline ('+columns+", frozen_at TEXT DEFAULT '2026-09-15 09:00:00')")
        conn.execute("INSERT INTO vk_ai.slow_moving_baseline(week_label,source_row_id) VALUES('2026-W37','old')");conn.commit()
        plan=wf.freeze_plan([source()],[],'2026-W38','2026-W38')
        self.assertEqual(1,wf.simulate_freeze(conn,plan)['written'])
        with self.assertRaisesRegex(wf.WorkflowError,'SYNTHETIC_FAILURE'):wf.simulate_freeze(conn,plan,fail_after_delete=True)
        self.assertEqual(2,conn.execute('SELECT COUNT(*) FROM vk_ai.slow_moving_baseline').fetchone()[0])
        with TemporaryDirectory() as tmp:
            realfile=sqlite3.connect(str(Path(tmp)/'not-memory.db'))
            try:
                with self.assertRaisesRegex(wf.WorkflowError,'IN_MEMORY'):wf.simulate_freeze(realfile,plan)
            finally:realfile.close()
        with self.assertRaisesRegex(wf.WorkflowError,'IN_MEMORY'):wf.simulate_freeze(object(),plan)

    def test_recipients_deduplicate_roles_and_filter_active_departments(self):
        cfg=recipients();cfg['HCM']['executors'].append({'account':'synthetic-s','name':'Sales'})
        result=wf.task_recipients(cfg,employees()+[{**employees()[0],'wecom_account':'inactive','wecom_status':'left'}],['HCM'])['HCM']
        self.assertEqual(3,len(result));self.assertEqual(['executor','sales'],next(r['roles'] for r in result if r['account']=='synthetic-s'))
        with self.assertRaisesRegex(wf.WorkflowError,'MISSING'):wf.task_recipients(cfg,[],['HCM'])
        price=wf.price_recipients(cfg,employees()*2,['HCM'],['synthetic-fixed'])
        self.assertEqual(1,len(price['HCM']['sales']));self.assertEqual(['synthetic-fixed'],price['HCM']['managers'])

    def test_component_partial_failure_unknown_and_forced_new_cycle(self):
        first=wf.delivery_preview('report','2026-W38','HCM','s',['text','file'])
        receipts=[{'component_key':first[0]['component_key'],'status':'provider_accepted'},{'component_key':first[1]['component_key'],'status':'failed'}]
        retry=wf.delivery_preview('report','2026-W38','HCM','s',['text','file'],receipts)
        self.assertEqual(['skip_confirmed_component','would_send'],[r['proposed_action'] for r in retry])
        receipts[-1]['status']='unknown'
        forced=wf.delivery_preview('report','2026-W38','HCM','s',['text','file'],receipts,force=True)
        self.assertEqual(['would_send','hold_for_review'],[r['proposed_action'] for r in forced]);self.assertTrue(all(r['human_received']=='unknown' for r in forced))
        import hashlib
        old={'version':2,'mode':'report','week':'2026-W38','region':'HCM','completed_recipient_tokens':[hashlib.sha256(b's|text').hexdigest()]}
        mapped=wf.legacy_progress_receipts(old,'report','2026-W38','HCM',['s'])
        self.assertEqual(['skip_confirmed_component','would_send'],[r['proposed_action'] for r in wf.delivery_preview('report','2026-W38','HCM','s',['text','file'],mapped)])
        self.assertEqual('provider_accepted',wf.official_receipt({'success':True,'message_id':'synthetic'},'key')['status'])
        self.assertEqual('unverified_success',wf.official_receipt({'success':True},'key')['status'])
        self.assertEqual('unknown',wf.official_receipt({'success':False,'error':'timeout'},'key')['status'])
        self.assertEqual('not_delivered',wf.official_receipt({'success':True,'delivered':False},'key')['status'])

    def test_old_same_week_recipient_and_customer_cache_decisions(self):
        existing={'version':2,'week':'2026-W38','regions':{'HCM':['old-target']}}
        self.assertEqual('reuse_frozen_week_plan',wf.recipient_plan_decision('2026-W38',{'HCM':['new-target']},existing,preview=False)['action'])
        self.assertEqual({'HCM':['new-target']},wf.recipient_plan_decision('2026-W38',{'HCM':['new-target']},existing,preview=True)['regions'])
        with self.assertRaisesRegex(wf.WorkflowError,'MISMATCH'):wf.customer_plan_decision('2026-W38','new',{'version':4,'week':'2026-W38','baseline_digest':'old'},preview=False)
        self.assertEqual('rebuild_for_refreeze',wf.customer_plan_decision('2026-W38','new',rebuild=True))

    def test_customer_join_duplicates_cross_region_and_missing_identity(self):
        rows=[{'whse_dept':'HCM','goods_no':'SYN-1001','attr_val':'Red','total_piece':2},{'whse_dept':'IDK','goods_no':'SYN-1001','attr_val':'Red','total_piece':3}]
        m=mapping();m['productsByCustomer']['c1'].append({'goods_no':'SYN-1001','whse_dept':'IDK'})
        plan=wf.contact_plan(rows,m);self.assertEqual([['SYN-1001','Red',5]],plan['sales_packages'][0]['customers'][0]['products'])
        rounding=wf.contact_plan([dict(rows[0],total_piece=2.5)],mapping())
        self.assertEqual(3,rounding['sales_packages'][0]['customers'][0]['products'][0][2])
        m['customerInfo']['c1']['name']=None;plan=wf.contact_plan(rows,m)
        self.assertEqual([],plan['sales_packages']);self.assertEqual('Missing Customer Name',plan['audit_rows'][0]['exception'])

    def test_png_zip_and_xlsx_actual_formats_and_formula_text(self):
        from PIL import Image
        from xml.etree import ElementTree as ET
        with TemporaryDirectory() as tmp:
            root=Path(tmp);plan=wf.contact_plan([{'whse_dept':'HCM','goods_no':'SYN-1001','attr_val':'Red','total_piece':2}],mapping())
            archive=wf.customer_zip(plan['sales_packages'][0],root,'2026-W38')
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(['SYN-C1_Synthetic Customer.png'],z.namelist());self.assertTrue(z.read(z.namelist()[0]).startswith(b'\x89PNG'))
                self.assertEqual((1980,1,1,0,0,0),z.infolist()[0].date_time)
            image=next(root.rglob('*.png'))
            with Image.open(image) as img:self.assertGreater(img.width,400)
            book=root/'test.xlsx';wf.gen_workbook_xlsx([('Detail',['Item No','Rolls'],[['=UNTRUSTED',2]])],book,borders=True,landscape=True)
            with zipfile.ZipFile(book) as z:
                xml=z.read('xl/worksheets/sheet1.xml');ET.fromstring(xml)
                self.assertNotIn(b'<f>',xml);self.assertIn(b'autoFilter',xml);self.assertIn(b'landscape',xml)

    def test_workflow_preview_cannot_send_freeze_or_accept_price_baseline(self):
        data={'evidence_origin':'synthetic','week':'2026-W38','current_week':'2026-W38','start':'2026-09-14','end':'2026-09-20','region':'HCM','source_rows':[source()],'recipient_map':recipients(),'employees':employees(),'customer_mapping':mapping()}
        with TemporaryDirectory() as tmp,patch.object(base.tools,'_execute_with_source',side_effect=AssertionError('No DB')):
            root=Path(tmp);inputs=root/'report_inputs'/'legacy';inputs.mkdir(parents=True);(inputs/'slow_task.json').write_text(json.dumps(data),encoding='utf-8')
            result=wf.preview_from_file(root,'slow_task');manifest=json.loads((result/'manifest.json').read_text(encoding='utf-8'))
            self.assertFalse(manifest['sent']);self.assertFalse(manifest['frozen']);self.assertFalse(manifest['price_baseline_accepted'])
            self.assertTrue(any(f.endswith('.xlsx') for f in manifest['files']));self.assertTrue(any(f.endswith('.zip') for f in manifest['files']))

    def test_six_fixed_adapters_default_to_disabled_without_runtime_binding(self):
        scripts=base.PROFILE_ROOT/'scripts'
        for job in wf.policy()['jobs']:
            result=subprocess.run([sys.executable,'-B',str(scripts/f'datasage_legacy_{job}.py')],capture_output=True,text=True)
            self.assertEqual(2,result.returncode,job);self.assertIn('WORKFLOW_EXECUTION_NOT_ENABLED',result.stderr)

    def test_multi_region_reports_keep_weekly_before_monthly(self):
        zero={'opening_skus':0,'closing_skus':0,'opening_rolls':0,'closing_rolls':0,'new':0,'exited':0,'net_outbound_rolls':0,'high_net_rolls':0}
        group={'weekly':{'period':'2026-W38','summary':zero,'detail_rows':[],'detail_complete':True},'monthly':{'period':'2026-09','summary':zero,'detail_rows':[],'detail_complete':True}}
        data={'evidence_origin':'synthetic','week':'2026-W38','region_reports':{'HCM':group,'IDK':group},'recipient_map':{'HCM':{'executors':[],'managers':['m']},'IDK':{'executors':[],'managers':['m']}}}
        with TemporaryDirectory() as tmp:
            root=Path(tmp);wf.build_preview('slow_report',data,root)
            texts=[(root/f'message-{n:02d}.txt').read_text(encoding='utf-8') for n in range(1,5)]
            self.assertTrue(all('Weekly' in t for t in texts[:2]));self.assertTrue(all('Monthly' in t for t in texts[2:]))

    def test_collect_uses_bounded_readonly_specs_and_rejects_truncation(self):
        class Source:
            def __init__(self,cut=False):self.calls=[];self.cut=cut
            def execute(self,sql,params,limit):
                self.calls.append((sql,params,limit));return [],self.cut,{}
        source=Source();result=wf.collect_readonly_inputs(source,'2026-W38')
        self.assertEqual({'source_rows','existing_rows','employees'},set(result));self.assertEqual(3,len(source.calls))
        self.assertTrue(all(sql.startswith('SELECT ') and limit==10000 for sql,_,limit in source.calls))
        with self.assertRaisesRegex(wf.WorkflowError,'INCOMPLETE'):wf.collect_readonly_inputs(Source(True),'2026-W38')

    def test_prices_first_observation_is_silent_and_changed_sides_separate(self):
        self.assertIn('No price-change',wf.price_draft('purchase',[]))
        text=wf.price_draft('purchase',[{'goods_no':'SYN','old_inc':12,'new_inc':10,'old_exc':9,'new_exc':9,'currency_no':'CNY','unit_cuur':'m'}])
        self.assertIn('采购价下调',text);self.assertIn('含税价：',text);self.assertNotIn('不含税价：',text)
        adapted=wf.operation_preview_input('sales_price',{'kind':'sales_prices','status':'success','baseline_id':None,'events':[{'event':'price_changed'}]})
        self.assertEqual([],adapted['changes']);self.assertEqual('unaccepted_observation_no_change_claim',adapted['baseline_state'])
