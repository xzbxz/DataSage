"""Fixed test storage adapter for the Profile workflow runner. Production disabled."""
from pathlib import Path
from contextlib import contextmanager
import sys,importlib,json,re,hashlib
from . import db_runtime as runtime,legacy_workflow as wf,tools,contract_store,operations

def profile():return contract_store.profile_root()
def root():
    home=profile();target=home/'report_runs'/'workflow_v1'
    for path in (home/'report_runs',target):
        if path.is_symlink() or not path.resolve().is_relative_to(home.resolve()):raise ValueError('WORKFLOW_STATE_PATH_INVALID')
    target.mkdir(parents=True,exist_ok=True)
    return target
def schema():return json.loads((Path(__file__).parent/'contracts/workflow-test-store.json').read_text(encoding='utf-8'))['columns']
def binding_path():return profile()/'workflow-runtime.json'
PREFIX='ds_test_profile_v1_'
OWNER='b80f997a-e964-47c8-bdde-bdc58d32bf59'
SERVER='4d705dec-f28f-11ef-9b1d-00163e016c40'
ROLES=('registry','stock_input','monthly_stock_input','slow_baseline','sales_snapshot','purchase_snapshot','price_input','cycles')
TABLES={r:'`vk_ai`.`'+PREFIX+r+'`' for r in ROLES}
MAP={'vk_ods.slow_moving_goods_ods':'stock_input','vk_dw.inventory_barcode_detail_bymonth_dw':'monthly_stock_input','vk_ai.slow_moving_baseline':'slow_baseline'}
MONTH_FIELDS='id month_date goods_id goods_sku_id goods_name goods_no attr_val whse_dept unit goods_num piece_num whse_org whse_type is_handing_sales is_discountable'.split()
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def normalized(role,rows):
    from datetime import datetime
    from decimal import Decimal
    columns=schema()[role]
    result=[]
    for row in rows:
        value=dict(row)
        for c in columns:
            k=c['COLUMN_NAME'];v=value.get(k)
            if v is None:continue
            typ=c['COLUMN_TYPE']
            if typ.startswith('decimal'):value[k]=str(Decimal(str(v)).normalize())
            elif typ=='datetime':value[k]=datetime.fromisoformat(str(v)).isoformat()
            elif typ in ('int','bigint'):value[k]=int(v)
        result.append(value)
    return sorted(result,key=canonical)
def save(name,value):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+',name):raise ValueError('WORKFLOW_ARTIFACT_NAME_INVALID')
    operations._atomic(root()/name,value)
def ddl():
    layout=schema();result={}
    for role in ROLES:
        if role in layout:
            fields=[]
            for c in layout[role]:
                name,typ=c['COLUMN_NAME'],c['COLUMN_TYPE']
                if role=='monthly_stock_input' and name not in MONTH_FIELDS:continue
                if not re.fullmatch(r'[a-z_]+',name) or not re.fullmatch(r'(?:bigint|int|datetime|(?:var)?char\(\d+\)|decimal\(\d+,\d+\))',typ):raise ValueError('UNEXPECTED_SOURCE_TYPE')
                suffix=''
                if role=='slow_baseline' and name=='id':suffix=' NOT NULL AUTO_INCREMENT PRIMARY KEY'
                if role=='slow_baseline' and name=='frozen_at':suffix=' DEFAULT CURRENT_TIMESTAMP'
                fields.append('`'+name+'` '+typ+suffix)
            if role in ('sales_snapshot','purchase_snapshot'):fields+=['test_scope varchar(48) NOT NULL','PRIMARY KEY(test_scope,id)']
            elif role=='stock_input':fields+=['PRIMARY KEY(id)']
            elif role=='slow_baseline':fields+=['UNIQUE KEY scope_source(week_label,source_row_id)']
        elif role=='registry':fields=['id int PRIMARY KEY','owner_id varchar(36) NOT NULL','manifest_hash char(64) NOT NULL']
        elif role=='price_input':fields=['test_scope varchar(48) NOT NULL','side varchar(12) NOT NULL','ordinal int NOT NULL','payload json NOT NULL','PRIMARY KEY(test_scope,side,ordinal)']
        else:fields=['cycle_id varchar(64) PRIMARY KEY','test_scope varchar(48) NOT NULL','status varchar(32) NOT NULL','payload json NOT NULL','updated_at datetime(6) DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)']
        result[role]='CREATE TABLE '+TABLES[role]+' ('+','.join(fields)+") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='datasage-acceptance-owner:"+OWNER+"'"
    return result
