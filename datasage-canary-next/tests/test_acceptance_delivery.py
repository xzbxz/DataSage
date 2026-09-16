import unittest,importlib,json,hashlib,zipfile
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import httpx
import test_business_contracts as base

a=importlib.import_module(base.TEST_PACKAGE+'.acceptance_delivery');io=a.workflow

class AcceptanceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.profile=Path(self.tmp.name)
        self.url='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000'
        self.doc={'version':1,'mode':'acceptance_only','enabled':True,'private_target':'zhangzhengwei',
            'application':{'name':'test-app','corp_id':'synthetic-corp','agent_id':'1000043','corp_secret':'synthetic-secret'},
            'test_webhook':self.url,'approved_webhook_sha256':hashlib.sha256(self.url.encode()).hexdigest(),
            'max_logical_notifications_per_batch':2,'max_messages_per_batch':12}
        for key,value in [('APPROVED_WEBHOOK_SHA256',self.doc['approved_webhook_sha256']),('APPROVED_CORP_SHA256',hashlib.sha256(b'synthetic-corp').hexdigest())]:
            p=patch.object(a,key,value);p.start();self.addCleanup(p.stop)
        p=patch.object(a.time,'sleep');p.start();self.addCleanup(p.stop)
        self.save()
        self.home=a.runtime_home(self.profile);self.path=io.private_root(self.home)/'中文验收.zip'
        with zipfile.ZipFile(self.path,'w') as z:z.writestr('测试.txt','SYNTHETIC')
        self.events=[];self.fail_file=False;self.timeout_file=False;self.fail_upload=False
    def save(self):(self.profile/a.SECRET_FILE).write_text(json.dumps(self.doc),encoding='utf-8')
    def notice(self,channel='private',logical='old-sales-a',body='原始业务正文'):
        return {'channel':channel,'logical_id':logical,'role':'原销售角色','body':body,'attachments':[str(self.path)]}
    def handle(self,request):
        endpoint=request.url.path.split('/cgi-bin/')[-1]
        record={'endpoint':endpoint}
        if endpoint.endswith('/send'):
            value=json.loads(request.content);record['payload']=value
            if endpoint=='message/send':self.assertEqual('zhangzhengwei',value['touser']);self.assertEqual(1000043,value['agentid'])
            else:self.assertEqual('00000000-0000-0000-0000-000000000000',request.url.params['key'])
        self.events.append(record)
        if endpoint in ('media/upload','webhook/upload_media'):
            self.assertIn('中文验收.zip'.encode(),request.content)
            return httpx.Response(200,json={'errcode':40005} if self.fail_upload else {'errcode':0,'media_id':'synthetic-media'})
        if endpoint=='gettoken':return httpx.Response(200,json={'errcode':0,'access_token':'synthetic-token','expires_in':7200})
        if endpoint=='user/get':return httpx.Response(200,json={'errcode':0,'userid':'zhangzhengwei','status':1})
        if value['msgtype']=='file':
            if self.timeout_file:raise httpx.ReadTimeout('SECRET MUST NOT ESCAPE',request=request)
            if self.fail_file:return httpx.Response(200,json={'errcode':45009})
        return httpx.Response(200,json={'errcode':0})
    def deliver(self,notices,case='synthetic-case'):
        parts=[p for n in notices for p in a.notification_parts(n,case)]
        factory=lambda:httpx.Client(transport=httpx.MockTransport(self.handle))
        sender=a.AcceptanceTransport(self.profile,parts,private_client_factory=factory,group_client_factory=factory)
        progress=io.Progress(self.home,'acceptance-'+case,case);progress.run_id=case
        io.deliver_components(parts,sender,progress,enabled=True)
        return progress,parts
    def sends(self):return [e for e in self.events if e['endpoint'].endswith('/send')]
    def test_mixed_batch_uploads_all_before_any_message_and_routes_exactly(self):
        self.deliver([self.notice(),self.notice('group','old-group')])
        positions=[i for i,e in enumerate(self.events) if e['endpoint'].endswith('upload') or e['endpoint'].endswith('upload_media')]
        first_send=next(i for i,e in enumerate(self.events) if e['endpoint'].endswith('/send'))
        self.assertTrue(all(i<first_send for i in positions));self.assertEqual(6,len(self.sends()))
        self.assertEqual({'message/send','webhook/send'},{e['endpoint'] for e in self.sends()})
    def test_two_logical_users_do_not_collide_or_lose_personalized_body(self):
        p,parts=self.deliver([self.notice(logical='A',body='甲的个性化内容'),self.notice(logical='B',body='乙的个性化内容')])
        self.assertEqual(len(parts),len({i['key'] for i in parts}))
        bodies=[e['payload'].get('text',{}).get('content') for e in self.sends()]
        self.assertIn('甲的个性化内容',bodies);self.assertIn('乙的个性化内容',bodies)
        count=len(self.events);self.deliver([self.notice(logical='A',body='甲的个性化内容'),self.notice(logical='B',body='乙的个性化内容')]);self.assertEqual(count,len(self.events))
    def test_changed_private_target_or_webhook_or_corp_rejected_before_http(self):
        for field,value in [('private_target','real-person'),('test_webhook',self.url+'，')]:
            old=self.doc[field];self.doc[field]=value;self.save()
            with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice()])
            self.doc[field]=old;self.save()
        self.doc['application']['corp_id']='another-corp';self.save()
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice()])
        self.assertEqual([],self.events)
    def test_unknown_channel_and_mixed_logical_route_fail_preflight(self):
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('unknown')])
        notice=self.notice('group');notice['message_format']='unsupported'
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice(),notice])
        self.assertEqual([],self.events)
    def test_upload_failure_does_not_send_any_private_or_group_text(self):
        self.fail_upload=True
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('group'),self.notice()])
        self.assertEqual([],self.sends())
    def test_file_known_failure_retries_only_missing_file(self):
        self.fail_file=True
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('group')])
        self.fail_file=False;self.deliver([self.notice('group')])
        self.assertEqual(['text','text','file','file'],[e['payload']['msgtype'] for e in self.sends()])
    def test_unknown_send_pauses_even_with_different_case_same_target(self):
        self.timeout_file=True
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('group')])
        count=len(self.events);self.timeout_file=False
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('group')])
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice('group')],case='other-case')
        self.assertEqual(count,len(self.events))
    def test_markdown_and_mention_all_keep_old_payload_types(self):
        notice=self.notice('group');notice.update(message_format='markdown',mention_all=True,attachments=[])
        self.deliver([notice])
        self.assertEqual(['text','markdown','text'],[e['payload']['msgtype'] for e in self.sends()])
        self.assertEqual(['@all'],self.sends()[-1]['payload']['text']['mentioned_list'])
    def test_wrong_attachment_and_batch_burst_rejected_before_http(self):
        outside=self.profile/'outside.zip';outside.write_bytes(self.path.read_bytes());n=self.notice();n['attachments']=[str(outside)]
        with self.assertRaises(io.IOErrorBoundary):self.deliver([n])
        with self.assertRaises(io.IOErrorBoundary):self.deliver([self.notice(logical=str(i)) for i in range(3)])
        self.assertEqual([],self.events)
    def test_recovery_state_is_not_production_progress_and_does_not_store_secrets(self):
        p,_=self.deliver([self.notice('group')])
        self.assertTrue(p.path.is_relative_to(self.profile/'report_runs/reminder_acceptance'))
        self.assertFalse((self.profile/'report_runs/legacy_execution').exists())
        persisted=''.join(p.read_text(encoding='utf-8') for p in self.home.rglob('*.json'))
        self.assertNotIn(self.url,persisted);self.assertNotIn('synthetic-secret',persisted);self.assertNotIn('synthetic-token',persisted)
    def test_webhook_cannot_be_smuggled_into_source_explanation(self):
        n=self.notice();n['logical_id']=self.url
        with self.assertRaises(io.IOErrorBoundary):self.deliver([n])

if __name__=='__main__':unittest.main()
