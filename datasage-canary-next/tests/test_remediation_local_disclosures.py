"""Local disclosure facts at the real public boundary, with independent arithmetic."""
import unittest
import test_remediation_remaining_cases as public

class LocalDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)

    def disclosure(self,response,identifier,request_ids):
        item=next((d for d in response.get('disclosures',[]) if d['disclosure_id']==identifier),None)
        self.assertIsNotNone(item,identifier)
        self.assertEqual(set(request_ids),set(item['request_ids']))
        self.assertTrue(item['text'])
        for private in ('vk_dwd','detail_deal_amount','exchange_rate','sales_id','detail_unsettled_amount'):
            self.assertNotIn(private,item['text'])
        for result in response['results']:
            self.assertEqual(result['request_id'] in request_ids,identifier in result.get('disclosure_refs',[]))

    def test_direct_return_attribution_preserves_each_salesperson_amount(self):
        self.h.insert('vk_dwd.sale_bill_split_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,sales_name,sales_id',
            [(50,6,'n','2026-08-15','Alice','s1'),(50,6,'n','2026-08-15','Bob','s2')])
        self.h.insert('vk_dwd.delivery_return_detail_dwd','return_amount_rmb,status,complnt_type,channel_type,is_inner_cus,statement_time,customer_dept,sales_name,sales_id',
            [(80,4,1,1,'n','2026-08-17','Synthetic','Alice','s1')])
        response=self.h.query(public.metric('allocated_net_delivery_amount','target',request_id='allocated',
            attribution_mode='salesperson_allocation',dimensions=['salesperson']),
            public.metric('delivery_amount','delivery',request_id='ordinary'))
        result=self.h.result(response,'allocated')
        actual={row['dimensions'][0]['value']:row['facts']['metric_value'] for row in result['rows']}
        # Allocated gross is 50 each; the recorded return is directly Alice's,
        # not another 50/50 split. This is an independent numerical counterexample.
        self.assertEqual({'Alice':-30,'Bob':50},actual)
        self.disclosure(response,'target.allocated_net_delivery_amount.attribution',{'allocated'})

    def test_detail_fx_before_sum_and_metric_specific_disclosures(self):
        self.h.insert('vk_dwd.receive_bill_detail_dwd','detail_receive_rmb,detail_deal_amount,exchange_rate,bill_status,bill_time',
            [(777,10,2,'C','2026-08-15'),(888,20,3,'C','2026-08-16')])
        self.h.conn.execute('ALTER TABLE vk_dwd.receive_return_bill_detail_dwd ADD COLUMN detail_deal_amount REAL')
        self.h.conn.execute('ALTER TABLE vk_dwd.receive_return_bill_detail_dwd ADD COLUMN exchange_rate REAL')
        self.h.insert('vk_dwd.receive_return_bill_detail_dwd','detail_return_rmb,detail_deal_amount,exchange_rate,bill_status,bill_time',
            [(1,4,2,'C','2026-08-15'),(1,3,5,'C','2026-08-16')])
        response=self.h.query(public.metric('actual_receipt_amount','receipt',request_id='actual'),
            public.metric('actual_refund_amount','receipt',request_id='refund'),
            public.metric('receipt_amount','receipt',request_id='registered'))
        self.assertEqual(80,self.h.result(response,'actual')['rows'][0]['facts']['metric_value'])
        self.assertEqual(23,self.h.result(response,'refund')['rows'][0]['facts']['metric_value'])
        self.assertEqual(1665,self.h.result(response,'registered')['rows'][0]['facts']['metric_value'])
        self.disclosure(response,'receipt.actual_receipt_amount.detail-conversion',{'actual'})
        self.disclosure(response,'receipt.actual_refund_amount.detail-conversion',{'refund'})

    def test_positive_balance_rule_is_independent_of_verification_status(self):
        self.h.conn.execute('ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_verification_status TEXT')
        self.h.insert('vk_dwd.receivable_bill_detail_dwd',
            'detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,bill_verification_status',
            [(100,1,'C','2026-08-01','n','c1','0'),(50,1,'C','2026-08-01','n','c2','1'),(-20,1,'C','2026-08-01','n','c3','0')])
        for changed in (False,True):
            if changed:self.h.conn.execute("UPDATE vk_dwd.receivable_bill_detail_dwd SET bill_verification_status='different_status'")
            response=self.h.query(public.metric('open_receivable_amount','receivable',month=None,request_id='balance'),
                public.metric('open_receivable_customer_count','receivable',month=None,request_id='count'))
            self.assertEqual(150,self.h.result(response,'balance')['rows'][0]['facts']['metric_value'])
            self.assertEqual(2,self.h.result(response,'count')['rows'][0]['facts']['metric_value'])
            self.disclosure(response,'receivable.open_receivable.scope',{'balance'})

if __name__=='__main__':unittest.main()

