import unittest,importlib,copy,json
from contextlib import nullcontext
from unittest.mock import patch
from datetime import datetime,timedelta,timezone
import test_business_contracts as base
from test_legacy_price_bridge import old_sales,sales,old_purchase,purchase,NOW

live=importlib.import_module(base.TEST_PACKAGE+'.workflow_live_store')
prices=importlib.import_module(base.TEST_PACKAGE+'.workflow_live_prices')
slow=importlib.import_module(base.TEST_PACKAGE+'.workflow_live_slow')
schedule=importlib.import_module(base.TEST_PACKAGE+'.workflow_schedule')
runner=importlib.import_module(base.TEST_PACKAGE+'.workflow_live_runner')
storage=importlib.import_module(base.TEST_PACKAGE+'.workflow_storage')
bridge=importlib.import_module(base.TEST_PACKAGE+'.legacy_price_bridge')

def key_issues(side,before,current,document):
    return prices.continuity.plan(side,before,current,NOW)['document']['continuation']['anomaly_counts']

class LiveWorkflowTests(unittest.TestCase):
    def test_namespace_does_not_adopt_fixture_or_production(self):
        live.validate_binding(live.expected_binding())
        for k,v in [('prefix',storage.PREFIX),('mode','production'),('owner',storage.OWNER),('enabled',1),('production_writes',0),('schedule_enabled',1)]:
            with self.assertRaises(ValueError):live.validate_binding({**live.expected_binding(),k:v})
        self.assertFalse(set(live.TABLES.values()) & set(storage.TABLES.values()))
    def test_old_sales_nominal_comparison_does_not_invent_history(self):
        before=[old_sales()];current=[sales()];doc=bridge.compare('sales',before,current,NOW)
        self.assertEqual(key_issues('sales',before,current,doc),{})
        self.assertNotIn('unit',before[0])
    def test_recorded_purchase_basis_can_compare_without_inventing_validity(self):
        before=[old_purchase()];current=[purchase()];doc=bridge.compare('purchase',before,current,NOW)
        self.assertEqual(key_issues('purchase',before,current,doc),{})
    def test_own_complete_sales_snapshot_checks_unit_tax_changes(self):
        original=sales();before=[{**old_sales(),'current_record':json.dumps(original),'reference_kind':'observed'}]
        for field in ('unit','unit_cuur','is_inclue_tax','currency_no'):
            current={**original,field:'CHANGED'};doc=bridge.compare('sales',before,[current],NOW)
            self.assertIn('recorded_basis_changed' if field=='currency_no' else 'observed_unit_tax_changed_keep_reference',key_issues('sales',before,[current],doc))
    def test_own_complete_basis_unchanged_passes(self):
        current=sales();old={**old_sales(),'current_record':json.dumps(current),'reference_kind':'observed'}
        self.assertEqual(key_issues('sales',[old],[current],bridge.compare('sales',[old],[current],NOW)),{})
    def test_missing_current_basis_blocks_even_if_price_same(self):
        current=purchase();current['unit_cuur']=None
        self.assertIn('unresolved_recorded_basis',key_issues('purchase',[old_purchase()],[current],bridge.compare('purchase',[old_purchase()],[current],NOW)))
    def test_absent_identity_and_duplicate_source_are_not_silent_advancement(self):
        old=old_purchase();doc=bridge.compare('purchase',[old],[],NOW)
        self.assertEqual(key_issues('purchase',[old],[],doc)['absent_selection_keep_reference'],1)
        current=purchase();doc=bridge.compare('purchase',[old],[current,current],NOW)
        self.assertTrue(any(k.startswith('unresolved') for k in key_issues('purchase',[old],[current,current],doc)))
    def test_scope_mapping_handles_optional_aliases_without_touching_literals(self):
        raw="SELECT b.id FROM `vk_ai`.`slow_moving_baseline` b WHERE b.source_table='vk_ods.slow_moving_goods_ods'"
        result=live.mapped(raw,'hcm-2026-w38-g0')
        self.assertIn('FROM live_slow_baseline b',result);self.assertIn("source_table='vk_ods.slow_moving_goods_ods'",result)
        self.assertIn("test_scope='hcm-2026-w38-g0'",result)
        with self.assertRaises(ValueError):live.mapped(raw,"x' OR 1=1")
    def test_generated_schema_scopes_full_department_data_and_uses_json_history(self):
        ddl=live.ddl()
        self.assertIn('PRIMARY KEY(test_scope,id)',ddl['stock_input'])
        self.assertIn('scope_source(test_scope,week_label,source_row_id)',ddl['slow_baseline'])
        self.assertIn('current_record json',ddl['sales_snapshot'])
        for statement in ddl.values():
            self.assertNotIn(storage.PREFIX,statement);self.assertNotIn('IF NOT EXISTS',statement)
    def test_page_budget_does_not_return_partial_source(self):
        class DB:
            def execute(self,*a):return [{'id':1}]*2000,True,{}
        with self.assertRaisesRegex(ValueError,'BUDGET'):slow.page_rows(DB(),'SELECT id FROM fixed',[],['id'],budget=2000)
    def test_json_representation_normalizes_snapshot_digest(self):
        a={**old_sales(),'current_record':'{"a":1,"b":2}','reference_kind':'observed'}
        b={**old_sales(),'current_record':'{"b": 2, "a": 1}','reference_kind':'observed'}
        self.assertEqual(prices.snapshot_digest('sales',[a]),prices.snapshot_digest('sales',[b]))
    def test_fixture_actions_unreachable_from_real_mode(self):
        for action in ('seed','change-fixture','fault-exit','recovery-check'):
            with self.assertRaisesRegex(ValueError,'ACTION_REJECTED'):runner.run(action)
    def test_schedule_default_has_no_registration(self):
        plan=schedule.plan();self.assertEqual(plan['status'],'prepared_not_registered')
        self.assertEqual(plan['test_duration_hours'],6)
        with patch.object(live,'binding') as binding:
            binding.return_value.read_text.return_value=json.dumps(live.expected_binding())
            with patch.object(live,'Store',side_effect=AssertionError('must not open DB')):
                self.assertEqual(schedule.tick('sales')['status'],'schedule_disabled')
    def test_schedule_rejects_unbounded_expired_and_wrong_departments(self):
        at=datetime(2026,9,16,10,tzinfo=timezone(timedelta(hours=8)))
        good={'enabled':True,'starts_at':at.isoformat(),'ends_at':(at+timedelta(hours=6)).isoformat(),'max_invocations':20,'departments':['HCM']}
        schedule.approval(good,at)
        for changes in ({'ends_at':(at+timedelta(hours=25)).isoformat()},{'enabled':False},{'departments':['ALL']},{'max_invocations':999}):
            with self.assertRaises(ValueError):schedule.approval({**good,**changes},at)
    def test_schedule_minutes_and_slow_periods_are_explicit(self):
        self.assertTrue(schedule.due('sales',datetime(2026,9,16,10,7)))
        self.assertFalse(schedule.due('sales',datetime(2026,9,16,10,8)))
        self.assertTrue(schedule.due('slow-task',datetime(2026,9,15,9,5)))
        self.assertFalse(schedule.due('slow-task',datetime(2026,9,16,9,5)))
        self.assertTrue(schedule.due('slow-report',datetime(2026,9,19,19,0)))
    def test_new_generation_requires_business_reason(self):
        with self.assertRaisesRegex(ValueError,'REASON_REQUIRED'):slow.prepare('HCM',new_generation=True)
    def test_department_generation_latest_does_not_reuse_previous_week(self):
        class Store:
            def rows(self,*a):return [{'cycle_id':'ls-hcm-2026-w37-g9','payload':{'generation':9}},{'cycle_id':'ls-hcm-2026-w38-g0','payload':{'generation':0}}]
        self.assertEqual(slow.latest(Store(),'HCM','2026-W38')['cycle_id'],'ls-hcm-2026-w38-g0')
    def test_observation_action_cannot_send_a_prepared_notice(self):
        class Store:
            def rows(self,*a):return [{'cycle_id':'lp-sales-pending','status':'planned','payload':{'notices':[{'body':'real data'}],'observation':{},'document':{'event_counts':{'changed':1}}}}]
            def close(self):pass
        with patch.object(live,'Store',return_value=Store()),patch.object(live,'lock',return_value=nullcontext()),patch.object(live,'save'),patch.object(prices.cycle,'complete_plan',side_effect=AssertionError('must not deliver')):
            self.assertEqual(prices.run('sales')['status'],'prepared_not_sent')
    def test_delivery_action_cannot_fetch_new_unreviewed_source(self):
        class Store:
            def rows(self,*a):return []
            def close(self):pass
        with patch.object(live,'Store',return_value=Store()),patch.object(live,'lock',return_value=nullcontext()),patch.object(prices,'observe',side_effect=AssertionError('must not read new source')):
            self.assertEqual(prices.run('sales',allow_send=True)['status'],'no_prepared_price_delivery')
    def test_same_hour_blocked_observation_does_not_requery(self):
        class Store:
            def rows(self,*a):return [{'cycle_id':'lp-sales-2026091615','status':'blocked','payload':{}}]
            def _read(self,*a):return [{'at':datetime(2026,9,16,15,9)}]
            def close(self):pass
        with patch.object(live,'Store',return_value=Store()),patch.object(live,'lock',return_value=nullcontext()),patch.object(prices,'observe',side_effect=AssertionError('must reuse slot')):
            result=prices.run('sales');self.assertEqual(result['status'],'same_hour_already_observed');self.assertFalse(result['new_source_query'])
    def test_live_writer_rejects_fixture_target_as_well_as_production(self):
        store=live.Store.__new__(live.Store)
        for table in (storage.TABLES['sales_snapshot'],'`vk_ai`.`ready_goods_price_snapshot`'):
            with self.assertRaisesRegex(ValueError,'TARGET_REJECTED'):store._write('DELETE FROM '+table+' WHERE test_scope=%s',['sales'])
    def test_bounded_batches_keep_unsent_events_and_skip_accepted_batches(self):
        data={};notices=[{'id':i} for i in range(5)];calls=[]
        def dispatch(key,items,state):
            calls.append([i['id'] for i in items]);return {'logical_notifications':len(items),'components':len(items)}
        with patch.object(prices.cycle,'persist'),patch.object(prices.cycle,'dispatch',side_effect=dispatch):
            fn=prices.bounded_dispatch(object(),'lp-sales-x','sales',data)
            with self.assertRaises(prices.cycle.DeliveryDeferred):fn('unused',notices,'planned',None)
            self.assertEqual(list(data['batch_receipts']),['0'])
            with self.assertRaises(prices.cycle.DeliveryDeferred):fn('unused',notices,'planned',None)
            result=fn('unused',notices,'planned',None)
        self.assertEqual(calls,[[0,1],[2,3],[4]]);self.assertEqual(result['logical_notifications'],5)
    def test_notice_seal_changes_when_body_or_file_changes(self):
        notice={'body':'first','attachments':['test.xlsx']}
        with patch.object(prices.delivery,'runtime_home'),patch.object(prices.delivery,'file_snapshot',return_value=('test.xlsx',b'first','type','digest')):
            first=prices.seal_notices([notice])
            changed=prices.seal_notices([{**notice,'body':'second'}])
            self.assertNotEqual(first,changed)
        with patch.object(prices.delivery,'runtime_home'),patch.object(prices.delivery,'file_snapshot',return_value=('test.xlsx',b'changed','type','digest')):
            self.assertNotEqual(first,prices.seal_notices([notice]))
    def test_price_precision_loss_blocks_before_send_or_snapshot_write(self):
        current=sales();current['ddp_price']='10.001'
        old={**old_sales(),'current_record':json.dumps(sales()),'reference_kind':'observed'}
        doc=bridge.compare('sales',[old],[current],NOW)
        self.assertIn('snapshot_decimal_precision_insufficient',key_issues('sales',[old],[current],doc))

if __name__=='__main__':unittest.main()
