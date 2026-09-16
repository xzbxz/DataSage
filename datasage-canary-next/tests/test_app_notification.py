"""Application HTTP contract exercised using httpx.MockTransport only."""
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import threading
import unittest
import zipfile

import httpx
import test_business_contracts as base

io=importlib.import_module(base.TEST_PACKAGE+'.workflow_io')
app=importlib.import_module(base.TEST_PACKAGE+'.wecom_app_transport')
wf=importlib.import_module(base.TEST_PACKAGE+'.legacy_workflow')

class AppNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.profile=Path(self.tmp.name)
        self.path=io.private_root(self.profile)/'客户通知_库存明细.zip'
        with zipfile.ZipFile(self.path,'w') as z:z.writestr('客户甲.txt','测试内容')
        self.app={'name':'business','corp_id':'corp','agent_id':'100','corp_secret':'SYNTHETIC_SECRET'}
        self.target={'platform':'wecom_app_http','app_name':'business','corp_id':'corp','agent_id':'100','target_kind':'user','target_id':'test-user'}
        self.binding={'send_enabled':True,'target_map':{'legacy':self.target}}
        gate=patch.object(io,'require_action',return_value=self.binding);gate.start();self.addCleanup(gate.stop)
        self.progress=io.Progress(self.profile,'first-job','period');self.progress.run_id='run-1'
        self.parts=[io.component('legacy','text','你好，这是一条测试通知。','scope','text'),io.component('legacy','file',self.path,'scope','file')]
        self.events=[];self.bodies=[];self.fail_at=None;self.timeout_at=None;self.invalid=False

    def handler(self,request):
        endpoint=request.url.path.rsplit('/',1)[-1]
        if request.url.path.endswith('/message/send') or request.url.path.endswith('/appchat/send'):
            body=json.loads(request.content);event='send-'+body['msgtype'];self.bodies.append(body)
        else:event=endpoint
        self.events.append(event)
        if self.timeout_at==event:raise httpx.ReadTimeout('do not leak SYNTHETIC_SECRET',request=request)
        if self.fail_at==event:return httpx.Response(200,json={'errcode':45009,'errmsg':'SYNTHETIC_SECRET'})
        if event=='gettoken':value={'errcode':0,'access_token':'SYNTHETIC_TOKEN','expires_in':7200}
        elif event=='get':
            value=({'errcode':0,'chat_info':{'chatid':self.target['target_id']}} if '/appchat/' in request.url.path
                   else {'errcode':0,'userid':self.target['target_id'],'status':1})
        elif event=='upload':
            self.assertIn(self.path.name.encode('utf-8'),request.content)
            self.assertIn(b'name="media"',request.content)
            value={'errcode':0,'media_id':'synthetic-media'}
        else:value={'errcode':0,'msgid':'synthetic-'+event,**({'invaliduser':'test-user'} if self.invalid else {})}
        return httpx.Response(200,json=value)

    def transport(self,job='first-job',handler=None):
        return app.AppTransport(job,self.profile,app_loader=lambda:[self.app],
            client_factory=lambda:httpx.Client(transport=httpx.MockTransport(handler or self.handler)))

    def deliver(self,*,force=False,parts=None,transport=None,progress=None):
        io.deliver_components(parts or self.parts,transport or self.transport(),progress or self.progress,enabled=True,force=force)

    def test_all_uploads_before_text_same_app_user_and_chinese_name(self):
        self.deliver()
        self.assertEqual(['gettoken','get','upload','send-text','send-file'],self.events)
        for body in self.bodies:
            self.assertEqual('test-user',body['touser']);self.assertEqual(100,body['agentid'])
        self.assertEqual('provider_accepted',self.progress.status('notification-'+self.parts[0]['notification_key']))
        self.assertEqual('unknown',self.progress.data['components'][self.parts[0]['key']]['human_received'])

    def test_upload_failure_sends_nothing_and_can_retry(self):
        self.fail_at='upload'
        with self.assertRaisesRegex(io.IOErrorBoundary,'UPLOAD_FAILED'):self.deliver()
        self.assertEqual([],self.bodies)
        self.fail_at=None;self.deliver()
        self.assertEqual(['text','file'],[b['msgtype'] for b in self.bodies])

    def test_upload_timeout_is_not_message_unknown_and_sends_nothing(self):
        self.timeout_at='upload'
        with self.assertRaisesRegex(io.IOErrorBoundary,'HTTP_OUTCOME_UNKNOWN'):self.deliver()
        self.assertEqual('upload_unknown',self.progress.status('upload-'+self.parts[1]['key']))
        self.assertEqual('not_attempted',self.progress.status(self.parts[0]['key']))
        self.timeout_at=None;self.deliver()
        self.assertEqual(2,len(self.bodies))

    def test_text_failure_does_not_send_file(self):
        self.fail_at='send-text'
        with self.assertRaisesRegex(io.IOErrorBoundary,'COMPONENT_FAILED'):self.deliver()
        self.assertEqual(['text'],[b['msgtype'] for b in self.bodies])
        self.fail_at=None;self.deliver(force=True)
        self.assertEqual(['text','text','file'],[b['msgtype'] for b in self.bodies])

    def test_file_failure_even_force_retries_only_file(self):
        self.fail_at='send-file'
        with self.assertRaisesRegex(io.IOErrorBoundary,'COMPONENT_FAILED'):self.deliver()
        self.fail_at=None;self.progress.run_id='run-2';self.deliver(force=True)
        self.assertEqual(['text','file','file'],[b['msgtype'] for b in self.bodies])

    def test_message_timeout_blocks_all_retry_even_force(self):
        for kind in ('text','file'):
            self.target['target_id']='test-'+kind
            progress=io.Progress(self.profile,kind,'period')
            self.timeout_at='send-'+kind
            with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):self.deliver(progress=progress)
            count=len(self.events);self.timeout_at=None
            reloaded=io.Progress(self.profile,kind,'period')
            with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):self.deliver(progress=reloaded,force=True)
            self.assertEqual(count,len(self.events))

    def test_duplicate_run_no_http_and_force_new_generation(self):
        self.deliver();count=len(self.events)
        self.deliver();self.deliver(force=True) # same run_id is not another scheduled run
        self.assertEqual(count,len(self.events))
        self.progress.run_id='run-2';self.fail_at='send-file'
        with self.assertRaisesRegex(io.IOErrorBoundary,'COMPONENT_FAILED'):self.deliver(force=True)
        self.progress.run_id='run-3';self.fail_at=None;self.deliver(force=True)
        self.assertEqual(['text','file','text','file','file'],[b['msgtype'] for b in self.bodies])
        self.assertEqual(1,len(self.progress.data['notification_history']))

    def test_group_uses_appchat_never_user_or_webhook(self):
        self.target.update(target_kind='appchat',target_id='test-group')
        self.deliver()
        for body in self.bodies:
            self.assertEqual('test-group',body['chatid']);self.assertNotIn('touser',body)
        self.target['target_id']='https://qyapi.weixin.qq.com/webhook/send?key=secret'
        with self.assertRaisesRegex(io.IOErrorBoundary,'TARGET_REQUIRED'):self.deliver()

    def test_invalid_recipient_zero_code_is_failed(self):
        self.invalid=True
        with self.assertRaisesRegex(io.IOErrorBoundary,'COMPONENT_FAILED'):self.deliver()
        self.assertEqual('failed',self.progress.status(self.parts[0]['key']))
        self.assertEqual(1,len(self.bodies))

    def test_target_verification_fails_before_upload(self):
        self.fail_at='get'
        with self.assertRaisesRegex(io.IOErrorBoundary,'TARGET_NOT_VERIFIED'):self.deliver()
        self.assertNotIn('upload',self.events)

    def test_no_credentials_no_http_and_disabled_gate(self):
        self.app['corp_secret']=''
        with self.assertRaisesRegex(io.IOErrorBoundary,'CREDENTIALS'):self.deliver()
        self.assertEqual([],self.events)
        with self.assertRaisesRegex(io.IOErrorBoundary,'SEND_NOT_ENABLED'):
            io.deliver_components(self.parts,self.transport(),self.progress)

    def test_file_preflight_failure_before_any_http(self):
        self.path.write_bytes(b'not a zip')
        with self.assertRaisesRegex(io.IOErrorBoundary,'ARCHIVE_INVALID'):self.deliver()
        self.assertEqual([],self.events)

    def test_partial_retry_changed_content_or_target_is_blocked(self):
        self.fail_at='send-file'
        with self.assertRaises(io.IOErrorBoundary):self.deliver()
        self.fail_at=None;count=len(self.events)
        with zipfile.ZipFile(self.path,'w') as z:z.writestr('客户甲.txt','changed')
        with self.assertRaisesRegex(io.IOErrorBoundary,'CONTENT_CHANGED'):self.deliver(force=True)
        self.assertEqual(count,len(self.events))

    def test_regenerated_zip_timestamp_does_not_change_identity(self):
        original=app.file_snapshot(self.path,self.profile)[3]
        with zipfile.ZipFile(self.path,'w') as z:
            info=zipfile.ZipInfo('客户甲.txt',(2020,1,1,0,0,0));z.writestr(info,'测试内容')
        self.assertEqual(original,app.file_snapshot(self.path,self.profile)[3])

    def test_long_chinese_text_split_preserves_all_bytes_and_order(self):
        self.parts[0]['text']='中文🙂\n'*700
        self.deliver()
        texts=[b['text']['content'] for b in self.bodies if b['msgtype']=='text']
        self.assertEqual(self.parts[0]['text'],''.join(texts))
        self.assertTrue(all(len(t.encode('utf-8'))<=2048 for t in texts))
        self.assertEqual('file',self.bodies[-1]['msgtype'])
        count=len(self.events);self.deliver();self.assertEqual(count,len(self.events))

    def test_no_secrets_in_persisted_state_or_errors(self):
        self.timeout_at='send-text'
        with self.assertRaises(io.IOErrorBoundary) as caught:self.deliver()
        content=str(caught.exception)+''.join(p.read_text(encoding='utf-8') for p in io.private_root(self.profile).glob('*.json'))
        self.assertNotIn('SYNTHETIC_SECRET',content);self.assertNotIn('SYNTHETIC_TOKEN',content)

    def test_cross_job_same_target_never_interleaves(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def blocking(request):
            if request.url.path.endswith('/message/send') and json.loads(request.content)['msgtype']=='text':
                entered.set()
                if not release.wait(5):raise AssertionError('test timed out')
            return self.handler(request)
        def first():
            try:self.deliver(transport=self.transport(handler=blocking))
            except Exception as e:errors.append(e)
        thread=threading.Thread(target=first);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            other=io.Progress(self.profile,'second-job','period')
            with self.assertRaisesRegex(io.IOErrorBoundary,'BUSY'):
                self.deliver(transport=self.transport('second-job'),progress=other)
        finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual([],errors)
        self.deliver(transport=self.transport('second-job'),progress=other)
        self.assertEqual(['text','file','text','file'],[b['msgtype'] for b in self.bodies])

    def test_target_alias_case_uses_same_lock(self):
        transport=self.transport();transport.preflight(self.parts)
        first=transport.target_key('legacy')
        transport.targets['alias']={**self.target,'target_id':'TEST-USER'}
        self.assertEqual(first,transport.target_key('alias'))

    def test_partial_notification_holds_target_across_jobs_until_repaired(self):
        self.fail_at='send-file'
        with self.assertRaises(io.IOErrorBoundary):self.deliver()
        self.fail_at=None;other=io.Progress(self.profile,'second-job','period');count=len(self.events)
        with self.assertRaisesRegex(io.IOErrorBoundary,'TARGET_NOTIFICATION_PENDING'):
            self.deliver(transport=self.transport('second-job'),progress=other)
        self.assertEqual(count,len(self.events))
        self.deliver(force=True)
        self.deliver(transport=self.transport('second-job'),progress=other)
        self.assertEqual(['text','file','file','text','file'],[b['msgtype'] for b in self.bodies])

    def test_unknown_blocks_other_jobs_same_target(self):
        self.timeout_at='send-file'
        with self.assertRaises(io.IOErrorBoundary):self.deliver()
        self.timeout_at=None;count=len(self.events)
        with self.assertRaisesRegex(io.IOErrorBoundary,'TARGET_DELIVERY_UNKNOWN'):
            self.deliver(transport=self.transport('second-job'),progress=io.Progress(self.profile,'second-job','period'))
        self.assertEqual(count,len(self.events))

    def test_sensitive_http_log_is_filtered(self):
        import logging
        self.deliver()
        logger=logging.getLogger('httpx')
        record=logging.LogRecord('httpx',logging.INFO,'',0,'HTTP Request: GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpsecret=SYNTHETIC_SECRET',(),None)
        self.assertFalse(logger.filter(record))

    def test_changed_chunk_membership_cannot_bypass_unknown_or_partial_progress(self):
        self.parts[0]['text']='中文'*1500;self.fail_at='send-file'
        with self.assertRaises(io.IOErrorBoundary):self.deliver()
        self.parts[0]['text']='short';self.fail_at=None
        with self.assertRaisesRegex(io.IOErrorBoundary,'CONTENT_CHANGED'):self.deliver(force=True)

    def test_all_pending_attachments_uploaded_before_first_text(self):
        extra=[io.component('legacy','text','second','other','text'),io.component('legacy','file',self.path,'other','file')]
        self.deliver(parts=self.parts+extra)
        self.assertEqual(['gettoken','get','upload','upload','send-text','send-file','send-text','send-file'],self.events)

    def test_response_without_errcode_is_unknown_not_accepted(self):
        def malformed(request):
            if request.url.path.endswith('/message/send'):return httpx.Response(200,json={'msgid':'untrusted'})
            return self.handler(request)
        with self.assertRaisesRegex(io.IOErrorBoundary,'UNKNOWN'):self.deliver(transport=self.transport(handler=malformed))

    def test_factory_selects_http_and_rejects_mixed_targets(self):
        self.assertIsInstance(io.make_transport(self.profile,'first-job',self.binding),app.AppTransport)
        self.binding['target_map']['other']={'platform':'wecom','chat_id':'chat'}
        with self.assertRaisesRegex(io.IOErrorBoundary,'MIXED'):io.make_transport(self.profile,'first-job',self.binding)

    def test_official_config_loader_reused_without_gateway_start(self):
        from types import SimpleNamespace
        import gateway.config as config
        with patch.object(config,'load_gateway_config',return_value=SimpleNamespace(platforms={'wecom_callback':SimpleNamespace(extra={'apps':[self.app]})})) as loader:
            self.assertEqual([self.app],app.load_apps());loader.assert_called_once()


if __name__=='__main__':unittest.main()
