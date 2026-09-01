"""Focused regression tests for the DataSage runtime/security remediation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unicodedata
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_remediation_security_runtime"
_package = types.ModuleType(PACKAGE)
_package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, _package)


def _load(name: str):
    qualified = f"{PACKAGE}.{name}"
    existing = sys.modules.get(qualified)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(qualified, PLUGIN_ROOT / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


entities = _load("entities")
entitlements = _load("entitlements")
db_security = _load("db_security")
db_runtime = _load("db_runtime")
db_executor = _load("db_executor")
runtime_health = _load("runtime_health")


class _Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows=None):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


class _Factory:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _Connection([])


class EntityProjectionTests(unittest.TestCase):
    def test_no_metric_role_candidates_are_always_ambiguous(self):
        status, must_clarify = entities._exact_resolution_status(
            [{"filter_role_candidates": ["customer_region", "department"]}],
            metric=None,
        )
        self.assertEqual("ambiguous", status)
        self.assertTrue(must_clarify)

    def test_resolver_no_metric_role_ambiguity_sets_business_stop(self):
        registry = {
            "known_entities": [],
            "candidate_sources": {},
            "entity_types": {
                "department": {
                    "roles_by_domain": {
                        "delivery": ["customer_region", "department"]
                    }
                }
            },
        }
        candidate = {
            "entity_type": "department",
            "display_name": "Team",
            "filter_values": ["team-id"],
            "match_kind": "registered_exact",
            "confidence": "exact",
        }
        with (
            mock.patch.object(entities, "_registry", return_value=registry),
            mock.patch.object(entities, "_known_matches", return_value=[candidate]),
        ):
            payload = json.loads(
                entities.datasage_entity_resolve(
                    {
                        "token": "Team",
                        "entity_types": ["department"],
                        "domain": "delivery",
                    }
                )
            )
        self.assertEqual("ambiguous", payload["status"])
        self.assertTrue(payload["must_clarify"])
        self.assertTrue(payload["must_stop_business_query"])

    def test_registered_exact_count_truncation_keeps_exact_count(self):
        registry = {
            "known_entities": [],
            "candidate_sources": {},
            "entity_types": {"department": {"roles_by_domain": {}}},
        }
        matches = [
            {
                "entity_type": "department",
                "display_name": f"Team {index}",
                "filter_values": [f"team-{index}"],
                "match_kind": "registered_exact",
                "confidence": "exact",
            }
            for index in range(6)
        ]
        with (
            mock.patch.object(entities, "_registry", return_value=registry),
            mock.patch.object(entities, "_known_matches", return_value=matches),
            mock.patch.object(
                entities,
                "_with_roles",
                side_effect=lambda candidate, *_args, **_kwargs: dict(candidate),
            ),
        ):
            for args in (
                {
                    "token": "Team",
                    "entity_types": ["department"],
                    "limit": 5,
                },
                {"token": "Team", "limit": 5},
            ):
                with self.subTest(args=args):
                    payload = json.loads(entities.datasage_entity_resolve(args))
                    self.assertEqual(6, payload["candidate_count"])
                    self.assertEqual(5, len(payload["candidates"]))
                    self.assertTrue(payload["truncated"])
                    self.assertFalse(payload["candidate_count_is_lower_bound"])
                    self.assertEqual("ambiguous", payload["status"])
                    self.assertTrue(payload["must_stop_business_query"])

    def test_db_public_truncation_does_not_make_exact_count_a_lower_bound(self):
        registry = {
            "known_entities": [],
            "candidate_sources": {"department": {}},
            "entity_types": {"department": {"roles_by_domain": {}}},
        }
        rows = [
            {
                "entity_type": "department",
                "canonical_id": f"team-{index}",
                "canonical_code": f"TEAM-{index}",
                "display_name": f"Team {index}",
                "matched_value": "Team",
                "match_rank": 0,
            }
            for index in range(2)
        ]
        with (
            mock.patch.object(entities, "_registry", return_value=registry),
            mock.patch.object(entities, "_known_matches", return_value=[]),
            mock.patch.object(
                entities,
                "_build_candidate_query",
                return_value=("SELECT 1", []),
            ),
            mock.patch.object(entities.db_runtime, "execute", return_value=(rows, False)),
        ):
            payload = json.loads(
                entities.datasage_entity_resolve(
                    {"token": "Team", "entity_types": ["department"], "limit": 1}
                )
            )
        self.assertEqual(2, payload["candidate_count"])
        self.assertEqual(1, len(payload["candidates"]))
        self.assertTrue(payload["truncated"])
        self.assertFalse(payload["candidate_count_is_lower_bound"])

        with (
            mock.patch.object(entities, "_registry", return_value=registry),
            mock.patch.object(entities, "_known_matches", return_value=[]),
            mock.patch.object(
                entities,
                "_build_candidate_query",
                return_value=("SELECT 1", []),
            ),
            mock.patch.object(entities.db_runtime, "execute", return_value=(rows, True)),
        ):
            payload = json.loads(
                entities.datasage_entity_resolve(
                    {"token": "Team", "entity_types": ["department"], "limit": 1}
                )
            )
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["candidate_count_is_lower_bound"])

    def test_public_candidates_are_bounded_and_marked_untrusted(self):
        long_text = "部门\u202e" + "中" * 800 + "\u200b\x1b\x00"
        rows = [
            {
                "entity_type": "department",
                "canonical_id": f"id-{index}-" + "中" * 600,
                "canonical_code": f"code-{index}-\u2066" + "中" * 600,
                "display_name": long_text,
                "matched_value": "token",
                "match_rank": 2,
            }
            for index in range(12)
        ]
        with mock.patch.object(
            entities,
            "_with_roles",
            side_effect=lambda candidate, *_args, **_kwargs: dict(candidate),
        ):
            candidates = entities._candidate_rows(
                rows,
                "delivery",
                metric=None,
                attribution_mode=None,
                semantics=None,
                token="token",
                public=True,
            )
        bounded, truncated = entities._public_payload_candidates(candidates, 10)
        self.assertTrue(truncated)
        self.assertTrue(bounded)
        encoded = json.dumps(
            bounded, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(encoded), entities.PUBLIC_CANDIDATE_TOTAL_BYTES)
        for candidate in bounded:
            self.assertIs(candidate["untrusted"], True)
            self.assertEqual(
                "untrusted_entity_metadata", candidate["untrusted_source"]
            )
            for field in (
                "entity_type",
                "canonical_id",
                "canonical_code",
                "display_name",
            ):
                value = candidate.get(field)
                if value is not None:
                    self.assertLessEqual(
                        len(value), entities.PUBLIC_CANDIDATE_FIELD_MAX_CHARS
                    )
                    self.assertLessEqual(
                        len(value.encode("utf-8")),
                        entities.PUBLIC_CANDIDATE_FIELD_MAX_BYTES,
                    )
                    self.assertFalse(
                        any(
                            ord(character) <= 0x1F
                            or 0x7F <= ord(character) <= 0x9F
                            or unicodedata.category(character) in {"Cf", "Cs"}
                            for character in value
                        )
                    )


class DatabaseSecurityTests(unittest.TestCase):
    @staticmethod
    def _policy(**overrides):
        value = {
            "production_mode": False,
            "require_tls": False,
            "canary_accept_existing_account": False,
        }
        value.update(overrides)
        return value

    def _source_connection(self, row):
        return _Connection([row])

    def test_source_identity_requires_database_user_and_port_match(self):
        row = {
            "server_uuid": "123e4567-e89b-12d3-a456-426614174000",
            "server_id": "1",
            "server_hostname": "warehouse",
            "server_port": "3307",
            "database_name": "other_schema",
            "authenticated_user": "other_user@%",
        }
        tls = {
            "transport_mode": "plaintext",
            "tls_required": False,
            "tls_configured": False,
            "insecure_transport_allowed": True,
            "tls_verified": False,
            "transport_encrypted": False,
        }
        grants = {
            "grants_verified": True,
            "grant_policy": "strict_object_read_only",
        }
        with (
            mock.patch.object(
                db_security.settings, "get", side_effect=self._policy().get
            ),
            mock.patch.object(db_security.settings, "get_list", return_value=[]),
            self.assertRaises(db_security.DatabaseSecurityError) as caught,
        ):
            db_security.verify_mysql_source_identity(
                self._source_connection(row),
                tls_evidence=tls,
                grant_evidence=grants,
                configured_identity={
                    "host": "warehouse",
                    "port": 3306,
                    "database": "governed_schema",
                    "user": "governed_user",
                },
            )
        self.assertEqual(
            "DATABASE_SOURCE_IDENTITY_MISMATCH", caught.exception.code
        )

        valid_row = {
            **row,
            "server_port": "3306",
            "database_name": "governed_schema",
            "authenticated_user": "governed_user@%",
        }
        with (
            mock.patch.object(
                db_security.settings, "get", side_effect=self._policy().get
            ),
            mock.patch.object(db_security.settings, "get_list", return_value=[]),
        ):
            evidence = db_security.verify_mysql_source_identity(
                self._source_connection(valid_row),
                tls_evidence=tls,
                grant_evidence=grants,
                configured_identity={
                    "host": "warehouse",
                    "port": 3306,
                    "database": "governed_schema",
                    "user": "governed_user",
                },
            )
        self.assertTrue(evidence["connection_verified"])

    def test_production_requires_and_enforces_server_uuid_allowlist(self):
        row = {
            "server_uuid": "123e4567-e89b-12d3-a456-426614174000",
            "server_id": "1",
            "server_hostname": "warehouse",
            "server_port": "3306",
            "database_name": "governed_schema",
            "authenticated_user": "governed_user@%",
        }
        tls = {
            "transport_mode": "tls",
            "tls_required": True,
            "tls_configured": True,
            "insecure_transport_allowed": False,
            "tls_verified": True,
            "transport_encrypted": True,
        }
        grants = {
            "grants_verified": True,
            "grant_policy": "strict_object_read_only",
        }
        with (
            mock.patch.object(
                db_security.settings,
                "get",
                side_effect=self._policy(
                    production_mode=True, require_tls=True
                ).get,
            ),
            mock.patch.object(db_security.settings, "get_list", return_value=[]),
            self.assertRaises(db_security.DatabaseSecurityError) as missing,
        ):
            db_security.verify_mysql_source_identity(
                self._source_connection(row),
                tls_evidence=tls,
                grant_evidence=grants,
                configured_identity={
                    "host": "warehouse",
                    "port": 3306,
                    "database": "governed_schema",
                    "user": "governed_user",
                },
            )
        self.assertEqual(
            "DATABASE_SERVER_UUID_ALLOWLIST_REQUIRED", missing.exception.code
        )

        with (
            mock.patch.object(
                db_security.settings,
                "get",
                side_effect=self._policy(
                    production_mode=True, require_tls=True
                ).get,
            ),
            mock.patch.object(
                db_security.settings,
                "get_list",
                return_value=["123e4567-e89b-12d3-a456-426614174001"],
            ),
            self.assertRaises(db_security.DatabaseSecurityError) as denied,
        ):
            db_security.verify_mysql_source_identity(
                self._source_connection(row),
                tls_evidence=tls,
                grant_evidence=grants,
                configured_identity={
                    "host": "warehouse",
                    "port": 3306,
                    "database": "governed_schema",
                    "user": "governed_user",
                },
            )
        self.assertEqual(
            "DATABASE_SERVER_UUID_NOT_ALLOWED", denied.exception.code
        )

    def test_canary_only_relaxes_object_scope(self):
        grants = {"Grant": "GRANT SELECT ON analytics.* TO 'reader'@'%'"}
        with mock.patch.object(
            db_security.settings,
            "get",
            side_effect=self._policy(canary_accept_existing_account=True).get,
        ):
            accepted = db_security.verify_mysql_read_only_grants(
                _Connection([grants])
            )
        self.assertEqual(
            "user_accepted_canary_existing_account",
            accepted["grant_policy"],
        )
        rejected = (
            "GRANT ALL PRIVILEGES ON analytics.* TO 'reader'@'%'",
            "GRANT SELECT, FILE ON analytics.* TO 'reader'@'%'",
            "GRANT SELECT ON analytics.* TO 'reader'@'%' WITH GRANT OPTION",
            "GRANT SELECT ON *.* TO 'reader'@'%'",
        )
        for grant in rejected:
            with (
                self.subTest(grant=grant),
                mock.patch.object(
                    db_security.settings,
                    "get",
                    side_effect=self._policy(
                        canary_accept_existing_account=True
                    ).get,
                ),
                self.assertRaises(db_security.DatabaseSecurityError),
            ):
                db_security.verify_mysql_read_only_grants(
                    _Connection([{"Grant": grant}])
                )

    def test_required_tls_rejects_plaintext_even_with_snapshot(self):
        policy = {"tls_required": True, "tls_configured": False}
        with self.assertRaises(db_security.DatabaseSecurityError) as caught:
            db_security.verify_mysql_tls(_Connection([]), policy=policy)
        self.assertEqual(
            "DATABASE_TLS_CONFIGURATION_MISSING", caught.exception.code
        )

    def test_profile_and_vendor_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "approved"
            outside = Path(raw) / "outside"
            root.mkdir()
            outside.mkdir()
            ca = root / "ca.pem"
            ca.write_text("synthetic-ca", encoding="utf-8")
            with (
                mock.patch.object(
                    db_security,
                    "get_secret",
                    side_effect=lambda name, default="": (
                        str(ca)
                        if name == "DATA_QUERY_MYSQL_SSL_CA"
                        else default
                    ),
                ),
                mock.patch.object(db_security.settings, "get_list", return_value=[]),
            ):
                tls_kwargs = db_security.mysql_tls_kwargs(
                    policy={"tls_required": True, "tls_configured": True},
                    profile_root=root,
                )
            self.assertEqual(str(ca.resolve()), tls_kwargs["ssl_ca"])
            with (
                mock.patch.object(
                    db_security,
                    "get_secret",
                    side_effect=lambda name, default="": (
                        str(outside / "ca.pem")
                        if name == "DATA_QUERY_MYSQL_SSL_CA"
                        else default
                    ),
                ),
                mock.patch.object(db_security.settings, "get_list", return_value=[]),
                self.assertRaises(db_security.DatabaseSecurityError),
            ):
                db_security.mysql_tls_kwargs(
                    policy={"tls_required": True, "tls_configured": True},
                    profile_root=root,
                )
            with mock.patch.object(
                runtime_health, "_profile_root", return_value=root
            ):
                status = runtime_health.runtime_identity_status(
                    profile_root=outside
                )
            self.assertFalse(status["ready"])
            self.assertEqual(
                "DATASAGE_PROFILE_ROOT_OUTSIDE_APPROVED_ROOT",
                status["reason_code"],
            )
            with mock.patch.object(db_runtime, "_is_reparse", return_value=True):
                with self.assertRaises(db_runtime.DatabaseRuntimeError) as caught:
                    db_runtime._validate_vendor_path(root, root)
            self.assertEqual("DEPENDENCY_UNTRUSTED", caught.exception.code)


class ExecutorAndEntitlementTests(unittest.TestCase):
    def test_invalid_sql_and_limits_are_rejected_before_connection(self):
        factory = _Factory()
        executor = db_executor.ReadOnlyDbExecutor(
            mode="single_statement",
            connection_factory=factory,
            confirm_read_only_transaction=lambda _connection: {
                "read_only": True
            },
            query_timeout_seconds=5,
        )
        for sql, limit in (
            ("UPDATE fact SET value = 1", 1),
            ("SELECT value INTO OUTFILE 'x' FROM fact", 1),
            ("SELECT 1; SELECT 2", 1),
            ("WITH changed AS (DELETE FROM fact) SELECT 1", 1),
            ("SELECT SLEEP(1)", 1),
            ("/* comment */ SELECT 1", 1),
            ("SELECT 1", 0),
            ("SELECT 1", db_executor.MAX_EXECUTOR_ROWS + 1),
        ):
            with self.subTest(sql=sql, limit=limit), self.assertRaises(ValueError):
                executor.execute(sql, (), limit)
        self.assertEqual([], factory.calls)
        db_executor._validate_read_only_statement(
            "WITH source_rows AS (SELECT 1 AS value) SELECT value FROM source_rows",
            1,
        )

    def test_coarse_authorization_ignores_row_shape_but_gates_scope(self):
        rule = {
            "tools": ["datasage_query"],
            "domains": ["delivery"],
            "metrics": {"delivery": ["delivery_amount"]},
            "allow_all_rows": True,
        }
        with (
            mock.patch.object(entitlements.settings, "get", return_value={}),
            mock.patch.object(
                entitlements, "_principal_rule", return_value=rule
            ),
            mock.patch.object(
                entitlements,
                "_session_value",
                side_effect=lambda name: {
                    "HERMES_SESSION_PLATFORM": "wecom",
                    "HERMES_SESSION_USER_ID": "user-1",
                }.get(name, ""),
            ),
        ):
            self.assertTrue(
                entitlements.coarse_authorized(
                    "datasage_query",
                    {
                        "requests": [
                            {
                                "domain": "delivery",
                                "metric": "delivery_amount",
                                "metric_filters": "not-yet-normalized",
                            }
                        ]
                    },
                )
            )
            self.assertFalse(
                entitlements.coarse_authorized(
                    "datasage_query",
                    {
                        "requests": [
                            {
                                "domain": "outside",
                                "metric": "delivery_amount",
                            }
                        ]
                    },
                )
            )

    def test_authorization_audit_contains_only_hashes_and_decision(self):
        rule = {
            "tools": ["datasage_query"],
            "domains": ["delivery"],
            "metrics": {"delivery": ["delivery_amount"]},
            "allow_all_rows": True,
        }
        args = {
            "requests": [
                {
                    "request_id": "sensitive-request-id",
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "metric_filters": {"customer": "Sensitive Customer"},
                }
            ]
        }
        with (
            mock.patch.object(entitlements.settings, "get", return_value={}),
            mock.patch.object(
                entitlements, "_principal_rule", return_value=rule
            ),
            mock.patch.object(
                entitlements,
                "_session_value",
                side_effect=lambda name: {
                    "HERMES_SESSION_PLATFORM": "wecom",
                    "HERMES_SESSION_USER_ID": "user-1",
                }.get(name, ""),
            ),
            self.assertLogs(entitlements.logger, level="INFO") as captured,
        ):
            self.assertTrue(entitlements.authorized("datasage_query", args))
        line = captured.output[-1]
        event = json.loads(line[line.index("{") :])
        self.assertEqual(
            "datasage_entitlement_decision", event["event"]
        )
        self.assertTrue(event["allowed"])
        self.assertIn("principal_sha256", event)
        self.assertNotIn("Sensitive Customer", captured.output[-1])
        self.assertNotIn("sensitive-request-id", captured.output[-1])


if __name__ == "__main__":
    unittest.main()
