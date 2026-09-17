import unittest,importlib,copy,json
from contextlib import contextmanager
from unittest.mock import patch
import test_business_contracts as base
from test_legacy_price_bridge import old_sales,sales,NOW

s=importlib.import_module(base.TEST_PACKAGE+'.workflow_storage')
c=importlib.import_module(base.TEST_PACKAGE+'.workflow_cycle')
runner=importlib.import_module(base.TEST_PACKAGE+'.workflow_runner')
fixture=importlib.import_module(base.TEST_PACKAGE+'.workflow_fixture')
slow=importlib.import_module(base.TEST_PACKAGE+'.workflow_slow')

class FakeStore:
    def __init__(self):
        current=sales();current['ddp_price']='11'
        self.data={'sales_snapshot':[{**old_sales(),'test_scope':'main'}],
                   'price_input':[{'test_scope':'main','side':'sales','ordinal':0,'payload':json.dumps(current)}],
                   'cycles':[{'cycle_id':'seed-sales','test_scope':'main','status':'committed','payload':{'observed_at':NOW.isoformat()}}]}
    def rows(self,role,scope=None):return copy.deepcopy([r for r in self.data.get(role,[]) if scope is None or r['test_scope']==scope])
    def cycle(self,key,scope,status,data):
        self.data['cycles']=[r for r in self.data['cycles'] if r['cycle_id']!=key]+[{'cycle_id':key,'test_scope':scope,'status':status,'payload':copy.deepcopy(data)}]
    def replace_snapshot(self,side,scope,rows):self.data[side+'_snapshot']=[{**r,'test_scope':scope} for r in rows]
    @contextmanager
    def transaction(self):
        backup=copy.deepcopy(self.data)
        try:yield
        except BaseException:self.data=backup;raise
    def _read(self,sql,args=()):return [{'acquired':1,'released':1}]
    def close(self):pass

class InstalledWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db=FakeStore();self.saved=[];self.sent=[]
        self.patches=[patch.object(s,'Store',return_value=self.db),patch.object(s,'save',side_effect=lambda *a:self.saved.append(a))]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
    def send(self,key,items):self.sent.append((key,copy.deepcopy(items)));return {'status':'injected_provider_accepted'}
    def test_change_commit_then_identical_input_silent(self):
        first=c.run('sales',send=self.send);second=c.run('sales',send=self.send)
        self.assertEqual(first['events'],{'legacy_nominal_price_changed':1})
        self.assertEqual(second['events'],{'recorded_price_unchanged':1});self.assertEqual(len(self.sent),1)
        self.assertEqual(second['delivery']['components'],0)
    def test_missing_baseline_never_cold_starts(self):
        self.db.data['sales_snapshot']=[]
        with self.assertRaisesRegex(ValueError,'EMPTY_INPUT'):c.run('sales',send=self.send)
        self.assertFalse(self.sent)
    def test_unknown_send_preserves_snapshot_and_blocks_next_run(self):
        before=copy.deepcopy(self.db.data['sales_snapshot'])
        def unknown(*a):raise c.io.IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
        with self.assertRaises(c.io.IOErrorBoundary):c.run('sales',send=unknown)
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):c.run('sales',send=self.send)
        self.assertEqual(before,self.db.data['sales_snapshot']);self.assertFalse(self.sent)
    def test_status_checks_unknown_baseline_without_counting_fake_receipts(self):
        def unknown(*a):raise c.io.IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
        with self.assertRaises(c.io.IOErrorBoundary):c.run('sales',send=unknown)
        result=runner.status()
        self.assertEqual(result['latest_snapshot_proof'][0]['status'],'unknown')
        self.assertTrue(result['latest_snapshot_proof'][0]['snapshot_verified'])
        self.assertEqual(result['delivery_evidence']['real_components'],0)

    def test_status_keeps_old_provider_acceptance_historical_and_unverified(self):
        self.db.data['cycles'].append({'cycle_id':'slow-2026-w38','test_scope':'main','status':'committed','payload':{'phases':{'weekly':{'notices':[{'channel':'private','logical_id':'old-logical','role':'role','body':'historical body','attachments':[]}],'receipt':{'status':'provider_accepted_not_human_read','components':1}}}}})
        result=runner.status();evidence=result['delivery_evidence']
        self.assertEqual(1,evidence['real_components'])
        self.assertEqual(1,evidence['historical_components'])
        self.assertEqual(1,evidence['historical_provider_accepted_count'])
        self.assertFalse(evidence['all_component_receipts_verified'])
    def test_known_failure_can_resume_frozen_event(self):
        def failed(*a):raise c.io.IOErrorBoundary('DELIVERY_COMPONENT_FAILED')
        with self.assertRaises(c.io.IOErrorBoundary):c.run('sales',send=failed)
        result=c.run('sales',send=self.send)
        self.assertEqual(result['cycle'],'sales-main-0');self.assertEqual(len(self.sent),1)
    def test_before_commit_rollback_then_recovery_without_resend(self):
        before=copy.deepcopy(self.db.data['sales_snapshot'])
        def crash(event,key):
            if event=='before_commit':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):c.run('sales',send=self.send,fault=crash)
        self.assertEqual(before,self.db.data['sales_snapshot'])
        c.run('sales',send=self.send);self.assertEqual(len(self.sent),1)
    def test_after_commit_next_run_is_silent(self):
        def crash(event,key):
            if event=='after_commit':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):c.run('sales',send=self.send,fault=crash)
        result=c.run('sales',send=self.send)
        self.assertEqual(result['events'],{'recorded_price_unchanged':1});self.assertEqual(len(self.sent),1)
    def test_lost_commit_ack_resolves_using_atomic_marker(self):
        original=self.db.transaction;count=[0]
        @contextmanager
        def uncertain():
            with original():yield
            count[0]+=1
            if count[0]==4:raise ConnectionError('ack lost after server commit')
        self.db.transaction=uncertain
        with self.assertRaises(ConnectionError):c.run('sales',send=self.send)
        self.db.transaction=original
        result=c.run('sales',send=self.send)
        self.assertEqual(result['events'],{'recorded_price_unchanged':1});self.assertEqual(len(self.sent),1)
    def test_process_resume_uses_persisted_event_not_changed_input(self):
        def crash(event,key):
            if event=='planned':raise RuntimeError('stop')
        with self.assertRaises(RuntimeError):c.run('sales',send=self.send,fault=crash)
        row=json.loads(self.db.data['price_input'][0]['payload']);row['ddp_price']='99'
        self.db.data['price_input'][0]['payload']=json.dumps(row)
        result=c.run('sales',send=self.send)
        self.assertEqual(self.db.data['sales_snapshot'][0]['ddp_price'],'11')
        self.assertEqual(result['cycle'],'sales-main-0')
    def test_out_of_band_baseline_change_is_rejected(self):
        c.run('sales',send=self.send);self.db.data['sales_snapshot'][0]['ddp_price']='77'
        with self.assertRaisesRegex(ValueError,'BASELINE_CHANGED'):c.run('sales',send=self.send)
        self.assertEqual(len(self.sent),1)
    def test_missing_seed_refuses_without_writes(self):
        self.db.data['cycles']=[]
        with self.assertRaisesRegex(ValueError,'SEED_REQUIRED'):c.run('sales',send=self.send)
        self.assertFalse(self.sent)
    def test_ambiguous_input_never_advances(self):
        self.db.data['price_input'].append(copy.deepcopy(self.db.data['price_input'][0]))
        with self.assertRaisesRegex(ValueError,'AMBIGUOUS'):c.run('sales',send=self.send)
        self.assertFalse(self.sent)
    def test_fault_scopes_cannot_use_real_transport(self):
        with self.assertRaisesRegex(ValueError,'REAL_SEND_FORBIDDEN'):c.run('sales','fault-unknown')
    def test_production_mode_rejected_before_runtime(self):
        with self.assertRaises(SystemExit):runner.main(base.PROFILE_ROOT,['--mode','production','prices'])
    def test_test_binding_exact_and_no_boolean_coercion(self):
        valid={'mode':'isolated_acceptance','enabled':True,'instance':'profile-v1','database':'vk_ai','prefix':s.PREFIX,'owner':s.OWNER,'server_uuid':s.SERVER,'account':'Cody@%','credential_source':'explicit_existing_DATA_QUERY_MYSQL','production_writes':False}
        s.assert_binding(valid)
        for key in valid:
            bad=dict(valid);bad.pop(key)
            with self.assertRaises(ValueError):s.assert_binding(bad)
        for key,value in [('enabled',1),('production_writes',0),('prefix','production'),('mode','production')]:
            with self.assertRaises(ValueError):s.assert_binding({**valid,key:value})
    def test_table_mapping_preserves_literal_and_comments(self):
        sql="SELECT * FROM `vk_ai`.`slow_moving_baseline` WHERE source_table='vk_ods.slow_moving_goods_ods' /* vk_ai.slow_moving_baseline */"
        self.assertEqual(s.map_sql(sql),"SELECT * FROM "+s.TABLES['slow_baseline']+" WHERE source_table='vk_ods.slow_moving_goods_ods' /* vk_ai.slow_moving_baseline */")
    def test_no_arbitrary_write_target(self):
        # Inspect the actual class through the patcher's original reference.
        cls=self.patches[0].temp_original;obj=cls.__new__(cls);obj.audit=[]
        for sql in ['DELETE FROM vk_ai.slow_moving_baseline WHERE week_label=%s','UPDATE `vk_ai`.`ready_goods_purchase_snapshot` SET tax_inclue_price=%s','INSERT INTO '+s.TABLES['stock_input']+' SELECT * FROM vk_ods.slow_moving_goods_ods']:
            with self.assertRaisesRegex(ValueError,'TARGET_REJECTED'):obj._write(sql)
        self.assertEqual(obj.audit,[])
    def test_installed_sources_have_no_workspace_bootstrap(self):
        root=base.PLUGIN_ROOT
        for name in ('workflow_storage.py','workflow_cycle.py','workflow_fixture.py','workflow_slow.py','workflow_runner.py','workflow_recovery_check.py'):
            text=(root/name).read_text(encoding='utf-8')
            for forbidden in ('read_legacy_prices','outputs/','Documents/Codex','datasage-old-reference'):
                self.assertNotIn(forbidden,text)
    def test_no_if_exists_or_source_clone_ddl(self):
        for sql in s.ddl().values():
            for forbidden in ('IF NOT EXISTS',' LIKE ','FOREIGN KEY','DROP '):self.assertNotIn(forbidden,sql)
            self.assertIn('CHARSET=utf8mb4',sql)
    def test_completed_slow_cycle_does_not_rebuild_or_resend(self):
        with patch.object(slow.delivery,'load_settings'),patch.object(slow.session,'_reference'),patch.object(slow.fixture,'seeded',return_value={'week':'2026-W38'}),patch.object(slow,'freeze',return_value=('slow-2026-w38','committed',{})),patch.object(slow,'build') as build,patch.object(c,'dispatch') as dispatch:
            self.assertEqual(slow.run()['status'],'already_completed_no_resend')
            build.assert_not_called();dispatch.assert_not_called()
    def test_all_quarantined_observation_does_not_claim_reference_advance(self):
        before=c.clean(self.db.rows('sales_snapshot','main'));fingerprint=c.snapshot_digest('sales',before)
        data={'document':{'event_counts':{'unresolved':1},'continuation':{'advanced_keys':0}},'notices':[],'before_digest':fingerprint,'after_digest':fingerprint,'after':before,'real_transport':False}
        with patch.object(self.db,'replace_snapshot',side_effect=AssertionError('same reference must not be rewritten')):
            result=c.complete_plan(self.db,'sales','main','sales-main-0',data,'planned')
        self.assertFalse(result['test_snapshot_advanced']);self.assertEqual(result['delivery']['components'],0)

if __name__=='__main__':unittest.main()
