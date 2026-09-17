"""Finite legacy I/O adapters. Activation is separate from preview and default off.

No generic SQL/target endpoint or message queue. Business progress is local,
scoped and crash-conservative; the finite app HTTP path also works in cron children.
"""
from pathlib import Path
from contextlib import contextmanager,nullcontext
from datetime import datetime
import json,os,uuid,time
from . import legacy_workflow as wf,operations

class IOErrorBoundary(wf.WorkflowError):pass

def require_action(job,action):
    from .contract_store import profile_root
    binding=load_activation(profile_root(),job,for_registration=action=='register_schedule_enabled')
    if binding.get(action) is not True:raise IOErrorBoundary('WORKFLOW_ACTION_NOT_ENABLED')
    return binding

def load_activation(profile,job,*,for_registration=False):
    from .local_report import load_bindings,_select_binding,_assert_local_context
    _assert_local_context()
    report_id=wf.policy()['jobs'][job]['report_id']
    try:_,binding=_select_binding(load_bindings(profile),report_id)
    except Exception as exc:raise IOErrorBoundary('WORKFLOW_EXECUTION_NOT_ENABLED') from exc
    allowed={'kind','enabled','read_enabled','customer_mapping_enabled','freeze_enabled','send_enabled','price_accept_enabled','register_schedule_enabled','schedule','failure_deliver','recipients_file','target_map','operation'}
    if not isinstance(binding,dict) or set(binding)-allowed or binding.get('kind')!='legacy_execution':raise IOErrorBoundary('WORKFLOW_ACTIVATION_INVALID')
    for key in ('enabled','read_enabled','customer_mapping_enabled','freeze_enabled','send_enabled','price_accept_enabled','register_schedule_enabled'):
        if type(binding.get(key,False)) is not bool:raise IOErrorBoundary('WORKFLOW_ACTIVATION_INVALID')
    if for_registration:
        if binding.get('register_schedule_enabled') is not True:raise IOErrorBoundary('SCHEDULE_REGISTRATION_NOT_ENABLED')
    elif binding.get('enabled') is not True or binding.get('read_enabled') is not True:raise IOErrorBoundary('WORKFLOW_EXECUTION_NOT_ENABLED')
    if binding.get('freeze_enabled') and job!='slow_task':raise IOErrorBoundary('FREEZE_ACTION_NOT_ALLOWED')
    if binding.get('price_accept_enabled') and job not in ('idk','sales_price','purchase_price'):raise IOErrorBoundary('PRICE_ACCEPT_ACTION_NOT_ALLOWED')
    if binding.get('price_accept_enabled') and job in ('sales_price','purchase_price') and (binding.get('operation') or {}).get('reference_source','legacy_database')=='legacy_database':raise IOErrorBoundary('LEGACY_DATABASE_REFERENCE_IS_READ_ONLY')
    if binding.get('send_enabled') and job in ('slow_task','sales_price') and binding.get('customer_mapping_enabled') is not True:raise IOErrorBoundary('CUSTOMER_MAPPING_NOT_ENABLED_FOR_LEGACY_DELIVERY')
    return binding

def private_root(profile):
    root=profile/'report_runs'/'legacy_execution'
    for path in (profile/'report_runs',root):
        if path.is_symlink() or not path.resolve().is_relative_to(profile.resolve()):raise IOErrorBoundary('WORKFLOW_STATE_PATH_INVALID')
    root.mkdir(parents=True,exist_ok=True)
    return root

class Progress:
    """Same business component scope as legacy; not a transport retry queue."""
    def __init__(self,profile,job,period):
        self.root=private_root(profile);self.path=self.root/(wf.digest([job,period])+'.json')
        self.scope={'job':job,'period':period};self.data={'scope':self.scope,'components':{}}
        if self.path.is_symlink():raise IOErrorBoundary('WORKFLOW_STATE_PATH_INVALID')
        if self.path.exists():
            self.data=json.loads(self.path.read_text(encoding='utf-8'))
            if self.data.get('scope')!=self.scope or not isinstance(self.data.get('components'),dict):raise IOErrorBoundary('WORKFLOW_STATE_CORRUPT')
    def status(self,key):return self.data['components'].get(key,{}).get('status','not_attempted')
    def set(self,key,status,**evidence):
        self.data['components'][key]={'status':status,'run_id':getattr(self,'run_id',None),**evidence};operations._atomic(self.path,self.data)

@contextmanager
def run_lock(profile,job):
    path=private_root(profile)/(job+'.lock')
    try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise IOErrorBoundary('WORKFLOW_BUSY_OR_STALE_LOCK_REVIEW_REQUIRED')
    try:os.close(fd);yield
    finally:path.unlink()

def read_recipients(profile,binding):
    # The old filename is a parameter compatibility alias, never a second source.
    if binding.get('recipients_file') not in ('local/workflow-roles.json','legacy-recipients.json'):
        raise IOErrorBoundary('RECIPIENT_CONFIGURATION_REQUIRED')
    from . import workflow_roles
    try:return workflow_roles.load(profile)
    except workflow_roles.RoleConfigurationError as exc:raise IOErrorBoundary(exc.code) from exc

