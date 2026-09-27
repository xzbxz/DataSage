"""Analysis evidence must remain bound to the selected population and facts."""
import copy
import unittest

from test_remediation_remaining_cases import plugin

analysis = plugin.tools.analysis_evidence


class AnalysisScopeBindingTests(unittest.TestCase):
    def context(self):
        request = {'analysis': {'group_filters': [{'field': 'completion_rate', 'op': 'lt', 'value': '0.8'}]}}
        scope = {'analysis': {'operation': 'target'}, 'analysis_counts': {
            name: 'analysis_' + name + '_count' for name in ('population', 'match', 'unknown', 'excluded')
        }, 'analysis_count_grain': 'department_groups'}
        facts = dict(analysis_population_count=3, analysis_match_count=1,
                     analysis_unknown_count=1, analysis_excluded_count=1)
        period = {'start': '2026-08-01', 'end': '2026-09-01'}
        context = analysis.public_context(request, scope, [facts], period=period,
                                          scope_fingerprint='scope_one', projection_fingerprint='projection_one')
        result = {'scope_fingerprint': 'scope_one', 'projection_fingerprint': 'projection_one',
                  'applied_time_range': period, 'claim_ledger': [{'facts': facts}]}
        return context, result

    def test_changed_population_cannot_reuse_context_proof(self):
        context, result = self.context()
        self.assertTrue(analysis.context_is_valid(context, result))
        changed = copy.deepcopy(context)
        changed['counts']['match'] = 2
        changed['counts']['unknown'] = 0
        self.assertFalse(analysis.context_is_valid(changed, result))
        changed['evidence_seal'] = analysis._context_seal(changed)
        self.assertFalse(analysis.context_is_valid(changed, result))

    def test_other_scope_cannot_reuse_context(self):
        context, result = self.context()
        result['scope_fingerprint'] = 'another_scope'
        self.assertFalse(analysis.context_is_valid(context, result))

    def test_price_slice_does_not_expose_parent_totals_as_selected_facts(self):
        row = {'metric_value': 30, 'net_rolls': 3, 'gross_rolls': 3, 'scope_net_rolls': 900,
               'high_net_rolls': 700, 'gross_known_quantity': 1000, 'outbound_unknown_rows': 0}
        scope = {'analysis': {'operation': 'inventory_flow'},
                 'analysis_selected_fields': ['metric_value', 'net_rolls', 'gross_rolls'],
                 'analysis_reference_fields': ['high_net_rolls', 'scope_net_rolls']}
        selected = analysis.selected_rows(scope, [row])[0]
        self.assertEqual({'metric_value': 30, 'net_rolls': 3, 'gross_rolls': 3, 'outbound_unknown_rows': 0}, selected)
        self.assertEqual([row], analysis.selected_rows({}, [row]))


if __name__ == '__main__':
    unittest.main()
