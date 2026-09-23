"""Create a reviewed, portable DataSage source archive.

This is an audit/share helper.  It is deliberately separate from Hermes'
``export_profile`` implementation: a named-profile export excludes only
``.env`` and ``auth.json`` and can therefore still contain databases, reports,
sessions, and other running-profile material.  This module copies an explicit
file manifest instead.  It does not install, publish, start, or migrate a
profile.

The manifest is intentionally written out as concrete file names.  New source
files are not included by a glob; they must be reviewed and added here.  The
list is conservative and is expected to change when the profile gains a
reviewed module or resource.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import Iterable, Sequence

import yaml


PACKAGE_NAME = "datasage-canary-next"


class ExportError(RuntimeError):
    """Base class for an export that cannot be proven safe."""


class UnsafePathError(ExportError):
    """The source or destination contains a symlink or escapes its root."""


class MissingResourceError(ExportError):
    """A reviewed file/resource required by the profile is absent."""


class SensitiveContentError(ExportError):
    """The staged package contains a high-signal secret or machine path."""


def _paths(directory: str, names: Sequence[str]) -> tuple[str, ...]:
    """Expand a reviewed list of names into profile-relative POSIX paths."""

    return tuple(f"{directory}/{name}" for name in names)


# These lists are explicit on purpose.  Do not replace them with rglob/glob
# discovery: an unreviewed file in a profile must never silently enter an
# audit package.  Keep source and contract files separate in the declaration so
# reviewers can see what is required for a plugin load.
_ROOT_FILES = (
    ".env.EXAMPLE",
    ".gitignore",
    "ARCHITECTURE.md",
    "README.md",
    "SOUL.md",
    "config.yaml",  # written as a generated, redacted template
    "profile.yaml",
    "docs/maintenance.md",
    "docs/legacy-checklist-state-20260923.md",
    "docs/remediation-r13-20260923.md",
    "docs/remediation-r14-20260923.md",
    "docs/remediation-r16-20260923.md",
    "docs/remediation-r17-20260923.md",
    "docs/remediation-r18-20260923.md",
    "docs/remediation-r19-20260923.md",
    "docs/remediation-r20-20260923.md",
    "docs/remediation-r21-20260923.md",
    "docs/remediation-r22-20260923.md",
    "docs/remediation-r23-20260923.md",
    "docs/remediation-r24-20260923.md",
    "docs/remediation-r25-20260923.md",
    "docs/remediation-r26-20260923.md",
    "docs/remediation-r28-20260923.md",
    "docs/remediation-r27-20260923.md",
    "docs/remediation-b-batch-20260923.md",
    "docs/remediation-c-batch-20260923.md",
    "docs/rule-ownership-20260923.md",
    "docs/vendor-provenance-20260923.md",
    "docs/structure-convergence.md",
    "docs/source-export.md",
    "scripts/datasage_source_export.py",
    "tests/test_source_export.py",
    "tests/metric_readiness.py",
    "tests/context_cost.py",
    "tests/release_matrix.py",
    "tests/test_metric_readiness_register.py",
    "tests/test_domain_diagnostic_paths.py",
    "tests/test_answer_presentation.py",
    "tests/test_session_isolation.py",
    "tests/test_prompt_injection_boundary.py",
    "tests/test_execution_budget_isolation.py",
    "tests/test_external_verification_register.py",
    "tests/test_operational_entry_inventory.py",
    "tests/test_artifact_integrity.py",
    "tests/test_context_cost_baseline.py",
    "tests/test_release_matrix.py",
)

_PLUGIN_PY = _paths(
    "plugins/datasage-query",
    (
        "__init__.py",
        "acceptance_delivery.py",
        "analytical_handlers.py",
        "analytical_queries.py",
        "capability_contract.py",
        "contract_store.py",
        "contracts.py",
        "customer_history.py",
        "db_executor.py",
        "db_runtime.py",
        "db_security.py",
        "entities.py",
        "entitlements.py",
        "evidence.py",
        "fabric_report.py",
        "fabric_source_queries.py",
        "idk_batch.py",
        "idk_content.py",
        "idk_query.py",
        "idk_runner.py",
        "query_errors.py",
        "query_execution.py",
        "query_builders.py",
        "query_sql.py",
        "query_validation.py",
        "legacy_customer_compat.py",
        "legacy_message_templates.py",
        "legacy_price_bridge.py",
        "legacy_price_compat.py",
        "legacy_workflow.py",
        "legacy_xlsx.py",
        "local_report.py",
        "monthly_slow_pool.py",
        "operations.py",
        "pattern_queries.py",
        "price_reference.py",
        "price_workflow.py",
        "public_fields.py",
        "purchase_price_content.py",
        "purchase_price_runner.py",
        "regional_acceptance.py",
        "reminder_acceptance.py",
        "report_evidence.py",
        "request_contract.py",
        "result_completeness.py",
        "result_projection.py",
        "runtime_health.py",
        "sales_price_runner.py",
        "sales_reference.py",
        "schemas.py",
        "scorecard.py",
        "settings.py",
        "slow_report_batch.py",
        "slow_report_runner.py",
        "slow_task_coverage.py",
        "slow_task_retry.py",
        "sql_identifiers.py",
        "tools.py",
        "wecom_app_transport.py",
        "weekly_acceptance.py",
        "wire.py",
        "workflow_customer_audit.py",
        "workflow_cycle.py",
        "workflow_delivery_review.py",
        "workflow_fixture.py",
        "workflow_inputs.py",
        "workflow_io.py",
        "workflow_live_prices.py",
        "workflow_live_runner.py",
        "workflow_live_slow.py",
        "workflow_live_store.py",
        "workflow_manual_boundaries.py",
        "workflow_price_continuity.py",
        "workflow_price_diagnostics.py",
        "workflow_recovery_check.py",
        "workflow_roles.py",
        "workflow_runner.py",
        "workflow_schedule.py",
        "workflow_slow.py",
        "workflow_storage.py",
    ),
)

_PLUGIN_DOCS = _paths(
    "plugins/datasage-query",
    (
        "APP_NOTIFICATIONS.md",
        "LEGACY_PRICE_REFERENCE.md",
        "LEGACY_WORKFLOWS.md",
        "LIVE_WORKFLOW.md",
        "LOCAL_REPORTS.md",
        "OPERATIONS.md",
        "REMINDER_ACCEPTANCE.md",
        "WEEKLY_ACCEPTANCE.md",
        "WORKFLOW_IO.md",
        "WORKFLOW_RUNTIME.md",
    ),
)

_PLUGIN_FIXED = (
    "plugins/datasage-query/plugin.yaml",
    "plugins/datasage-query/e2e/answer_ground_truth.py",
    "plugins/datasage-query/e2e/canary_transcript_adapter.py",
    "plugins/datasage-query/e2e/golden_expert_cases.json",
    "plugins/datasage-query/e2e/golden_expert_scorer.py",
)

_CONTRACT_FILES = _paths(
    "plugins/datasage-query/contracts",
    (
        "datasets.yaml",
        "delivery-semantics.yaml",
        "entity-registry.yaml",
        "entity-rules-maintainer.md",
        "inventory-semantics.yaml",
        "legacy-workflows.json",
        "operations.yaml",
        "pattern-matching-semantics.yaml",
        "profit-semantics.yaml",
        "query-policy.yaml",
        "receipt-semantics.yaml",
        "receivable-semantics.yaml",
        "target-gap-decomposition.yaml",
        "target-semantics.yaml",
        "workflow-test-store.json",
    ),
)

_VENDOR_FILES = _paths(
    "plugins/datasage-query/vendor/pymysql-1.2.0.dist-info",
    (
        "INSTALLER",
        "licenses/LICENSE",
        "METADATA",
        "RECORD",
        "REQUESTED",
        "top_level.txt",
        "WHEEL",
    ),
) + _paths(
    "plugins/datasage-query/vendor/pymysql",
    (
        "__init__.py",
        "_auth.py",
        "charset.py",
        "connections.py",
        "converters.py",
        "cursors.py",
        "err.py",
        "optionfile.py",
        "protocol.py",
        "times.py",
    ),
) + _paths(
    "plugins/datasage-query/vendor/pymysql/constants",
    (
        "__init__.py",
        "CLIENT.py",
        "COMMAND.py",
        "CR.py",
        "ER.py",
        "FIELD_TYPE.py",
        "FLAG.py",
        "SERVER_STATUS.py",
    ),
)

_SKILL_FILES = _paths(
    "skills/business-analytics/datasage",
    (
        "SKILL.md",
        "references/answer-boundary.md",
        "references/delivery-analysis.md",
        "references/entity-guidance.md",
        "references/query-rules.md",
        "references/receipt-analysis.md",
        "references/target-analysis.md",
        "references/inventory-analysis.md",
        "references/pattern-matching-analysis.md",
        "references/profit-analysis.md",
        "references/receivable-analysis.md",
        "references/cross-domain-analysis.md",
    ),
)

_SCRIPT_FILES = _paths(
    "scripts",
    (
        "datasage_legacy_fabric.py",
        "datasage_legacy_idk.py",
        "datasage_legacy_purchase_price.py",
        "datasage_legacy_sales_price.py",
        "datasage_legacy_slow_report.py",
        "datasage_legacy_slow_task.py",
        "datasage_live_purchase.py",
        "datasage_live_sales.py",
        "datasage_live_slow_report.py",
        "datasage_live_slow_task.py",
        "datasage_region_acceptance.py",
        "datasage_reminder_acceptance.py",
        "datasage_slow_report.py",
        "datasage_weekly_acceptance.py",
        "datasage_workflow.py",
    ),
)

_TEST_FILES = _paths(
    "tests",
    (
        "business_replay.py",
        "plugin_registration_probe.py",
        "role_config_fixture.py",
        "test_acceptance_delivery.py",
        "test_analysis_evidence.py",
        "test_analytical_handlers.py",
        "test_app_notification.py",
        "test_business_contracts.py",
        "test_c01_c02_public.py",
        "test_catalog_business_definitions.py",
        "test_compact_payloads.py",
        "test_customer_audit.py",
        "test_customer_history.py",
        "test_customer_lifecycle.py",
        "test_db_executor.py",
        "test_delivery_binding.py",
        "test_delivery_l3_catalog.py",
        "test_delivery_l3_entity_time.py",
        "test_delivery_l3_golden.py",
        "test_delivery_l3_runtime.py",
        "test_delivery_l3_semantics.py",
        "test_delivery_review.py",
        "test_expert_architecture.py",
        "test_expert_authority_inventory.py",
        "test_fabric_source.py",
        "test_governed_scope_names.py",
        "test_host_compaction_e2e.py",
        "test_host_tool_schema.py",
        "test_ht_high_quantity.py",
        "test_idk_batch.py",
        "test_idk_content.py",
        "test_idk_delivery.py",
        "test_idk_entry.py",
        "test_idk_observation.py",
        "test_installed_workflow.py",
        "test_integration_boundaries.py",
        "test_interface_capability_evidence.py",
        "test_legacy_customer_compatibility.py",
        "test_legacy_message_templates.py",
        "test_legacy_price_bridge.py",
        "test_legacy_price_compatibility.py",
        "test_legacy_report_compatibility.py",
        "test_legacy_workflow.py",
        "test_live_workflow.py",
        "test_local_report_host.py",
        "test_local_report.py",
        "test_manual_boundaries.py",
        "test_module_dependencies.py",
        "test_monthly_slow_pool.py",
        "test_operations.py",
        "test_p1_projection_population.py",
        "test_pattern_matching.py",
        "test_pool_contribution_presentation.py",
        "test_price_continuity.py",
        "test_price_reference.py",
        "test_price_workflow.py",
        "test_profile_diagnostics.py",
        "test_profit_contract.py",
        "test_purchase_price_content.py",
        "test_purchase_price_recovery.py",
        "test_purchase_price_target_schema.py",
        "test_purchase_reference_entry.py",
        "test_purchase_webhook_transport.py",
        "test_push_compatibility_boundaries.py",
        "test_regional_acceptance.py",
        "test_registered_slow_pool.py",
        "test_remediation_analytical_integrity.py",
        "test_remediation_analytical_queries.py",
        "test_remediation_business_acceptance.py",
        "test_remediation_business_pilot.py",
        "test_remediation_catalog_fact_boundary.py",
        "test_remediation_contracts.py",
        "test_remediation_conversation_receipts.py",
        "test_remediation_cross_role_entity.py",
        "test_remediation_deadline.py",
        "test_remediation_delivery_scope_contract.py",
        "test_remediation_dependency_ownership.py",
        "test_remediation_domain_consolidation.py",
        "test_remediation_dso_windows.py",
        "test_remediation_entity_capacity.py",
        "test_remediation_evidence_integrity.py",
        "test_remediation_expert_evaluation.py",
        "test_remediation_host_compatibility.py",
        "test_remediation_inventory_turnover.py",
        "test_remediation_local_disclosures.py",
        "test_remediation_null_integrity.py",
        "test_remediation_overdue_coverage.py",
        "test_remediation_profile_surface.py",
        "test_remediation_public_branch_isolation.py",
        "test_remediation_remaining_cases.py",
        "test_remediation_runtime_immutability.py",
        "test_remediation_scope_composability.py",
        "test_remediation_security_runtime.py",
        "test_remediation_statement_limits.py",
        "test_remediation_target_missing_value.py",
        "test_remediation_timeout_wire.py",
        "test_remediation_tools_core.py",
        "test_reminder_acceptance.py",
        "test_repair_xlsx_precision.py",
        "test_report_completeness.py",
        "test_report_sales_unknowns.py",
        "test_request_contract_equivalence.py",
        "test_request_validation_boundary.py",
        "test_result_identity_currency.py",
        "test_role_configuration.py",
        "test_sales_price_recovery.py",
        "test_sales_price_runner.py",
        "test_sales_reference.py",
        "test_sales_reference_entry.py",
        "test_semantic_inventory.py",
        "test_slow_baseline_comparison.py",
        "test_slow_baseline_net_outbound.py",
        "test_slow_baseline_sales.py",
        "test_slow_progress.py",
        "test_slow_report_batch.py",
        "test_slow_report_delivery.py",
        "test_slow_report_entry.py",
        "test_slow_task_coverage.py",
        "test_slow_task_preparation.py",
        "test_slow_task_retry_policy.py",
        "test_slow_task_window_empty.py",
        "test_source_export.py",
        "test_structural_acceptance.py",
        "test_target_catalog_schema.py",
        "test_target_golden_cases.py",
        "test_target_runtime_customer_breakdown.py",
        "test_query_structure.py",
        "test_query_builders.py",
        "test_report_presentation.py",
        "test_readonly_skill_trace.py",
        "test_readonly_skill_registration.py",
        "test_answer_ground_truth.py",
        "test_v015_refactor_acceptance.py",
        "test_weekly_acceptance.py",
        "test_workflow_io.py",
        "test_workflow_report_gate.py",
    ),
)

_TEST_FIXTURES = _paths(
    "tests/fixtures",
    (
        "business_acceptance_cases.json",
        "business_replay_contract.json",
        "external_verification_register.json",
        "host_compaction_ordering.json",
        "legacy_customer_compatibility.json",
        "legacy_report_golden.json",
        "metric_readiness_register.json",
        "context_cost_baseline.json",
        "release_matrix.json",
        "reviewed_host_skills.json",
    ),
)

# Public for review tooling and tests.  The list is sorted once so that the
# manifest and archive are stable across runs.
SOURCE_ALLOWLIST = tuple(
    sorted(
        set(
            _ROOT_FILES
            + _PLUGIN_PY
            + _PLUGIN_DOCS
            + _PLUGIN_FIXED
            + _CONTRACT_FILES
            + _VENDOR_FILES
            + _SKILL_FILES
            + _SCRIPT_FILES
            + _TEST_FILES
            + _TEST_FIXTURES
        )
    )
)

# A caller may narrow the reviewed set only after retaining every contract and
# the plugin entrypoint.  This prevents a seemingly successful package from
# omitting the resources required for a second-Home registration.
REQUIRED_RESOURCES = frozenset(
    _CONTRACT_FILES + ("plugins/datasage-query/plugin.yaml", "plugins/datasage-query/__init__.py")
)

GENERATED_FILES = ("EXPORT_MANIFEST.json",)

_FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".env",
        "auth.json",
        ".anthropic_oauth.json",
        "state.db",
        "sessions",
        "session",
        "memory",
        "memories",
        "logs",
        "report_runs",
        "reports",
        "secrets",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".bash",
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".json",
        ".jsonl",
        ".js",
        ".jsx",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_MACHINE_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|(?:^|[\\/])(?:users|home)[\\/])"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*[A-Z][A-Z0-9_]*(?:API_KEY|PASSWORD|SECRET|TOKEN|CREDENTIAL|AUTH)[A-Z0-9_]*\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
)
_SAFE_TEMPLATE_VALUES = frozenset(
    {
        "changeme",
        "dummy",
        "example",
        "placeholder",
        "replace-me",
        "test-key-not-a-secret",
    }
)


@dataclass(frozen=True)
class ExportResult:
    """Result of an export, without exposing the source's absolute path."""

    output: Path
    archive: bool
    files: tuple[str, ...]
    manifest_sha256: str