def cached_plan(profile,kind,week):
    if kind not in ('recipients','customers'):raise IOErrorBoundary('PLAN_KIND_INVALID')
    path=private_root(profile)/(kind+'-'+week+'.json')
    if path.is_symlink():raise IOErrorBoundary('WORKFLOW_STATE_PATH_INVALID')
    if not path.exists():return None
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('week')!=week or value.get('version')!=(2 if kind=='recipients' else 4):raise IOErrorBoundary('CACHED_PLAN_SCOPE_INVALID')
    return value

def baseline_digest(rows):
    # This fingerprint follows the legacy grouped-stock representation. It is
    # a cache identity, not a replacement for precise business calculations.
    grouped={}
    for row in rows:
        key=(row['whse_dept'],str(row['goods_no']).strip(),str(row.get('attr_val') or '').strip())
        value=wf.number(row.get('total_piece'))
        if value is None:raise IOErrorBoundary('CUSTOMER_BASELINE_ROLLS_UNKNOWN')
        grouped[key]=grouped.get(key,0.0)+float(value)
    return wf.digest([list(k)+[v] for k,v in sorted(grouped.items())])

def persist_plan(profile,kind,week,value):
    operations._atomic(private_root(profile)/(kind+'-'+week+'.json'),value)

class OfficialTransport:
    """Concrete official live-gateway adapter bridge, with no standalone fallback.

    The installed WS standalone sender can open a second socket and ignores its
    media_files argument. Refuse that route instead of disturbing the Q&A bot.
    """
    def __init__(self,job):
        self.job=job;self.target_map=require_action(job,'send_enabled').get('target_map') or {};self.targets={}
    def preflight(self,components):
        from gateway.run import _gateway_runner_ref
        from gateway.platforms.base import BasePlatformAdapter
        runner=_gateway_runner_ref() if callable(_gateway_runner_ref) else None
        if runner is None:raise IOErrorBoundary('OFFICIAL_LIVE_GATEWAY_REQUIRED_NO_STANDALONE_FALLBACK')
        for item in components:
            target=self.target_map.get(item['account'])
            if not isinstance(target,dict) or set(target)-{'platform','chat_id','app_name'} or target.get('platform') not in ('wecom','wecom_callback') or not target.get('chat_id'):
                raise IOErrorBoundary('EXPLICIT_OFFICIAL_TARGET_MAPPING_REQUIRED')
            adapter=next((a for p,a in runner.adapters.items() if getattr(p,'value',p)==target['platform']),None)
            if adapter is None:raise IOErrorBoundary('OFFICIAL_TARGET_PLATFORM_NOT_CONNECTED')
            if target['platform']=='wecom_callback':
                app=adapter._resolve_app_for_chat(target['chat_id'])
                if not target.get('app_name') or app.get('name')!=target['app_name'] or not target['chat_id'].startswith(str(app.get('corp_id'))+':'):raise IOErrorBoundary('CALLBACK_APP_TARGET_NOT_EXPLICITLY_RESOLVED')
            if item['kind']=='file' and (getattr(type(adapter),'send_document',None) is None or getattr(type(adapter),'send_document') is BasePlatformAdapter.send_document):raise IOErrorBoundary('OFFICIAL_PLATFORM_FILE_UNSUPPORTED')
            if target['platform']=='wecom_callback' and item['kind']=='text' and len(item['text'])>2048:raise IOErrorBoundary('CALLBACK_TEXT_WOULD_BE_TRUNCATED')
            if target['platform']=='wecom' and item['kind']=='text' and len(item['text'])>4000:raise IOErrorBoundary('OFFICIAL_MESSAGE_LIMIT_REQUIRES_REPORT_SPLIT')
            if target['platform']=='wecom' and target['chat_id'] in getattr(adapter,'_group_chat_ids',set()) and not adapter._cached_reply_req_id(target['chat_id'],None):raise IOErrorBoundary('OFFICIAL_GROUP_PASSIVE_CONTEXT_MISSING')
            self.targets[item['account']]=(runner,adapter,target['chat_id'])
    def send(self,item):
        import asyncio
        from tools.send_message_tool import _dispatch_on_gateway_loop
        from model_tools import _run_async
        if require_action(self.job,'send_enabled').get('target_map')!=self.target_map:raise IOErrorBoundary('DELIVERY_CONFIGURATION_CHANGED')
        runner,adapter,chat_id=self.targets[item['account']]
        async def send():
            if item['kind']=='file':return await adapter.send_document(chat_id=chat_id,file_path=item['path'],file_name=Path(item['path']).name)
            return await adapter.send(chat_id=chat_id,content=item['text'],metadata={'force_proactive_send':True})
        return _run_async(_dispatch_on_gateway_loop(runner,send,'DataSage official workflow delivery'))

def deliver_components(components,transport,progress,*,enabled=False,force=False):
    if enabled is not True:raise IOErrorBoundary('WORKFLOW_SEND_NOT_ENABLED')
    if hasattr(transport,'normalize'):components=transport.normalize(components,progress)
    if len({i['key'] for i in components})!=len(components):raise IOErrorBoundary('DUPLICATE_DELIVERY_COMPONENT')
    if any(progress.status(i['key']) in ('in_flight','unknown','unverified_success','not_delivered') for i in components):raise IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
    transport.preflight(components) # All capabilities/targets checked before the first send.
    lock=transport.delivery_lock(progress) if hasattr(transport,'delivery_lock') else nullcontext()
    with lock:
        return _deliver_notifications(components,transport,progress,force=force)


