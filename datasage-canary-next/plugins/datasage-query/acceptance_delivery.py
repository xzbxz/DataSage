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
BINDING_SCHEMA='datasage-delivery-binding/v1'
_PERIOD_RE=re.compile(r'(?<!\d)(\d{4}-w\d{2})(?!\d)',re.I)
_GENERATION_RE=re.compile(r'(?:^|-)g(\d+)(?:-|$)',re.I)
_SCOPE_RE=re.compile(r'^[A-Za-z0-9_-]{1,80}$')


class _BindingProgress:
    """Stateless progress view used only to derive deterministic component keys."""
    def status(self,key):
        del key
        return 'not_attempted'


def delivery_context(cycle_id,*,period=None,generation=None,phase=None):
    """Return the stable cycle/period/generation context used by receipts."""
    cycle_id=str(cycle_id)
    period_match=_PERIOD_RE.search(cycle_id)
    if period is None:
        period=period_match.group(1).upper() if period_match else cycle_id
    else:
        period=str(period).upper()
    generation_match=_GENERATION_RE.search(cycle_id)
    if generation is None:
        generation=int(generation_match.group(1)) if generation_match else 0
    if type(generation) is not int or not 0<=generation<=999:
        raise workflow.IOErrorBoundary('DELIVERY_GENERATION_INVALID')
    if period_match and str(period).upper()!=period_match.group(1).upper():
        raise workflow.IOErrorBoundary('DELIVERY_PERIOD_INVALID')
    if generation_match and generation!=int(generation_match.group(1)):
        raise workflow.IOErrorBoundary('DELIVERY_GENERATION_INVALID')
    if phase is None:
        if generation_match:
            phase=cycle_id[generation_match.end():].lstrip('-') or 'cycle'
        else:
            phase=cycle_id.rsplit('-',1)[-1] if '-' in cycle_id else 'cycle'
    if not isinstance(phase,str) or not phase or len(phase)>80 or not re.fullmatch(r'[A-Za-z0-9_-]+',phase):
        raise workflow.IOErrorBoundary('DELIVERY_PHASE_INVALID')
    return {'cycle_id':cycle_id,'period':period,'generation':generation,'phase':phase}


def business_scope_for(cycle_id,scope=None):
    match=_PERIOD_RE.search(str(cycle_id))
    prefix=str(cycle_id)[:match.start()].strip('-') if match else ''
    derived=prefix[3:] if prefix.lower().startswith('ls-') else None
    if scope is None:scope=derived
    if scope is None:return None
    if not isinstance(scope,str) or not _SCOPE_RE.fullmatch(scope):
        raise workflow.IOErrorBoundary('DELIVERY_BUSINESS_SCOPE_INVALID')
    normalized=scope.casefold()
    if derived is not None and normalized!=derived.casefold():
        raise workflow.IOErrorBoundary('DELIVERY_BUSINESS_SCOPE_INVALID')
    return normalized


def _target_descriptor(target):
    """Hash account identity fields; never place corp secrets in a receipt."""
    if not isinstance(target,dict):
        raise workflow.IOErrorBoundary('ACCEPTANCE_TARGET_BINDING_INVALID')
    platform=target.get('platform')
    if platform=='wecom_app_http':
        target_kind=target.get('target_kind')
        target_id=str(target.get('target_id',''))
        return {'platform':platform,
                'corp_id_sha256':hashlib.sha256(str(target.get('corp_id','')).encode()).hexdigest(),
                'agent_id':str(target.get('agent_id','')),
                'target_kind':str(target_kind),
                'target_id':target_id.lower() if target_kind=='user' else target_id}
    if platform=='test_webhook':
        return {'platform':platform,'target_ref':str(target.get('target_ref',''))}
    raise workflow.IOErrorBoundary('ACCEPTANCE_TARGET_BINDING_INVALID')


def approved_target_binding_digest(notices):
    """Derive the fixed acceptance target binding without reading credentials."""
    entries=[]
    for notice in notices:
        channel=notice.get('channel')
        if channel not in ('private','group'):
            raise workflow.IOErrorBoundary('ACCEPTANCE_CHANNEL_REQUIRED')
        if channel=='private':
            target_descriptor={'platform':'wecom_app_http','corp_id_sha256':APPROVED_CORP_SHA256,
                              'agent_id':'1000043','target_kind':'user','target_id':TEST_USER}
        else:
            target_descriptor={'platform':'test_webhook','target_ref':APPROVED_WEBHOOK_SHA256}
        entries.append(target_descriptor)
    entries={json.dumps(value,sort_keys=True,separators=(',',':')):value for value in entries}
    entries=[entries[key] for key in sorted(entries)]
    return workflow.wf.digest(['datasage-acceptance-target-binding/v1',entries])


