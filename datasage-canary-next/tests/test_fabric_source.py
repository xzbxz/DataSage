"""Synthetic source facts through registered tools; no real identities or network."""
import unittest
from unittest.mock import patch
import yaml
import test_remediation_remaining_cases as public
from test_remediation_remaining_cases import metric, facts


class FabricTests(unittest.TestCase):
    insert = public.RemainingCaseTests.insert
    invoke = public.RemainingCaseTests.invoke
    query = public.RemainingCaseTests.query
    result = public.RemainingCaseTests.result
    execute = public.RemainingCaseTests.execute

    def setUp(self):
        public.RemainingCaseTests.setUp(self)
        self.conn.create_function('NOW', -1, lambda *args: '2026-09-12T10:00:00')
        self.conn.create_function('UTC_TIMESTAMP', -1, lambda *args: '2026-09-12T02:00:00')
        self.conn.create_function('YEAR', 1, lambda value: None if value is None else int(value[:4]))
        self.conn.execute("ATTACH DATABASE ':memory:' AS vk_ads")
        self.conn.execute('ALTER TABLE vk_dw.inventory_barcode_detail_dw ADD COLUMN id INTEGER')
        datasets = yaml.safe_load((public.PLUGIN_ROOT/'contracts/datasets.yaml').read_text(encoding='utf-8'))['datasets']
        self.fields = {}
        integers = {'barcode_detail_id', 'inventory_id', 'goods_id', 'goods_sku_id', 'source_channel_cnt', 'root_candidate_cnt'}
        for side in ('delivery', 'inventory'):
            table = 'vk_ads.'+side+'_barcode_source_ads'
            self.fields[side] = datasets[table]['allowed_columns']
            columns = ','.join(f'`{f}` '+('INTEGER' if f in integers else 'REAL' if f in {'piece_num','ddp_amount_rmb','sale_amount_rmb'} else 'TEXT') for f in self.fields[side])
            self.conn.execute(f'CREATE TABLE {table}({columns})')

    def row(self, side='delivery', key=1, status=1, **changes):
        values = dict(goods_id=1, goods_name='Synthetic product', goods_sku_id=101,
            piece_num=10, ddp_amount_rmb=100, source_channel='中国订单', source_channel_set='中国订单',
            source_channel_cnt=1, source_confidence='高', trace_confidence='高', source_ambiguity_flag='n',
            root_candidate_cnt=1, inventory_cohort='本期形成', cohort_basis_time='2026-01-01',
            cohort_basis_type='首次入库时间', cohort_confidence='高', first_in_whse_time='2026-01-01',
            first_in_whse_time_max='2026-01-01', etl_time='2026-09-12T09:00:00',
            barcode_detail_id=key, inventory_id=key, delivery_time='2026-08-15', current_customer_dept='BKK-HT-X',
            is_low_price='y', sale_amount_rmb=50, amount_exception_flag='n', is_inner_cus='n', is_ccbs_cus='n', bill_type='bulk',
            whse_dept='Synthetic warehouse', is_stagnant='y', whse_org='Synthetic org', whse_type='普通仓')
        values.update(changes)
        cols = self.fields[side]
        self.insert('vk_ads.'+side+'_barcode_source_ads', ','.join(cols), [tuple(values[f] for f in cols)])
        if side == 'inventory' and status is not None:
            self.insert('vk_dw.inventory_barcode_detail_dw','id,status,ddp_amount_rmb',[(key,status,values['ddp_amount_rmb'])])

    def run_source(self, side='delivery', **kw):
        return self.query(metric('fabric_'+side+'_source_summary',side,month=None,**kw))

    def value(self, side='delivery', **kw):
        return facts(self.result(self.run_source(side,**kw)))[0]

    def test_registered_source_scope_amounts_units_and_clock(self):
        self.row()
        f=self.value()
        self.assertEqual(1,f['metric_value']); self.assertEqual(10,f['fabric_rolls'])
        self.assertEqual(50,f['fabric_tagged_ddp_gap_rmb']); self.assertEqual(1,f['fabric_tag_rate'])
        self.assertEqual('2026-09-12T10:00:00',f['fabric_read_at'])
        self.assertNotIn('goods_num',f)
        row=self.result(self.run_source())['rows'][0]
        self.assertEqual('卷',row['fact_units']['fabric_rolls'])
        self.assertEqual('人民币元',row['fact_units']['fabric_tagged_ddp_gap_rmb'])
        self.assertEqual('比例',row['fact_units']['fabric_tag_rate'])

    def test_ht_contains_default_and_explicit_non_ht(self):
        self.row(bill_type='sample'); self.row(key=2,current_customer_dept='BKK',piece_num=20)
        self.assertEqual(30,self.value()['fabric_rolls'])
        self.assertEqual(20,self.value(metric_filters={'fabric_ht':'非HT'})['fabric_rolls'])
        self.assertEqual(10,self.value(metric_filters={'fabric_ht':'含HT'})['fabric_rolls'])

    def test_missing_values_unknown_label_and_known_zero(self):
        self.row(piece_num=None,ddp_amount_rmb=None,is_low_price=None)
        self.row(key=2,is_low_price='n',piece_num=0,sale_amount_rmb=0,ddp_amount_rmb=0)
        f=self.value()
        self.assertIsNone(f['fabric_rolls']); self.assertEqual(0,f['fabric_known_rolls'])
        self.assertIsNone(f['fabric_tagged_rolls']); self.assertEqual(0,f['fabric_known_tagged_rolls'])
        self.assertIsNone(f['fabric_tag_rate']); self.assertEqual(1,f['fabric_unknown_tags'])
        self.assertEqual(1,f['fabric_missing_ddp'])

    def test_negative_values_are_retained_and_rates_withheld(self):
        self.row(piece_num=-1,ddp_amount_rmb=-20,sale_amount_rmb=-10)
        f=self.value(); self.assertEqual(-1,f['fabric_rolls']); self.assertEqual(-10,f['fabric_tagged_ddp_gap_rmb'])
        self.assertIsNone(f['fabric_tag_rate']); self.assertIsNone(f['fabric_tag_contribution'])

    def test_contribution_full_population_and_unknown_channels(self):
        self.row(piece_num=10); self.row(key=2,source_channel='无来源',piece_num=20)
        self.row(key=3,source_channel=None,piece_num=30)
        all_rows=facts(self.result(self.run_source(dimensions=['fabric_channel'])))
        self.assertEqual(3,len(all_rows)); self.assertAlmostEqual(1,sum(f['fabric_tag_contribution'] for f in all_rows))
        one=facts(self.result(self.run_source(dimensions=['fabric_channel'],limit=1)))[0]
        self.assertEqual(60,one['fabric_scope_tagged_rolls']); self.assertEqual(3,one['fabric_population_groups'])
        self.assertLess(one['fabric_tag_contribution'],1)

    def test_month_contribution_uses_each_month_population(self):
        self.row(piece_num=10); self.row(key=2,source_channel='无来源',piece_num=30)
        self.row(key=3,piece_num=20,delivery_time='2026-07-15')
        self.row(key=4,piece_num=20,source_channel='无来源',delivery_time='2026-07-15')
        rows=facts(self.result(self.run_source(dimensions=['fabric_channel'],time_bucket='month')))
        self.assertEqual([.25,.5,.5,.75],sorted(f['fabric_tag_contribution'] for f in rows))

    def test_inventory_transit_and_on_hand_current_state(self):
        self.row('inventory',status=3); self.row('inventory',key=2,status=2,piece_num=20)
        self.assertEqual(30,self.value('inventory')['fabric_rolls'])
        f=self.value('inventory',inventory_scope='on_hand')
        self.assertEqual(20,f['fabric_rolls']); self.assertEqual(0,f['fabric_in_transit_rows'])

    def test_inventory_unknown_and_duplicate_base_do_not_multiply_rows(self):
        self.row('inventory',status=None); self.row('inventory',key=2,piece_num=20)
        self.insert('vk_dw.inventory_barcode_detail_dw','id,status,ddp_amount_rmb',[(2,2,100)])
        self.assertEqual(30,self.value('inventory')['fabric_rolls'])
        f=self.value('inventory',inventory_scope='on_hand')
        self.assertEqual(2,f['fabric_group_rows']); self.assertIsNone(f['fabric_rolls'])
        self.assertEqual(2,f['fabric_current_status_unknown'])

    def test_duplicate_source_identity_withholds_all_amounts(self):
        self.row(); self.row()
        f=self.value(); self.assertEqual(1,f['fabric_identity_errors'])
        self.assertIsNone(f['metric_value']); self.assertIsNone(f['fabric_rolls']); self.assertIsNone(f['fabric_known_rolls'])

    def test_formation_pending_preserves_source_label(self):
        self.row(cohort_basis_time=None)
        self.row(key=2,cohort_confidence='低')
        self.row(key=3,root_candidate_cnt=2,first_in_whse_time='2025-01-01')
        f=self.value(); self.assertEqual(3,f['fabric_formation_pending']); self.assertEqual(1,f['fabric_cross_year_roots'])
        result=self.result(self.run_source(dimensions=['fabric_formation','fabric_cohort_label']))
        self.assertEqual(3,len(result['rows']))
        self.assertIn('本期形成',str(result['rows'])); self.assertIn('待核验',str(result['rows']))

    def test_incomplete_root_timing_and_future_basis_are_pending(self):
        self.row(root_candidate_cnt=2,first_in_whse_time_max=None)
        self.row(key=2,cohort_basis_time='2027-01-01')
        self.row(key=3,cohort_confidence=None)
        self.assertEqual(3,self.value()['fabric_formation_pending'])

    def test_inventory_missing_tagged_ddp_and_missing_rolls_remain_distinct(self):
        self.row('inventory',piece_num=None,ddp_amount_rmb=None)
        self.row('inventory',key=2,is_stagnant='n',piece_num=20,ddp_amount_rmb=200)
        f=self.value('inventory')
        self.assertIsNone(f['fabric_ddp_rmb']);self.assertEqual(200,f['fabric_known_ddp_rmb'])
        self.assertIsNone(f['fabric_tagged_ddp_rmb']);self.assertEqual(0,f['fabric_known_tagged_ddp_rmb'])
        self.assertIsNone(f['fabric_tag_rate']);self.assertEqual(1,f['fabric_unresolved_tagged_rolls_rows'])

    def test_registered_catalog_exposes_correct_scope_and_no_generic_comparison(self):
        response=self.invoke('datasage_catalog',{'requests':[{'domain':'delivery','metric':'fabric_delivery_source_summary'}]})
        self.assertEqual('success',response['status'])
        definition=response['results'][0]['metric']
        self.assertEqual('source_recorded',definition['delivery_scope_policy']['default'])
        self.assertFalse(definition['scope_flags']['external_customers_only'])
        self.assertEqual([],definition['comparison_kinds'])
        self.assertIn('fabric_sku',definition['allowed_dimensions'])

    def test_policy_drift_retained_as_unknown_scope(self):
        self.row(is_inner_cus='y'); self.row(key=2)
        f=self.value(); self.assertEqual(2,f['fabric_group_rows']); self.assertIsNone(f['metric_value'])
        self.assertEqual(10,f['fabric_known_rolls']); self.assertEqual(1,f['fabric_scope_unknown'])

    def test_explicit_window_missing_time_not_silently_removed(self):
        self.row(delivery_time=None); self.row(key=2); self.row(key=3,delivery_time='2026-07-01')
        f=self.value(time_range={'start':'2026-08-01','end':'2026-09-01'})
        self.assertEqual(2,f['fabric_group_rows']); self.assertIsNone(f['fabric_rolls'])

    def test_sku_and_candidate_aliases_are_public_and_filterable(self):
        self.row()
        r=self.result(self.run_source(dimensions=['fabric_sku','fabric_candidate_count'],metric_filters={'fabric_candidate_count':1}))
        self.assertIn('101',str(r['rows'])); self.assertEqual(1,facts(r)[0]['metric_value'])

    def test_scope_dimension_and_history_rejections_before_io(self):
        requests=[dict(delivery_scope='default_net'),dict(dimensions=['warehouse_department']),dict(dimensions=['barcode']),dict(order_by='metric_value')]
        for request in requests:
            self.assertEqual('failed',self.run_source(**request)['status'])
        self.assertEqual('failed',self.run_source('inventory',time_bucket='month')['status'])
        self.assertEqual('failed',self.run_source('inventory',time_range={'start':'2026-08-01','end':'2026-09-01'})['status'])
        self.assertEqual([],self.sql_trace)

    def test_empty_source_preserves_observation_without_fake_zero_row(self):
        r=self.result(self.run_source())
        self.assertEqual([],r['rows'])

    def test_conflicting_read_clock_is_failed_evidence_not_successful_rows(self):
        self.row();self.row(key=2,source_channel='无来源')
        def inconsistent(*args,**kwargs):
            rows,cut,source=self.execute(*args,**kwargs)
            rows[1]['fabric_read_at']='2026-09-13T10:00:00'
            return rows,cut,source
        with patch.object(public.plugin.tools,'_execute_with_source',inconsistent):
            response=self.run_source(dimensions=['fabric_channel'])
        self.assertEqual('failed',response['status'])
        self.assertEqual('SOURCE_OBSERVATION_INVALID',response['results'][0]['error']['code'])
        self.assertEqual([],response['results'][0]['rows'])


if __name__=='__main__':unittest.main()
