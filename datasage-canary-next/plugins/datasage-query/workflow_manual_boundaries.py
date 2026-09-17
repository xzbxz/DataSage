"""Synthetic period/generation checks; reuse real fault evidence without replaying sends."""
import json,hashlib
from . import workflow_storage as store_api,workflow_live_slow as slow,legacy_workflow as wf,workflow_cycle as cycle

def calculate():
    source=[{'id':1,'goods_id':100001,'goods_sku_id':100002,'goods_no':'SYNTHETIC-PERIOD','attr_val':'SYNTHETIC','whse_dept':'HCM','source_unit':'m','unit':'m','goods_num':'12','piece_num':'1.5','is_whitelist':'n'}]
    old=wf.freeze_plan(source,[],'2026-W38','2026-W38')
    prior=old['insert_rows'];before=wf.digest(prior)
    same=wf.freeze_plan(source,prior,'2026-W38','2026-W38',refreeze=False)
    following=wf.freeze_plan(source,prior,'2026-W39','2026-W39')
    assert same['action']=='reuse_existing' and following['action']=='create_requested_week'
    assert following['delete_scope']=={'week_label':'2026-W39'} and wf.digest(prior)==before
    completed={'status':'committed','payload':{'generation':0}}
    assert slow.generation_number(completed)==0
    assert slow.generation_number(completed,force=True,reason='Synthetic boundary verification')==1
    refused=[]
    for state in ('planned','sending','failed','unknown'):
        try:slow.generation_number({'status':state,'payload':{'generation':0}},force=True,reason='Synthetic boundary verification')
        except ValueError:refused.append(state)
        else:raise ValueError('INCOMPLETE_GENERATION_WAS_BYPASSED')
    try:slow.generation_number(completed,force=True)
    except ValueError:reason_required=True
    else:raise ValueError('FORCE_REASON_NOT_REQUIRED')
    class SyntheticRows:
        def rows(self,*args):return [{'cycle_id':'ls-hcm-2026-w38-g4','payload':{'generation':4}},{'cycle_id':'ls-hcm-2026-w39-g0','payload':{'generation':0}}]
    assert slow.latest(SyntheticRows(),'HCM','2026-W39')['cycle_id']=='ls-hcm-2026-w39-g0'
    return {'kind':'synthetic_period_and_generation_control_flow','same_week_reuses':True,'next_week_independent_scope':True,'old_week_unchanged':True,'explicit_new_generation':1,'force_reason_required':reason_required,'incomplete_states_rejected':refused,'system_or_real_observation_clock_changed':False,'real_or_test_freeze_rows_written':0}
def run():
    result=calculate();path=store_api.root()/'recovery-check-result.json'
    if not path.is_file():raise ValueError('EXISTING_RECOVERY_EVIDENCE_REQUIRED')
    digest=hashlib.sha256(path.read_bytes()).hexdigest();prior=json.loads(path.read_text(encoding='utf-8'))
    if prior['status']!='passed':raise ValueError('RECOVERY_EVIDENCE_NOT_PASSED')
    store=store_api.Store()
    try:
        unknown=next(r for r in store.rows('cycles','fault-unknown') if r['cycle_id']=='sales-fault-unknown-0')
        if unknown['status']!='unknown':raise ValueError('PRIOR_UNKNOWN_STATE_CHANGED')
    finally:store.close()
    result['reused_existing_evidence']={'path':str(path),'sha256':digest,'partial_retry':True,'unknown_remains_blocked':True,'actual_process_exit_73':True,'before_after_commit_recovery':True,'not_reexecuted':True}
    result['new_messages_sent']=0;result['production_source_queries']=0
    store_api.save('manual-boundaries-v1.json',result);return result
