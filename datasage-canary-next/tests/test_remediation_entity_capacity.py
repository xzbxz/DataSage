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


if __name__ == '__main__': unittest.main()