def _deliver_notifications(components,transport,progress,*,force=False):
    intent=[{k:item[k] for k in ('account','kind','stage','key')} for item in components]
    operations._atomic(progress.root/('intent-'+wf.digest(intent)+'.json'),{'scope':progress.scope,'components':intent,'purpose':'audit_manifest_not_a_send_queue'})
    groups={}
    # An incomplete generation is a recovery run, not a new business force run.
    # Do not resend its already completed notifications while repairing another.
    force=force and not any(len(k)==64 and v.get('status')=='failed' for k,v in progress.data['components'].items())
    for item in components:
        groups.setdefault(item.get('notification_key') or wf.digest([item['account']]),[]).append(item)
    pending=[];fingerprints={}
    for notification,items in groups.items():
        items.sort(key=lambda i:i['kind']!='text')
        complete=all(progress.status(i['key'])=='provider_accepted' for i in items)
        same_run=getattr(progress,'run_id',None) is not None and all(progress.data['components'].get(i['key'],{}).get('run_id')==progress.run_id for i in items)
        resend=bool(force and complete and not same_run)
        for item in items:
            old=progress.data['components'].get(item['key'],{})
            fingerprint=transport.fingerprint(item) if hasattr(transport,'fingerprint') else None
            fingerprints[item['key']]=fingerprint
            # A regenerated report/changed destination must not be combined with
            # an already accepted component of an incomplete notification.
            if (not complete and old.get('fingerprint') and old['fingerprint']!=fingerprint):
                raise IOErrorBoundary('NOTIFICATION_CONTENT_CHANGED_REVIEW_REQUIRED')
        if hasattr(transport,'fingerprint'):
            manifest=wf.digest([[i['key'],fingerprints[i['key']]] for i in items])
            manifests=progress.data.setdefault('notification_manifests',{})
            if notification in manifests and manifests[notification]!=manifest and not resend:
                raise IOErrorBoundary('NOTIFICATION_CONTENT_CHANGED_REVIEW_REQUIRED')
            manifests[notification]=manifest
        if resend:
            history=progress.data.setdefault('notification_history',[])
            history.append({'notification':notification,'components':{i['key']:progress.data['components'][i['key']] for i in items}})
            # Persist the entire new generation before its first send. A crash or
            # failed file in this generation must not reuse last generation's file.
            for item in items:progress.data['components'][item['key']]={'status':'not_attempted','run_id':getattr(progress,'run_id',None)}
            operations._atomic(progress.path,progress.data)
        pending.extend(i for i in items if progress.status(i['key'])!='provider_accepted')
    operations._atomic(progress.path,progress.data)
    if pending and hasattr(transport,'prepare'):transport.prepare(pending,progress)
    failed=False
    for notification,items in groups.items():
        if not any(progress.status(i['key'])!='provider_accepted' for i in items):continue
        if hasattr(transport,'begin_notification'):transport.begin_notification(items,progress)
        notification_status='provider_accepted'
        for item in items:
            key=item['key']
            if progress.status(key)=='provider_accepted':continue
            progress.set(key,'in_flight',fingerprint=fingerprints[key])
            try:receipt=wf.official_receipt(transport.send(item),key)
            except Exception:receipt={'status':'unknown','human_received':'unknown'}
            progress.set(key,receipt['status'],fingerprint=fingerprints[key],human_received='unknown',provider_message_id=receipt.get('provider_message_id'),api_errcode=receipt.get('api_errcode'))
            if receipt['status']!='provider_accepted':
                notification_status=receipt['status'];failed=True
                break # A failed/unknown text must never be followed by its file.
        progress.set('notification-'+notification,notification_status,human_received='unknown',component_keys=[i['key'] for i in items])
        if hasattr(transport,'finish_notification'):transport.finish_notification(items,progress,notification_status)
        if notification_status in ('unknown','unverified_success','not_delivered'):raise IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
    if failed:raise IOErrorBoundary('DELIVERY_COMPONENT_FAILED')


def make_transport(profile,job,binding):
    targets=binding.get('target_map') or {}
    platforms={t.get('platform') for t in targets.values() if isinstance(t,dict)}
    if 'wecom_app_http' in platforms:
        if platforms!={'wecom_app_http'}:raise IOErrorBoundary('MIXED_DELIVERY_TRANSPORTS_NOT_ALLOWED')
        from .wecom_app_transport import AppTransport
        return AppTransport(job,profile)
    return OfficialTransport(job)

