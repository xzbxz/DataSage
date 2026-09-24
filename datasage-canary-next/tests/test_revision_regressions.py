"""Offline regressions for the 2026-09-24 source-only review.

Loads pure DataSage modules without running the Hermes plugin registration.
No host replacement, network, database, model, scheduler or delivery is used.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import types
import unittest
from unittest import mock

import yaml
from reference_authority import rule_link_errors

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins/datasage-query'
PACKAGE = 'datasage_revision_pure'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules.setdefault(PACKAGE, package)
STORE = importlib.import_module(f'{PACKAGE}.contract_store')
spec = importlib.util.spec_from_file_location('datasage_revision_answer', PLUGIN / 'e2e/answer_ground_truth.py')
GT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GT)


def case_for(value='100', unit='万元', label='目标'):
    return {'case_id': 'synthetic-review', 'facts': [
        {'id': 'target', 'labels': [label], 'value': value, 'unit': unit,
         'state': 'known', 'tolerance': '0.001', 'period': None}],
         'arithmetic': [], 'table_text': [], 'review_requirements': None}


def score(text, case=None):
    return GT.score_answer_case(case or case_for(), text)['dimensions']


class ContractOwnershipRegressions(unittest.TestCase):
    def setUp(self):
        STORE.parse_yaml_cached.cache_clear()
        self.text = 'metrics:\n  a:\n    value: 1\n    labels: [A, B]\n'
        self.key = ('synthetic-review', hashlib.sha256(self.text.encode()).hexdigest(), self.text)
        self.value = STORE.parse_yaml_cached(*self.key)
        self.before = json.dumps(self.value, sort_keys=True)

    def assert_unchanged(self):
        self.assertEqual(self.before, json.dumps(STORE.parse_yaml_cached(*self.key), sort_keys=True))

    def test_mapping_ordinary_mutators_refuse_without_side_effects(self):
        operations = [lambda x: x.__setitem__('x', 2), lambda x: x.update(x=2),
                      lambda x: x.setdefault('x', 2), lambda x: x.pop('metrics'),
                      lambda x: x.clear(), lambda x: x.__ior__({'x': 2}),
                      lambda x: x.__init__({'x': 2})]
        for operation in operations:
            with self.subTest(operation=operations.index(operation)):
                with self.assertRaises(TypeError): operation(self.value)
                self.assert_unchanged()

    def test_nested_augmented_assignment_refuses_before_inner_update(self):
        with self.assertRaises(TypeError):
            self.value['metrics']['a'] |= {'value': 9}
        self.assert_unchanged()

    def test_list_mutators_refuse_without_side_effects(self):
        x = self.value['metrics']['a']['labels']
        operations = [lambda: x.append('C'), lambda: x.extend(['C']), lambda: x.reverse(),
                      lambda: x.sort(), lambda: x.__iadd__(['C']), lambda: x.__imul__(0),
                      lambda: x.__setitem__(slice(None), []), lambda: x.__init__(['C'])]
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                with self.assertRaises(TypeError): operation()
                self.assert_unchanged()

    def test_explicit_deepcopy_can_change_without_polluting_cache(self):
        detached = copy.deepcopy(self.value)
        detached['metrics']['a']['value'] = 9
        detached['metrics']['a']['labels'].append('C')
        self.assert_unchanged()
        self.assertIsInstance(self.value, dict)
        self.assertIsInstance(self.value['metrics']['a']['labels'], list)

    def test_concurrent_readers_keep_identical_values(self):
        def read(_):
            x = STORE.parse_yaml_cached(*self.key)
            try: x['metrics']['a'] |= {'value': 9}
            except TypeError: pass
            return json.dumps(x, sort_keys=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual([self.before] * 24, list(executor.map(read, range(24))))


class YamlCompatibilityRegressions(unittest.TestCase):
    def test_legal_merge_and_alias_combinations_match_safe_loader(self):
        documents = [
            'last:\n  <<: &mid\n    <<: &base {x: 1}\n    x: 2\nmid: *mid\n',
            'base: &b {x: 1, y: 2}\na: &a {<<: *b, x: 3}\nz: {<<: *a, y: 4}\nagain: *a\n',
            'a: &a {x: 1}\nb: &b {y: 2}\nc: {<<: [*a, *b], x: 3}\n',
            'ref: {<<: &a {<<: &b {x: 1}, x: 2}}\na: *a\nb: *b\n',
        ]
        for text in documents:
            with self.subTest(text=text):
                self.assertEqual(yaml.safe_load(text), yaml.load(text, Loader=STORE._ContractLoader))

    def test_duplicate_explicit_keys_are_rejected_even_when_reached_through_merge(self):
        for text in ['x: 1\nx: 2\n', 'a: {x: 1, x: 2}\n',
                     'last: {<<: &a {x: 1, x: 2}}\na: *a\n']:
            with self.subTest(text=text):
                with self.assertRaises(STORE.DuplicateContractKeyError):
                    yaml.load(text, Loader=STORE._ContractLoader)

    def test_all_current_yaml_contracts_keep_their_parsed_values(self):
        for path in (PLUGIN / 'contracts').glob('*.yaml'):
            with self.subTest(path=path.name):
                text = path.read_text(encoding='utf-8')
                self.assertEqual(yaml.safe_load(text), yaml.load(text, Loader=STORE._ContractLoader))

    def test_operator_parse_fault_is_isolated_but_query_fault_is_not(self):
        STORE.reset_contract_snapshot_for_tests()
        original = STORE._pin_one_contract
        def read(path):
            if path in STORE.OPERATOR_CONTRACT_PATHS:
                raise yaml.YAMLError('synthetic operator failure')
            return original(path)
        try:
            with mock.patch.object(STORE, '_pin_one_contract', side_effect=read):
                STORE.pin_contract_snapshot()
                self.assertEqual(2, len(STORE.contract_snapshot_status()['operator_contracts_unavailable']))
            STORE.reset_contract_snapshot_for_tests()
            with mock.patch.object(STORE, '_pin_one_contract', side_effect=yaml.YAMLError('synthetic query failure')):
                with self.assertRaises(STORE.ContractStoreError): STORE.pin_contract_snapshot()
        finally: STORE.reset_contract_snapshot_for_tests()


class AnswerScopeRegressions(unittest.TestCase):
    def test_plain_positive_statements_remain_accepted(self):
        for text in ['目标为100万元。', '2026年9月目标为100万元。',
                     '目标不是80万元，目标为100万元。',
                     '目标为100万元，实际未知。']:
            with self.subTest(text=text):
                self.assertEqual('passed', score(text)['numbers']['status'])

    def test_nonasserted_scopes_do_not_pass_as_verified_numbers(self):
        for text in ['假设目标为100万元。', '原问题说“目标为100万元”。',
                     '目标为100万元这个说法不正确。', '目标为100万元，尚未核实。',
                     '例如目标为100万元。', '目标并不是100万元。',
                     '目标未知（参考100万元）。', '目标为100万元，但未经验证。',
                     '目标至少100万元。', '目标>100万元。',
                     '目标可能为100万元。', '目标为100万元？']:
            with self.subTest(text=text):
                self.assertNotEqual('passed', score(text)['numbers']['status'])

    def test_conditional_scope_carries_to_next_fact_in_same_sentence(self):
        result=score('假设目标为100万元，实际为80万元。', case_for(value='80',label='实际'))
        self.assertEqual('not_verified',result['numbers']['status'])

    def test_wrong_affirmed_number_is_still_failure(self):
        self.assertEqual('failed', score('目标不是80万元，目标为90万元。')['numbers']['status'])

    def test_nonasserted_table_heading_is_inherited(self):
        for heading in ['假设如下：', '以下是示例，不是实际结果：']:
            answer = f'{heading}\n\n| 指标 | 数值 |\n|---|---|\n|目标|100万元|'
            self.assertNotEqual('passed', score(answer)['numbers']['status'])

    def test_plain_table_is_a_positive_control(self):
        answer='实际核验结果：\n|指标|数值|\n|---|---|\n|目标|100万元|'
        self.assertEqual('passed', score(answer)['numbers']['status'])

    def test_wrong_unit_on_denied_value_does_not_override_corrected_unit(self):
        self.assertEqual('passed', score('目标不是80元，目标为100万元。')['units']['status'])

    def test_unit_digits_are_not_a_second_business_value(self):
        for unit in ['m2', 'm²', '平方米']:
            with self.subTest(unit=unit):
                result=score(f'面积为100{unit}。', case_for(unit='m2',label='面积'))
                self.assertEqual('passed', result['numbers']['status'])
                self.assertEqual('passed', result['units']['status'])

    def test_arithmetic_cannot_use_only_hypothetical_result(self):
        case=case_for()
        case['facts'] += [dict(case['facts'][0],id='actual',labels=['实际'],value='80'),
                          dict(case['facts'][0],id='gap',labels=['差额'],value='-20')]
        case['arithmetic']=[{'id':'gap_check','operands':['actual','target'],
                            'expected_fact':'gap','operation':'subtract','tolerance':'0.001'}]
        result=score('目标100万元，实际80万元。假设差额为-20万元。', case)
        self.assertEqual('not_verified',result['arithmetic']['status'])

    def test_table_consistency_uses_affirmed_corrections(self):
        case=case_for();case['table_text']=[{'fact_id':'target','require_table':True,'require_text':True}]
        result=score('|目标|100万元|\n目标不是80万元，目标为100万元。',case)
        self.assertEqual('passed',result['table_text_consistency']['status'])
        unverified=score('假设如下：\n|目标|100万元|\n实际目标为100万元。',case)
        self.assertEqual('not_verified',unverified['table_text_consistency']['status'])

    def test_unknown_fact_is_never_silently_zero(self):
        case=case_for();case['facts'][0].update(state='unknown',value=None)
        self.assertEqual('failed',score('目标为0万元。',case)['numbers']['status'])


class ReferenceAuthorityRegressions(unittest.TestCase):
    def test_peer_cycles_and_fenced_examples_are_allowed(self):
        a=Path('/synthetic/a.md');b=Path('/synthetic/b.md')
        docs={a: 'Rule ID: `datasage.a/v1`\n[`datasage.b/v1`](b.md)\n```text\nWITH compatible periods, compare.\n```\n',
              b:'Rule ID: `datasage.b/v1`\n[`datasage.a/v1`](a.md)\n'}
        self.assertEqual([],rule_link_errors(docs))

    def test_example_rule_ids_and_links_are_not_live_declarations(self):
        path=Path('/synthetic/a.md')
        text='Rule ID: `datasage.a/v1`\n```text\nRule ID: `datasage.example/v1`\n[`datasage.example/v1`](not-a-real-target.md)\n```\n'
        self.assertEqual([],rule_link_errors({path:text}))

    def test_mislabelled_authority_or_duplicate_owner_is_rejected(self):
        a=Path('/synthetic/a.md');b=Path('/synthetic/b.md')
        docs={a:'Rule ID: `datasage.a/v1`\n[`datasage.wrong/v1`](b.md)', b:'Rule ID: `datasage.b/v1`'}
        self.assertTrue(rule_link_errors(docs))
        self.assertTrue(rule_link_errors({a:'Rule ID: `datasage.a/v1`',b:'Rule ID: `datasage.a/v1`'}))

    def test_all_actual_references_have_resolvable_authorities(self):
        skill=ROOT/'skills/business-analytics/datasage'
        docs={p:p.read_text(encoding='utf-8') for p in [skill/'SKILL.md',*sorted((skill/'references').glob('*.md'))]}
        self.assertEqual([],rule_link_errors(docs))

    def test_relative_posix_order_is_portable(self):
        names=['pymysql/__init__.py','pymysql-1.2.0.dist-info/METADATA','pymysql/converters.py']
        sort=lambda cls: sorted((cls(n) for n in names),key=lambda p:p.as_posix())
        self.assertEqual([p.as_posix() for p in sort(PureWindowsPath)], [p.as_posix() for p in sort(PurePosixPath)])


if __name__ == '__main__':
    unittest.main()
