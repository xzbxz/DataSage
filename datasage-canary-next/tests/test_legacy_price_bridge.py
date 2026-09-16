import unittest,importlib,copy
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import test_business_contracts as base

b=importlib.import_module(base.TEST_PACKAGE+'.legacy_price_bridge');op=b.op
NOW=datetime(2026,9,16,15)

def old_sales():return {'id':1,'goods_id':1,'dept':'HCM','customer_grade':'A','color_label':None,'ddp_price':'10','currency_no':'USD','matched_detail_id':5,'snapshot_at':'2026-09-16 14:00:00'}
def sales():return {'goods_id':1,'goods_no':'SYN-1','goods_name':'Synthetic','dept':'HCM','customer_grade':'A','color_label':'','ddp_price':'10','currency_no':'USD','unit':'m','unit_cuur':'m','is_inclue_tax':'n','detail_id':5,'effective_date':'2026-01-01','expiration_date':None,'gmt_modified':'2026-09-16 14:30:00','observed_at':NOW.isoformat()}
def old_purchase():return {'id':1,'goods_no':'SYN-1','color_label':None,'supplier_no':'SYN-S','supplier_name':'Synthetic supplier','goods_name':'Synthetic','tax_inclue_price':'12','tax_exclue_price':'10','currency_no':'USD','unit_cuur':'m','snapshot_at':'2026-09-16 14:00:00'}
def purchase():return {'goods_no':'SYN-1','goods_name':'Synthetic','color_label':'','supplier_no':'SYN-S','supplier_name':'Synthetic supplier','tax_inclue_price':'12','tax_exclue_price':'10','currency_no':'USD','unit_cuur':'m','detail_id':5,'effective_date':None,'expiration_date':None,'gmt_modified':'2026-09-16 14:30:00','observed_at':NOW.isoformat()}