def assert_binding(binding):
    expected={'mode':'isolated_acceptance','enabled':True,'instance':'profile-v1','database':'vk_ai','prefix':PREFIX,'owner':OWNER,'server_uuid':SERVER,'account':'Cody@%','credential_source':'explicit_existing_DATA_QUERY_MYSQL','production_writes':False}
    if not isinstance(binding,dict) or binding!=expected or binding.get('enabled') is not True or binding.get('production_writes') is not False:raise ValueError('TEST_BINDING_MISMATCH_NO_FALLBACK')
def map_sql(sql):
    # Lexer consumes strings/comments first, preserving literal lineage labels.
    token=re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|--[^\n]*|/\*[\s\S]*?\*/|(?:`[a-z_]+`|[a-z_]+)\s*\.\s*(?:`[a-z_]+`|[a-z_]+)",re.I)
    def sub(m):
        value=m.group()
        if value.startswith(("'",'"','--','/*')):return value
        key=re.sub(r'[`\s]','',value).lower()
        return TABLES[MAP[key]] if key in MAP else value
    return token.sub(sub,sql)
class Store:
    def __init__(self,*,bootstrap=False):
        path=binding_path()
        if path.is_symlink() or not path.is_file():raise ValueError('TEST_BINDING_REQUIRED')
        assert_binding(json.loads(path.read_text(encoding='utf-8')))
        self.conn=runtime.connect();self.conn.select_db('vk_ai');self.audit=[];self.audit_name='write-audit-'+str(__import__('time').time_ns())+'.json'
        identity=self._read('SELECT CURRENT_USER() AS account,@@server_uuid AS server_uuid,DATABASE() AS db')[0]
        if identity!={'account':'Cody@%','server_uuid':SERVER,'db':'vk_ai'}:self.close();raise ValueError('TEST_SERVER_IDENTITY_MISMATCH')
        if not bootstrap:
            try:self.verify()
            except BaseException:self.conn.close();raise
    def _read(self,sql,args=()):
        with self.conn.cursor() as c:c.execute(sql,args);return list(c.fetchall())
    def verify(self):
        rows=self._read('SELECT table_name,table_comment,engine FROM information_schema.tables WHERE table_schema=%s AND table_name IN ('+','.join(['%s']*len(ROLES))+')',['vk_ai']+[PREFIX+r for r in ROLES])
        if len(rows)!=len(ROLES) or any(r['TABLE_COMMENT']!='datasage-acceptance-owner:'+OWNER or r['ENGINE']!='InnoDB' for r in rows):raise ValueError('TEST_OBJECT_OWNERSHIP_MISMATCH')
        registry=self._read('SELECT * FROM '+TABLES['registry'])
        if registry!=[{'id':1,'owner_id':OWNER,'manifest_hash':digest(ddl())}]:raise ValueError('TEST_REGISTRY_MISMATCH')
        actual=self._read('SELECT table_name,column_name,column_type FROM information_schema.columns WHERE table_schema=%s AND table_name IN ('+','.join(['%s']*len(ROLES))+')',['vk_ai']+[PREFIX+r for r in ROLES])
        expected={}
        for role,sql in ddl().items():
            fields=sql.split(' (',1)[1].rsplit(') ENGINE=',1)[0]
            expected[PREFIX+role]={m.group(1) or m.group(2):m.group(3).lower() for m in re.finditer(r'(?:^|,)(?:`([a-z_]+)`|([a-z_]+))\s+((?:var)?char\(\d+\)|decimal\(\d+,\d+\)|datetime(?:\(6\))?|bigint|int|json)(?=[ ,]|$)',fields,re.I)}
        found={PREFIX+r:{} for r in ROLES}
        for r in actual:found[r['TABLE_NAME']][r['COLUMN_NAME']]=r['COLUMN_TYPE'].lower()
        if found!=expected:raise ValueError('TEST_SCHEMA_DRIFT')
        triggers=self._read('SELECT trigger_name FROM information_schema.triggers WHERE event_object_schema=%s AND event_object_table IN ('+','.join(['%s']*len(ROLES))+')',['vk_ai']+[PREFIX+r for r in ROLES])
        if triggers:raise ValueError('TEST_TRIGGER_SIDE_EFFECT_REJECTED')
    def _write(self,sql,args=(),*,creating=False):
        if creating:
            if sql not in ddl().values():raise ValueError('TEST_DDL_NOT_ALLOWLISTED')
        else:
            # Only complete generated INSERT/UPDATE/DELETE statements; no SELECT subqueries or multi-statements.
            m=re.match(r'^(?:INSERT INTO|UPDATE|DELETE FROM) (`vk_ai`\.`[a-z0-9_]+`)(?: |\()',sql)
            if not m or m[1] not in TABLES.values() or ';' in sql or re.search(r'\b(?:SELECT|JOIN|TRUNCATE|DROP|ALTER)\b',sql,re.I):raise ValueError('TEST_WRITE_TARGET_REJECTED')
        self.audit.append({'sql':sql,'parameter_count':len(args),'parameter_digest':digest(args)})
        save(self.audit_name,self.audit)
        with self.conn.cursor() as c:c.execute(sql,args);return c.rowcount
    @contextmanager
    def transaction(self):
        self.verify();self.conn.begin()
        try:yield;self.conn.commit()
        except BaseException:self.conn.rollback();raise
    def insert(self,role,rows):
        if role not in ROLES or not rows:raise ValueError('TEST_INSERT_SCOPE_REQUIRED')
        for row in rows:
            if any(not re.fullmatch(r'[a-z_]+',k) for k in row):raise ValueError('TEST_COLUMN_REJECTED')
            self._write('INSERT INTO '+TABLES[role]+' ('+','.join('`'+k+'`' for k in row)+') VALUES ('+','.join(['%s']*len(row))+')',list(row.values()))
    def rows(self,role,scope=None):
        if role not in ROLES:raise ValueError('TEST_ROLE_REJECTED')
        sql='SELECT * FROM '+TABLES[role];args=[]
        if scope is not None:
            if role not in ('sales_snapshot','purchase_snapshot','price_input','cycles'):raise ValueError('TEST_SCOPE_REJECTED')
            sql+=' WHERE test_scope=%s';args=[scope]
        return self._read(sql,args)
    def replace_snapshot(self,side,scope,rows):
        if side not in ('sales','purchase') or not re.fullmatch(r'[a-z0-9_-]{1,48}',scope):raise ValueError('TEST_PRICE_SCOPE_REJECTED')
        role=side+'_snapshot'
        self._write('DELETE FROM '+TABLES[role]+' WHERE test_scope=%s',[scope])
        self.insert(role,[{**r,'test_scope':scope} for r in rows])
    def cycle(self,key,scope,status,payload):
        if not re.fullmatch(r'[a-z0-9-]{1,64}',key) or status not in ('planned','sending','failed','unknown','delivered','committed'):raise ValueError('CYCLE_ID_OR_STATE_INVALID')
        existing=self._read('SELECT cycle_id,test_scope FROM '+TABLES['cycles']+' WHERE cycle_id=%s',[key])
        if existing:
            if existing[0]['test_scope']!=scope:raise ValueError('CYCLE_SCOPE_COLLISION')
            self._write('UPDATE '+TABLES['cycles']+' SET status=%s,payload=%s WHERE cycle_id=%s AND test_scope=%s',[status,canonical(payload),key,scope])
        else:self.insert('cycles',[{'cycle_id':key,'test_scope':scope,'status':status,'payload':canonical(payload)}])
    def close(self):
        if hasattr(self,'conn'):self.conn.close()
        if getattr(self,'audit',None):save(self.audit_name,self.audit)
