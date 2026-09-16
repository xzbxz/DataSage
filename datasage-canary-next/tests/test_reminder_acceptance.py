import importlib,unittest,json
from unittest.mock import patch
from datetime import date
from pathlib import Path
from test_acceptance_delivery import AcceptanceDeliveryTests,a,io
import test_business_contracts as base

r=importlib.import_module(base.TEST_PACKAGE+'.reminder_acceptance')
class ReminderAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=AcceptanceDeliveryTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.profile=self.fixture.profile
        self.home=a.runtime_home(self.profile)
        (self.home/'legacy-recipient-reference.json').write_text(json.dumps({'regions':{'IDK':{'executors':[{'account':'original-a'},{'account':'original-b'}]}}}),encoding='utf-8')
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        for p in (patch.object(local,'configure_runtime'),patch.object(r.report_evidence.tools,'_business_today',return_value=date(2026,9,16))):p.start();self.addCleanup(p.stop)
    def test_synthetic_sales_keep_distinct_content_and_attachments(self):
        with patch.object(r.operations,'execute',side_effect=AssertionError('no business query')):
            value=r.prepare(self.profile,'sales-personalized-synthetic')
        self.assertEqual(2,len(value['notices']));self.assertNotEqual(value['notices'][0]['attachments'],value['notices'][1]['attachments'])
        self.assertNotEqual(value['notices'][0]['logical_id'],value['notices'][1]['logical_id'])
        self.assertEqual(value,r.prepare(self.profile,'sales-personalized-synthetic'))
    def test_private_group_purchase_fixture_is_explicitly_synthetic(self):
        value=r.prepare(self.profile,'purchase-private-group-synthetic')
        self.assertEqual('synthetic',value['evidence']['origin'])
        self.assertEqual(['private','group'],[n['channel'] for n in value['notices']]);self.assertTrue(value['notices'][1]['mention_all'])
        self.assertLess(value['notices'][0]['body'].index('采购价下调'),value['notices'][0]['body'].index('采购价上调'))

    def test_manager_no_buyer_and_customer_audit_variants_do_not_query_business(self):
        with patch.object(r.operations,'execute',side_effect=AssertionError('query')):
            value=r.prepare(self.profile,'sales-manager-no-buyers-synthetic')
            self.assertEqual(1,len(value['notices'][0]['attachments']));self.assertEqual([],value['notices'][1]['attachments'])
            self.assertIn('no customers',value['notices'][1]['body'])
            value=r.prepare(self.profile,'customer-package-audit-synthetic')
            self.assertEqual(2,len(value['notices']));self.assertTrue(value['evidence']['simulated_receipts'])
    def test_first_price_observation_does_not_alert_or_accept_baseline(self):
        doc={'kind':'sales_prices','status':'success','baseline_id':None,'records':[],'events':[],
             'event_counts':{},'observed_at':'2026-09-16T10:00:00','source_rows':0}
        with patch.object(r.operations,'execute',return_value=doc) as query,patch.object(r.operations,'accept_snapshot',side_effect=AssertionError('baseline')),patch.object(a,'deliver_batch',side_effect=AssertionError('send')):
            value=r.prepare(self.profile,'sales-observation')
            self.assertEqual('no_accepted_price_reference_no_alert',value['status'])
            self.assertNotEqual(self.profile,query.call_args.args[0]);self.assertFalse(r.send(self.profile,'sales-observation')['sent'])
    def test_idk_same_body_role_aggregation_preserves_old_recipients(self):
        doc={'kind':'idk_unpriced','status':'success','observed_at':'2026-09-16T10:00:00','source_rows':1,
             'records':[{'product':'SYN','color':'Red'}]}
        with patch.object(r.operations,'execute',return_value=doc):value=r.prepare(self.profile,'idk-current')
        self.assertEqual(['original-a','original-b'],value['evidence']['original_recipients'])
        self.assertEqual(1,len(value['notices']));self.assertIn('SYN | Color Red',value['notices'][0]['body'])
    def test_changed_staged_attachment_cannot_send(self):
        value=r.prepare(self.profile,'sales-personalized-synthetic')
        Path(value['notices'][0]['attachments'][0]).write_bytes(b'changed')
        with patch.object(a,'deliver_batch',side_effect=AssertionError('send')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'FILE_CHANGED'):r.send(self.profile,'sales-personalized-synthetic')
    def test_customers_cannot_precede_task_acceptance(self):
        with patch.object(r.workflow_inputs,'customer_mapping',side_effect=AssertionError('query')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'TASK_STAGE'):r.prepare(self.profile,'hcm-customer-packages')

    def test_monthly_send_cannot_precede_weekly_acceptance(self):
        with patch.object(a,'deliver_batch',side_effect=AssertionError('send')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'WEEKLY_STAGE'):r.send(self.profile,'hcm-monthly')

    def test_real_audit_only_covers_selected_accepted_packages(self):
        (self.home/'legacy-recipient-reference.json').write_text(json.dumps({'regions':{'HCM':{'executors':[{'account':'exec'}],'managers':['manager']}}}),encoding='utf-8')
        folder=r._case(self.profile,'hcm-customer-packages')
        packages=[{'account':str(i),'region':'HCM','sales_name':'Synthetic '+str(i),'sales_names':['Synthetic '+str(i)],
            'customers':[{'customer_no':'SYN-'+str(i),'customer_name':'Synthetic'}]} for i in range(3)]
        for name,value in [('send-result.json',{'status':'provider_accepted_not_human_read'}),
            ('manifest.json',{'evidence':{'selected_original_accounts':['0','1']}}),('full-hcm-plan.json',{'sales_packages':packages})]:
            (folder/name).write_text(json.dumps(value),encoding='utf-8')
        value=r.prepare(self.profile,'hcm-dispatch-audit')
        self.assertEqual(2,value['evidence']['selected_packages']);self.assertEqual(1,value['evidence']['unselected_packages_not_attempted'])
        self.assertEqual(1,len(value['notices']));self.assertIn('2 sales owners',value['notices'][0]['body'])
        self.assertIn('非原销售/客户接收',value['notices'][0]['role'])
    def test_unregistered_modes_and_production_actions_are_rejected(self):
        with patch.object(r,'prepare',side_effect=AssertionError('prepare')),patch.object(r,'send',side_effect=AssertionError('send')):
            for argv in ([],['--enable'],['--send','all'],['--prepare','qc'],['--prepare','floating_ball'],['--send','hcm-weekly','--force']):self.assertEqual(2,r.main(self.profile,argv))
    def test_cycle_expiry_stops_before_business_query(self):
        with patch.object(r.report_evidence.tools,'_business_today',return_value=date(2026,9,23)),patch.object(r.operations,'execute',side_effect=AssertionError('query')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'CYCLE_EXPIRED'):r.prepare(self.profile,'idk-current')

    def test_task_uses_aware_legacy_week_period_with_naive_database_timestamp(self):
        from contextlib import contextmanager
        from test_legacy_workflow import source
        reference={'regions':{'HCM':{'executors':[{'account':'original-exec','name':'Exec'}],
            'managers':['original-manager'],'dynamic_sales_departments':['HCM Sales']}}}
        (self.home/'legacy-recipient-reference.json').write_text(json.dumps(reference),encoding='utf-8')
        rows=r.wf.freeze_plan([source()],[],r.WEEK,r.WEEK)['insert_rows']
        rows[0]['frozen_at']='2026-09-15 09:00:00'
        class DB:
            def execute(self,sql,args,limit,**kwargs):
                assert sql.startswith('SELECT')
                if 'slow_moving_baseline' in sql:return rows,False,{}
                if 'NOW(6)' in sql:return [{'at':'2026-09-16 10:00:00'}],False,{}
                return [{'region':'HCM','main_dept':'HCM Sales','is_delete':'n','wecom_status':'payroll','wecom_account':'original-sales','person_name':'Synthetic Sales','position':'Sales'}],False,{}
        @contextmanager
        def snapshots(**kwargs):yield DB()
        with patch.object(r.report_evidence.tools,'_ConsistentSnapshotExecutor',side_effect=snapshots):
            value=r.prepare(self.profile,'hcm-task')
        self.assertEqual('prepared',value['status']);self.assertFalse(value['evidence']['frozen'])
        self.assertIn('Period:',value['notices'][0]['body'])
if __name__=='__main__':unittest.main()
