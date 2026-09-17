import unittest,importlib,copy
from datetime import timedelta
import test_business_contracts as base
from test_legacy_price_bridge import old_sales,sales,old_purchase,purchase,NOW
p=importlib.import_module(base.TEST_PACKAGE+'.workflow_price_continuity')

class ContinuityTests(unittest.TestCase):
    def test_purchase_membership_does_not_replace_quote_identity_or_multiply_rows(self):
        sql,args=p.op.build_observation({'kind':'purchase_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000})
        self.assertIn('SELECT p.goods_no,p.goods_name',sql)
        self.assertIn('EXISTS (SELECT 1 FROM pool x WHERE p.goods_no=x.goods_no)',sql)
        self.assertNotIn('FROM pool x LEFT JOIN',sql)
        self.assertIn('UNION ALL SELECT goods_no',sql)
        self.assertIn('GROUP BY BINARY x.goods_no',sql)
        self.assertIn('DENSE_RANK()',sql)
        self.assertEqual(sql.count('%s'),len(args))
    def test_legacy_sales_nominal_unchanged_advances_without_fake_history(self):
        result=p.plan('sales',[old_sales()],[sales()],NOW)
        self.assertEqual(result['document']['deliverable_event_count'],0)
        self.assertEqual(result['document']['continuation']['advanced_keys'],1)
        self.assertEqual(result['document']['events'][0]['before']['historical_fields_not_recorded'][0],'unit')
        self.assertNotIn('unit',result['document']['events'][0]['before']['basis'])
    def test_legacy_sales_change_is_nominal_not_economic(self):
        row=sales();row['ddp_price']='11'
        result=p.plan('sales',[old_sales()],[row],NOW)
        self.assertEqual(result['document']['deliverable_event_count'],1)
        self.assertIn('unverified',result['document']['events'][0]['comparison_level'])
    def test_bad_identity_and_absent_key_do_not_stop_other_quotes(self):
        old=old_purchase();missing={**old,'id':2,'goods_no':'ABSENT'}
        good={**purchase(),'tax_inclue_price':'13'};bad={**purchase(),'goods_no':'NO-QUOTE','supplier_no':None,'detail_id':None}
        result=p.plan('purchase',[old,missing],[good,bad],NOW)
        self.assertEqual(result['document']['deliverable_event_count'],1)
        self.assertEqual(result['document']['continuation']['advanced_keys'],1)
        retained=next(r for r in result['after'] if r['goods_no']=='ABSENT')
        self.assertEqual(retained,missing)
        self.assertEqual(len(result['anomalies']),2)
    def test_new_key_initializes_without_zero_price_alert(self):
        result=p.plan('purchase',[],[purchase()],NOW)
        self.assertEqual(result['document']['deliverable_event_count'],0)
        self.assertEqual(result['document']['continuation']['new_reference_keys'],1)
        repeat=p.plan('purchase',result['after'],[purchase()],NOW+timedelta(hours=1))
        self.assertEqual(repeat['document']['event_counts'],{'recorded_price_unchanged':1})
    def test_disappearance_then_return_compares_retained_reference_once(self):
        original=old_purchase();missing=p.plan('purchase',[original],[],NOW)
        self.assertEqual(missing['after'],[original])
        returned={**purchase(),'tax_exclue_price':'9'}
        resumed=p.plan('purchase',missing['after'],[returned],NOW+timedelta(hours=1))
        self.assertEqual(resumed['document']['deliverable_event_count'],1)
        repeat=p.plan('purchase',resumed['after'],[returned],NOW+timedelta(hours=2))
        self.assertEqual(repeat['document']['deliverable_event_count'],0)
    def test_duplicate_key_is_retained_then_repaired_without_lost_change(self):
        row=sales();row['ddp_price']='11'
        duplicate={**row,'color_label':None}
        held=p.plan('sales',[old_sales()],[row,duplicate],NOW)
        self.assertEqual(held['after'],[old_sales()]);self.assertEqual(held['document']['deliverable_event_count'],0)
        fixed=p.plan('sales',held['after'],[row],NOW+timedelta(hours=1))
        self.assertEqual(fixed['document']['deliverable_event_count'],1)
    def test_basis_change_isolated_and_old_value_retained(self):
        old=old_purchase();different={**purchase(),'currency_no':'CNY','tax_inclue_price':'100'}
        held=p.plan('purchase',[old],[different],NOW)
        self.assertEqual(held['after'],[old]);self.assertEqual(held['anomalies'][0]['reason'],'recorded_basis_changed')
    def test_precision_loss_isolated_from_other_keys(self):
        a=old_sales();b={**a,'goods_id':2,'id':2};x={**sales(),'ddp_price':'10.001'};y={**sales(),'goods_id':2}
        result=p.plan('sales',[a,b],[x,y],NOW)
        self.assertEqual(result['document']['continuation']['advanced_keys'],1)
        self.assertEqual(next(r for r in result['after'] if r['goods_id']==1),a)
    def test_missing_old_price_recovers_as_reference_not_price_change(self):
        old=old_purchase();old['tax_inclue_price']=None
        result=p.plan('purchase',[old],[purchase()],NOW)
        self.assertEqual(result['document']['event_counts'],{'recovered_reference_without_known_price':1})
        self.assertEqual(result['document']['deliverable_event_count'],0)
    def test_existing_ids_are_stable_and_new_ids_unique(self):
        a=old_purchase();b={**a,'id':9,'goods_no':'RETAINED'};new={**purchase(),'goods_no':'NEW'}
        result=p.plan('purchase',[a,b],[purchase(),new],NOW)
        ids={r['goods_no']:r['id'] for r in result['after']}
        self.assertEqual(ids,{'SYN-1':1,'RETAINED':9,'NEW':10})
    def test_same_quote_whitespace_alias_does_not_repeat_already_seen_change(self):
        import json
        canonical={**old_purchase(),'tax_inclue_price':'13','snapshot_at':NOW.isoformat(),'current_record':json.dumps({**purchase(),'tax_inclue_price':'13'})}
        alias={**old_purchase(),'id':2,'goods_no':'SYN-1 ','current_record':json.dumps({**purchase(),'goods_no':'SYN-1 '})}
        row={**purchase(),'goods_no':'SYN-1 ','tax_inclue_price':'13'}
        result=p.plan('purchase',[canonical,alias],[row],NOW+timedelta(hours=1))
        self.assertEqual(result['document']['deliverable_event_count'],0)
        self.assertEqual(result['document']['continuation']['notes']['source_quote_identity_reference_reused'],1)
        actual_change={**purchase(),'tax_inclue_price':'14'}
        changed=p.plan('purchase',result['after'],[actual_change],NOW+timedelta(hours=2))
        self.assertEqual(changed['document']['deliverable_event_count'],1)
        repeat=p.plan('purchase',changed['after'],[actual_change],NOW+timedelta(hours=3))
        self.assertEqual(repeat['document']['deliverable_event_count'],0)
    def test_whitespace_without_matching_quote_id_is_not_merged(self):
        import json
        reference={**old_purchase(),'snapshot_at':NOW.isoformat(),'current_record':json.dumps({**purchase(),'detail_id':99,'tax_inclue_price':'13'})}
        result=p.plan('purchase',[reference],[{**purchase(),'goods_no':'SYN-1 '}],NOW+timedelta(hours=1))
        self.assertEqual(result['document']['event_counts']['new_key_without_legacy_reference'],1)
        self.assertNotIn('source_quote_identity_reference_reused',result['document']['continuation']['notes'])

if __name__=='__main__':unittest.main()
