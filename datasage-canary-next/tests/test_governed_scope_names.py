"""Synthetic public-boundary regressions for code-shaped governed names."""
import json,unittest
from copy import deepcopy
from unittest.mock import patch
from test_remediation_remaining_cases import plugin
import test_customer_history as history_fixture


class GovernedScopeNameTests(unittest.TestCase):
    def setUp(self):
        self.sem=plugin.contract_store.read_yaml('plugins/datasage-query/contracts/inventory-semantics.yaml')
    def candidate(self,name='SYNTH-CODE',code='SYNTH-CODE',identity='internal-42',kind='product',public=False):
        return plugin.entities._candidate_rows([{'entity_type':kind,'canonical_id':identity,'canonical_code':code,'display_name':name,'match_rank':0}],
            'inventory',metric='registered_slow_historical_customers',attribution_mode=None,semantics=self.sem,public=public)[0]
    def evidence(self,candidate):
        return {'entity_type':candidate['entity_type'],'filter_role':candidate.get('filter_role','product'),
            'canonical_ids':[candidate['canonical_id']],'canonical_codes':[candidate['canonical_code']],
            'filter_values':[candidate['canonical_id']],'display_names':[candidate['display_name']]}
    def project(self,evidence):
        return plugin.tools._public_scope_entities([evidence],self.sem,'registered_slow_historical_customers')
    def denied(self,evidence):
        with self.assertRaises(plugin.tools.QueryFailure) as caught:self.project(evidence)
        self.assertEqual('ENTITY_PUBLIC_IDENTITY_UNAVAILABLE',caught.exception.code)
    def test_governed_name_equal_code_is_public_but_raw_id_is_not(self):
        e=self.evidence(self.candidate())
        output=self.project(e)
        self.assertEqual('SYNTH-CODE',output[0]['display_name'])
        self.assertNotIn('internal-42',json.dumps(output))
        self.assertEqual(output,self.project(deepcopy(e)))
        self.denied(self.evidence(self.candidate(name='internal-42')))
    def test_normal_name_and_customer_path(self):
        self.assertEqual('Public product',self.project(self.evidence(self.candidate(name='Public product')))[0]['display_name'])
        output=self.project(self.evidence(self.candidate(kind='customer')))
        self.assertEqual('customer',output[0]['role'])
        self.assertEqual('SYNTH-CODE',output[0]['display_name'])
    def test_missing_name_and_code_or_id_fallback_stay_blocked(self):
        for name,code in [(None,'SYNTH-CODE'),('','SYNTH-CODE'),(' ', 'SYNTH-CODE'),(None,None)]:
            with self.subTest(name=name,code=code):self.denied(self.evidence(self.candidate(name=name,code=code)))
    def test_long_and_normalized_fallbacks_stay_blocked_before_display_shortening(self):
        long_value='SYNTHETIC_'+('x'*120)
        self.denied(self.evidence(self.candidate(name=None,code=long_value)))
        self.denied(self.evidence(self.candidate(name=None,code=None,identity=long_value)))
        self.denied(self.evidence(self.candidate(name=None,code='  SYNTH-CODE\u200b ')))
        output=self.project(self.evidence(self.candidate(name=long_value,code=long_value)))
        self.assertLessEqual(len(output[0]['display_name']),80)
    def test_plain_forged_flags_and_json_roundtrip_cannot_grant_provenance(self):
        trusted=self.evidence(self.candidate())
        forged=json.loads(json.dumps(trusted))
        forged.update(trusted=True,display_name_source='governed',resolution_path='master_exact_preflight')
        self.denied(forged)
        with self.assertRaises(ValueError):plugin.entities._GovernedDisplayName('SYNTH-CODE')
    def test_public_resolver_has_no_transferable_trust_type(self):
        candidate=self.candidate(public=True)
        self.assertFalse(plugin.entities.is_governed_display_name(candidate['display_name']))
        self.denied(self.evidence(candidate))
    def test_identity_field_as_display_source_and_filter_value_collisions_stay_blocked(self):
        registry=deepcopy(plugin.entities._registry())
        registry['candidate_sources']['product']['display_column']=registry['candidate_sources']['product']['code_column']
        with patch.object(plugin.entities,'_registry',lambda:registry):candidate=self.candidate()
        self.denied(self.evidence(candidate))
        e=self.evidence(self.candidate());e['filter_values']=['SYNTH-CODE'];self.denied(e)
    def test_unregistered_alias_cannot_self_certify_code_name(self):
        e={'filter_role':'warehouse_department','display_names':['SYNTH-DEPT'],'filter_values':['SYNTH-DEPT'],'resolution_path':'registered_exact','trusted':True}
        self.denied(e)
    def test_provenance_cannot_move_to_another_role_or_execution_identity(self):
        e=self.evidence(self.candidate())
        changed=deepcopy(e);changed['entity_type']='customer';changed['filter_role']='customer';self.denied(changed)
        changed=deepcopy(e);changed['canonical_ids']=['other-internal-id'];changed['filter_values']=['other-internal-id'];self.denied(changed)
        with self.assertRaises(AttributeError):e['display_names'][0]._origin=('fake',)
        registry=deepcopy(plugin.entities._registry())
        registry['candidate_sources']['product']['table']='unregistered.synthetic_source'
        with patch.object(plugin.entities,'_registry',lambda:registry):self.denied(e)
    def test_real_registered_query_binds_code_shaped_product_name(self):
        h=history_fixture.CustomerHistoryTests();h.setUp();self.addCleanup(h.doCleanups)
        h.out();h.h.conn.execute('UPDATE vk_dwd.goods_detail_dwd SET goods_name=goods_no')
        result=h.result(metric_filters={'product':'P1','warehouse_department':'A'})
        self.assertEqual('success',result['status'])
        public=json.dumps(result,ensure_ascii=False)
        self.assertIn('P1',public)
        self.assertTrue(any(e['role']=='product' and e['display_name']=='P1' for e in result.get('scope_entities',[])))

    def test_public_request_cannot_supply_internal_binding_or_name_proof(self):
        h=history_fixture.CustomerHistoryTests();h.setUp();self.addCleanup(h.doCleanups)
        for field in ['_entity_bindings','resolved_entities','_display_name_origin']:
            with self.subTest(field=field):
                before=len(h.h.sql_trace)
                result=h.query(**{field:{'trusted':True,'display_names':['SYNTH-CODE']}})
                self.assertNotEqual('success',result.get('status'))
                self.assertEqual(before,len(h.h.sql_trace))


if __name__=='__main__':unittest.main()
