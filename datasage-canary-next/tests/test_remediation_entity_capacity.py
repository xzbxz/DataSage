"""Exercise the registered handler; only database I/O is synthetic."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / 'datasage-query'
spec = importlib.util.spec_from_file_location('datasage_entity_capacity_tests', PLUGIN / '__init__.py', submodule_search_locations=[str(PLUGIN)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from gateway.session_context import set_session_vars, clear_session_vars


class Context:
    def __init__(self): self.tools = {}
    def get_config(self, name, default=None): return 4 if name == 'max_concurrent_queries' else default
    def register_tool(self, **kwargs): self.tools[kwargs['name']] = kwargs


class EntityCapacityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        plugin.register(self.ctx)
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)

    def invoke(self):
        tokens = set_session_vars(platform='wecom', source='wecom', user_id='offline-fixture', chat_id='offline-fixture', chat_type='dm')
        try:
            return json.loads(self.ctx.tools['datasage_entity_resolve']['handler'](
                dict(token='offline-customer-no-match', entity_types=['customer'], domain='delivery', metric='delivery_amount')))
        finally:
            clear_session_vars(tokens)

    def test_full_query_capacity_rejects_entity_without_database_io(self):
        held = 0
        try:
            for _ in range(4):
                self.assertTrue(plugin.tools._try_acquire_query_slot())
                held += 1
            with patch.object(plugin.tools, '_execute_with_source', return_value=([], False, {})) as db:
                result = self.invoke()
                self.assertEqual('QUERY_CONCURRENCY_LIMIT', result.get('error', {}).get('code'))
                db.assert_not_called()
        finally:
            for _ in range(held): plugin.tools._release_query_slot()
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)

    def test_entity_receives_deadline_and_releases_slot_after_failure(self):
        observed = []
        def database(sql, params, limit, *, deadline_at=None):
            observed.append((deadline_at, plugin.tools._ACTIVE_QUERY_CALLS))
            raise RuntimeError('offline I/O failure')
        before = time.monotonic()
        with patch.object(plugin.tools, '_execute_with_source', side_effect=database):
            self.assertEqual('failed', self.invoke()['status'])
        self.assertEqual(1, len(observed))
        self.assertIsNotNone(observed[0][0])
        self.assertGreater(observed[0][0], before)
        self.assertEqual(1, observed[0][1])
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)

    def test_eight_entities_share_four_global_slots(self):
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        state = {'active': 0, 'peak': 0}
        def database(sql, params, limit, *, deadline_at=None):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                if state['active'] == 4: entered.set()
            try:
                self.assertTrue(release.wait(5))
                return [], False, {}
            finally:
                with lock: state['active'] -= 1
        with patch.object(plugin.tools, '_execute_with_source', side_effect=database):
            with ThreadPoolExecutor(max_workers=8) as pool:
                first = [pool.submit(self.invoke) for _ in range(4)]
                try:
                    self.assertTrue(entered.wait(5))
                    rest = [pool.submit(self.invoke) for _ in range(4)]
                    denied = [future.result(timeout=2) for future in rest]
                    self.assertTrue(all(result.get('error', {}).get('code') == 'QUERY_CONCURRENCY_LIMIT' for result in denied))
                finally:
                    release.set()
                for future in first: future.result(timeout=2)
        self.assertEqual(4, state['peak'])
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)

    def test_parallel_worker_failure_releases_leased_slots_for_next_full_batch(self):
        source = {
            'schema': 'datasage-query-source-evidence/v1',
            'identity_sha256': '1' * 64,
            'connection_verified': True,
            'transport_mode': 'plaintext',
            'transport_policy_verified': True,
            'grant_policy': 'strict_object_read_only',
            'grants_verified': True,
            'read_only': True,
            'source_commitment_sha256': '2' * 64,
            'security_evidence_sha256': '',
        }
        db_security = importlib.import_module(f'{spec.name}.db_security')
        source['security_evidence_sha256'] = (
            db_security._source_evidence_hash(source)
        )
        requests = [
            {
                'request_id': f'parallel-{index}',
                'domain': 'delivery',
                'metric': 'delivery_amount',
            }
            for index in range(3)
        ]
        state = {
            'phase': 'failure',
            'calls': 0,
            'active': 0,
            'peak': 0,
        }
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def database(sql, params, limit, *, deadline_at=None):
            del sql, params, limit, deadline_at
            with lock:
                state['calls'] += 1
                fail_this_call = state['phase'] == 'failure' and state['calls'] == 1
                if state['phase'] == 'full':
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
            if fail_this_call:
                raise RuntimeError('synthetic single worker failure')
            if state['phase'] == 'full':
                try:
                    try:
                        barrier.wait(timeout=2)
                    except threading.BrokenBarrierError:
                        pass
                finally:
                    with lock:
                        state['active'] -= 1
            return (
                [
                    {
                        'metric_value': '10.00',
                        '__matched_row_count': 1,
                        'missing_value_count': 0,
                        'known_value_count': 1,
                        'metric_data_state': 'complete',
                    }
                ],
                False,
                source,
            )

        with patch.object(plugin.tools, '_execute_with_source', side_effect=database):
            first = json.loads(plugin.tools.datasage_query({'requests': requests}))
        self.assertEqual('partial', first['status'])
        self.assertEqual(3, len(first['results']))
        self.assertEqual(1, sum(result['status'] == 'failed' for result in first['results']))
        self.assertEqual(2, sum(result['status'] == 'success' for result in first['results']))
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)

        state['phase'] = 'full'
        state['calls'] = 0
        barrier = threading.Barrier(4)
        full_requests = [
            {
                'request_id': f'full-{index}',
                'domain': 'delivery',
                'metric': 'delivery_amount',
            }
            for index in range(4)
        ]
        with patch.object(plugin.tools, '_execute_with_source', side_effect=database):
            second = json.loads(
                plugin.tools.datasage_query({'requests': full_requests})
            )
        self.assertEqual('success', second['status'])
        self.assertEqual(4, state['peak'])
        self.assertEqual(0, plugin.tools._ACTIVE_QUERY_CALLS)


if __name__ == '__main__': unittest.main()