def open_freeze_writer(*,enabled=False):
    if enabled is not True:raise IOErrorBoundary('FREEZE_NOT_ENABLED')
    require_action('slow_task','freeze_enabled')
    from agent.secret_scope import get_secret
    from . import db_runtime,db_security,contract_store,tools
    # Intentionally distinct credentials; never fall back to DATA_QUERY user/password.
    required={k:get_secret('DATASAGE_FREEZE_MYSQL_'+k.upper(),'') for k in ('host','user','password')}
    if not all(required.values()):raise IOErrorBoundary('SEPARATE_FREEZE_CREDENTIALS_NOT_CONFIGURED')
    if required['host']!=get_secret('DATA_QUERY_MYSQL_HOST','') or required['user']==get_secret('DATA_QUERY_MYSQL_USER',''):raise IOErrorBoundary('FREEZE_IDENTITY_CONFIGURATION_INVALID')
    expected,cut,_=tools._execute_with_source('SELECT @@server_uuid AS server_uuid',[],1)
    if cut or len(expected)!=1:raise IOErrorBoundary('FREEZE_SOURCE_IDENTITY_UNAVAILABLE')
    driver=db_runtime.load_pymysql();policy=db_security.mysql_tls_policy(profile_root=contract_store.profile_root())
    conn=driver.connect(**required,port=db_runtime.connection_port(),database='vk_ai',charset='utf8mb4',autocommit=True,connect_timeout=8,read_timeout=30,write_timeout=30,cursorclass=driver.cursors.DictCursor,**db_security.mysql_tls_kwargs(policy=policy))
    try:
        db_security.verify_mysql_tls(conn,policy=policy)
        with conn.cursor() as cur:
            cur.execute('SELECT @@server_uuid AS server_uuid,DATABASE() AS database_name,CURRENT_USER() AS account')
            actual=cur.fetchone()
        if actual['server_uuid']!=expected[0]['server_uuid'] or actual['database_name']!='vk_ai' or actual['account'].split('@',1)[0]!=required['user']:raise IOErrorBoundary('FREEZE_SOURCE_IDENTITY_MISMATCH')
        return conn
    except Exception:conn.close();raise

def freeze_current_week(snapshot_factory,writer_factory,progress,*,enabled=False,refreeze=True):
    """Only this fixed action. Caller cannot supply rows, SQL, table, week or IDs."""
    if enabled is not True:raise IOErrorBoundary('FREEZE_NOT_ENABLED')
    require_action('slow_task','freeze_enabled')
    if progress.status('freeze') in ('in_flight','unknown'):raise IOErrorBoundary('FREEZE_UNKNOWN_REVIEW_REQUIRED')
    conn=writer_factory();locked=False;committing=False;committed=False
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT NOW(6) AS read_at');clock=cur.fetchone()['read_at']
            y,w,_=datetime.fromisoformat(str(clock)).isocalendar();week=f'{y}-W{w:02d}'
            if progress.scope['period']!=week:raise IOErrorBoundary('FREEZE_PERIOD_CHANGED_REVIEW_REQUIRED')
            lock='datasage_slow_baseline_'+week
            cur.execute('SELECT GET_LOCK(%s,10) AS acquired',(lock,));locked=cur.fetchone()['acquired']==1
            if not locked:raise IOErrorBoundary('FREEZE_LOCK_BUSY')
        with snapshot_factory() as db:
            specs=wf.source_query_specs(week);source,cut,_=db.execute(specs[0]['sql'],specs[0]['params'],10000);existing,cut2,_=db.execute(specs[1]['sql'],specs[1]['params'],10000)
        if cut or cut2:raise IOErrorBoundary('FREEZE_SOURCE_INCOMPLETE')
        plan=wf.freeze_plan(source,existing,week,week,refreeze=refreeze)
        if plan['status']!='ready':raise IOErrorBoundary('FREEZE_PLAN_BLOCKED')
        if plan['action']=='reuse_existing':return existing
        progress.set('freeze','in_flight',week=week)
        conn.begin()
        with conn.cursor() as cur:
            cur.execute('DELETE FROM vk_ai.slow_moving_baseline WHERE week_label=%s',(week,))
            row_marks='('+','.join(['%s']*len(wf.BASELINE_COLUMNS))+')'
            values=[r[k] for r in plan['insert_rows'] for k in wf.BASELINE_COLUMNS]
            cur.execute('INSERT INTO vk_ai.slow_moving_baseline ('+','.join(wf.BASELINE_COLUMNS)+') VALUES '+','.join([row_marks]*len(plan['insert_rows'])),values)
            cur.execute('SELECT '+','.join(wf.BASELINE_COLUMNS)+',frozen_at FROM vk_ai.slow_moving_baseline WHERE week_label=%s',(week,));saved=cur.fetchall()
            if len(saved)!=plan['insert_count'] or len({r['source_row_id'] for r in saved})!=len(saved) or any(r['baseline_version']!=2 or r['source_table']!='vk_ods.slow_moving_goods_ods' or r.get('frozen_at') is None for r in saved) or len({str(r['frozen_at']) for r in saved})!=1:raise IOErrorBoundary('FREEZE_POSTWRITE_VERIFICATION_FAILED')
            verified=wf.freeze_plan([{**r,'id':r['source_row_id'],'goods_num':r['total_qty'],'piece_num':r['total_piece'],'is_whitelist':'n'} for r in saved],[],week,week)
            if verified['status']!='ready' or verified.get('insert_count')!=len(saved) or datetime.fromisoformat(str(saved[0]['frozen_at'])).isocalendar()[:2]!=(y,w):raise IOErrorBoundary('FREEZE_POSTWRITE_VERIFICATION_FAILED')
        committing=True;conn.commit();committed=True;committing=False
        progress.set('freeze','committed',week=week,row_count=len(saved));return saved
    except Exception:
        try:conn.rollback()
        except Exception:pass
        if progress.status('freeze')=='in_flight':progress.set('freeze','unknown' if committing or committed else 'failed')
        raise
    finally:
        if locked:
            try:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
            except Exception:pass
        conn.close()

