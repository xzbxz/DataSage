"""One additional fixed namespace for real-source acceptance; never production."""
from contextlib import contextmanager
from datetime import datetime
import json,re
from . import workflow_storage as base,operations,tools

PREFIX='ds_test_live_v1_';OWNER='257c7e85-c3c8-4a1b-99ad-4b042dd45cf0'
TABLES={r:'`vk_ai`.`'+PREFIX+r+'`' for r in base.ROLES}
DEPARTMENTS=('HCM','HN','BKK','IDK','HCM-HT','HN-HT','BKK-HT','IDK-HT')
def root():
    path=base.profile()/'report_runs'/'workflow_live'
    if path.is_symlink() or path.parent.is_symlink():raise ValueError('LIVE_STATE_PATH_INVALID')
    path.mkdir(parents=True,exist_ok=True);return path
def save(name,value):
    if not re.fullmatch(r'[a-z0-9_.-]+',name):raise ValueError('LIVE_ARTIFACT_NAME_INVALID')
    operations._atomic(root()/name,value)
def binding():return base.profile()/'workflow-live-runtime.json'
def expected_binding():return {'mode':'real_source_acceptance','enabled':True,'instance':'live-v1','database':'vk_ai','prefix':PREFIX,'owner':OWNER,'server_uuid':base.SERVER,'account':'Cody@%','credential_source':'explicit_existing_DATA_QUERY_MYSQL','production_writes':False,'clock':'database_observation','max_logical_notifications_per_invocation':2,'schedule_enabled':False}
def validate_binding(value):
    if not isinstance(value,dict) or {**value,'schedule_enabled':False}!=expected_binding() or value.get('enabled') is not True or value.get('production_writes') is not False or type(value.get('schedule_enabled')) is not bool:raise ValueError('LIVE_BINDING_MISMATCH_NO_FALLBACK')
def ddl():
    result={}
    for role,sql in base.ddl().items():
        sql=sql.replace(base.TABLES[role],TABLES[role]).replace(base.OWNER,OWNER)
        marker=sql.index(') ENGINE=')
        if role in ('stock_input','monthly_stock_input','slow_baseline'):
            sql=sql[:marker]+',test_scope varchar(48) NOT NULL'+sql[marker:]
            if role=='stock_input':sql=sql.replace('PRIMARY KEY(id)','PRIMARY KEY(test_scope,id)')
            if role=='slow_baseline':sql=sql.replace('scope_source(week_label,source_row_id)','scope_source(test_scope,week_label,source_row_id)')
        if role in ('sales_snapshot','purchase_snapshot'):
            sql=sql[:marker]+',current_record json,reference_kind varchar(16)'+sql[marker:]
        result[role]=sql
    return result
def normalized(role,rows):
    result=base.normalized(role,rows)
    for row in result:
        if isinstance(row.get('current_record'),str):row['current_record']=json.loads(row['current_record'])
    return sorted(result,key=base.canonical)
class Store(base.Store):
    tables=TABLES;prefix=PREFIX;owner=OWNER
    definitions=staticmethod(ddl);binding=staticmethod(binding);validate_binding=staticmethod(validate_binding);save=staticmethod(save)
    def replace_scope(self,role,scope,rows):
        if role not in ('stock_input','monthly_stock_input','price_input'):raise ValueError('LIVE_REPLACE_ROLE_INVALID')
        valid_scope(scope)
        self._write('DELETE FROM '+TABLES[role]+' WHERE test_scope=%s',[scope])
        if rows:self.insert(role,[{**r,'test_scope':scope} for r in rows])
def valid_scope(scope):
    if scope in ('sales','purchase'):return
    if not re.fullmatch(r'(?:hcm|hn|bkk|idk)(?:-ht)?-\d{4}-w\d{2}-g\d{1,3}',scope):raise ValueError('LIVE_SCOPE_INVALID')
@contextmanager
def lock(store,name):
    if not re.fullmatch(r'[a-z0-9-]{1,40}',name):raise ValueError('LIVE_LOCK_INVALID')
    lockname='ds_live_v1_'+name
    if store._read('SELECT GET_LOCK(%s,0) AS acquired',[lockname])[0]['acquired']!=1:raise ValueError('LIVE_WORKFLOW_BUSY')
    try:yield
    finally:
        try:store._read('SELECT RELEASE_LOCK(%s) AS released',[lockname])
        except Exception:pass
def bootstrap():
    store=Store(bootstrap=True)
    try:
        conflicts=store._read('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name IN ('+','.join(['%s']*len(base.ROLES))+')',['vk_ai']+[PREFIX+r for r in base.ROLES])
        if conflicts:store.verify();return {'status':'owned_live_test_objects_verified','created':False}
        for sql in ddl().values():store._write(sql,creating=True)
        store.insert('registry',[{'id':1,'owner_id':OWNER,'manifest_hash':base.digest(ddl())}]);store.verify()
        result={'status':'created','tables':list(TABLES.values()),'mode':'real_source_acceptance'};save('bootstrap.json',result);return result
    finally:store.close()
def mapped(sql,scope):
    valid_scope(scope)
    used=set()
    token=re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|--[^\n]*|/\*[\s\S]*?\*/|(?:`[a-z_]+`|[a-z_]+)\s*\.\s*(?:`[a-z_]+`|[a-z_]+)",re.I)
    def sub(match):
        text=match.group()
        if text.startswith(("'",'"','--','/*')):return text
        logical=re.sub(r'[`\s]','',text).lower();role=base.MAP.get(logical)
        if not role:return text
        used.add(role);return 'live_'+role
    rewritten=token.sub(sub,sql)
    if not used:return rewritten
    definitions=','.join('live_'+role+' AS (SELECT * FROM '+TABLES[role]+" WHERE test_scope='"+scope+"')" for role in sorted(used))
    return 'WITH '+definitions+','+rewritten[5:] if rewritten.startswith('WITH ') else 'WITH '+definitions+' '+rewritten
class Snapshot:
    def __init__(self,scope):valid_scope(scope);self.scope=scope
    def __enter__(self):
        check=Store();check.close()
        self.inner=tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None));self.inner.__enter__();self.marker=self.inner.marker;return self
    def __exit__(self,*args):return self.inner.__exit__(*args)
    def execute(self,sql,args,limit,**kw):return self.inner.execute(mapped(sql,self.scope),args,limit,**kw)
