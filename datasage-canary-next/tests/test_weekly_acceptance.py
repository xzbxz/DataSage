"""Counterexamples built through registered SQL over independent SQLite data."""
import unittest,importlib,copy,json
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import contextmanager
from unittest.mock import patch,Mock
from datetime import date
import test_business_contracts as base
import test_slow_baseline_net_outbound as net
from test_remediation_remaining_cases import plugin

PACKAGE=plugin.__name__
proof=importlib.import_module(PACKAGE+'.report_evidence')
inputs=importlib.import_module(PACKAGE+'.workflow_inputs')
local=importlib.import_module(PACKAGE+'.weekly_acceptance')
io=importlib.import_module(PACKAGE+'.workflow_io')

class WeeklyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.h=net.BaselineNetTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        h=self.h
        h.conn.create_function('NOW',-1,lambda *args:'2026-09-16T10:00:00')
        h.conn.create_function('UTC_TIMESTAMP',-1,lambda *args:'2026-09-16T02:00:00')
        h.conn.execute("UPDATE vk_ai.slow_moving_baseline SET week_label='2026-W38',frozen_at='2026-09-15T09:00:00',whse_dept='HCM'")
        h.conn.execute("UPDATE vk_dwd.whse_info_dwd SET dept_name='HCM' WHERE whse_id=1")
        h.add([(1,'HCM',1,11,'m','m',25,1.75,'handing',None,'n')])
        h.outgoing(10,rolls=1,dept='HCM',when='2026-09-16T09:00:00')
        h.returning(3,rolls=0.25,dept='HCM',when='2026-09-16T09:30:00')
        p=patch.object(proof.tools,'_business_today',return_value=date(2026,9,16));p.start();self.addCleanup(p.stop)
        self.evidence={'region':'HCM','period':'2026-W38','phase':'weekly','snapshot_marker':'synthetic-common-snapshot','partitioned':False,'packets':{},'evidence_origin':'synthetic'}
        self.evidence['snapshot_members']={name:'synthetic-common-snapshot' for name in ('pool','flow','summary','flow_total')}
        for request in proof.requests('HCM','2026-W38','weekly'):
            self.evidence['packets'][request['request_id']]=h.query(request)
            self.assertEqual('success',self.evidence['packets'][request['request_id']]['status'],self.evidence['packets'][request['request_id']])
        self.labels=[{'goods_sku_id':11,'whse_dept':'HCM','goods_no':'SYN-001','attr_val':'红'}]

    def adapt(self,e=None,labels=None):
        e=e or self.evidence
        return inputs.legacy_report_packet(e['packets']['pool'],e['packets']['flow'],self.labels if labels is None else labels,'HCM','2026-W38',evidence=e)

    def rows(self,key,e=None):return (e or self.evidence)['packets'][key]['results'][0]['rows']

    def test_complete_independent_evidence_and_render(self):
        adapted=self.adapt();self.assertTrue(adapted['detail_complete']);self.assertTrue(adapted['label_coverage']['complete'])
        self.assertEqual(0.75,adapted['summary']['net_outbound_rolls'])
        self.assertEqual(7,adapted['summary']['net_outbound_qty_by_unit']['m'])
        with TemporaryDirectory() as tmp:
            result=local.render_local(self.evidence,self.labels,Path(tmp)/'report')
            self.assertEqual('local_review_ready',result['status']);self.assertFalse(result['sent']);self.assertFalse(result['monthly_executed'])
            self.assertIn('非全周最终结果',(Path(tmp)/'report/message.txt').read_text(encoding='utf-8'))

    def test_missing_packet_truncated_or_missing_truncation_flag_rejected(self):
        for mode in ['packet','truncated','missing_flag']:
            e=copy.deepcopy(self.evidence)
            if mode=='packet':del e['packets']['summary']
            elif mode=='truncated':e['packets']['pool']['results'][0]['truncated']=True
            else:del e['packets']['pool']['results'][0]['truncated']
            with self.assertRaises(io.IOErrorBoundary):self.adapt(e)

    def test_lost_flow_row_and_duplicate_are_not_complete(self):
        for mode in ['drop','duplicate']:
            e=copy.deepcopy(self.evidence);r=e['packets']['flow']['results'][0]
            if mode=='drop':r['rows'].pop()
            else:r['rows'].append(copy.deepcopy(r['rows'][0]))
            r['row_count']=len(r['rows'])
            with self.assertRaises(io.IOErrorBoundary):self.adapt(e)

    def test_forged_count_cannot_hide_missing_zero_net_events(self):
        e=copy.deepcopy(self.evidence);r=e['packets']['flow']['results'][0]
        r['rows']=[];r['row_count']=0;r['data_state']='empty'
        with self.assertRaisesRegex(io.IOErrorBoundary,'MISMATCH'):self.adapt(e)

    def test_equal_population_count_does_not_hide_dropped_pool_or_duplicate(self):
        e=copy.deepcopy(self.evidence);r=e['packets']['pool']['results'][0]
        r['rows']*=2;r['row_count']=2
        for row in r['rows']:row['facts']['population_union_groups']=2
        with self.assertRaisesRegex(io.IOErrorBoundary,'DUPLICATE'):self.adapt(e)

    def test_numeric_detail_and_aggregate_disagreement_rejected(self):
        e=copy.deepcopy(self.evidence);self.rows('summary',e)[0]['facts']['closing_rolls']=999
        with self.assertRaisesRegex(io.IOErrorBoundary,'MISMATCH'):self.adapt(e)
        e=copy.deepcopy(self.evidence);self.rows('flow_total',e)[0]['facts']['metric_value']=999
        with self.assertRaisesRegex(io.IOErrorBoundary,'MISMATCH'):self.adapt(e)

    def test_missing_and_ambiguous_labels_do_not_change_numeric_counts(self):
        for labels,field in [([], 'missing_groups'),([*self.labels,{**self.labels[0],'goods_no':'CONFLICT'}],'ambiguous_groups')]:
            result=self.adapt(labels=labels)
            self.assertTrue(result['detail_complete']);self.assertFalse(result['label_coverage']['complete'])
            self.assertEqual(1,result['label_coverage'][field]);self.assertEqual(1,result['summary']['opening_skus'])
            self.assertEqual('Unknown',result['detail_rows'][0][0])
            with TemporaryDirectory() as tmp:
                report=local.render_local(self.evidence,labels,Path(tmp)/'out')
                self.assertEqual('label_review_required',report['status']);self.assertIn('REVIEW_ONLY',report['files'][0])

    def test_unknown_quantity_or_state_cannot_be_complete(self):
        for field in ['closing_quantity','closing_rolls']:
            e=copy.deepcopy(self.evidence);self.rows('pool',e)[0]['facts'][field]=None
            with self.assertRaisesRegex(io.IOErrorBoundary,'INCOMPLETE'):self.adapt(e)
        e=copy.deepcopy(self.evidence);self.rows('pool',e)[0]['states']['pool_movement_state']='Unassessable'
        with self.assertRaises(io.IOErrorBoundary):self.adapt(e)

    def test_mysql_decimal_strings_are_numeric_not_unknown_counts(self):
        e=copy.deepcopy(self.evidence)
        for packet in e['packets'].values():
            for row in packet['results'][0]['rows']:
                row['facts']={key:str(value) if type(value) in (int,float) else value for key,value in row['facts'].items()}
        result=self.adapt(e);self.assertTrue(result['detail_complete'])
        self.assertEqual(0.75,result['summary']['net_outbound_rolls'])

    def test_period_region_and_no_snapshot_rejected(self):
        e=copy.deepcopy(self.evidence);e['snapshot_marker']=None
        with self.assertRaisesRegex(io.IOErrorBoundary,'SNAPSHOT'):self.adapt(e)
        e=copy.deepcopy(self.evidence);e['packets']['flow']['results'][0]['applied_time_range']['baseline_week']='2026-W37'
        with self.assertRaisesRegex(io.IOErrorBoundary,'PERIOD'):self.adapt(e)
        e=copy.deepcopy(self.evidence)
        for dim in self.rows('flow',e)[0]['dimensions']:
            if dim['label']=='仓库部门':dim['value']='HN'
        with self.assertRaisesRegex(io.IOErrorBoundary,'REGION'):self.adapt(e)

    def test_mixed_snapshots_rejected(self):
        e=copy.deepcopy(self.evidence);e['snapshot_members']['flow']='other-snapshot'
        with self.assertRaisesRegex(io.IOErrorBoundary,'SNAPSHOT'):self.adapt(e)

    def test_historical_label_lookup_is_bound_to_month_department_and_report_skus(self):
        calls=[]
        class DB:
            def execute(self,sql,args,limit,**kwargs):
                calls.append((sql,args,limit));return [{'goods_sku_id':11,'goods_no':'HIST','attr_val':'Red','whse_dept':'HCM'}],False,{}
        labels=proof.monthly_labels(DB(),'HCM','2026-09',self.evidence['packets']['pool'],None)
        self.assertEqual('HIST',labels[0]['goods_no']);self.assertEqual(['2026-08','2026-09','HCM','11'],calls[0][1])
        self.assertIn('GROUP BY goods_sku_id,goods_no,attr_val,whse_dept',calls[0][0])
        self.assertEqual(10000,calls[0][2])

    def test_real_query_truncation_at_existing_100_row_boundary(self):
        self.h.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        self.h.conn.execute('DELETE FROM vk_dwd.delivery_return_detail_dwd')
        for i in range(101):self.h.outgoing(1,rolls=1,dept='HCM',when='2026-09-16T09:00:00',sales_id=1000+i)
        request=proof.requests('HCM','2026-W38','weekly')[1]
        self.assertEqual(100,request['limit'])
        self.evidence['packets']['flow']=self.h.query(request)
        with self.assertRaises(io.IOErrorBoundary):self.adapt()

    def test_local_entry_refuses_other_regions_monthly_and_expired_week(self):
        for field,value in [('region','HN'),('phase','monthly'),('period','2026-W37')]:
            e=copy.deepcopy(self.evidence);e[field]=value
            with TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(io.IOErrorBoundary,'SCOPE'):local.render_local(e,self.labels,Path(tmp)/'out')
        e=copy.deepcopy(self.evidence)
        for packet in e['packets'].values():packet['results'][0]['applied_time_range']['read_at']='2026-09-22T10:00:00'
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(io.IOErrorBoundary,'OUTSIDE_WEEK'):local.render_local(e,self.labels,Path(tmp)/'out')

    def test_empty_sales_is_zero_only_with_independent_zero_event_totals(self):
        self.h.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        self.h.conn.execute('DELETE FROM vk_dwd.delivery_return_detail_dwd')
        for request in proof.requests('HCM','2026-W38','weekly'):
            self.evidence['packets'][request['request_id']]=self.h.query(request)
        result=self.adapt();self.assertTrue(result['detail_complete'])
        self.assertEqual(0,result['summary']['net_outbound_rolls']);self.assertEqual([],result['sales_rows'])

    def test_failed_evidence_never_creates_workbook(self):
        self.evidence['packets']['pool']['results'][0]['truncated']=True
        with TemporaryDirectory() as tmp:
            with self.assertRaises(io.IOErrorBoundary):local.render_local(self.evidence,self.labels,Path(tmp)/'out')
            self.assertFalse(list(Path(tmp).rglob('*.xlsx')))

    def test_default_off_and_no_arbitrary_cli_arguments(self):
        with patch.object(proof,'collect') as collect,patch.object(io,'deliver_components') as send:
            for args in [[],['--read'],['--read-hcm-2026-w38','--send'],['--department','HN']]:
                self.assertEqual(2,local.main(base.PROFILE_ROOT,args))
            with self.assertRaisesRegex(io.IOErrorBoundary,'NOT_ENABLED'):local.run_local(base.PROFILE_ROOT)
            collect.assert_not_called();send.assert_not_called()

    def test_local_run_calls_only_fixed_hcm_weekly_and_no_business_side_effect(self):
        config=importlib.import_module(PACKAGE+'.local_report')
        with TemporaryDirectory() as tmp,patch.object(importlib.import_module(PACKAGE+'.contract_store'),'profile_root',return_value=Path(tmp)),patch.object(config,'configure_runtime'),patch.object(config,'_assert_local_context'),patch.object(proof,'collect',return_value=(self.evidence,self.labels)) as collect,patch.object(io,'deliver_components',side_effect=AssertionError('send')),patch.object(io,'freeze_current_week',side_effect=AssertionError('freeze')),patch.object(io,'read_recipients',side_effect=AssertionError('recipients')),patch.object(io.operations,'accept_snapshot',side_effect=AssertionError('price')):
            with patch.object(local,'render_local',return_value={'status':'local_review_ready'}) as renderer:
                result=local.run_local(Path(tmp),read_enabled=True)
                self.assertEqual((self.evidence,self.labels),renderer.call_args.args[:2])
            collect.assert_called_once_with('HCM','2026-W38','weekly','2026-W38')
            self.assertEqual('local_review_ready',result['status'])

    def test_collector_reuses_governed_sql_and_one_snapshot(self):
        h=self.h
        for table in ['vk_ai.slow_moving_baseline','vk_ods.slow_moving_goods_ods']:
            schema,name=table.split('.')
            columns={r[1] for r in h.conn.execute('PRAGMA '+schema+'.table_info('+name+')')}
            for column in ('goods_no','attr_val'):
                if column not in columns:h.conn.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' TEXT')
            h.conn.execute("UPDATE "+table+" SET goods_no='SYN-001',attr_val='Red'")
        calls=[]
        class DB:
            marker='synthetic-shared-snapshot'
            def execute(self,sql,params,limit,**kwargs):calls.append((sql,params));return h.execute(sql,params,limit)
        @contextmanager
        def snapshots():yield DB()
        with patch.object(importlib.import_module(PACKAGE+'.runtime_health'),'query_readiness_status',return_value={'ready':True}),patch.object(importlib.import_module(PACKAGE+'.local_report'),'_assert_local_context'):
            evidence,labels=proof.collect('HCM','2026-W38','weekly','2026-W38',snapshots=snapshots)
        result=self.adapt(evidence,labels)
        self.assertTrue(result['detail_complete']);self.assertEqual(7,len(calls))
        self.assertTrue(all(sql.startswith(('WITH','SELECT')) for sql,args in calls))
        self.assertTrue(all('HCM' in args for sql,args in calls if 'report_at' not in sql))
        self.assertFalse(any('employee' in sql or 'customer' in sql or 'INSERT ' in sql for sql,args in calls))

    def test_snapshot_pages_are_complete_stable_and_missing_page_is_rejected(self):
        h=self.h
        h.conn.execute('DELETE FROM vk_dwd.delivery_bill_barcode_detail_dwd')
        h.conn.execute('DELETE FROM vk_dwd.delivery_return_detail_dwd')
        for i in range(105):h.outgoing(1,rolls=1,dept='HCM',when='2026-09-16T09:00:00',sales_id=2000+i)
        calls=[]
        class DB:
            marker='synthetic-page-snapshot'
            def execute(self,sql,params,limit,**kwargs):
                calls.append((sql,params))
                if sql.startswith('SELECT goods_sku_id'):return self_labels,False,{}
                return h.execute(sql,params,limit)
        self_labels=self.labels
        @contextmanager
        def snapshots():yield DB()
        with patch.object(importlib.import_module(PACKAGE+'.runtime_health'),'query_readiness_status',return_value={'ready':True}),patch.object(importlib.import_module(PACKAGE+'.local_report'),'_assert_local_context'):
            e,labels=proof.collect('HCM','2026-W38','weekly','2026-W38',snapshots=snapshots)
            self.assertTrue(e['partitioned']);self.assertEqual([100,5],[p['row_count'] for p in e['page_proof']['flow']])
            result=self.adapt(e,labels);self.assertTrue(result['detail_complete']);self.assertEqual(105,result['summary']['net_outbound_rolls'])
            bad=copy.deepcopy(e);bad['page_proof']['flow'].pop()
            with self.assertRaisesRegex(io.IOErrorBoundary,'PAGE'):self.adapt(bad,labels)
            bad=copy.deepcopy(e);bad['page_proof']['flow'][1]['snapshot_marker']='other'
            with self.assertRaisesRegex(io.IOErrorBoundary,'PAGE'):self.adapt(bad,labels)
            with patch.object(proof,'MAX_PAGES',1):
                with self.assertRaisesRegex(io.IOErrorBoundary,'BUDGET'):proof.collect('HCM','2026-W38','weekly','2026-W38',snapshots=snapshots)
        pages=[(sql,args) for sql,args in calls if sql.startswith('WITH')]
        self.assertTrue(all('NOW(6)' not in sql and 'UTC_TIMESTAMP(6)' not in sql for sql,args in pages))
        self.assertTrue(all(args[-2]==101 for sql,args in pages))

    def test_production_keeps_all_weekly_before_monthly_and_gates_labels(self):
        adapted=self.adapt();events=[]
        regions=list(io.wf.policy()['regions'])
        def collect(region,period,phase,week,**kwargs):
            events.append(('collect',phase,region));return {'packets':{'pool':{},'flow':{}},'phase':phase},[]
        def adapt(pool,flow,labels,region,period,**kwargs):
            value=copy.deepcopy(adapted);value['period']=period;return value
        class Transport:
            def preflight(self,components):pass
            def send(self,item):events.append(('send',item['stage'],item['account']));return {'success':True,'message_id':'synthetic'}
        with TemporaryDirectory() as tmp,patch.object(proof,'collect',side_effect=collect),patch.object(inputs,'legacy_report_packet',side_effect=adapt),patch.object(io,'read_recipients',return_value={'regions':{}}),patch.object(io.wf,'report_recipients',return_value=['synthetic']):
            out=Path(tmp)/'out';out.mkdir();progress=io.Progress(Path(tmp),'slow_report','2026-W38')
            result=io._produce_and_execute(Path(tmp),'slow_report',{'send_enabled':True},out,'2026-W38','2026-09',progress,None,Transport(),None)
            self.assertEqual('success',result['status'])
            weekly=[e[2] for e in events if e[:2]==('collect','weekly')];monthly=[e[2] for e in events if e[:2]==('collect','monthly')]
            self.assertEqual(regions,weekly);self.assertEqual(regions,monthly)
            self.assertGreater(next(i for i,e in enumerate(events) if e[:2]==('collect','monthly')),max(i for i,e in enumerate(events) if e[:2]==('send','weekly_file')))
        events.clear();adapted['label_coverage']['complete']=False
        with TemporaryDirectory() as tmp,patch.object(proof,'collect',side_effect=collect),patch.object(inputs,'legacy_report_packet',side_effect=adapt),patch.object(io,'read_recipients',return_value={'regions':{}}):
            out=Path(tmp)/'out';out.mkdir()
            with self.assertRaisesRegex(io.IOErrorBoundary,'LABEL_REVIEW'):
                io._produce_and_execute(Path(tmp),'slow_report',{'send_enabled':True},out,'2026-W38','2026-09',io.Progress(Path(tmp),'slow_report','2026-W38'),None,Transport(),None)
            self.assertFalse(any(e[0]=='send' or e[1]=='monthly' for e in events))

    def test_monthly_adapter_still_accepts_independently_complete_monthly_evidence(self):
        import test_monthly_slow_pool as month
        h=month.MonthlyTests();h.setUp()
        try:
            h.snap();h.current();h.out(qty=10,rolls=1);h.ret(qty=3,rolls=0.25)
            e={'region':'HCM','period':'2026-09','phase':'monthly','snapshot_marker':'synthetic-monthly','partitioned':False,'packets':{},
               'snapshot_members':{k:'synthetic-monthly' for k in ('pool','flow','summary','flow_total')}}
            for request in proof.requests('HCM','2026-09','monthly'):e['packets'][request['request_id']]=h.h.query(request)
            result=inputs.legacy_report_packet(e['packets']['pool'],e['packets']['flow'],self.labels,'HCM','2026-09',evidence=e)
            self.assertTrue(result['detail_complete']);self.assertEqual(0.75,result['summary']['net_outbound_rolls'])
        finally:h.doCleanups()

if __name__=='__main__':unittest.main()