def create_paused_official_job(job,*,enabled=False):
    """Real official registration adapter, never invoked without a separate opt-in."""
    if enabled is not True:raise IOErrorBoundary('SCHEDULE_REGISTRATION_NOT_ENABLED')
    binding=require_action(job,'register_schedule_enabled')
    spec=wf.policy()['jobs'][job];schedule=spec['schedule']
    if not schedule or schedule=='hourly':schedule=binding.get('schedule')
    if not isinstance(schedule,str) or not schedule:raise IOErrorBoundary('EXACT_LEGACY_SCHEDULE_MISSING')
    from tools.cronjob_tools import cronjob
    from cron.jobs import _hermes_now
    from datetime import timedelta
    if _hermes_now().utcoffset()!=timedelta(hours=8):raise IOErrorBoundary('OFFICIAL_CRON_TIMEZONE_NOT_UTC8')
    listing=json.loads(cronjob(action='list',include_disabled=True))
    if 'jobs' not in listing:raise IOErrorBoundary('OFFICIAL_JOB_LIST_UNAVAILABLE')
    matches=[r for r in listing['jobs'] if r.get('name')==spec['report_id']]
    if len(matches)>1:raise IOErrorBoundary('DUPLICATE_OFFICIAL_JOB_NAME')
    if matches:
        from cron.jobs import get_job
        existing=get_job(matches[0].get('job_id')) or {}
        if matches[0].get('enabled') is not False or matches[0].get('state')!='paused' or existing.get('script')!=f'datasage_legacy_{job}.py' or existing.get('schedule',{}).get('expr')!=schedule or existing.get('no_agent') is not True or existing.get('deliver')!='local':raise IOErrorBoundary('EXISTING_JOB_REQUIRES_REVIEW_NO_CHANGE')
        return json.dumps({'success':True,'existing_job':matches[0],'created':False})
    return cronjob(action='create',name=spec['report_id'],schedule=schedule,script=f'datasage_legacy_{job}.py',no_agent=True,deliver='local',failure_deliver=binding.get('failure_deliver'),paused=True,paused_reason='Awaiting individual business acceptance')

def component(account,kind,payload,scope,stage):
    item={'account':account,'kind':kind,'stage':stage,'key':wf.digest([scope,account,kind,stage]),'notification_key':wf.digest([scope,account])}
    item['path' if kind=='file' else 'text']=str(payload)
    if kind=='text' and stage in ('weekly_text','monthly_text','idk','purchase'):item['message_format']='markdown'
    return item

def price_reference_disclosures(components,note):
    if not note:return components
    seen=set();result=[]
    for item in components:
        identity=item['notification_key']
        if identity not in seen:
            result.append({'account':item['account'],'kind':'text','stage':'legacy_reference_disclosure',
                'key':wf.digest([identity,'legacy_reference_disclosure']),'notification_key':identity,
                'text':'【价格参考口径】'+note})
            seen.add(identity)
        result.append(item)
    return result

def run_bound(profile,job,*,transport=None,writer_factory=None,snapshot_factory=None):
    """Production input -> existing rules/artifacts -> separately permitted I/O."""
    from .contract_store import profile_root
    if profile.resolve()!=profile_root().resolve():raise IOErrorBoundary('WORKFLOW_PROFILE_MISMATCH')
    binding=load_activation(profile,job) # Before credentials, sockets, state or artifacts.
    from .local_report import configure_runtime
    from . import tools,wire,workflow_inputs as inputs
    configure_runtime(profile)
    snapshot_factory=snapshot_factory or (lambda:tools._ConsistentSnapshotExecutor(deadline_at=time.monotonic()+120))
    transport=transport or (make_transport(profile,job,binding) if binding.get('send_enabled') else None)
    if binding.get('send_enabled'):
        # Avoid committing a freeze before discovering the official transport cannot
        # deliver the necessary file type or target namespace.
        targets=binding.get('target_map') or {}
        if not targets:raise IOErrorBoundary('EXPLICIT_OFFICIAL_TARGET_MAPPING_REQUIRED')
        transport.preflight([{'account':a,'kind':'file' if job in ('slow_task','slow_report','sales_price','fabric') else 'text','text':''} for a in targets])
    with run_lock(profile,job):
        with snapshot_factory() as db:
            clock=inputs.complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at']
        at=datetime.fromisoformat(str(clock));y,w,_=at.isocalendar();week=f'{y}-W{w:02d}';month=at.strftime('%Y-%m')
        progress=Progress(profile,job,'price-events' if job in ('sales_price','purchase_price') else week)
        run_dir=private_root(profile)/(job+'-'+uuid.uuid4().hex);run_dir.mkdir()
        progress.run_id=run_dir.name
        try:
            result=_produce_and_execute(profile,job,binding,run_dir,week,month,progress,snapshot_factory,transport,writer_factory)
            operations._atomic(run_dir/'run.json',result);return result
        except Exception as exc:
            operations._atomic(run_dir/'run.json',{'status':'failed_or_unknown','run_id':run_dir.name,'code':str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__,'freeze_progress':progress.data['components'].get('freeze'),'no_cross_system_rollback_claim':True})
            raise

