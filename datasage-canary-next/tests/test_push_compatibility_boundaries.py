"""Fixed synthetic integration cases; no real database or transport."""
import copy,importlib,unittest,hashlib,zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch,Mock
import test_business_contracts as base
from test_workflow_io import install_synthetic_roles
from test_legacy_workflow import source,mapping

io=importlib.import_module(base.TEST_PACKAGE+'.workflow_io')
wf=io.wf
audit=importlib.import_module(base.TEST_PACKAGE+'.workflow_customer_audit')

class PushCompatibilityBoundaries(unittest.TestCase):
    def test_baseline_digest_is_exact_and_normalizes_business_key_whitespace(self):
        row={'whse_dept':'HCM','goods_no':'SYN','attr_val':'Red','total_piece':'0.3'}
        split=[{**row,'whse_dept':' HCM ','total_piece':'0.1'},{**row,'goods_no':' SYN ','total_piece':'0.20'}]
        self.assertEqual(io.baseline_digest([row]),io.baseline_digest(split))
        self.assertNotEqual(io.baseline_digest([row]),io.baseline_digest([{**row,'total_piece':'0.3001'}]))
    def test_idk_real_query_preserves_old_order_with_stable_tie_break(self):
        sql,params=io.operations.build_observation({'kind':'idk_unpriced','limit':10000})
        self.assertIn('ORDER BY gmt_create DESC,goods_no,id LIMIT %s',sql)
        self.assertIn('(promotion_price IS NULL OR promotion_price<=0)',sql)
        self.assertEqual(sql.count('%s'),len(params))
    def test_customer_identity_uses_legacy_trim_and_duplicate_relation_grain(self):
        rows=[{'whse_dept':' HCM ','goods_no':' SYN-1001 ','attr_val':' Red ','total_piece':'1.1'},
              {'whse_dept':'HCM','goods_no':'SYN-1001','attr_val':'Red','total_piece':'1.4'}]
        m=mapping();m['productsByCustomer']['c1']=[{'goods_no':' SYN-1001 ','whse_dept':' HCM '}]*2
        m['employeeBySales']['Sales']['region']=' HCM '
        plan=wf.contact_plan(rows,m)
        self.assertEqual([['SYN-1001','Red',3]],plan['sales_packages'][0]['customers'][0]['products'])
        self.assertEqual(1,len(plan['audit_rows']))
        self.assertEqual(1,audit.verify_plan(rows,m,plan)['customer_cards'])
        m['customerInfo']['c1']['name']=None
        self.assertEqual([],wf.contact_plan(rows,m)['sales_packages'])

    def test_accepted_receipt_without_manifest_cannot_hide_changed_content(self):
        class Transport:
            def preflight(self,items):pass
            def fingerprint(self,item):return 'new-content'
            def send(self,item):raise AssertionError('must not send')
        with TemporaryDirectory() as tmp:
            progress=io.Progress(Path(tmp),'synthetic','2026-W38')
            item=io.component('synthetic','text','changed','scope','text')
            progress.set(item['key'],'provider_accepted',fingerprint='old-content')
            with self.assertRaisesRegex(io.IOErrorBoundary,'CONTENT_CHANGED'):io.deliver_components([item],Transport(),progress,enabled=True)
            progress.set(item['key'],'provider_accepted')
            with self.assertRaisesRegex(io.IOErrorBoundary,'LEGACY_RECEIPT_BINDING'):io.deliver_components([item],Transport(),progress,enabled=True)

    def test_full_chain_rerun_continues_and_explicit_resend_starts_new_delivery(self):
        rows=wf.freeze_plan([source()],[],'2026-W38','2026-W38')['insert_rows']
        rows[0]['frozen_at']='2026-09-15 09:00:00'
        class DB:
            def execute(self,sql,params,limit):
                if sql.startswith('SELECT NOW'):value=[{'at':'2026-09-15 12:00:00'}]
                elif 'FROM vk_ai.slow_moving_baseline' in sql:value=rows
                elif 'FROM vk_dwd.delivery_' in sql:value=[{'customer_id':1,'goods_no':'SYN-1001','whse_dept':'HCM'}]
                elif 'FROM vk_dwd.customer_dwd' in sql:value=[{'customer_id':1,'customer_no':'SYN-C','customer_name':'Synthetic Customer','sales_name':'Sales'}]
                else:value=[{'region':'HCM','main_dept':'HCM Sales','is_delete':'n','wecom_status':'payroll','wecom_account':'s','person_name':'Sales','position':'Sales'}]
                return value,False,{}
        @contextmanager
        def snapshots():yield DB()
        class Transport:
            def __init__(self):self.calls=[];self.fail=True
            def preflight(self,parts):pass
            def fingerprint(self,item):
                raw=Path(item['path']).read_bytes() if item['kind']=='file' else item['text'].encode()
                return wf.digest([item['account'],item['kind'],hashlib.sha256(raw).hexdigest()])
            def send(self,item):
                self.calls.append(item['stage'])
                return {'success':False,'raw_response':{'errcode':1}} if self.fail and item['stage']=='customer_zip' else {'success':True,'message_id':'synthetic'}
        with TemporaryDirectory() as tmp,patch.object(io.time,'sleep'):
            root=Path(tmp);install_synthetic_roles(root)
            binding={'recipients_file':'local/workflow-roles.json','customer_mapping_enabled':True,'send_enabled':True}
            transport=Transport();p=io.Progress(root,'slow_task','2026-W38')
            def run(n,flags=None):
                out=root/str(n);out.mkdir();p.run_id=str(n)
                return io._produce_and_execute(root,'slow_task',{**binding,**(flags or {})},out,'2026-W38','2026-09',p,snapshots,transport,None)
            with self.assertRaisesRegex(io.IOErrorBoundary,'DELIVERY_COMPONENT_FAILED'):run(1)
            first_tasks=transport.calls.count('task_text');transport.fail=False
            run(2)
            self.assertEqual(first_tasks,transport.calls.count('task_text'))
            count=len(transport.calls);run(3)
            self.assertEqual(count,len(transport.calls))
            run(4,{'_force_resend':True})
            self.assertEqual(first_tasks*2,transport.calls.count('task_text'))
            # First invocation uses its two bounded continuation retries; later
            # ordinary continuation and explicit resend each add one ZIP call.
            self.assertEqual(5,transport.calls.count('customer_zip'))

    def test_xlsx_packaging_time_is_not_a_retry_content_change(self):
        xlsx=importlib.import_module(base.TEST_PACKAGE+'.legacy_xlsx')
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'sample.xlsx'
            xlsx.gen_workbook_xlsx([('Sheet1',['Customer No'],[['SYN-C1']])],path)
            with zipfile.ZipFile(path) as archive:self.assertTrue(all(i.date_time==(1980,1,1,0,0,0) for i in archive.infolist()))

    def test_replay_requires_explicit_valid_reason_before_io(self):
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        binding={'enabled':True,'read_enabled':True,'freeze_enabled':True,'send_enabled':True}
        with patch.object(io,'load_activation',return_value=binding),patch.object(local,'configure_runtime') as configure:
            for options in ({'refreeze':True},{'force_resend':True},{'refreeze':True,'reason':'explicit rebaseline'}):
                with self.assertRaises(io.IOErrorBoundary):io.run_bound(base.PROFILE_ROOT,'slow_task',**options)
            configure.assert_not_called()

if __name__=='__main__':unittest.main()
