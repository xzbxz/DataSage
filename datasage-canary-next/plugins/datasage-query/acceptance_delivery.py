"""Final-hop test redirection. Production routes/state are never activated.

Only the two user-approved destinations are possible. Logical identities remain
distinct for progress; actual destinations share the existing target locks/fences.
"""
from contextlib import contextmanager,ExitStack
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import hashlib,json,time,re
from . import workflow_io as workflow
from .wecom_app_transport import AppTransport,file_snapshot

APPROVED_WEBHOOK_SHA256='2ba1b5a17138c4f0729bd05b12d7f4b124fc674659aca765f0770af9b721e1be'
APPROVED_CORP_SHA256='7356f7bd7b38969a141936003acd13dec59e72a3d66c1279d5f3f8109c5a1d87'
TEST_USER='zhangzhengwei'
SECRET_FILE='acceptance-delivery.secrets.json'

def load_settings(profile):
    path=Path(profile)/SECRET_FILE
    if path.is_symlink() or not path.is_file() or path.stat().st_size>16384:
        raise workflow.IOErrorBoundary('ACCEPTANCE_CONFIGURATION_REQUIRED')
    try:doc=json.loads(path.read_text(encoding='utf-8'))
    except Exception:raise workflow.IOErrorBoundary('ACCEPTANCE_CONFIGURATION_INVALID') from None
    url=doc.get('test_webhook','');parts=urlsplit(url);query=parse_qs(parts.query)
    if (doc.get('version')!=1 or doc.get('mode')!='acceptance_only' or doc.get('enabled') is not True
        or doc.get('private_target')!=TEST_USER or parts.scheme!='https' or parts.netloc!='qyapi.weixin.qq.com'
        or parts.path!='/cgi-bin/webhook/send' or parts.fragment or set(query)!={'key'} or len(query['key'])!=1
        or not re.fullmatch(r'[0-9a-f-]{36}',query['key'][0])
        or hashlib.sha256(url.encode()).hexdigest()!=APPROVED_WEBHOOK_SHA256
        or doc.get('approved_webhook_sha256')!=APPROVED_WEBHOOK_SHA256
        or doc.get('max_logical_notifications_per_batch')!=2 or doc.get('max_messages_per_batch')!=12):
        raise workflow.IOErrorBoundary('ACCEPTANCE_DESTINATION_OR_BOUNDARY_INVALID')
    app=doc.get('application',{})
    if not all(app.get(k) for k in ('name','corp_id','agent_id','corp_secret')) or str(app['agent_id'])!='1000043' or hashlib.sha256(str(app['corp_id']).encode()).hexdigest()!=APPROVED_CORP_SHA256:
        raise workflow.IOErrorBoundary('ACCEPTANCE_APPLICATION_REQUIRED')
    return doc

def runtime_home(profile):
    home=Path(profile)/'report_runs'/'reminder_acceptance'
    if any(p.is_symlink() for p in (home,home.parent)) or not home.resolve().is_relative_to(Path(profile).resolve()):
        raise workflow.IOErrorBoundary('ACCEPTANCE_STATE_PATH_INVALID')
    home.mkdir(parents=True,exist_ok=True)
    return home

def _config_digest(doc):return hashlib.sha256(json.dumps(doc,sort_keys=True).encode()).hexdigest()

class _Private(AppTransport):
    def __init__(self,profile,config,accounts,*,client_factory=None):
        self.actual_profile=Path(profile);self.configuration=config;self.accounts=accounts
        super().__init__('reminder-acceptance',runtime_home(profile),app_loader=lambda:[config['application']],client_factory=client_factory)
    def _binding(self):
        if _config_digest(load_settings(self.actual_profile))!=_config_digest(self.configuration):
            raise workflow.IOErrorBoundary('ACCEPTANCE_CONFIGURATION_CHANGED')
        app=self.configuration['application']
        return {'target_map':{a:{'platform':'wecom_app_http','app_name':app['name'],'corp_id':app['corp_id'],
            'agent_id':str(app['agent_id']),'target_kind':'user','target_id':TEST_USER} for a in self.accounts}}