def _produce_and_execute(profile,job,binding,out,week,month,progress,snapshots,transport,writer_factory):
    from . import tools,wire,workflow_inputs as inputs
    from .legacy_xlsx import gen_workbook_xlsx
    components=[];outputs=[];raw_document=None;raw_path=None
    recipients=read_recipients(profile,binding) if job in ('slow_report',) or job in ('slow_task','sales_price') and binding.get('customer_mapping_enabled') or job=='idk' and binding.get('send_enabled') else {'regions':{} }
    if job=='slow_task':
        if binding.get('freeze_enabled'):
            baseline=freeze_current_week(snapshots,writer_factory or (lambda:open_freeze_writer(enabled=True)),progress,enabled=True,refreeze=True)
        else:
            with snapshots() as db:
                spec=wf.source_query_specs(week)[1];baseline=inputs.complete(db,spec['sql'],spec['params'])
                read_at=datetime.fromisoformat(str(inputs.complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at']))
            if not baseline:raise IOErrorBoundary('CURRENT_WEEK_BASELINE_MISSING_NO_FREEZE')
            if len({str(r.get('frozen_at')) for r in baseline})!=1 or any(r.get('frozen_at') is None for r in baseline):raise IOErrorBoundary('BASELINE_CLOCK_INCONSISTENT')
            frozen=datetime.fromisoformat(str(baseline[0]['frozen_at']))
            if frozen>read_at or frozen.isocalendar()[:2]!=read_at.isocalendar()[:2]:raise IOErrorBoundary('BASELINE_CURRENT_WEEK_CLOCK_MISMATCH')
            checked=wf.freeze_plan([{**r,'id':r['source_row_id'],'goods_num':r['total_qty'],'piece_num':r['total_piece'],'is_whitelist':'n'} for r in baseline],[],week,week)
            if checked['status']!='ready' or checked.get('insert_count')!=len(baseline) or any(r.get('baseline_version')!=2 or r.get('source_table')!='vk_ods.slow_moving_goods_ods' for r in baseline):raise IOErrorBoundary('BASELINE_IDENTITY_OR_COVERAGE_INVALID')
        regions=list(dict.fromkeys(r['whse_dept'] for r in baseline))
        if not binding.get('customer_mapping_enabled'):
            periods=wf.legacy_periods(at_utc8(week))
            for region in regions:
                message,sheet=wf.task_draft(region,week,periods['planned_start'][:10],periods['planned_end'][:10],[r for r in baseline if r['whse_dept']==region])
                path=out/f'{week}_{wf.safe_name(region)}_Products.xlsx';gen_workbook_xlsx([sheet],path);outputs.append(str(path))
            return {'status':'success','job':job,'artifacts':outputs,'freeze_status':progress.status('freeze'),'customer_mapping':'not_enabled','delivery':'not_requested'}
        recipient_cache=cached_plan(profile,'recipients',week)
        with snapshots() as db:
            if recipient_cache is None:
                spec=wf.source_query_specs(week)[2];employees=inputs.complete(db,spec['sql'],spec['params'])
                targets=wf.task_recipients(recipients['regions'],employees,regions)
                recipient_cache={'version':2,'week':week,'regions':targets};persist_plan(profile,'recipients',week,recipient_cache)
            if any(not recipient_cache['regions'].get(r) for r in regions):raise IOErrorBoundary('CACHED_RECIPIENT_REGION_MISSING')
        periods=wf.legacy_periods(at_utc8(week))
        for region in regions:
            text,sheet=wf.task_draft(region,week,periods['planned_start'][:10],periods['planned_end'][:10],[r for r in baseline if r['whse_dept']==region])
            path=out/f'{week}_{wf.safe_name(region)}_Products.xlsx';gen_workbook_xlsx([sheet],path);outputs.append(str(path))
            targets=recipient_cache['regions'][region]
            for target in targets:
                components += [component(target['account'],'text',text,week+region,'task_text'),component(target['account'],'file',path,week+region,'task_file')]
        # Delivery stage failure aborts before customer packages, matching the wrapper.
        if binding.get('send_enabled'):deliver_components(components,transport,progress,enabled=True,force=True)
        customer_cache=cached_plan(profile,'customers',week);fingerprint=baseline_digest(baseline)
        if customer_cache and not binding.get('freeze_enabled') and customer_cache.get('baseline_digest')!=fingerprint:raise IOErrorBoundary('CACHED_CUSTOMER_BASELINE_MISMATCH')
        if customer_cache is None or binding.get('freeze_enabled'):
            with snapshots() as db:mapping=inputs.customer_mapping(db,[(r['goods_no'],r['whse_dept']) for r in baseline])
            customer_cache={'version':4,'week':week,'baseline_digest':fingerprint,'plan':wf.contact_plan(baseline,mapping)}
            persist_plan(profile,'customers',week,customer_cache)
        components=[];zip_status={};package_errors=[]
        for package in customer_cache['plan']['sales_packages']:
            try:
                scope=wf.digest([week,package['account'],package['customers']]);archive=wf.customer_zip(package,out,week);outputs.append(str(archive))
                components=[component(package['account'],'text',wf.customer_package_message(package,week),scope,'customer_summary'),component(package['account'],'file',archive,scope,'customer_zip')]
                if binding.get('send_enabled'):
                    deliver_components(components,transport,progress,enabled=True,force=True);zip_status[package['account']]='provider_accepted'
                else:zip_status[package['account']]='not_requested'
            except Exception as exc:
                code=str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__
                zip_status[package['account']]='unknown' if 'UNKNOWN' in code else 'failed';package_errors.append(code)
        # The audit must describe confirmed component evidence, not preview labels.
        for audit in dispatch_audits(customer_cache['plan']['sales_packages'],zip_status,recipients['regions'],week,out,preview=not binding.get('send_enabled')):
            outputs.append(str(audit['path']))
            if binding.get('send_enabled'):
                for target in audit['targets']:
                    components=[component(target,'text',audit['text'],audit['scope'],'audit_text'),component(target,'file',audit['path'],audit['scope'],'audit_file')]
                    try:deliver_components(components,transport,progress,enabled=True)
                    except IOErrorBoundary as exc:package_errors.append(str(exc))
        if package_errors:raise IOErrorBoundary('CUSTOMER_DELIVERY_PARTIAL_OR_UNKNOWN')
        return {'status':'success','job':job,'artifacts':outputs,'freeze_status':progress.status('freeze'),'delivery':'provider_accepted_not_human_read' if binding.get('send_enabled') else 'not_requested'}
    if job=='slow_report':
        from . import report_evidence
        for period_name in ('weekly','monthly'):
            period=month if period_name=='monthly' else week
            collected={region:report_evidence.collect(region,period,period_name,week,snapshots=snapshots) for region in wf.policy()['regions']}
            # Validate every region of this phase before its first artifact/send.
            reports={}
            for region,(evidence,label_rows) in collected.items():
                packets=evidence['packets']
                reports[region]=inputs.legacy_report_packet(packets['pool'],packets['flow'],label_rows,region,period,evidence=evidence)
                if not reports[region]['label_coverage']['complete']:raise IOErrorBoundary('REPORT_LABEL_REVIEW_REQUIRED')
            for region,adapted in reports.items():
                text=wf.report_draft(region,adapted['period'],adapted['summary'],adapted['sales_rows'],monthly=period_name=='monthly')
                text+='\n'+adapted['completeness']['as_of_label']+'：'+adapted['completeness']['observed_to']+'\nSKU counts use registered SKU/department/unit groups.'
                operations._atomic(out/(region+'_'+period_name+'_evidence.json'),adapted)
                path=out/f'{region}_{period_name}.xlsx';gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,adapted['detail_rows'])],path,borders=True,landscape=True);outputs.append(str(path))
                for target in wf.report_recipients(recipients['regions'],region):
                    components=[component(target,'text',text,(month if period_name=='monthly' else week)+region,period_name+'_text'),component(target,'file',path,(month if period_name=='monthly' else week)+region,period_name+'_file')]
                    if binding.get('send_enabled'):deliver_components(components,transport,progress,enabled=True,force=period_name=='monthly')
        return {'status':'success','job':job,'artifacts':outputs,'delivery':'provider_accepted_not_human_read' if binding.get('send_enabled') else 'not_requested','format':'governed evidence columns; old exact display adapter remains separately reviewable'}
    if job in ('idk','sales_price','purchase_price'):
        kind={'idk':'idk_unpriced','sales_price':'sales_prices','purchase_price':'purchase_prices'}[job]
        op=binding.get('operation') or {'kind':kind,'limit':10000,**({} if job=='idk' else {'regions':['HCM','HN','BKK','IDK']})}
        if job in ('sales_price','purchase_price'):op={**op,'reference_source':op.get('reference_source','legacy_database')}
        if op.get('kind')!=kind:raise IOErrorBoundary('OPERATION_KIND_MISMATCH')
        raw_document=operations.execute(profile,wf.policy()['jobs'][job]['report_id'],op);raw_path=operations.save_observation(profile,wf.policy()['jobs'][job]['report_id'],raw_document)
        data=wf.operation_preview_input(job,raw_document);data['region']='IDK' if job=='idk' else 'HCM'
        if job=='idk':
            data['idk_executors']=recipients['regions'].get('IDK',{}).get('executors',[]);bundle=wf.build_preview(job,data,out)
            components=[component(t['account'],'text',bundle['message_bodies'][0],week,'idk') for t in data['idk_executors']]
        elif job=='purchase_price':
            bundle=wf.build_preview(job,data,out)
            components=[component(a,'text',bundle['message_bodies'][0],wf.digest(data['changes']),'purchase') for a in (binding.get('target_map') or {})] if data['changes'] else []
        else:
            changes=data['changes'];affected=list(dict.fromkeys(r['dept'] for r in changes));bundle={'files':[]}
            if changes and binding.get('customer_mapping_enabled'):
                with snapshots() as db:
                    spec=wf.source_query_specs(week)[2];employees=inputs.complete(db,spec['sql'],spec['params']);mapping=inputs.customer_mapping(db,[(r['goods_no'],r['dept']) for r in changes])
                    executor_accounts=list({r['account'] for v in recipients['regions'].values() for r in v.get('executors',[])})
                    if executor_accounts:
                        employees+=inputs.complete(db,"SELECT wecom_account,position,is_delete FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_account IN ("+','.join('%s' for _ in executor_accounts)+') LIMIT 10001',executor_accounts)
                target_plan=wf.price_recipients(recipients['regions'],employees,affected,recipients.get('price_manager_fixed',[]))
                for region in affected:
                    regional=[r for r in changes if r['dept']==region]
                    buyers={}
                    for cid,products in mapping['productsByCustomer'].items():
                        info=mapping['customerInfo'].get(cid,{})
                        for r in regional:
                            if info.get('customer_no') and any(p['goods_no']==r['goods_no'] and p['whse_dept']==region for p in products):
                                record=(info.get('sales'),info['customer_no'],info.get('name'));group=buyers.setdefault(r['goods_no'],[])
                                if any(x[1]==record[1] and x!=record for x in group):raise IOErrorBoundary('BUYER_CUSTOMER_NUMBER_AMBIGUOUS')
                                if record not in group:group.append(record)
                    for target in target_plan[region]['sales']:
                        own={g:[[n,name] for owner,n,name in rows if mapping['wecomBySales'].get(owner)==target['account']] for g,rows in buyers.items()}
                        personal={**data,'region':region,'sales_name':target['name'],'changes':regional,'customers_by_goods':own,'customer_mapping_complete':True}
                        folder=out/wf.account_token(target['account'])[:12];folder.mkdir(exist_ok=True);b=wf.build_preview(job,personal,folder);scope=wf.digest([regional,own])
                        components.append(component(target['account'],'text',b['message_bodies'][0],scope,'sales_text'))
                        components += [component(target['account'],'file',folder/f,scope,'sales_file') for f in b['files'] if f.endswith('.xlsx')]
                    if buyers:
                        folder=out/(region+'-managers');folder.mkdir();b=wf.build_preview(job,{**data,'region':region,'changes':regional,'manager_rows_by_goods':buyers},folder);scope=wf.digest([regional,buyers])
                        from .legacy_message_templates import sales as manager_message
                        manager_text=manager_message(regional,manager=True,region=region)+"\n\nSee attachment for all sales' customers across this region (one Sheet per product)."
                        for target in target_plan[region]['managers']:
                            components.append(component(target,'text',manager_text,scope,'manager_text'))
                            components += [component(target,'file',folder/f,scope,'manager_file') for f in b['files'] if f.endswith('.xlsx')]
            elif changes:
                wf.build_preview(job,{**data,'customers_by_goods':{},'customer_mapping_complete':False},out)
        components=price_reference_disclosures(components,data.get('comparison_disclosure'))
        if components and binding.get('send_enabled'):deliver_components(components,transport,progress,enabled=True,force=job=='idk')
        if binding.get('price_accept_enabled'):
            operations.accept_snapshot(profile,wf.policy()['jobs'][job]['report_id'],raw_path.stem,expected_scope=operations.scope_fingerprint(op))
        return {'status':'success','job':job,'observation':str(raw_path),'components':len(components),'delivery':'provider_accepted_not_human_read' if components and binding.get('send_enabled') else 'not_requested','baseline_accepted':binding.get('price_accept_enabled',False)}
    if job=='fabric':
        op=binding.get('operation')
        if not isinstance(op,dict) or op.get('kind')!='fabric_review':raise IOErrorBoundary('FABRIC_SCOPE_CONFIGURATION_REQUIRED')
        doc=operations.execute(profile,wf.policy()['jobs'][job]['report_id'],op)
        from .fabric_report import export_report
        from .result_completeness import gate_for_document
        report_gate=gate_for_document(doc)
        files=export_report(doc,out)
        if report_gate['allowed'] and binding.get('send_enabled'):
            scope=wf.digest([op,doc])
            text='货源分析报告已生成，详见随附工作簿。报告保留各来源观察时间；未知项不视为零，接口接受不代表人工已读。'
            components=[part for a in binding['target_map'] for part in (component(a,'text',text,scope,'fabric_text'),component(a,'file',files[0],scope,'fabric_workbook'))]
            deliver_components(components,transport,progress,enabled=True)
        delivery_state=('provider_accepted_not_human_read' if report_gate['allowed'] else 'blocked_incomplete_report') if binding.get('send_enabled') else 'not_requested'
        result_status='partial' if doc['status']=='success' and not report_gate['allowed'] else doc['status']
        return {'status':result_status,'job':job,'artifacts':files,'report_delivery_gate':report_gate,'delivery':delivery_state}
    raise IOErrorBoundary('WORKFLOW_KIND_UNSUPPORTED')

