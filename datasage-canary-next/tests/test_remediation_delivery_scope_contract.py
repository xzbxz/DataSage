"""Delivery scope comes from the selected metric contract, not its spelling."""
import copy
from datetime import date
import unittest
from unittest.mock import patch
import test_business_contracts as shared

tools=shared.tools
contracts=shared.contracts
OBSERVED=date(2026,9,9)
NET={'allowed_scopes':['default_net'],'required':False,'default':'default_net'}
GROSS={'allowed_scopes':['explicit_gross','order_delivery_alignment'],'required':True,'default':None}

class DeliveryScopeContractTests(unittest.TestCase):
    def setUp(self):
        self.datasets,self.sem=contracts.execution_contracts('delivery')
        self.sem=copy.deepcopy(self.sem)

    def prepare(self,code,scope=None):
        request={'request_id':'scope_test','domain':'delivery','metric':code,'calendar_month':'2026-05'}
        if scope is not None:request['delivery_scope']=scope
        with patch.object(tools,'_contracts',return_value=(self.datasets,self.sem)):
            return tools._validate_request_plan_without_entities(request,observed_on=OBSERVED)

    def test_same_contract_renamed_metrics_keep_scope_and_sql(self):
        for original,renamed,policy,scope in [
            ('delivery_amount','gross_delivery_only_a_name',NET,None),
            ('gross_delivery_amount','renamed_before_returns',GROSS,'explicit_gross'),
        ]:
            with self.subTest(renamed=renamed):
                self.sem['metrics'][original]['delivery_scope_policy']=copy.deepcopy(policy)
                self.sem['metrics'][renamed]=copy.deepcopy(self.sem['metrics'][original])
                old,datasets,sem=self.prepare(original,scope)
                new,_,_=self.prepare(renamed,scope)
                sql1,params1,s1=tools._build_metric_query(old,datasets,sem,10,observed_on=OBSERVED)
                sql2,params2,s2=tools._build_metric_query(new,datasets,sem,10,observed_on=OBSERVED)
                self.assertEqual(sql1,sql2);self.assertEqual(params1,params2)
                self.assertEqual(s1['time_range'],s2['time_range']);self.assertEqual(s1['filters'],s2['filters'])
                projected=contracts._model_semantic_projection('delivery',sem)
                entry=next(m for m in projected['metrics'] if m['code']==renamed)
                self.assertEqual(policy,entry['delivery_scope_policy'])

    def test_missing_or_malformed_contract_never_falls_back_to_a_name(self):
        for policy in [None,{'allowed_scopes':[],'required':False,'default':'default_net'},
            {'allowed_scopes':['default_net'],'required':'false','default':'default_net'},
            {'allowed_scopes':['default_net'],'required':False,'default':'explicit_gross'},
            {'allowed_scopes':['default_net','default_net'],'required':False,'default':'default_net'}]:
            with self.subTest(policy=policy):
                definition=self.sem['metrics']['delivery_amount']
                if policy is None:definition.pop('delivery_scope_policy',None)
                else:definition['delivery_scope_policy']=policy
                with self.assertRaises(tools.QueryFailure) as failure:self.prepare('delivery_amount')
                self.assertEqual('CONTRACT_UNAVAILABLE',failure.exception.code)

    def test_unknown_names_are_rejected_independent_of_scope(self):
        for code in ('gross_delivery_custom','order_delivery_custom','net_custom'):
            for scope in (None,'default_net','explicit_gross','order_delivery_alignment'):
                with self.subTest(code=code,scope=scope):
                    with self.assertRaises(tools.QueryFailure) as failure:self.prepare(code,scope)
                    self.assertEqual('UNSUPPORTED_METRIC',failure.exception.code)

    def test_model_cannot_supply_its_own_scope_contract(self):
        request={'domain':'delivery','metric':'delivery_amount','delivery_scope_policy':GROSS}
        with self.assertRaises(tools.QueryFailure):tools._validate_request_plan_without_entities(request,observed_on=OBSERVED)

    def test_omitted_scope_fingerprint_uses_declared_default(self):
        policy={'allowed_scopes':['default_net','order_delivery_alignment'],'required':False,'default':'order_delivery_alignment'}
        self.sem['metrics']['order_amount']['delivery_scope_policy']=policy
        omitted,datasets,sem=self.prepare('order_amount')
        explicit,_,_=self.prepare('order_amount','order_delivery_alignment')
        _,_,a=tools._build_metric_query(omitted,datasets,sem,10,observed_on=OBSERVED)
        _,_,b=tools._build_metric_query(explicit,datasets,sem,10,observed_on=OBSERVED)
        definition=sem['metrics']['order_amount']
        self.assertEqual(tools._scope_fingerprints(omitted,a,definition,datasets),tools._scope_fingerprints(explicit,b,definition,datasets))

if __name__=='__main__':unittest.main()