class _Webhook(_Private):
    """Robot HTTPS media API; never application chat_id or a websocket."""
    def _binding(self):
        if _config_digest(load_settings(self.actual_profile))!=_config_digest(self.configuration):
            raise workflow.IOErrorBoundary('ACCEPTANCE_CONFIGURATION_CHANGED')
        return {'target_map':{a:{'platform':'test_webhook','target_ref':APPROVED_WEBHOOK_SHA256} for a in self.accounts}}
    def preflight(self,components):
        self._gate();self.targets={};self.files={}
        for item in components:
            if item['account'] not in self.target_map:raise workflow.IOErrorBoundary('ACCEPTANCE_GROUP_NOT_MAPPED')
            self.targets[item['account']]=self.target_map[item['account']]
            if item['kind']=='file':self.files[item['key']]=file_snapshot(item['path'],self.profile)
            elif item['kind']!='text' or len(item['text'].encode())>2048:
                raise workflow.IOErrorBoundary('ACCEPTANCE_GROUP_COMPONENT_INVALID')
            if item['kind']=='text' and (item.get('message_format','text') not in ('text','markdown') or item.get('mention_all') and item.get('message_format','text')!='text'):
                raise workflow.IOErrorBoundary('ACCEPTANCE_GROUP_COMPONENT_INVALID')
    def target_key(self,account):
        if account not in self.targets:raise workflow.IOErrorBoundary('ACCEPTANCE_GROUP_NOT_MAPPED')
        return workflow.wf.digest(['webhook',APPROVED_WEBHOOK_SHA256])
    def prepare(self,items,progress):
        checked=False
        for item in items:
            if not checked:self._check_fence(item,progress);checked=True
        key=parse_qs(urlsplit(self.configuration['test_webhook']).query)['key'][0]
        for item in items:
            if item['kind']!='file':continue
            name,data,mime,digest=self.files[item['key']];upload_key='upload-'+item['key']
            progress.set(upload_key,'upload_in_flight',artifact_digest=digest)
            try:value=self._request('POST','webhook/upload_media',params={'key':key,'type':'file'},files={'media':(name,data,mime)})
            except Exception:
                progress.set(upload_key,'upload_unknown',artifact_digest=digest);raise
            if value.get('errcode',0)!=0 or not isinstance(value.get('media_id'),str) or not value['media_id']:
                progress.set(upload_key,'upload_failed',api_errcode=value.get('errcode') if type(value.get('errcode')) is int else None)
                raise workflow.IOErrorBoundary('ACCEPTANCE_GROUP_UPLOAD_FAILED')
            self.media[item['key']]=value['media_id']
            progress.set(upload_key,'uploaded_not_sent',artifact_digest=digest,media_id=value['media_id'])
    def send(self,item):
        self._gate()
        # Shared target lock is held. Persist pacing across batches/processes;
        # this is a timestamp, not a transport queue or automatic retry system.
        path=workflow.private_root(self.profile)/'webhook-send-clock.json'
        last=json.loads(path.read_text()).get('last_attempt',0) if path.exists() else 0
        wait=max(0,3.2-(time.time()-last))
        if wait>4:raise workflow.IOErrorBoundary('ACCEPTANCE_RATE_CLOCK_REVIEW_REQUIRED')
        if wait:time.sleep(wait)
        workflow.operations._atomic(path,{'last_attempt':time.time()})
        if item['kind']=='file':payload={'msgtype':'file','file':{'media_id':self.media[item['key']]}}
        else:
            kind=item.get('message_format','text')
            if kind not in ('text','markdown'):raise workflow.IOErrorBoundary('ACCEPTANCE_FORMAT_INVALID')
            payload={'msgtype':kind,kind:{'content':item['text']}}
            if item.get('mention_all'):
                if kind!='text':raise workflow.IOErrorBoundary('ACCEPTANCE_MENTION_FORMAT_INVALID')
                payload['text']['mentioned_list']=['@all']
        key=parse_qs(urlsplit(self.configuration['test_webhook']).query)['key'][0]
        value=self._request('POST','webhook/send',params={'key':key},json=payload)
        code=value.get('errcode')
        return {'success':type(code) is int and code==0,'raw_response':{'errcode':code if type(code) is int else None},'message_id':value.get('msgid')}

