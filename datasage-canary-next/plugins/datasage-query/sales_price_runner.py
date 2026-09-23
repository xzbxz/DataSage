"""Local-reference sales-price workflow.

This is the narrow ``profile_local`` entry used by ``workflow_io``.  Source
observation, role resolution, customer matching, rendering, delivery
preflight, and reference CAS are kept in one batch so a failed or incomplete
delivery cannot advance the local price head.  The reference store owns the
only local head/pending state; this module does not create a second pointer.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import legacy_price_bridge as bridge
from . import legacy_workflow as wf
from . import operations
from . import workflow_inputs as inputs
from . import workflow_io as io
from . import workflow_price_continuity as continuity
from . import sales_reference
from . import price_workflow as price


REGIONS = ("HCM", "HN", "BKK", "IDK")
REFERENCE_SOURCE = "profile_local"


class SalesPriceRunnerError(io.IOErrorBoundary):
    """A fail-closed local sales-price workflow error."""


def _json_safe(value: Any) -> Any:
    """Compatibility seam retained for existing sales tests and callers."""

    return price.json_safe(value)


def _digest(value: Any) -> str:
    return price.digest(value)


def _sha256(path: Path) -> str:
    return price.sha256(path)


def _now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _parse_time(value: Any) -> datetime:
    return price.parse_time(value, error_type=SalesPriceRunnerError)


def _validate_scope(binding: Mapping[str, Any]) -> dict[str, Any]:
    operation = binding.get("operation")
    if not isinstance(operation, dict):
        raise SalesPriceRunnerError("PROFILE_PRICE_OPERATION_REQUIRED")
    if operation.get("kind") != "sales_prices":
        raise SalesPriceRunnerError("PROFILE_PRICE_OPERATION_KIND_INVALID")
    if operation.get("reference_source") != REFERENCE_SOURCE:
        raise SalesPriceRunnerError("PROFILE_PRICE_REFERENCE_SOURCE_INVALID")
    try:
        checked = operations.validate_binding(operation)
    except operations.OperationError as exc:
        raise SalesPriceRunnerError(str(exc)) from exc
    if checked.get("regions") and set(checked["regions"]) != set(REGIONS):
        raise SalesPriceRunnerError("PROFILE_PRICE_REQUIRES_FOUR_REGIONS")
    if binding.get("send_enabled") and binding.get("price_accept_enabled") is not True:
        raise SalesPriceRunnerError("PROFILE_PRICE_REFERENCE_WRITE_NOT_ENABLED")
    if type(binding.get("send_enabled", False)) is not bool:
        raise SalesPriceRunnerError("PROFILE_PRICE_SEND_FLAG_INVALID")
    if type(binding.get("price_accept_enabled", False)) is not bool:
        raise SalesPriceRunnerError("PROFILE_PRICE_ACCEPT_FLAG_INVALID")
    return checked


def _head(reference_store: sales_reference.SalesReferenceStore) -> dict[str, Any] | None:
    value = reference_store.load()
    if value is None:
        return None
    reference = value.get("reference")
    if not isinstance(reference, dict) or not isinstance(reference.get("rows"), list):
        raise SalesPriceRunnerError("SALES_REFERENCE_HEAD_INVALID")
    return value


def _same_hour(head: Mapping[str, Any] | None, now: datetime) -> bool:
    return price.same_hour(head, now)


def _last_observed_at(head: Mapping[str, Any] | None) -> datetime | None:
    return price.last_observed_at(head)


def _source_rows(
    snapshots: Any,
    operation: Mapping[str, Any],
    week: str,
    *,
    with_roles: bool = False,
    before: list[Mapping[str, Any]] | None = None,
    recipients: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], datetime, dict[str, Any], dict[str, Any] | None]:
    """Read current prices and optional roles/mapping in one consistent snapshot."""

    source_operation = {key: value for key, value in operation.items() if key != "reference_source"}
    sql, params = operations.build_observation(source_operation)
    # ``build_observation`` validates the ordinary operation shape; source
    # selection itself is still the fixed registered sales query.
    role_rows: list[dict[str, Any]] = []
    mapping: dict[str, Any] | None = None
    planned: dict[str, Any] | None = None
    with snapshots() as db:
        rows = inputs.complete(db, sql, params, limit=operation["limit"])
        if not rows:
            raise SalesPriceRunnerError("PRICE_LOCAL_EMPTY_SOURCE_KEEP_REFERENCE")
        if rows:
            stamps = [_parse_time(row.get("observed_at")) for row in rows if row.get("observed_at") is not None]
            if len(stamps) != len(rows) or len(set(stamps)) != 1:
                raise SalesPriceRunnerError("PRICE_LOCAL_OBSERVATION_CLOCK_INCONSISTENT")
            observed_at = stamps[0]
        if with_roles:
            specs = wf.source_query_specs(week)
            role_rows = inputs.complete(db, specs[2]["sql"], specs[2]["params"])
            executor_accounts = sorted({str(item.get("account") or "").strip() for region in (recipients or {}).get("regions", {}).values() for item in (region.get("executors") or []) if item.get("account")})
            if executor_accounts:
                role_rows.extend(inputs.complete(db, "SELECT wecom_account,position,is_delete FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_account IN (" + ",".join("%s" for _ in executor_accounts) + ") LIMIT 10001", executor_accounts))
            if before is not None:
                planned = continuity.plan("sales", before, rows, observed_at, exact_prices=True)
                planned_changes = bridge.changes(planned["document"])
                if planned_changes:
                    pairs = [(str(row.get("goods_no") or ""), str(row.get("dept") or "")) for row in planned_changes]
                    mapping = inputs.customer_mapping(db, pairs)
                else:
                    mapping = {}
            else:
                planned = None
        marker = getattr(db, "marker", None)
    if not marker:
        raise SalesPriceRunnerError("PRICE_LOCAL_SNAPSHOT_MARKER_MISSING")
    observation = {
        "observed_at": observed_at.isoformat(sep=" "),
        "snapshot_marker": marker,
        "source_rows": len(rows),
        "source_limit": operation["limit"],
        "source_kind": "sales_prices",
        "clock": "database_observation",
    }
    if role_rows:
        observation["role_rows"] = len(role_rows)
    return rows, observed_at, observation, {"employees": role_rows, "mapping": mapping, "planned": planned} if with_roles else None


def _buyers_by_region(mapping: Mapping[str, Any], changes: list[Mapping[str, Any]], region: str) -> dict[str, list[list[Any]]]:
    buyers: dict[str, list[list[Any]]] = {}
    for customer_id, products in (mapping.get("productsByCustomer") or {}).items():
        info = mapping.get("customerInfo", {}).get(customer_id, {})
        customer_no = str(info.get("customer_no") or "").strip()
        if not customer_no:
            continue
        for change in changes:
            goods_no = str(change.get("goods_no") or "").strip()
            if not goods_no or str(change.get("dept") or "").strip() != region:
                continue
            if not any(str(product.get("goods_no") or "").strip() == goods_no and str(product.get("whse_dept") or "").strip() == region for product in products):
                continue
            record = [info.get("sales"), customer_no, info.get("name")]
            rows = buyers.setdefault(goods_no, [])
            if any(existing[1] == record[1] and existing != record for existing in rows):
                raise SalesPriceRunnerError("BUYER_CUSTOMER_NUMBER_AMBIGUOUS")
            if record not in rows:
                rows.append(record)
    return buyers


def _observed_body(body: str, observed_at: str, disclosure: str) -> str:
    notice = str(disclosure or "").strip()
    suffix = f"\n\nObserved at: {observed_at}"
    return (notice + "\n\n" + body if notice else body) + suffix


def _prepare_components(
    profile: Path,
    out: Path,
    batch_id: str,
    document: Mapping[str, Any],
    changes: list[Mapping[str, Any]],
    recipients: Mapping[str, Any],
    employees: list[Mapping[str, Any]],
    mapping: Mapping[str, Any],
    observed_at: str,
    target_map: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    affected = list(dict.fromkeys(str(change.get("dept") or "").strip() for change in changes if change.get("dept")))
    target_plan = wf.price_recipients(recipients["regions"], employees, affected, recipients.get("price_manager_fixed", []))
    components: list[dict[str, Any]] = []
    attachments: dict[str, str] = {}
    prepared: dict[str, Any] = {"target_plan": target_plan, "sales": {}, "managers": {}}
    disclosure = document.get("scope_notice") or ""
    for region in affected:
        regional = [dict(change) for change in changes if change.get("dept") == region]
        buyers = _buyers_by_region(mapping, regional, region)
        prepared["sales"][region] = {"buyers": buyers, "targets": target_plan[region]["sales"]}
        for target in target_plan[region]["sales"]:
            account = target["account"]
            own = {
                goods: [[customer_no, customer_name] for owner, customer_no, customer_name in rows if mapping.get("wecomBySales", {}).get(owner) == account]
                for goods, rows in buyers.items()
            }
            folder = out / "sales" / wf.account_token(account)[:12]
            folder.mkdir(parents=True, exist_ok=True)
            data = {
                "evidence_origin": "existing_local_observation",
                "observed_at": observed_at,
                "region": region,
                "sales_name": target.get("name") or account,
                "changes": regional,
                "customers_by_goods": own,
                "customer_mapping_complete": True,
            }
            bundle = wf.build_preview("sales_price", data, folder)
            body = _observed_body(bundle["message_bodies"][0], observed_at, disclosure)
            scope = _digest([batch_id, region, account, "sales", regional, own, body])
            components.append(io.component(account, "text", body, scope, "sales_text"))
            for file_name in bundle["files"]:
                if not str(file_name).endswith(".xlsx"):
                    continue
                path = folder / file_name
                components.append(io.component(account, "file", path, scope, "sales_file"))
                attachments[str(path)] = _sha256(path)
        if buyers:
            manager_rows = buyers
            prepared["managers"][region] = {"buyers": manager_rows, "targets": target_plan[region]["managers"]}
            for account in target_plan[region]["managers"]:
                folder = out / "managers" / wf.account_token(account)[:12]
                folder.mkdir(parents=True, exist_ok=True)
                data = {
                    "evidence_origin": "existing_local_observation",
                    "observed_at": observed_at,
                    "region": region,
                    "changes": regional,
                    "manager_rows_by_goods": manager_rows,
                    "customer_mapping_complete": True,
                }
                bundle = wf.build_preview("sales_price", data, folder)
                from .legacy_message_templates import sales as manager_message

                body = _observed_body(manager_message(regional, manager=True, region=region) + "\n\nSee attachment for all sales' customers across this region (one Sheet per product).", observed_at, disclosure)
                scope = _digest([batch_id, region, account, "manager", regional, manager_rows, body])
                components.append(io.component(account, "text", body, scope, "manager_text"))
                for file_name in bundle["files"]:
                    if not str(file_name).endswith(".xlsx"):
                        continue
                    path = folder / file_name
                    components.append(io.component(account, "file", path, scope, "manager_file"))
                    attachments[str(path)] = _sha256(path)
    used_accounts = {item["account"] for item in components}
    missing_targets = sorted(account for account in used_accounts if account not in target_map)
    prepared["unbound_accounts"] = missing_targets
    if len({item["key"] for item in components}) != len(components):
        raise SalesPriceRunnerError("PRICE_COMPONENT_KEY_DUPLICATE")
    return components, attachments, prepared


def _validate_pending(profile: Path, pending: Mapping[str, Any], target_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    return price.validate_pending(profile, pending, target_map, error_type=SalesPriceRunnerError)


def _record_batch_binding(progress: Any, batch_id: str, content_seal: str) -> None:
    price.record_batch_binding(progress, batch_id, content_seal, side="sales", error_type=SalesPriceRunnerError)


def _validate_existing_delivery_binding(progress: Any, components: list[dict[str, Any]], transport: Any, batch_id: str, content_seal: str) -> None:
    price.validate_existing_delivery_binding(
        progress,
        components,
        transport,
        batch_id,
        content_seal,
        side="sales",
        error_type=SalesPriceRunnerError,
    )


def _assert_pending_normalized(components: list[dict[str, Any]], transport: Any, progress: Any) -> None:
    price.assert_pending_normalized(components, transport, progress, error_type=SalesPriceRunnerError)


def _preview_validate(profile: Path, components: list[dict[str, Any]]) -> dict[str, Any]:
    return price.preview_validate(profile, components, error_type=SalesPriceRunnerError)


def _receipt_evidence(progress: Any, components: list[dict[str, Any]], batch_id: str, content_seal: str, *, status: str = "provider_accepted") -> dict[str, Any]:
    return price.receipt_evidence(
        progress,
        components,
        batch_id,
        content_seal,
        status=status,
        error_type=SalesPriceRunnerError,
    )


def _write_manifest(out: Path, value: Mapping[str, Any]) -> None:
    price.write_manifest(out, value, filename="sales-price-manifest.json")


def _sales_reader(
    snapshots: Any,
    operation: Mapping[str, Any],
    *,
    profile: Path,
    before: list[Mapping[str, Any]] | None,
    head: Mapping[str, Any] | None,
    week: str,
    month: str,
    binding: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Adapt the existing sales reader to the shared price engine protocol."""

    del month
    recipients = io.read_recipients(profile, binding) if head is not None else None
    rows, observed_at, observation, role_data = _source_rows(
        snapshots,
        operation,
        week,
        with_roles=head is not None,
        before=before,
        recipients=recipients,
    )
    planned = (role_data or {}).get("planned") if head is not None else None
    if planned is None:
        if head is not None:
            raise SalesPriceRunnerError("PRICE_LOCAL_CONTINUITY_PLAN_MISSING")
        planned = continuity.plan("sales", [], rows, observed_at, exact_prices=True)
    document = dict(planned["document"])
    document["baseline_source"] = (
        "profile_local_reference" if head is not None else "profile_local_reference_initialization_candidate"
    )
    document["reference_source"] = REFERENCE_SOURCE
    if head is None:
        document["scope_notice"] = "本批尚无已审核的本地销售价格参考；仅生成静默初始化候选，不把首次观察当作变价提醒。"
    else:
        document["reference"] = {
            "source": REFERENCE_SOURCE,
            "digest": head["reference"]["digest"],
            "rows": len(before or []),
            "history_unknown": bool(head.get("history_unknown")),
        }
        document["scope_notice"] = (
            "本批以 Profile 中已审核的本地销售价格参考为比较基线；"
            "库存单位、计价单位、税标记和有效期若未在参考中提供，保持未核验；"
            "以下只表示同币种名义 DDP 字段变化，不等同于可执行报价。"
        )
    changes = bridge.changes(document) if head is not None else []
    return {
        "rows": rows,
        "before": before or [],
        "head": head,
        "observed_at": observed_at,
        "observation": observation,
        "planned": planned,
        "document": document,
        "changes": changes,
        "after_reference": planned["after"],
        "recipients": recipients,
        "role_data": role_data,
        "queried_roles": head is not None,
    }


