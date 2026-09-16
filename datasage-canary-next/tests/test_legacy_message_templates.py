import unittest,importlib
import test_business_contracts as base
wf=importlib.import_module(base.TEST_PACKAGE+'.legacy_workflow')
templates=importlib.import_module(base.TEST_PACKAGE+'.legacy_message_templates')

class LegacyTemplateTests(unittest.TestCase):
    def test_weekly_restores_old_layout_without_losing_fractional_high_discount(self):
        summary={'opening_skus':2,'closing_skus':1,'opening_rolls':5,'closing_rolls':4,
                 'new':0,'exited':1,'net_outbound_rolls':0.75,'high_net_rolls':-0.25}
        text=wf.report_draft('HCM','2026-W38',summary,[{'sales_name':'B','net_rolls':-0.25},{'sales_name':'A','net_rolls':1}])
        self.assertIn('Weekly Report | W38',text);self.assertIn('2026/09/14 - 2026/09/19',text)
        self.assertIn('Slow-moving stock improved',text);self.assertIn('High-Discount: **-0.25 rolls**',text)
        self.assertLess(text.index('**A**'),text.index('**B**'))
        self.assertIn('<font color="comment">Detailed SKU list',text)
    def test_unknown_and_ht_units_are_not_replaced_with_zero(self):
        summary={'opening_skus':None,'closing_skus':2,'opening_rolls':None,'closing_rolls':4,'net_outbound_qty_by_unit':{'m':10,'kg':2}}
        text=wf.report_draft('HCM-HT','2026-09',summary,[],monthly=True)
        self.assertIn('unassessable',text);self.assertIn('High-Discount: **Unknown**',text)
        self.assertIn('kg: 2 | m: 10',text);self.assertIn('Month-to-date',text)
    def test_sales_restores_grouped_product_lines_and_signed_delta(self):
        text=wf.price_draft('sales',[{'goods_no':'SYN','customer_grade':'A','color_label':'Red','old_ddp_price':12,'new_ddp_price':10,'currency_no':'CNY'},
                                      {'goods_no':'SYN','customer_grade':'B','color_label':'Blue','old_ddp_price':8,'new_ddp_price':9,'currency_no':'CNY'}])
        self.assertIn('Ready Product Price Adjustment',text);self.assertIn('1 product(s) price changed:',text)
        self.assertIn('12 -> 10 (-2.00) CNY',text);self.assertIn('8 -> 9 (+1.00) CNY',text)
if __name__=='__main__':unittest.main()
