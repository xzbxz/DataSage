import unittest,importlib,json
from unittest.mock import patch
from contextlib import contextmanager
from datetime import date
from test_acceptance_delivery import AcceptanceDeliveryTests
from test_legacy_workflow import source
import test_business_contracts as base

r=importlib.import_module(base.TEST_PACKAGE+'.regional_acceptance')

class RegionalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.f=AcceptanceDeliveryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.profile=self.f.profile
        ref={'regions':{'HN':{'executors':[{'account':'original-exec'}],'managers':['original-manager'],'dynamic_sales_departments':['HN Sales']}}}
        (self.f.home/'legacy-recipient-reference.json').write_text(json.dumps(ref),encoding='utf-8')
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        for p in (patch.object(local,'configure_runtime'),patch.object(r.report_evidence.tools,'_business_today',return_value=date(2026,9,16))):p.start();self.addCleanup(p.stop)
    def test_fixed_scope_excludes_already_sent_hcm_and_unknown_arguments(self):
        with patch.object(r,'prepare',side_effect=AssertionError('read')),patch.object(r,'send',side_effect=AssertionError('send')):
            for args in ([],['--prepare','HCM'],['--prepare','ANY'],['--send','HN','freeze'],['--prepare','HN','--send']):self.assertEqual(2,r.main(self.profile,args))
    def test_monthly_requires_accepted_weekly_and_prepared_evidence(self):
        root=r.folder(self.profile,'HN');(root/'monthly').mkdir()
        (root/'monthly/manifest.json').write_text(json.dumps({'status':'prepared'}),encoding='utf-8')
        with patch.object(r.delivery,'deliver_batch',side_effect=AssertionError('send')):
            with self.assertRaisesRegex(r.IOErrorBoundary,'WEEKLY'):r.send(self.profile,'HN','monthly')
    def test_prepare_only_selected_department_and_preserves_blocked_report(self):
        rows=r.wf.freeze_plan([{**source(),'whse_dept':'HN'}],[],r.session.WEEK,r.session.WEEK)['insert_rows']
        rows[0]['frozen_at']='2026-09-15 09:00:00';calls=[]
        class DB:
            def execute(self,sql,args,limit,**kwargs):
                calls.append((sql,args));assert sql.startswith('SELECT')
                if 'slow_moving_baseline' in sql:return rows,False,{}
                if 'NOW(6)' in sql:return [{'observed_at':'2026-09-16 10:00:00'}],False,{}
                return [{'region':'HN','main_dept':'HN Sales','wecom_account':'sales','person_name':'Synthetic Sales','is_delete':'n','wecom_status':'payroll'}],False,{}
        @contextmanager
        def snapshots(**kwargs):yield DB()
        with patch.object(r.report_evidence.tools,'_ConsistentSnapshotExecutor',side_effect=snapshots),patch.object(r.report_evidence,'collect',side_effect=r.IOErrorBoundary('SYNTHETIC_INCOMPLETE')) as collect,patch.object(r.delivery,'deliver_batch',side_effect=AssertionError('send')):
            result=r.prepare(self.profile,'HN')
        self.assertEqual(['prepared','blocked','blocked'],[p['status'] for p in result['phases']]);self.assertFalse(result['sent'])
        self.assertEqual(['HN','HN'],[c.args[0] for c in collect.call_args_list])
        self.assertTrue(all('HN' in args for sql,args in calls if 'NOW(6)' not in sql))
    def test_attachment_changed_rejected_before_network(self):
        root=r.folder(self.profile,'HN');(root/'weekly').mkdir()
        file=root/'weekly/a.xlsx';file.write_bytes(b'old')
        n=r.session._notice('logical','role','body',[file])
        r.seal(root,'weekly',n,{})
        file.write_bytes(b'changed')
        with patch.object(r.delivery,'deliver_batch',side_effect=AssertionError('send')):
            with self.assertRaisesRegex(r.IOErrorBoundary,'ATTACHMENT'):r.send(self.profile,'HN','weekly')
if __name__=='__main__':unittest.main()