class AcceptanceTransport:
    def __init__(self,profile,components,*,private_client_factory=None,group_client_factory=None):
        self.profile=Path(profile);self.config=load_settings(profile)
        self.routes={}
        for item in components:
            channel=item.get('acceptance_channel')
            if channel not in ('private','group'):raise workflow.IOErrorBoundary('ACCEPTANCE_CHANNEL_REQUIRED')
            account=item['account']
            if account in self.routes and self.routes[account]!=channel:raise workflow.IOErrorBoundary('ACCEPTANCE_LOGICAL_ID_COLLISION')
            self.routes[account]=channel
        self.children={}
        for channel,cls,factory in [('private',_Private,private_client_factory),('group',_Webhook,group_client_factory)]:
            accounts={a for a,c in self.routes.items() if c==channel}
            if accounts:self.children[channel]=cls(profile,self.config,accounts,client_factory=factory)
    def child(self,item):
        if self.routes.get(item['account'])!=item.get('acceptance_channel'):raise workflow.IOErrorBoundary('ACCEPTANCE_ROUTE_CHANGED')
        return self.children[item['acceptance_channel']]
    def normalize(self,items,progress):
        result=[]
        for item in items:result.extend(self.child(item).normalize([item],progress))
        if len(result)>self.config['max_messages_per_batch']:raise workflow.IOErrorBoundary('ACCEPTANCE_BATCH_MESSAGE_LIMIT')
        return result
    def preflight(self,items):
        if len({i['notification_key'] for i in items})>2:raise workflow.IOErrorBoundary('ACCEPTANCE_BATCH_NOTIFICATION_LIMIT')
        for item in items:self.child(item)
        for channel,child in self.children.items():child.preflight([i for i in items if i['acceptance_channel']==channel])
    @contextmanager
    def delivery_lock(self,progress):
        with ExitStack() as stack:
            for channel in sorted(self.children):stack.enter_context(self.children[channel].delivery_lock(progress))
            yield
    def fingerprint(self,item):return workflow.wf.digest([self.child(item).fingerprint(item),item.get('message_format','text'),item.get('mention_all',False)])
    def prepare(self,items,progress):
        for channel,child in self.children.items():child.prepare([i for i in items if i['acceptance_channel']==channel],progress)
    def begin_notification(self,items,progress):return self.child(items[0]).begin_notification(items,progress)
    def finish_notification(self,items,progress,status):return self.child(items[0]).finish_notification(items,progress,status)
    def send(self,item):return self.child(item).send(item)

def notification_parts(notice,scope):
    required={'logical_id','channel','role','body','attachments'}
    if not required<=set(notice) or notice['channel'] not in ('private','group'):
        raise workflow.IOErrorBoundary('ACCEPTANCE_LOGICAL_NOTICE_INVALID')
    # No raw former webhook may enter a manifest, account key or explanatory text.
    if any('key=' in str(notice.get(k,'')) or 'http' in str(notice.get(k,'')) for k in ('logical_id','role')):
        raise workflow.IOErrorBoundary('ACCEPTANCE_LOGICAL_REFERENCE_UNSAFE')
    account=notice['channel']+':'+str(notice['logical_id'])
    intro='【验收，不用于生产投递】业务/周期：'+scope+'；原角色：'+notice['role']+'；逻辑通知：'+notice['logical_id']+'。以下正文与附件按原规则生成。'
    parts=[workflow.component(account,'text',intro,scope,'acceptance_note'),workflow.component(account,'text',notice['body'],scope,'original_text')]
    parts[1]['message_format']=notice.get('message_format','text')
    parts += [workflow.component(account,'file',path,scope,'original_file_'+str(n)) for n,path in enumerate(notice['attachments'])]
    if notice.get('mention_all'):
        if notice['channel']!='group':raise workflow.IOErrorBoundary('ACCEPTANCE_PRIVATE_MENTION_INVALID')
        mention=workflow.component(account,'text',notice.get('mention_all_msg','请相关人员关注以上采购价变更'),scope,'original_mention_all')
        mention['mention_all']=True;parts.append(mention)
    for item in parts:item['acceptance_channel']=notice['channel']
    return parts

def deliver_batch(profile,case_id,notices):
    if not re.fullmatch(r'[a-z0-9_-]{1,64}',case_id):raise workflow.IOErrorBoundary('ACCEPTANCE_CASE_INVALID')
    if not 1<=len(notices)<=2:raise workflow.IOErrorBoundary('ACCEPTANCE_BATCH_NOTIFICATION_LIMIT')
    home=runtime_home(profile)
    with workflow.run_lock(home,'case-'+case_id):
        parts=[part for notice in notices for part in notification_parts(notice,case_id)]
        transport=AcceptanceTransport(profile,parts)
        progress=workflow.Progress(home,'acceptance-'+case_id,case_id)
        progress.run_id=case_id
        workflow.deliver_components(parts,transport,progress,enabled=True,force=False)
        return {'status':'provider_accepted_not_human_read','case_id':case_id,'logical_notifications':len(notices),
            'components':len(parts),'progress_file':str(progress.path),'production_state_changed':False}