def at_utc8(week):
    from datetime import date,time,timezone,timedelta
    return datetime.combine(date.fromisocalendar(int(week[:4]),int(week[6:]),2),time(9),timezone(timedelta(hours=8)))


def dispatch_audits(packages,statuses,recipient_map,week,out,*,preview=False):
    """Old regional audit membership, with unknown receipt distinguished from failure."""
    from .legacy_xlsx import gen_workbook_xlsx
    result=[]
    for group in wf.audit_groups(packages,statuses,recipient_map,{'not_requested'} if preview else {'provider_accepted'}):
        region=group['region'];regional=group['regional'];confirmed=group['confirmed'];unconfirmed=group['unconfirmed'];selected=group['selected'];sheets=[]
        for p in selected:sheets.append((p['sales_name'],['Customer No','Customer'],[list(r) for r in sorted({(c['customer_no'],c['customer_name']) for c in p['customers']})]))
        suffix='Dispatch' if confirmed else 'Failure';path=out/f'{week}_{wf.safe_name(region)}_Customer_Image_{suffix}.xlsx';gen_workbook_xlsx(sheets,path)
        text=f'Slow sales-stock Customer Image Dispatch\nRegion: {region}\nWeek: {week}\nConfirmed platform acceptance: {len(confirmed)} sales owners\nUnconfirmed/failed: {len(unconfirmed)} sales owners\nHuman receipt and onward customer sharing are not established.'
        if preview:text=f'Customer Image Dispatch PREVIEW\nRegion: {region}\nWeek: {week}\nPrepared owners: {len(confirmed)}\nNothing has been sent.'
        result.append({'targets':group['targets'],'text':text,'path':path,'scope':wf.digest([week,region,selected,{p['account']:statuses.get(p['account']) for p in regional}])})
    return result