def _assert_no_symlink(path: Path) -> None:
    """Reject symlinked components without resolving through them."""

    current = path
    if not current.exists() and not current.is_symlink():
        current = current.parent
    while True:
        try:
            if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
                raise UnsafePathError(f"symlink path component is not allowed: {current}")
        except OSError as exc:
            raise UnsafePathError(f"cannot inspect path component: {current}") from exc
        parent = current.parent
        if parent == current:
            break
        current = parent


def _assert_no_source_symlinks(root: Path) -> None:
    """Fail closed if *any* source entry is a symlink, including an unknown one."""

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in tuple(dirnames) + tuple(filenames):
            candidate = directory_path / name
            try:
                if candidate.is_symlink() or bool(getattr(candidate, "is_junction", lambda: False)()):
                    raise UnsafePathError(f"source contains a symlink: {candidate}")
            except OSError as exc:
                raise UnsafePathError(f"cannot inspect source entry: {candidate}") from exc


def _relative_path(value: str) -> PurePosixPath:
    """Validate one manifest path before joining it to a source root."""

    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise UnsafePathError(f"manifest path is not a normalized POSIX path: {value!r}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise UnsafePathError(f"manifest path escapes the profile: {value!r}")
    return relative


def _source_file(root: Path, relative: str) -> Path:
    relative_path = _relative_path(relative)
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in relative_path.parts):
        raise UnsafePathError(f"forbidden runtime path in manifest: {relative}")
    source = root.joinpath(*relative_path.parts)
    _assert_no_symlink(source)
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"manifest path escapes the source root: {relative}") from exc
    if not source.is_file():
        raise MissingResourceError(f"required reviewed resource is missing: {relative}")
    return source