class Snapshot:
    def __enter__(self):
        checked=Store();checked.close()
        self.inner=tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None));self.inner.__enter__();self.marker=self.inner.marker;return self
    def __exit__(self,*a):return self.inner.__exit__(*a)
    def execute(self,sql,args,limit,**kw):
        try:return self.inner.execute(map_sql(sql),args,limit,**kw)
        except Exception as exc:
            save('test-read-failure.json',{'code':getattr(exc,'code',type(exc).__name__),'cause':str(exc.__cause__),'sql':map_sql(sql)})
            raise
def bootstrap():
    store=Store(bootstrap=True)
    try:
        conflicts=store._read('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name IN ('+','.join(['%s']*len(ROLES))+')',['vk_ai']+[PREFIX+r for r in ROLES])
        if conflicts:
            store.verify()
            return {'status':'existing_owned_objects_verified','created':False}
        for sql in ddl().values():store._write(sql,creating=True)
        store.insert('registry',[{'id':1,'owner_id':OWNER,'manifest_hash':digest(ddl())}]);store.verify()
        save('bootstrap-result.json',{'created':[TABLES[r] for r in ROLES],'ownership_verified':True,'ddl_hash':digest(ddl()),'production_write':False,'account_isolation':'application allowlist; account has broader grants','transport':'existing approved canary plaintext; not TLS'})
        return {'status':'created_and_verified','created':True,'tables':list(TABLES.values())}
    finally:store.close()