def _sales_prepare(
    profile: Path,
    out: Path,
    context: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    batch_id: str | None,
    initial: bool,
) -> Mapping[str, Any]:
    """Adapt the existing sales preparer without changing its test seams."""

    document = dict(context.get("document") or {})
    changes = list(context.get("changes") or [])
    base_observation = {
        **dict(context.get("observation") or {}),
        "document": _json_safe(document),
        "before_records": _json_safe(context.get("before") or []),
        "current_records": _json_safe(context.get("rows") or []),
        "after_reference": _json_safe(context.get("after_reference") or []),
        "reference_before_digest": ((context.get("head") or {}).get("reference") or {}).get("digest") if isinstance(context.get("head"), Mapping) else None,
        "roles": _json_safe(((context.get("role_data") or {}).get("employees") or [])),
        "matching": {
            "window": _json_safe(((context.get("role_data") or {}).get("mapping") or {}).get("window")),
            "meaning": ((context.get("role_data") or {}).get("mapping") or {}).get("meaning"),
            "as_of": _json_safe((((context.get("role_data") or {}).get("mapping") or {}).get("window") or {}).get("end")),
        },
        "event_counts": document.get("event_counts", {}),
        "scope_notice": document.get("scope_notice"),
        "deliverable_events": len(changes),
    }
    if initial:
        return {
            "components": [],
            "attachments": {},
            "summary": {"initial_observation_only": True},
            "document": document,
            "changes": [],
            "after_reference": context.get("after_reference"),
            "observation": base_observation,
        }
    role_data = context.get("role_data")
    if role_data is None:
        raise SalesPriceRunnerError("PRICE_ROLE_OBSERVATION_MISSING")
    if not changes:
        return {
            "components": [],
            "attachments": {},
            "summary": {},
            "document": document,
            "changes": [],
            "after_reference": context.get("after_reference"),
            "observation": base_observation,
        }
    mapping = role_data.get("mapping") or {}
    components, attachments, prepared = _prepare_components(
        profile,
        out,
        batch_id or "pending",
        document,
        changes,
        context.get("recipients") or {},
        role_data.get("employees") or [],
        mapping,
        str(context.get("observed_at")),
        binding.get("target_map") or {},
    )
    if not components:
        raise SalesPriceRunnerError("PRICE_REQUIRED_RECIPIENT_PLAN_EMPTY")
    if binding.get("send_enabled") and prepared.get("unbound_accounts"):
        raise SalesPriceRunnerError("PRICE_TARGET_MAPPING_MISSING")
    return {
        "components": components,
        "attachments": attachments,
        "summary": prepared,
        "document": document,
        "changes": changes,
        "after_reference": context.get("after_reference"),
        "observation": base_observation,
    }