def _normalized_binding_parts(notices,case_id):
    parts=[part for notice in notices for part in notification_parts(notice,case_id)]
    return AppTransport.normalize(None,parts,_BindingProgress())


def content_manifest_digest(notices,attachment_digests):
    """Hash the exact delivered text/role and artifact bytes, excluding paths."""
    rows=[]
    for notice in notices:
        attachments=[]
        for path in notice.get('attachments',[]):
            digest=attachment_digests.get(str(path))
            if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
                raise workflow.IOErrorBoundary('DELIVERY_CONTENT_BINDING_INVALID')
            attachments.append(digest)
        rows.append({'channel':notice.get('channel'),'role':notice.get('role'),
            'body':notice.get('body'),'message_format':notice.get('message_format','text'),
            'mention_all':notice.get('mention_all',False),
            'mention_all_msg':notice.get('mention_all_msg'), 'attachments':attachments})
    return workflow.wf.digest(['datasage-delivery-content-manifest/v1',rows])


def make_delivery_binding(*,cycle_id,case_id,period,generation,phase,target_binding_digest,
                          content_digest,component_keys,progress_scope,business_scope=None):
    if not isinstance(cycle_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',cycle_id):
        raise workflow.IOErrorBoundary('DELIVERY_CYCLE_ID_INVALID')
    if not isinstance(case_id,str) or not re.fullmatch(r'[a-z0-9_-]{1,64}',case_id):
        raise workflow.IOErrorBoundary('DELIVERY_CASE_ID_INVALID')
    keys=sorted(component_keys)
    if (not keys or len(keys)!=len(set(keys)) or
        any(not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key) for key in keys)):
        raise workflow.IOErrorBoundary('DELIVERY_COMPONENT_BINDING_INVALID')
    if not isinstance(target_binding_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',target_binding_digest):
        raise workflow.IOErrorBoundary('DELIVERY_TARGET_BINDING_INVALID')
    if not isinstance(content_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',content_digest):
        raise workflow.IOErrorBoundary('DELIVERY_CONTENT_BINDING_INVALID')
    business_scope=business_scope_for(cycle_id,business_scope)
    if not isinstance(progress_scope,dict) or set(progress_scope)!={'job','period'}:
        raise workflow.IOErrorBoundary('DELIVERY_PROGRESS_SCOPE_INVALID')
    expected_scope={'job':'acceptance-'+str(case_id),'period':str(case_id)}
    if progress_scope!=expected_scope:
        raise workflow.IOErrorBoundary('DELIVERY_PROGRESS_SCOPE_INVALID')
    context=delivery_context(cycle_id,period=period,generation=generation,phase=phase)
    return {'schema':BINDING_SCHEMA,'version':1,**context,'case_id':str(case_id),
            'business_scope':business_scope,
            'target_binding_digest':target_binding_digest,
            'content_manifest_digest':content_digest,'component_keys':keys,
            'progress_scope':dict(progress_scope)}


def validate_delivery_binding(binding):
    required={'schema','version','cycle_id','case_id','period','generation','phase','business_scope',
              'target_binding_digest','content_manifest_digest','component_keys','progress_scope'}
    if not isinstance(binding,dict) or set(binding)!=required or binding.get('schema')!=BINDING_SCHEMA or binding.get('version')!=1:
        return False
    try:
        delivery_context(binding['cycle_id'],period=binding['period'],generation=binding['generation'],phase=binding['phase'])
        make_delivery_binding(cycle_id=binding['cycle_id'],case_id=binding['case_id'],period=binding['period'],generation=binding['generation'],phase=binding['phase'],business_scope=binding['business_scope'],target_binding_digest=binding['target_binding_digest'],content_digest=binding['content_manifest_digest'],component_keys=binding['component_keys'],progress_scope=binding['progress_scope'])
    except Exception:
        return False
    return True


def bindings_compatible(prior,current):
    """Strictly compare a receipt binding supplied as its expected context."""
    if not validate_delivery_binding(prior) or not validate_delivery_binding(current):
        return False
    return all(prior[field]==current[field] for field in
               ('business_scope','period','generation','phase','target_binding_digest','content_manifest_digest'))


def cross_entry_compatible(prior,current):
    """Compare only reusable period/代次/phase/final-target context."""
    if not validate_delivery_binding(prior) or not validate_delivery_binding(current):
        return False
    return (prior.get('business_scope') is not None and current.get('business_scope') is not None and
            all(prior[field]==current[field] for field in
                ('business_scope','period','generation','phase','target_binding_digest')))


def case_id_for_cycle(cycle_id):
    return 'profile-v1-'+str(cycle_id)


def binding_for_manifest(profile,cycle_id,notices,*,manifest=None,case_id=None,period=None,generation=None,phase=None):
    """Build the expected binding without opening a transport or sending."""
    del profile
    case_id=case_id or case_id_for_cycle(cycle_id)
    parts=_normalized_binding_parts(notices,case_id)
    files=(manifest or {}).get('files',{})
    attachment_digests={}
    for notice in notices:
        for path in notice.get('attachments',[]):
            metadata=files.get(str(path),files.get(path)) if isinstance(files,dict) else None
            digest=metadata.get('sha256') if isinstance(metadata,dict) else metadata
            if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
                raise workflow.IOErrorBoundary('DELIVERY_CONTENT_BINDING_INVALID')
            attachment_digests[str(path)]=digest
    context=delivery_context(cycle_id,period=period,generation=generation,phase=phase)
    evidence=(manifest or {}).get('evidence',{}) if isinstance(manifest,dict) else {}
    scope=(manifest or {}).get('business_scope') if isinstance(manifest,dict) else None
    scope=scope or evidence.get('business_scope') or evidence.get('department')
    return make_delivery_binding(cycle_id=context['cycle_id'],case_id=case_id,period=context['period'],generation=context['generation'],phase=context['phase'],business_scope=scope,target_binding_digest=approved_target_binding_digest(notices),content_digest=content_manifest_digest(notices,attachment_digests),component_keys=[part['key'] for part in parts],progress_scope={'job':'acceptance-'+case_id,'period':case_id})


def inspect_progress(receipt,profile=None):
    """Read old progress evidence without creating, rewriting, or upgrading it."""
    from . import workflow_storage as base
    profile=Path(profile or base.profile()).resolve()
    try:path=Path((receipt.get('progress_file','') if isinstance(receipt,dict) else '') or '')
    except (TypeError,ValueError):return {'present':False,'reason':'progress_path_invalid'}
    root=profile/'report_runs'/'reminder_acceptance'/'report_runs'/'legacy_execution'
    try:
        if (any(p.is_symlink() for p in (profile/'report_runs',profile/'report_runs'/'reminder_acceptance',root,path,*path.parents))
                or not root.resolve().is_relative_to(profile)
                or not path.resolve().is_relative_to(root.resolve())
                or not path.is_file()):
            return {'present':False,'reason':'progress_unavailable'}
        state=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,RuntimeError):
        return {'present':False,'reason':'progress_unreadable'}
    components=state.get('components') if isinstance(state,dict) else None
    if not isinstance(components,dict):return {'present':True,'reason':'progress_components_invalid'}
    keys=sorted(key for key in components if isinstance(key,str) and re.fullmatch(r'[0-9a-f]{64}',key))
    return {'present':True,'scope':state.get('scope'),'component_keys':keys,
            'accepted_component_keys':sorted(key for key in keys if isinstance(components[key],dict) and components[key].get('status')=='provider_accepted'),
            'binding_present':validate_delivery_binding(state.get('delivery_binding'))}


def classify_receipt(receipt,expected_binding=None):
    """Classify a receipt; historical acceptance is retained but never reusable."""
    if not isinstance(receipt,dict) or receipt.get('status')!='provider_accepted_not_human_read':
        return {'classification':'not_provider_accepted','reusable':False,'reason':'receipt_status_not_accepted'}
    binding=receipt.get('delivery_binding')
    if not validate_delivery_binding(binding):
        result={'classification':'historical_provider_accepted','evidence_level':'historical_record_declared_provider_acceptance','reusable':False,'reason':'delivery_binding_missing_or_invalid'}
        result['progress_observation']=inspect_progress(receipt)
        return result
    if expected_binding is not None and not bindings_compatible(binding,expected_binding):
        return {'classification':'binding_mismatch','reusable':False,'reason':'delivery_binding_mismatch'}
    try:path=Path((receipt.get('progress_file','') or ''))
    except (TypeError,ValueError):return {'classification':'unverified_receipt','reusable':False,'reason':'progress_path_invalid'}
    from . import workflow_storage as base
    allowed=workflow.private_root(runtime_home(base.profile())).resolve()
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(allowed) or not path.is_file():
            return {'classification':'unverified_receipt','reusable':False,'reason':'progress_path_invalid'}
        expected_path=(allowed/(workflow.wf.digest([binding['progress_scope']['job'],binding['progress_scope']['period']])+'.json')).resolve()
        if path.resolve()!=expected_path:
            return {'classification':'binding_mismatch','reusable':False,'reason':'progress_scope_path_mismatch'}
        state=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,RuntimeError):
        return {'classification':'unverified_receipt','reusable':False,'reason':'progress_unreadable'}
    if state.get('delivery_binding')!=binding or state.get('scope')!=binding['progress_scope']:
        return {'classification':'binding_mismatch','reusable':False,'reason':'progress_binding_or_scope_mismatch'}
    keys=binding['component_keys'];components=state.get('components')
    if type(receipt.get('components')) is not int or receipt['components']!=len(keys) or not isinstance(components,dict):
        return {'classification':'unverified_receipt','reusable':False,'reason':'component_manifest_mismatch'}
    observed={key for key in components if isinstance(key,str) and re.fullmatch(r'[0-9a-f]{64}',key)}
    if observed!=set(keys) or any(components[key].get('status')!='provider_accepted' for key in keys if isinstance(components.get(key),dict)):
        return {'classification':'unverified_receipt','reusable':False,'reason':'component_keys_or_status_mismatch'}
    if any(not isinstance(components.get(key),dict) or components[key].get('status')!='provider_accepted' for key in keys):
        return {'classification':'unverified_receipt','reusable':False,'reason':'component_keys_or_status_mismatch'}
    return {'classification':'verified_for_reuse','reusable':True,'reason':'all_delivery_binding_fields_verified'}

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
            elif item['kind']!='text' or len(item['text'].encode())>(4096 if item.get('message_format')=='markdown' else 2048):
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
    def target_binding_digest(self,items):
        """Hash every logical account's verified acceptance destination."""
        entries=[];seen=set()
        for item in items:
            account=item['account'];channel=item.get('acceptance_channel')
            marker=account
            if marker in seen:continue
            child=self.child(item);target=child.target_map.get(account)
            entries.append(_target_descriptor(target))
            seen.add(marker)
        entries={json.dumps(value,sort_keys=True,separators=(',',':')):value for value in entries}
        entries=[entries[key] for key in sorted(entries)]
        return workflow.wf.digest(['datasage-acceptance-target-binding/v1',entries])
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

