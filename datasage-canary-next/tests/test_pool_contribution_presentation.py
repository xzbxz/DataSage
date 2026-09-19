import importlib
import copy
import unittest
from unittest.mock import patch
import test_business_contracts as base
import test_weekly_acceptance as weekly

inputs=importlib.import_module(base.TEST_PACKAGE+'.workflow_inputs')
evidence=importlib.import_module(base.TEST_PACKAGE+'.report_evidence')
contracts=importlib.import_module(base.TEST_PACKAGE+'.contracts')
capability=importlib.import_module(base.TEST_PACKAGE+'.capability_contract')

class PoolContributionPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _,sem=contracts.execution_contracts('inventory');metric='registered_slow_pool_baseline_net_outbound'
        cls.labels={code:value['label'] for code,value in capability.effective_dimension_definitions(sem,metric).items()}
    def packet(self,state,opening_quantity=None,closing_quantity=None,opening_rolls=None,closing_rolls=None):
        d=self.labels
        pool={'results':[{'rows':[{'dimensions':[{'label':d['product'],'value':'P'},{'label':d['pool_sku'],'value':'101'},{'label':d['warehouse_department'],'value':'HCM'},{'label':d['unit'],'value':'m'}],'facts':{'opening_quantity':opening_quantity,'closing_quantity':closing_quantity,'opening_rolls':opening_rolls,'closing_rolls':closing_rolls},'states':{'pool_movement_state':state}}]}]}
        flow={'results':[{'rows':[]}]};evidence={'region':'HCM','period':'2026-W38','packets':{'pool':pool,'flow':flow}}
        proof={'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{},'observed_from':'2026-09-15','observed_to':'2026-09-18'}
        return pool,flow,evidence,proof
    def execute(self,state,oq,cq,ors,crs,proof):
        pool,flow,packet_evidence,_=self.packet(state,oq,cq,ors,crs)
        with patch.object(evidence,'validate',return_value=proof):
            return inputs.legacy_report_packet(pool,flow,[], 'HCM','2026-W38',evidence=packet_evidence)['detail_rows'][0]
    def test_complete_new_fills_only_missing_opening_side(self):
        self.assertEqual(0,self.execute('New',None,20,None,2,{'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}})[4])
        self.assertIsNone(self.execute('New',10,None,1,None,{'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}})[6])
    def test_complete_exited_fills_only_missing_closing_side(self):
        self.assertEqual(0,self.execute('Exited',10,None,1,None,{'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}})[6])
    def test_incomplete_proof_never_fills_missing_side(self):
        proof={'population_complete':False,'quantities_complete':False,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}}
        row=self.execute('New',None,20,None,2,proof)
        self.assertIsNone(row[4]);self.assertIsNone(row[7])

    def test_present_side_missing_value_is_not_relabelled_as_pool_zero(self):
        proof={'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}}
        new=self.execute('New',None,None,None,None,proof)
        self.assertEqual(0,new[4]);self.assertIsNone(new[6]);self.assertEqual(0,new[7]);self.assertIsNone(new[8]);self.assertIsNone(new[9])
        exited=self.execute('Exited',None,None,None,None,proof)
        self.assertIsNone(exited[4]);self.assertEqual(0,exited[6]);self.assertIsNone(exited[7]);self.assertEqual(0,exited[8]);self.assertIsNone(exited[9])

    def test_observed_zero_and_non_boundary_state_are_not_forged(self):
        proof={'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}}
        zero=self.execute('No Change',0,0,0,0,proof)
        self.assertEqual([0,0,0,0,0],[zero[4],zero[6],zero[7],zero[8],zero[9]])
        reduced=self.execute('Reduced',None,None,None,None,proof)
        self.assertEqual([None,None,None,None,None],[reduced[4],reduced[6],reduced[7],reduced[8],reduced[9]])

    def test_raw_fact_copy_is_not_mutated(self):
        pool,flow,packet_evidence,_=self.packet('Exited',10,None,1,None)
        proof={'population_complete':True,'quantities_complete':True,'net_rolls':0,'high_net_rolls':0,'unit_totals':{}}
        with patch.object(evidence,'validate',return_value=proof):
            inputs.legacy_report_packet(pool,flow,[], 'HCM','2026-W38',evidence=packet_evidence)
        facts=packet_evidence['packets']['pool']['results'][0]['rows'][0]['facts']
        self.assertIsNone(facts['closing_quantity']);self.assertIsNone(facts['closing_rolls'])

    def test_real_validator_complete_gate_projects_exited(self):
        fixture=weekly.WeeklyAcceptanceTests('run');fixture.setUp();self.addCleanup(fixture.doCleanups)
        packet_evidence=copy.deepcopy(fixture.evidence)
        fixture.h.conn.execute('DELETE FROM vk_ods.slow_moving_goods_ods')
        for request in weekly.proof.requests('HCM','2026-W38','weekly'):
            packet_evidence['packets'][request['request_id']]=fixture.h.query(request)
        raw=packet_evidence['packets']['pool']['results'][0]['rows'][0]
        self.assertEqual('Exited',raw['states']['pool_movement_state'])
        self.assertIsNone(raw['facts']['closing_quantity'])
        result=inputs.legacy_report_packet(packet_evidence['packets']['pool'],packet_evidence['packets']['flow'],fixture.labels,'HCM','2026-W38',evidence=packet_evidence)
        row=result['detail_rows'][0]
        self.assertEqual([0,0,-2],[row[6],row[8],row[9]])
        self.assertEqual(0.75,row[10])
        self.assertIsNone(raw['facts']['closing_quantity']);self.assertIsNone(raw['facts']['closing_rolls'])

    def test_real_validator_rejects_forged_state_before_projection(self):
        fixture=weekly.WeeklyAcceptanceTests('run');fixture.setUp();self.addCleanup(fixture.doCleanups)
        packet_evidence=copy.deepcopy(fixture.evidence)
        packet_evidence['packets']['pool']['results'][0]['rows'][0]['states']['pool_movement_state']='New'
        with self.assertRaises(weekly.io.IOErrorBoundary):
            weekly.proof.validate(packet_evidence)

if __name__=='__main__':unittest.main()
