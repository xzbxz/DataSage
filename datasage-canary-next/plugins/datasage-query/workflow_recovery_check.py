"""Finite test-only failure probes. No real transport in any fault scope."""
from pathlib import Path
import json,os,subprocess,sys,time
from . import workflow_storage as s,workflow_cycle as cycle,workflow_fixture as fixture,workflow_io as io

class InjectedTransport:
    def __init__(self,mode):self.mode=mode;self.calls=[]
    def preflight(self,items):pass
    def fingerprint(self,item):return io.wf.digest(item)
    def send(self,item):
        self.calls.append(item['stage'])
        if self.mode=='unknown':raise TimeoutError('INJECTED_NO_NETWORK')
        if self.mode=='failed' and item['stage']=='second':return {'success':False,'raw_response':{'errcode':45009}}
        return {'success':True,'raw_response':{'errcode':0},'message_id':'injected-only-'+item['stage']}
def sender(mode='ok'):
    def send(key,notices):
        home=s.profile()/'report_runs'/'wv1_fault';home.mkdir(exist_ok=True)
        progress=io.Progress(home,'probe-'+key,key);progress.run_id=key
        parts=[io.component('injected-only','text','Profile recovery probe '+stage,key,stage) for stage in ('first','second')]
        transport=InjectedTransport(mode)
        try:io.deliver_components(parts,transport,progress,enabled=True)
        finally:s.save(key+'-attempt-'+str(time.time_ns())+'.json',{'calls':transport.calls,'real_network':False,'progress':progress.data})
        return {'status':'injected_provider_accepted','real_network':False,'calls':transport.calls}
    return send
def terminate_after_plan(event,key):
    if event=='planned':os._exit(73)
def fail_at(stage):
    def fail(event,key):
        if event==stage:raise RuntimeError('INJECTED_'+stage.upper())
    return fail
def status():
    store=s.Store()
    try:
        result=[]
        for scope in cycle.SCOPES[1:]:
            rows=cycle.journals(store,'sales',scope)
            for row in rows:
                data=cycle.payload(row)
                latest=max(rows,key=lambda r:int(r['cycle_id'].rsplit('-',1)[1]))
                if latest['cycle_id']==row['cycle_id']:
                    expected=data['after_digest'] if row['status']=='committed' else data['before_digest']
                    if cycle.snapshot_digest('sales',store.rows('sales_snapshot',scope))!=expected:raise ValueError('RECOVERY_SNAPSHOT_MISMATCH')
                result.append({'cycle':row['cycle_id'],'status':row['status']})
        return result
    finally:store.close()
def run():
    finished=s.root()/'recovery-check-result.json'
    if finished.exists():return {'status':'existing_evidence_preserved','database_verification':status()}
    results=[]
    for scope in cycle.SCOPES[1:]:
        fixture.change('sales',scope)
        if scope=='fault-interrupt':
            script=s.profile()/'scripts/datasage_workflow.py'
            proc=subprocess.run([sys.executable,'-B',str(script),'--mode','test','fault-exit'],capture_output=True,text=True)
            if proc.returncode!=73:raise ValueError('EXPECTED_TEST_PROCESS_EXIT_NOT_OBSERVED')
            results.append({'scope':scope,'actual_process_exit':73})
        else:
            mode='failed' if scope=='fault-partial' else 'unknown' if scope=='fault-unknown' else 'ok'
            stage={'fault-beforecommit':'before_commit','fault-aftercommit':'after_commit'}.get(scope)
            try:cycle.run('sales',scope,send=sender(mode),fault=fail_at(stage) if stage else None)
            except (io.IOErrorBoundary,RuntimeError) as exc:results.append({'scope':scope,'injected_failure':str(exc)})
            else:raise ValueError('EXPECTED_TEST_FAULT_NOT_OBSERVED')
        if scope=='fault-unknown':
            for _ in range(2):
                try:cycle.run('sales',scope,send=sender())
                except ValueError as exc:
                    if str(exc)!='WORKFLOW_SEND_UNKNOWN_REVIEW_REQUIRED':raise
                else:raise ValueError('UNKNOWN_RETRIED')
            results.append({'scope':scope,'unknown_retry_blocked':True})
        else:results.append({'scope':scope,'recovery':cycle.run('sales',scope,send=sender())})
    attempts={}
    for scope in cycle.SCOPES[1:]:
        calls=[json.loads(path.read_text())['calls'] for path in sorted(s.root().glob('sales-'+scope+'-*-attempt-*.json'))]
        expected=[['first','second'],['second']] if scope=='fault-partial' else [['first']] if scope=='fault-unknown' else [['first','second']]
        if calls!=expected:raise ValueError('UNEXPECTED_TEST_TRANSPORT_CALLS:'+scope)
        attempts[scope]=calls
    result={'status':'passed','real_test_database':True,'real_network':False,'results':results,'transport_calls':attempts,'database_verification':status()}
    s.save('recovery-check-result.json',result);return result
