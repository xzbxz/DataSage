"""Public API checks for registered secondary facts and sealed projection."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_c01_c02_public as fixtures
import test_slow_baseline_net_outbound as inventory


class AnalysisCalculationIntegrationTests(unittest.TestCase):
    def query(self, *, grouped=False, left='actual_amount_rmb', right='target_amount_rmb'):
        owner = fixtures.PublicInterfaceRegressions()
        self.addCleanup(owner.doCleanups)
        harness = owner.target(targets=(100, 200), actuals=(60, 180))
        request = fixtures.metric(
            'delivery_target_completion', 'target', request_id='target',
            currency_basis='rmb', attribution_mode='transaction_detail',
            dimensions=['department'] if grouped else [],
            metric_filters={'department': 'Alpha'},
        )
        calculation = {
            'calculation_id': 'fraction', 'operation': 'ratio',
            'left_request_id': 'target', 'right_request_id': 'target',
        }
        if left is not None:
            calculation['left_field'] = left
        if right is not None:
            calculation['right_field'] = right
        tools = fixtures.plugin.tools
        with patch.object(tools, '_model_wire_calculations', wraps=tools._model_wire_calculations) as projection:
            payload = harness.invoke('datasage_query', {'requests': [request], 'calculations': [calculation]})
            self.projection_inputs = copy.deepcopy(projection.call_args.args)
        return payload

    def test_secondary_fact_ratio_survives_public_projection(self):
        payload = self.query()
        self.assertEqual('success', payload['status'], payload)
        calculation = payload['calculations'][0]
        self.assertEqual('success', calculation['status'], calculation)
        self.assertAlmostEqual(.6, float(calculation['value']))
        self.assertEqual(['actual_amount_rmb', 'target_amount_rmb'], [x['field'] for x in calculation['operands']])

    def test_same_verified_group_secondary_ratio_is_allowed(self):
        payload = self.query(grouped=True)
        self.assertEqual('success', payload['calculations'][0]['status'], payload)
        self.assertAlmostEqual(.6, float(payload['calculations'][0]['value']))

    def test_legacy_grouped_metric_value_stays_rejected(self):
        payload = self.query(grouped=True, left=None, right=None)
        self.assertEqual('CALCULATION_REQUIRES_SCALAR', payload['calculations'][0]['error']['code'])

    def test_resealed_selector_cannot_misrepresent_visible_fact(self):
        payload = self.query()
        calculations, public_results = self.projection_inputs
        calculation = copy.deepcopy(calculations[0])
        self.assertEqual('success', calculation['status'], payload)
        baseline = fixtures.plugin.tools._model_wire_calculations([calculation], public_results)
        self.assertEqual('success', baseline[0]['status'], baseline)
        calculation['operands'][0]['field'] = 'gap_amount_rmb'
        calculation.pop('calculation_seal', None)
        canonical = json.dumps(calculation, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()
        calculation['calculation_seal'] = 'sha256_' + hashlib.sha256(canonical).hexdigest()
        projected = fixtures.plugin.tools._model_wire_calculations([calculation], public_results)
        self.assertEqual('CALCULATION_SOURCE_INTEGRITY_INVALID', projected[0]['error']['code'])

    def inventory_calculation(self, left_field, *, unknown_return_scope=False, include_returns=True):
        harness = inventory.BaselineNetTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.outgoing(qty=100, rolls=14, deal_price=50, ddp_price=100)
        if include_returns:
            harness.returning(qty=20, rolls=13, whse=999 if unknown_return_scope else 1)
        request = fixtures.metric(
            'registered_slow_pool_baseline_net_outbound', 'inventory',
            request_id='inventory', month=None, dimensions=['unit'],
            analysis={'row_filters': [{'field': 'price_to_ddp_ratio', 'op': 'lte', 'value': '0.5'}]},
        )
        return harness.invoke('datasage_query', {'requests': [request], 'calculations': [{
            'calculation_id': 'same-slice', 'operation': 'ratio',
            'left_request_id': 'inventory', 'right_request_id': 'inventory',
            'left_field': left_field, 'right_field': 'gross_rolls',
        }]})

    def test_known_slice_gross_remains_usable_when_return_attribution_is_unknown(self):
        payload = self.inventory_calculation('gross_rolls')
        self.assertEqual('unknown_return_attribution', payload['results'][0]['rows'][0]['states']['analysis_net_state'])
        calculation = payload['calculations'][0]
        self.assertEqual('success', calculation['status'], payload)
        self.assertEqual(1, float(calculation['value']))

    def test_unknown_slice_net_stays_unavailable(self):
        payload = self.inventory_calculation('net_rolls')
        self.assertEqual('CALCULATION_VALUE_UNAVAILABLE', payload['calculations'][0]['error']['code'])

    def test_unknown_return_source_does_not_erase_complete_outbound_gross(self):
        payload = self.inventory_calculation('gross_rolls', unknown_return_scope=True)
        row = payload['results'][0]['rows'][0]
        self.assertEqual('unknown_return_scope', row['states']['analysis_net_state'])
        self.assertEqual(14, row['facts']['gross_rolls'])
        self.assertIsNone(row['facts']['net_rolls'])
        self.assertEqual('success', payload['calculations'][0]['status'], payload)

    def test_no_returns_proves_complete_slice_net_for_calculation(self):
        payload = self.inventory_calculation('net_rolls', include_returns=False)
        self.assertEqual('success', payload['calculations'][0]['status'], payload)
        self.assertEqual(1, float(payload['calculations'][0]['value']))


if __name__ == '__main__':
    unittest.main()