def _sales_reference_factory(profile: Path, *, side: str = "sales") -> Any:
    if side != "sales":
        raise SalesPriceRunnerError("SALES_REFERENCE_SIDE_INVALID")
    return sales_reference.SalesReferenceStore(profile, mode="local")


def _engine() -> price.PriceWorkflowEngine:
    return price.PriceWorkflowEngine(
        "sales",
        reference_factory=_sales_reference_factory,
        reader=_sales_reader,
        prepare=_sales_prepare,
        now=_now_local,
        save_manifest=_write_manifest,
        error_type=SalesPriceRunnerError,
        job="sales_price",
    )


def _pending_run(
    profile: Path,
    binding: Mapping[str, Any],
    out: Path,
    progress: Any,
    transport: Any,
    head: Mapping[str, Any],
    reference_store: Any | None = None,
) -> dict[str, Any]:
    """Compatibility seam delegating all recovery controls to the shared engine."""

    store = reference_store or sales_reference.SalesReferenceStore(profile, mode="local")
    return price.recover_pending(
        profile,
        binding,
        out,
        progress,
        transport,
        store,
        head,
        side="sales",
        job="sales_price",
        error_type=SalesPriceRunnerError,
        save_manifest=_write_manifest,
    )


def run(profile: Path, binding: Mapping[str, Any], out: Path, week: str, month: str, progress: Any, snapshots: Any, transport: Any) -> dict[str, Any]:
    try:
        return _run(profile,binding,out,week,month,progress,snapshots,transport)
    except (sales_reference.SalesReferenceError,operations.OperationError,wf.WorkflowError) as exc:
        if isinstance(exc,io.IOErrorBoundary):raise
        raise SalesPriceRunnerError(str(exc)) from exc


def _run(profile: Path, binding: Mapping[str, Any], out: Path, week: str, month: str, progress: Any, snapshots: Any, transport: Any) -> dict[str, Any]:
    """Run sales through the shared price engine and preserve sales seams."""

    _validate_scope(binding)
    return _engine().run(profile, binding, out, week, month, progress, snapshots, transport)

__all__ = ["SalesPriceRunnerError", "run"]