def _sanitize_config(text: str) -> str:
    """Redact identity/path fields after parsing YAML, including flow-style YAML."""

    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SensitiveContentError("config.yaml is not valid YAML") from exc
    if not isinstance(config, dict):
        raise SensitiveContentError("config.yaml must contain a mapping")

    def mapping_at(*keys: str) -> dict:
        value: object = config
        for key in keys:
            if not isinstance(value, dict):
                return {}
            value = value.get(key)
        return value if isinstance(value, dict) else {}

    home_channel = mapping_at("platforms", "wecom", "home_channel")
    for key in ("chat_id", "name", "user_id"):
        if key in home_channel:
            home_channel[key] = "${WECOM_HOME_CHANNEL}"

    # Approver identities follow the same rule as the home channel: the installed
    # profile keeps the real WeCom user id, the shareable template names the env
    # placeholder.  An empty list stays empty (no gate configured -> no leak).
    platform_extra = mapping_at("platforms", "wecom", "extra")
    for key in ("allow_admin_from", "group_allow_admin_from"):
        if platform_extra.get(key):
            platform_extra[key] = ["${WECOM_APPROVER_USER_ID}"]

    pyright = mapping_at("lsp", "servers", "pyright")
    command = pyright.get("command")
    if isinstance(command, list) and command:
        command[0] = "${PYRIGHT_LANGSERVER_COMMAND}"
    elif command is not None:
        raise SensitiveContentError("pyright command must be a list")

    placeholder = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
    identity_keys = {"chat_id", "user_id", "bot_id"}
    credential_key = re.compile(
        r"(?i)(?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token|credential|auth)"
    )

    def inspect(value: object, key_path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                inspect(child, key_path + (str(key),))
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, key_path + (str(index),))
            return
        key = key_path[-1].casefold() if key_path else ""
        if isinstance(value, bool):
            # Boolean switches are ordinary config values, even when their
            # surrounding mapping also contains credential-shaped keys.
            return
        if isinstance(value, (int, float)):
            if key in identity_keys or credential_key.search(key):
                raise SensitiveContentError(
                    f"inline identity/credential at {'.'.join(key_path)}"
                )
            return
        if not isinstance(value, str):
            return
        if key in identity_keys and value and not placeholder.fullmatch(value):
            raise SensitiveContentError(f"inline channel identity at {'.'.join(key_path)}")
        if credential_key.search(key) and value and not placeholder.fullmatch(value):
            raise SensitiveContentError(f"inline credential at {'.'.join(key_path)}")
        if _MACHINE_PATH_RE.search(value):
            raise SensitiveContentError(f"absolute machine path at {'.'.join(key_path)}")

    inspect(config)
    return yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def _sanitize_env_example(text: str) -> str:
    """Return an example env file with no values and a portable LSP default."""

    output: list[str] = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^(\s*)([A-Z][A-Z0-9_]*)\s*=.*?(\r?\n)?$", line)
        if match:
            newline = match.group(3) or ""
            line = f"{match.group(1)}{match.group(2)}={newline}"
        output.append(line)
    result = "".join(output)
    if "PYRIGHT_LANGSERVER_COMMAND=" not in result:
        if result and not result.endswith(("\n", "\r")):
            result += "\n"
        result += "\n# Optional local command used by the portable config template.\n"
        result += "PYRIGHT_LANGSERVER_COMMAND=pyright-langserver\n"
    return result