class LegacyPriceBridgeTests(unittest.TestCase):
    def test_existing_sales_reference_compares_not_cold_start_and_never_invents_basis(self):
        doc=b.compare('sales',[old_sales()],[sales()],NOW)
        self.assertEqual({'recorded_price_unchanged':1},doc['event_counts'])
        before=doc['events'][0]['before'];self.assertNotIn('unit',before['basis']);self.assertEqual({},before['validity'])
        self.assertIn('is_inclue_tax',before['historical_fields_not_recorded']);self.assertEqual(0,doc['deliverable_event_count'])
    def test_sales_nominal_change_explicitly_marks_unverified_historical_basis(self):
        row=sales();row['ddp_price']='10.0001'
        doc=b.compare('sales',[old_sales()],[row],NOW)
        self.assertEqual('legacy_nominal_price_changed',doc['events'][0]['event'])
        self.assertIn('unverified',doc['events'][0]['comparison_level'])
        change=b.changes(doc)[0];self.assertEqual('10.0001',change['new_ddp_price']);self.assertEqual('10',change['old_ddp_price'])
    def test_purchase_keeps_two_tax_sides_and_validity_unknown(self):
        row=purchase();row['tax_exclue_price']='9'
        doc=b.compare('purchase',[old_purchase()],[row],NOW)
        self.assertEqual(['tax_exclue_price'],doc['events'][0]['changed_price_fields'])
        change=b.changes(doc)[0];self.assertEqual('unknown_validity',change['validity_state']);self.assertEqual(change['old_inc'],change['new_inc'])
    def test_currency_or_purchase_unit_changes_are_not_pure_price_alerts(self):
        for side,old,current,field in [('sales',old_sales(),sales(),'currency_no'),('purchase',old_purchase(),purchase(),'unit_cuur')]:
            current[field]='CHANGED'
            doc=b.compare(side,[old],[current],NOW)
            self.assertEqual('recorded_basis_changed',doc['events'][0]['event']);self.assertEqual([],b.changes(doc))
    def test_null_prices_not_zero_and_ambiguous_current_not_arbitrary(self):
        old=old_purchase();old['tax_exclue_price']=None
        self.assertEqual('unresolved_legacy_price',b.compare('purchase',[old],[purchase()],NOW)['events'][0]['event'])
        doc=b.compare('sales',[old_sales()],[sales(),sales()],NOW)
        self.assertEqual(0,doc['deliverable_event_count']);self.assertEqual('unresolved_current_record',doc['events'][0]['event'])
    def test_duplicate_normalized_keys_and_future_timestamp_rejected(self):
        duplicate=old_sales();duplicate.update(id=2,color_label='')
        with self.assertRaisesRegex(op.OperationError,'DUPLICATE'):b.compare('sales',[old_sales(),duplicate],[sales()],NOW)
        future=old_purchase();future['snapshot_at']='2026-09-17 00:00:00'
        with self.assertRaisesRegex(op.OperationError,'FUTURE'):b.compare('purchase',[future],[purchase()],NOW)
    def test_new_missing_and_unknown_identity_are_not_manufactured_changes(self):
        doc=b.compare('sales',[],[sales()],NOW)
        self.assertEqual('new_key_without_legacy_reference',doc['events'][0]['event'])
        doc=b.compare('sales',[old_sales()],[],NOW);self.assertEqual('absent_from_current_selection',doc['events'][0]['event'])
        row=purchase();row['supplier_no']=None
        doc=b.compare('purchase',[old_purchase()],[row],NOW);self.assertEqual(0,doc['deliverable_event_count'])
    def test_external_reference_cannot_be_accepted_as_new_local_baseline(self):
        doc=b.compare('sales',[old_sales()],[sales()],NOW);doc['scope_hash']='synthetic'
        with TemporaryDirectory() as tmp:
            path=op.save_observation(Path(tmp),'test',doc)
            with self.assertRaisesRegex(op.OperationError,'READ_ONLY'):op.accept_snapshot(Path(tmp),'test',path.stem)
            self.assertFalse((path.parent/'accepted.json').exists())
    def test_read_bridge_uses_one_snapshot_and_exact_tables_no_dml(self):
        calls=[]
        class DB:
            marker='synthetic-snapshot'
            def execute(self,sql,params,limit):
                calls.append(sql)
                if sql.startswith('SELECT NOW'):rows=[{'observed_at':NOW.isoformat(),'observed_utc':'2026-09-16 07:00:00'}]
                elif 'FROM vk_ai.ready_goods_price_snapshot' in sql:rows=[old_sales()]
                else:rows=[sales()]
                return rows,False,{}
        @contextmanager
        def snapshots():yield DB()
        binding={'kind':'sales_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000,'reference_source':'legacy_database'}
        doc=b.observe(binding,snapshots=snapshots)
        self.assertEqual(3,len(calls));self.assertTrue(all(sql.startswith(('SELECT','WITH')) for sql in calls))
        self.assertEqual('synthetic-snapshot',doc['snapshot_marker']);self.assertEqual(1,doc['reference']['rows'])
    def test_truncated_old_reference_stops_before_current_read(self):
        calls=[]
        class DB:
            marker='snapshot'
            def execute(self,sql,params,limit):
                calls.append(sql)
                return ([{'observed_at':NOW.isoformat()}],False,{}) if sql.startswith('SELECT NOW') else ([old_sales()],True,{})
        @contextmanager
        def snapshots():yield DB()
        with self.assertRaisesRegex(op.OperationError,'REFERENCE_TRUNCATED'):
            b.observe({'kind':'sales_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000},snapshots=snapshots)
        self.assertEqual(2,len(calls))
    def test_operations_dispatch_uses_bridge_without_loading_local_accepted_pointer(self):
        with patch.object(b,'observe',return_value={'status':'success'}) as observe,patch.object(op,'load_baseline',side_effect=AssertionError('local reference')):
            op.execute(Path('unused'),'test',{'kind':'purchase_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000,'reference_source':'legacy_database'})
            observe.assert_called_once()
if __name__=='__main__':unittest.main()
