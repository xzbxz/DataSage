"""Real local socket cancellation plus synthetic-clock pipeline regressions."""
from contextlib import closing
import json
import importlib
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_db_executor as executor_tests
import test_business_contracts as business

db = executor_tests.db_executor


class SocketCursor:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self
    def __exit__(self, *args):
        if self.connection.drain and self.connection.business:
            while self.connection._sock.recv(1): pass
    def execute(self, sql, params=None):
        self.connection.business = sql.startswith('SELECT')
    def fetchmany(self, count):
        rows = []
        for _ in range(1 if self.connection.drain else count):
            if not self.connection._sock.recv(1): raise OSError('connection interrupted')
            rows.append({'metric_value': 1})
        return rows


class SocketConnection:
    def __init__(self, sock, drain=False, slow_rollback=False):
        self._sock = sock
        self.drain = drain
        self.slow_rollback = slow_rollback
        self.business = False
        self.closed = False
    def cursor(self): return SocketCursor(self)
    def rollback(self):
        if self.slow_rollback:
            while self._sock.recv(1): pass
    def close(self):
        self.closed = True
        self._sock.close()


class DeadlineTests(unittest.TestCase):
    def test_slow_drip_read_cursor_drain_and_rollback_are_cancelled(self):
        for phase in ('fetch', 'drain', 'rollback'):
            with self.subTest(phase=phase):
                local, peer = socket.socketpair()
                conn = SocketConnection(local, drain=phase == 'drain', slow_rollback=phase == 'rollback')
                stopped = threading.Event()
                def stream():
                    try:
                        for _ in range(60):
                            if stopped.wait(0.04): break
                            peer.sendall(b'x')
                    except OSError:
                        pass
                    finally:
                        peer.close()
                sender = threading.Thread(target=stream)
                sender.start()
                started = time.monotonic()
                executor = db.ReadOnlyDbExecutor(mode='single_statement', connection_factory=lambda **kw: conn,
                            confirm_read_only_transaction=lambda conn: {}, query_timeout_seconds=5,
                            deadline_at=started + 1.2)
                try:
                    with self.assertRaises(db.DeadlineExceeded):
                        executor.execute('SELECT amount FROM synthetic LIMIT 51', [], 50 if phase == 'fetch' else 1)
                    self.assertLess(time.monotonic() - started, 1.8)
                    self.assertTrue(executor.closed)
                    self.assertTrue(conn.closed)
                finally:
                    stopped.set()
                    local.close()
                    sender.join(timeout=2)
                self.assertFalse(sender.is_alive())

    def test_row_mapping_expiry_is_not_success(self):
        now = [0.0]
        conn = executor_tests.FakeConnection([{'metric_value': 1}])
        def mapper(row):
            now[0] = 61.0
            return dict(row)
        executor = db.ReadOnlyDbExecutor(mode='single_statement', connection_factory=lambda **kw: conn,
                   confirm_read_only_transaction=lambda conn: {}, query_timeout_seconds=30,
                   deadline_at=60, clock=lambda: now[0], row_mapper=mapper)
        with self.assertRaises(db.DeadlineExceeded): executor.execute('SELECT 1', [], 1)
        self.assertTrue(executor.closed)

    def test_postprocessing_expiry_does_not_publish_success(self):
        tools = business.tools
        now = [0.0]
        original = tools._model_wire_result
        def slow_projection(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 61.0
            return result
        with patch.object(tools.time, 'monotonic', side_effect=lambda: now[0]), patch.object(tools, '_model_wire_result', side_effect=slow_projection):
            payload, _, _ = business.BusinessContractTests._run_snapshot_change_operation()
        self.assertNotEqual('success', payload['status'])
        self.assertEqual('BATCH_DEADLINE_EXCEEDED', payload.get('error', {}).get('code'))

    def test_preparation_expiry_prevents_database_work(self):
        tools = business.tools
        now = [0.0]
        original = tools._validated_query_envelope
        def slow_envelope(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 61.0
            return result
        args = {'requests': [dict(request_id='deadline_prep', domain='receipt', mode='metric',
                                metric='actual_receipt_amount', calendar_month='2026-08')]}
        with patch.object(tools.time, 'monotonic', side_effect=lambda: now[0]), patch.object(tools, '_validated_query_envelope', side_effect=slow_envelope), patch.object(tools, '_execute_with_source') as io:
            payload = json.loads(tools.datasage_query(args))
        io.assert_not_called()
        self.assertEqual('failed', payload['status'])

    def test_completed_branch_survives_a_later_timeout(self):
        tools = business.tools
        now = [0.0]
        calls = []
        source = business.BusinessContractTests._read_only_source_evidence()
        original_bound = tools._bounded_int
        def bound(name, *args):
            return 1 if name == 'max_concurrent_queries' else original_bound(name, *args)
        def database(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2: now[0] = 61.0
            return [{'metric_value': 7, '__matched_row_count': 1, 'missing_value_count': 0,
                     'known_value_count': 1, 'metric_data_state': 'complete'}], False, source
        requests = [dict(request_id=name, domain='receipt', mode='metric', metric='actual_receipt_amount', calendar_month='2026-08')
                    for name in ('completed', 'late')]
        with patch.object(tools.time, 'monotonic', side_effect=lambda: now[0]), patch.object(tools, '_bounded_int', side_effect=bound), patch.object(tools, '_execute_with_source', side_effect=database):
            payload = json.loads(tools.datasage_query({'requests': requests}))
        self.assertEqual('partial', payload['status'])
        self.assertEqual(['completed', 'late'], [result['request_id'] for result in payload['results']])
        self.assertEqual(['success', 'failed'], [result['status'] for result in payload['results']])
        self.assertEqual('BATCH_DEADLINE_EXCEEDED', payload['error']['code'])
        self.assertEqual(0, tools._ACTIVE_QUERY_CALLS)

    def test_public_preflight_is_inside_the_original_budget(self):
        tools, wire = business.tools, business.wire
        entitlements = importlib.import_module(f'{tools.__package__}.entitlements')
        now = [0.0]
        original = tools._validate_query_dispatch
        def slow_preflight(*args, **kwargs):
            envelope = original(*args, **kwargs)
            now[0] = 61.0
            return envelope
        args = {'requests': [dict(request_id='public_prep', domain='receipt', mode='metric', metric='actual_receipt_amount', calendar_month='2026-08')]}
        handler = wire.bounded_json_handler('datasage_query', tools.entitlement_guarded_datasage_query)
        with patch.object(tools.time, 'monotonic', side_effect=lambda: now[0]), patch.object(entitlements, 'coarse_authorized', return_value=True), patch.object(entitlements, 'authorized', return_value=True), patch.object(tools, '_validate_query_dispatch', side_effect=slow_preflight), patch.object(tools, 'runtime_guarded_datasage_query') as runtime:
            result = json.loads(handler(args))
        runtime.assert_not_called()
        self.assertEqual('BATCH_DEADLINE_EXCEEDED', result['error']['code'])

    def test_final_wire_compaction_cannot_publish_late_success(self):
        wire = business.wire
        now = [0.0]
        raw = json.dumps({'status': 'success', 'results': [{'request_id': 'finished', 'status': 'success'}]})
        def slow_compaction(*args):
            now[0] = 61.0
            return raw
        handler = wire.bounded_json_handler('datasage_query', lambda args: raw)
        with patch.object(wire.time, 'monotonic', side_effect=lambda: now[0]), patch.object(wire, 'enforce_tool_result_budget', side_effect=slow_compaction):
            result = json.loads(handler({}))
        self.assertEqual('partial', result['status'])
        self.assertEqual('BATCH_DEADLINE_EXCEEDED', result['error']['code'])
        self.assertEqual('finished', result['results'][0]['request_id'])

    def test_connection_authentication_is_cancelled_before_security_queries(self):
        runtime = business.tools.db_runtime
        executor = business.tools.db_executor
        local, peer = socket.socketpair()
        conn = SocketConnection(local)
        stopped = threading.Event()
        def authenticate():
            while local.recv(1): pass
        conn.connect = authenticate
        def stream():
            try:
                for _ in range(60):
                    if stopped.wait(0.04): break
                    peer.sendall(b'x')
            except OSError: pass
            finally: peer.close()
        sender = threading.Thread(target=stream)
        sender.start()
        driver = SimpleNamespace(connect=lambda **kwargs: conn, cursors=SimpleNamespace(SSDictCursor=object))
        secrets = {'DATA_QUERY_MYSQL_HOST': 'offline.invalid', 'DATA_QUERY_MYSQL_DATABASE': 'offline',
                   'DATA_QUERY_MYSQL_USER': 'offline', 'DATA_QUERY_MYSQL_PASSWORD': 'offline'}
        started = time.monotonic()
        try:
            with patch.object(runtime, 'load_pymysql', return_value=driver), patch.object(runtime, 'get_secret', side_effect=lambda key, default='': secrets.get(key, default)), patch.object(runtime, 'mysql_tls_policy', return_value={}), patch.object(runtime, 'mysql_tls_kwargs', return_value={}), patch.object(runtime, 'verify_mysql_read_only_grants') as grants:
                with executor.deadline_scope(started + 0.3):
                    with self.assertRaises(executor.DeadlineExceeded): runtime.connect()
                grants.assert_not_called()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(conn.closed)
        finally:
            stopped.set()
            local.close()
            sender.join(timeout=2)
        self.assertFalse(sender.is_alive())


if __name__ == '__main__': unittest.main()