def _audit_staged_content(staged: Path) -> tuple[str, ...]:
    """Reject credentials and config paths; report reviewed code examples.

    Some existing renderer code intentionally names Windows font fallbacks and
    one synthetic fixture names an old repository.  Those are source-review
    findings, not credentials.  The generated config and env template are
    required to be portable, while source examples are recorded in the
    manifest for an owner to decide separately.
    """

    findings: list[str] = []
    for path in sorted(staged.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _MACHINE_PATH_RE.search(text):
            relative = path.relative_to(staged).as_posix()
            if path.name in {"config.yaml", ".env.EXAMPLE", "profile.yaml"}:
                raise SensitiveContentError(f"machine path in {relative}")
            findings.append(f"machine path in {relative}")
        for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(text):
            value = match.group(1).strip().casefold()
            if value not in _SAFE_TEMPLATE_VALUES and not value.startswith(("${", "<", "your-")):
                findings.append(f"credential-shaped value in {path.relative_to(staged).as_posix()}")
                break
    if findings:
        return tuple(findings)
    return ()


def _manifest_digest(files: Iterable[str]) -> str:
    payload = "\n".join(files).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_manifest(
    stage: Path, files: tuple[str, ...], machine_path_findings: Sequence[str] = ()
) -> str:
    digest = _manifest_digest(files)
    manifest = {
        "schema": "datasage-source-audit-export/v1",
        "package": PACKAGE_NAME,
        "purpose": "local controlled source review; not an install or release artifact",
        "allowlist_sha256": digest,
        "files": list(files),
        "generated_files": list(GENERATED_FILES),
        "config": {
            "source_config": "config.yaml",
            "output_config": "config.yaml",
            "transformation": "channel identities, approver ids and absolute LSP path replaced with environment placeholders",
        },
        "runtime_excluded": [
            ".env",
            "auth.json",
            "state.db",
            "sessions/",
            "memories/",
            "logs/",
            "report_runs/",
            "secrets/",
        ],
        "content_review": "High-signal credential scan passed and generated config paths were scrubbed; source-level machine-path examples are recorded for owner review. Entity mappings and synthetic fixtures still require owner review before public release.",
        "source_machine_path_findings": list(machine_path_findings),
    }
    (stage / "EXPORT_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return hashlib.sha256((stage / "EXPORT_MANIFEST.json").read_bytes()).hexdigest()


def _copy_to_stage(source_root: Path, stage: Path, files: tuple[str, ...]) -> str:
    stage.mkdir(parents=True, exist_ok=False)
    for relative in files:
        source = _source_file(source_root, relative)
        destination = stage.joinpath(*_relative_path(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == "config.yaml":
            destination.write_text(
                _sanitize_config(source.read_text(encoding="utf-8")), encoding="utf-8"
            )
        elif relative == ".env.EXAMPLE":
            destination.write_text(
                _sanitize_env_example(source.read_text(encoding="utf-8")), encoding="utf-8"
            )
        else:
            shutil.copyfile(source, destination)
        try:
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
    machine_path_findings = _audit_staged_content(stage)
    manifest_sha = _write_manifest(stage, files, machine_path_findings)
    return manifest_sha


def _assert_destination_safe(source_root: Path, destination: Path) -> None:
    destination_parent = destination.parent.resolve(strict=False)
    _assert_no_symlink(destination_parent)
    try:
        destination_parent.relative_to(source_root)
    except ValueError:
        return
    raise UnsafePathError("export destination must be outside the source profile")


def _write_reproducible_archive(stage: Path, output: Path) -> None:
    """Write a deterministic gzip tar without following links."""

    output.parent.mkdir(parents=True, exist_ok=True)
    import gzip

    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(stage.rglob("*")):
                    relative = path.relative_to(stage).as_posix()
                    if path.is_symlink():
                        raise UnsafePathError(f"staged export contains a symlink: {relative}")
                    info = archive.gettarinfo(str(path), arcname=f"{PACKAGE_NAME}/{relative}")
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = 0o755 if path.is_dir() else 0o644
                    if path.is_file():
                        with path.open("rb") as source:
                            archive.addfile(info, source)
                    else:
                        archive.addfile(info)


def export_source(
    source_root: str | Path,
    output: str | Path,
    *,
    allowlist: Sequence[str] = SOURCE_ALLOWLIST,
) -> ExportResult:
    """Export reviewed source to a directory or ``.tar.gz`` archive.

    ``output`` is treated as an archive when it ends in ``.tar.gz``/``.tgz``;
    otherwise it is a new directory containing the package contents.  Existing
    outputs are refused to make accidental overwrites impossible.
    """

    source = Path(source_root)
    if not source.exists() or not source.is_dir():
        raise ExportError(f"source profile directory does not exist: {source}")
    _assert_no_symlink(source)
    source = source.resolve()
    _assert_no_source_symlinks(source)

    files = tuple(sorted({_relative_path(item).as_posix() for item in allowlist}))
    if not files:
        raise ExportError("source allowlist is empty")
    missing_required = sorted(REQUIRED_RESOURCES.difference(files))
    if missing_required:
        raise MissingResourceError(
            "allowlist omits required contract/plugin resources: "
            + ", ".join(missing_required)
        )
    for relative in files:
        _source_file(source, relative)

    destination = Path(output)
    if destination.is_symlink() or bool(getattr(destination, "is_junction", lambda: False)()):
        raise UnsafePathError(f"export destination is a link: {destination}")
    if destination.exists():
        raise ExportError(f"refusing to overwrite existing export: {destination}")
    _assert_destination_safe(source, destination)

    archive_output = destination.name.casefold().endswith((".tar.gz", ".tgz"))
    with tempfile.TemporaryDirectory(prefix="datasage-source-export-") as temporary:
        stage = Path(temporary) / PACKAGE_NAME
        manifest_sha = _copy_to_stage(source, stage, files)
        if archive_output:
            _write_reproducible_archive(stage, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage, destination, symlinks=False)

    return ExportResult(
        output=destination,
        archive=archive_output,
        files=files,
        manifest_sha256=manifest_sha,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export an explicit, credential-free DataSage source review package."
    )
    parser.add_argument("source", type=Path, help="candidate DataSage profile root")
    parser.add_argument("output", type=Path, help="new output directory or .tar.gz archive")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = export_source(args.source, args.output)
    except ExportError as exc:
        print(f"export refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(result.output), "files": len(result.files), "manifest_sha256": result.manifest_sha256}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
