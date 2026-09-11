"""Catalog facts remain separate from presentation; producer enforces evidence validity."""
import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
import test_remediation_remaining_cases as public

class CatalogFactBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
    def test_registered_catalog_no_longer_orders_presentation_or_delegates_seal_checks(self):
        metrics=[('delivery','delivery_amount'),('receivable','formal_receivable_turnover_days'),('inventory','current_inventory_amount_rmb')]
        for domain,metric in metrics:
            for req in ({'domain':domain,'metric':metric},{'domain':domain,'view':'expert_index'}):
                with self.subTest(request=req):
                    response=self.h.invoke('datasage_catalog',{'requests':[req]});self.assertEqual('success',response['status'])
                    text=json.dumps(response,ensure_ascii=False)
                    for retired_directive in ('先呈现','同段或紧邻句','attestation_seal 与外层 claim_seal','清除正式周转数值、公式与推理主题'):
                        self.assertNotIn(retired_directive,text)
    def dso(self,row):
        args=public.metric('formal_receivable_turnover_days','receivable',request_id='dso',month=None,time_range={'start':'2025-08-01','end':'2026-08-01'})
        with patch.object(public.plugin.tools,'_execute_with_source',return_value=([copy.deepcopy(row)],False,self.h.source)):
            return self.h.query(args)
    def test_final_DSO_numeric_attestation_and_source_are_enforced_by_code(self):
        row={'metric_value':'40.5555555556','average_net_debt_rmb':'100.00','delivery_amount_rmb':'900.00','period_natural_days':365,'snapshot_month_count':13,'effective_month_count':'12'}
        response=self.dso(row);result=self.h.result(response,'dso');facts=result['rows'][0]['facts']
        self.assertAlmostEqual(float(facts['metric_value']),100/900*365,places=6)
        self.assertEqual('verified',facts['calculation_attestation']['status'])
        self.assertEqual(13,facts['calculation_attestation']['component_values']['snapshot_month_count'])
        self.assertEqual(64,len(response['source_evidence_ref']['source_ref_sha256']))
        ids={d['disclosure_id'] for d in response['disclosures']}
        for suffix in ('coverage','external-customer.scope','formula'):self.assertIn('receivable.formal-receivable-turnover.'+suffix,ids)
        for field,value in [('metric_value','999'),('average_net_debt_rmb',None),('delivery_amount_rmb','0'),('snapshot_month_count',12),('effective_month_count','13')]:
            with self.subTest(corruption=field):
                invalid=self.dso({**row,field:value})
                self.assertEqual('failed' if field=='metric_value' else 'success',invalid['status'])
                r=next(item for item in invalid['results'] if item['request_id']=='dso')
                if not r['rows']:
                    self.assertEqual('undefined',r['data_state']);self.assertEqual('EVIDENCE_INTEGRITY_INVALID',r['error']['code'])
                else:
                    self.assertTrue(all(item['facts']['calculation_attestation']['status']=='undefined' for item in r['rows']))
                self.assertTrue(all(item['facts'].get('metric_value') is None for item in r['rows']))
                self.assertNotIn('receivable.formal-receivable-turnover.formula',r.get('disclosure_refs',[]))
    def test_delivery_reconciliation_is_structural_and_scope_survives(self):
        self.h.delivery([(50,6,'n','2026-07-15','A'),(100,6,'n','2026-08-15','A')])
        response=self.h.query(public.metric('delivery_amount','delivery',request_id='change',comparison={'kind':'previous_period'},complete_change_decomposition={'dimension':'department'}))
        r=self.h.result(response,'change');self.assertEqual(Decimal(50),Decimal(r['change_reconciliation']['overall_delta']))
        self.assertTrue(any('structural_contribution' in row['allowed_relations'] for row in r['rows']))
        self.assertFalse(any('causal' in relation for row in r['rows'] for relation in row['allowed_relations']))
        self.assertTrue(r['applied_time_range']);self.assertTrue(response['source_evidence_ref'])
    def test_current_inventory_observation_fact_is_retained(self):
        self.h.insert('vk_dw.inventory_barcode_detail_dw','ddp_amount_rmb,status',[(100,1),(50,2),(900,9)])
        response=self.h.query(public.metric('current_inventory_amount_rmb','inventory',request_id='inventory',month=None))
        r=self.h.result(response,'inventory');self.assertEqual(150,r['rows'][0]['facts']['metric_value'])
        self.assertIn('2026-09-02',json.dumps(r['applied_time_range']))
        catalog=self.h.invoke('datasage_catalog',{'requests':[{'domain':'inventory','metric':'current_inventory_amount_rmb'}]})
        text=json.dumps(catalog,ensure_ascii=False);self.assertIn('ETL',text)
        self.assertTrue(response['source_evidence_ref'])

if __name__=='__main__':unittest.main()