def deliver_batch(profile,case_id,notices,*,cycle_id=None,period=None,generation=None,phase=None):
    if not re.fullmatch(r'[a-z0-9_-]{1,64}',case_id):raise workflow.IOErrorBoundary('ACCEPTANCE_CASE_INVALID')
    if not 1<=len(notices)<=2:raise workflow.IOErrorBoundary('ACCEPTANCE_BATCH_NOTIFICATION_LIMIT')
    home=runtime_home(profile)
    with workflow.run_lock(home,'case-'+case_id):
        raw_parts=[part for notice in notices for part in notification_parts(notice,case_id)]
        transport=AcceptanceTransport(profile,raw_parts)
        progress=workflow.Progress(home,'acceptance-'+case_id,case_id)
        progress.run_id=case_id
        parts=transport.normalize(raw_parts,progress)
        transport.preflight(parts)
        attachment_digests={}
        for item in parts:
            if item['kind']!='file':continue
            child=transport.child(item);snapshot=child.files.get(item['key'])
            if not snapshot:raise workflow.IOErrorBoundary('DELIVERY_CONTENT_BINDING_INVALID')
            attachment_digests[str(item['path'])]=hashlib.sha256(snapshot[1]).hexdigest()
        context=delivery_context(cycle_id or case_id,period=period,generation=generation,phase=phase)
        binding=make_delivery_binding(cycle_id=context['cycle_id'],case_id=case_id,period=context['period'],generation=context['generation'],phase=context['phase'],target_binding_digest=transport.target_binding_digest(parts),content_digest=content_manifest_digest(notices,attachment_digests),component_keys=[part['key'] for part in parts],progress_scope=progress.scope)
        existing_binding=progress.data.get('delivery_binding')
        if existing_binding is None:
            if (progress.data.get('components') or progress.data.get('notification_manifests') or
                    progress.data.get('notification_history')):
                raise workflow.IOErrorBoundary('DELIVERY_BINDING_LEGACY_REVIEW_REQUIRED')
        elif existing_binding!=binding:
            raise workflow.IOErrorBoundary('DELIVERY_BINDING_CHANGED_REVIEW_REQUIRED')
        progress.data['delivery_binding']=binding
        workflow.operations._atomic(progress.path,progress.data)
        workflow.deliver_components(parts,transport,progress,enabled=True,force=False)
        receipt={'status':'provider_accepted_not_human_read','case_id':case_id,'logical_notifications':len(notices),
            'components':len(parts),'progress_file':str(progress.path),'production_state_changed':False,'delivery_binding':binding,
            }
        return receipt
