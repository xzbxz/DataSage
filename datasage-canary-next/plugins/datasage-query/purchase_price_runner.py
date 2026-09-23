"""Local-reference purchase-price workflow.

Purchase price observations share the sales reminder's durable reference and
delivery protocol.  Only source selection and content preparation are
purchase-specific: the operation reads the existing four-region purchase pool,
continuity compares recorded purchase quotes, and one explicit appchat group
receives a bounded Markdown summary.  No customer or employee lookup is part
of this workflow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Mapping

from . import legacy_price_bridge as bridge
from . import legacy_workflow as wf
from . import operations
from . import price_workflow as price
from . import workflow_inputs as inputs
from . import workflow_io as io
from . import workflow_price_continuity as continuity

from .price_reference import PriceReferenceStore, PriceReferenceError


REGIONS = ("HCM", "HN", "BKK", "IDK")
REFERENCE_SOURCE = "profile_local"
JOB = "purchase_price"
ROBOT_PLATFORM = "wecom_webhook"
ROBOT_TARGET_KIND = "robot_group"
ROBOT_TARGET_REF_RE = re.compile(r"^[0-9a-f]{64}$")
ROBOT_WEBHOOK_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,127}$")


class PurchasePriceRunnerError(io.IOErrorBoundary):
    """Fail-closed purchase-price workflow error."""


class PurchaseReceiptGateTransport:
    """Receipt gate around the existing transport, not a second sender.

    The shared ``official_receipt`` adapter historically accepts a successful
    adapter result when it has a message id or arbitrary raw response.  The
    purchase provider contract requires an explicit business result code.  We
    delegate every transport capability and call the inner ``send`` exactly
    once, changing only the result presented to the shared receipt adapter.
    """

    def __init__(self, inner: Any):
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @staticmethod
    def _business_code(raw: Any) -> tuple[str, int | None]:
        """Return accepted/failed/unknown from all provider business codes."""

        if not isinstance(raw, Mapping):
            return "unknown", None
        main_values = []
        for key in ("errcode", "markdown_errcode"):
            if key not in raw:
                continue
            value = raw.get(key)
            if type(value) is not int:
                return "unknown", None
            main_values.append(value)
        # A helper mention result is auxiliary but still part of the business
        # outcome.  Presence with a non-integer value cannot prove acceptance.
        if "mention_errcode" in raw:
            mentions = raw.get("mention_errcode")
            values = list(mentions) if isinstance(mentions, (list, tuple)) else [mentions]
            if any(type(value) is not int for value in values):
                return "unknown", None
            for value in values:
                if value != 0:
                    return "failed", value
        if not main_values:
            return "unknown", None
        for value in main_values:
            if value != 0:
                return "failed", value
        return "accepted", 0

    def send(self, item: Mapping[str, Any]) -> dict[str, Any]:
        result = self.inner.send(item)
        if isinstance(result, Mapping):
            value = dict(result)
        else:
            value = {"result": result}
        raw = value.get("raw_response")
        code_state, code = self._business_code(raw)
        invalid = bool(value.get("invalid_recipient")) or (isinstance(raw, Mapping) and bool(raw.get("invalid_recipient")))
        delivered = value.get("delivered")
        explicit_success = value.get("success") is True and delivered is not False and not invalid
        if code_state == "accepted" and explicit_success:
            # Keep the provider evidence intact while making the explicit
            # business code visible to official_receipt.
            return value
        if code_state == "failed" and code is not None and code != 0:
            value["success"] = False
            if isinstance(raw, Mapping):
                value["raw_response"] = {**raw, "errcode": code}
            return value
        # HTTP/exit-level success without a business code is unknown.  Do not
        # turn it into provider acceptance; the shared adapter records unknown
        # and the durable pending batch remains for review.
        value["success"] = False
        if invalid:
            value["raw_response"] = {**raw, "invalid_recipient": True} if isinstance(raw, Mapping) else {"invalid_recipient": True}
        return value


def _json_safe(value: Any) -> Any:
    return price.json_safe(value)


def _digest(value: Any) -> str:
    return price.digest(value)


def _now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _parse_time(value: Any) -> datetime:
    return price.parse_time(value, error_type=PurchasePriceRunnerError)


def _write_manifest(out: Path, value: Mapping[str, Any]) -> None:
    price.write_manifest(out, value, filename="purchase-price-manifest.json")


def _reference_factory(profile: Path, *, side: str = "purchase") -> Any:
    """Construct the shared side-aware reference store.

    ``PriceReferenceStore`` accepts ``(profile, mode='local', side=...)``.
    Keeping this tiny factory as a named seam lets isolated tests inject a
    store without changing the workflow engine.
    """

    if side != "purchase":
        raise PurchasePriceRunnerError("PURCHASE_REFERENCE_SIDE_INVALID")
    return PriceReferenceStore(profile, mode="local", side=side)


def _validate_group_target(binding: Mapping[str, Any], *, required: bool) -> dict[str, Any]:
    """Require one explicit appchat or robot group; never infer a private recipient."""

    target_map = binding.get("target_map")
    if target_map is None and not required:
        return {}
    if not isinstance(target_map, Mapping) or len(target_map) != 1:
        raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
    account, target = next(iter(target_map.items()))
    if not isinstance(account, str) or not account.strip() or not isinstance(target, Mapping):
        raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
    target_kind = target.get("target_kind")
    platform = target.get("platform")
    if target_kind == "appchat" and platform == "wecom_app_http":
        required = {"platform", "app_name", "corp_id", "agent_id", "target_kind", "target_id"}
        if set(target) != required or any(type(target.get(field)) is not str or not target[field] for field in required):
            raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
        target_id = str(target.get("target_id"))
        lowered_target_id = target_id.lower()
        if (not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", target_id)
                or lowered_target_id == "@all"
                or lowered_target_id.startswith(("http://", "https://"))
                or "webhook" in lowered_target_id):
            raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
    elif target_kind == ROBOT_TARGET_KIND and platform == ROBOT_PLATFORM:
        # Production robot destinations carry only a URL digest and a
        # controlled credential-field reference.  The resolver/transport owns
        # the secret and URL; neither can enter this sealed target map/source.
        if set(target) != {"platform", "target_kind", "target_ref", "webhook_ref"}:
            raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
        target_ref = target.get("target_ref")
        webhook_ref = target.get("webhook_ref")
        if (not isinstance(target_ref, str) or not ROBOT_TARGET_REF_RE.fullmatch(target_ref)
                or not isinstance(webhook_ref, str) or not ROBOT_WEBHOOK_REF_RE.fullmatch(webhook_ref)
                or "test_webhook" in webhook_ref.lower()
                or webhook_ref != "purchase_price"):
            raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REFERENCE_INVALID")
    else:
        raise PurchasePriceRunnerError("PURCHASE_GROUP_TARGET_REQUIRED")
    return {str(account): dict(target)}


def _validate_scope(binding: Mapping[str, Any]) -> dict[str, Any]:
    operation = binding.get("operation")
    if not isinstance(operation, Mapping):
        raise PurchasePriceRunnerError("PROFILE_PRICE_OPERATION_REQUIRED")
    if operation.get("kind") != "purchase_prices":
        raise PurchasePriceRunnerError("PROFILE_PRICE_OPERATION_KIND_INVALID")
    if operation.get("reference_source") != REFERENCE_SOURCE:
        raise PurchasePriceRunnerError("PROFILE_PRICE_REFERENCE_SOURCE_INVALID")
    try:
        checked = operations.validate_binding(operation)
    except operations.OperationError as exc:
        raise PurchasePriceRunnerError(str(exc)) from exc
    if set(checked.get("regions") or ()) != set(REGIONS):
        raise PurchasePriceRunnerError("PROFILE_PRICE_REQUIRES_FOUR_REGIONS")
    if type(binding.get("send_enabled", False)) is not bool:
        raise PurchasePriceRunnerError("PROFILE_PRICE_SEND_FLAG_INVALID")
    if type(binding.get("price_accept_enabled", False)) is not bool:
        raise PurchasePriceRunnerError("PROFILE_PRICE_ACCEPT_FLAG_INVALID")
    if binding.get("send_enabled") and binding.get("price_accept_enabled") is not True:
        raise PurchasePriceRunnerError("PROFILE_PRICE_REFERENCE_WRITE_NOT_ENABLED")
    # A configured route is always a single group.  During a source-only
    # initialization candidate it may be omitted, but an attempted delivery
    # can never fall back to a private account.
    # If a route is supplied it must already be the one explicit group; an
    # empty route is allowed only for a source-only initialization candidate.
    if binding.get("target_map") is not None:
        _validate_group_target(binding, required=True)
    elif binding.get("send_enabled"):
        _validate_group_target(binding, required=True)
    return checked


def _observation_stamp(rows: list[Mapping[str, Any]]) -> datetime:
    stamps = []
    for row in rows:
        if row.get("observed_at") is None:
            raise PurchasePriceRunnerError("PRICE_LOCAL_OBSERVATION_CLOCK_INVALID")
        stamps.append(_parse_time(row.get("observed_at")))
    if not stamps or len(set(stamps)) != 1:
        raise PurchasePriceRunnerError("PRICE_LOCAL_OBSERVATION_CLOCK_INCONSISTENT")
    return stamps[0]


def _source_rows(
    snapshots: Any,
    operation: Mapping[str, Any],
    week: str,
    *,
    before: list[Mapping[str, Any]] | None = None,
    head: Mapping[str, Any] | None = None,
    recipients: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], datetime, dict[str, Any], dict[str, Any]]:
    """Read one bounded purchase pool snapshot and continuity plan.

    The unused compatibility parameters keep this reader easy to inject beside
    the existing sales seam.  There are deliberately no role/customer calls.
    """

    del week, head, recipients
    source_operation = {key: value for key, value in operation.items() if key != "reference_source"}
    sql, params = operations.build_observation(source_operation)
    with snapshots() as db:
        rows = inputs.complete(db, sql, params, limit=operation["limit"])
        if not rows:
            raise PurchasePriceRunnerError("PRICE_LOCAL_EMPTY_SOURCE_KEEP_REFERENCE")
        observed_at = _observation_stamp(rows)
        marker = getattr(db, "marker", None)
    if not marker:
        raise PurchasePriceRunnerError("PRICE_LOCAL_SNAPSHOT_MARKER_MISSING")
    observation = {
        "observed_at": observed_at.isoformat(sep=" "),
        "snapshot_marker": marker,
        "source_rows": len(rows),
        "source_limit": operation["limit"],
        "source_kind": "purchase_prices",
        "clock": "database_observation",
        "regions": list(REGIONS),
    }
    planned = None
    if before is not None:
        planned = continuity.plan("purchase", before, rows, observed_at)
    return rows, observed_at, observation, {"planned": planned, "queried_roles": False}


def _parts_result(changes: list[Mapping[str, Any]], observed_at: str, disclosure: str) -> tuple[list[str], dict[str, Any]]:
    """Adapt C's content builder while keeping a small integration seam."""

    from .purchase_price_content import build_purchase_parts

    built = build_purchase_parts(changes, observed_at=observed_at, disclosure=disclosure, max_bytes=4000)
    parts = list(built.parts)
    summary = dict(built.summary)
    if any(not isinstance(part, str) or not part for part in parts):
        raise PurchasePriceRunnerError("PURCHASE_CONTENT_PARTS_INVALID")
    for part in parts:
        if len(part.encode("utf-8")) > 4000:
            raise PurchasePriceRunnerError("PURCHASE_CONTENT_PART_TOO_LARGE")
    return parts, summary


def _prepare_components(
    profile: Path,
    out: Path,
    batch_id: str,
    document: Mapping[str, Any],
    changes: list[Mapping[str, Any]],
    target_map: Mapping[str, Any],
    observed_at: str,
    *,
    disclosure: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """Prepare one group's bounded Markdown parts; never create attachments."""

    del profile, out
    group = _validate_group_target({"target_map": target_map}, required=True)
    account = next(iter(group))
    parts, summary = _parts_result(changes, observed_at, disclosure)
    # One scope gives every part one logical notification.  The stage keeps
    # each component key distinct, and the engine adds the final batch identity.
    scope = _digest([batch_id or "purchase", account, observed_at, changes, disclosure])
    components = []
    for index, body in enumerate(parts):
        item = io.component(account, "text", body, scope, f"purchase_group_part_{index:04d}")
        item["message_format"] = "markdown"
        components.append(item)
    if len({item["key"] for item in components}) != len(components):
        raise PurchasePriceRunnerError("PRICE_COMPONENT_KEY_DUPLICATE")
    summary = {**summary, "part_count": len(parts), "group_account": account, "group_target_kind": group[account].get("target_kind")}
    return components, {}, summary


def _reader_adapter(
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
    del profile, month, binding
    rows, observed_at, observation, extra = _source_rows(snapshots, operation, week, before=before, head=head)
    planned = extra.get("planned")
    if planned is None:
        planned = continuity.plan("purchase", [], rows, observed_at)
    document = dict(planned["document"])
    document["baseline_source"] = "profile_local_reference"
    document["reference_source"] = REFERENCE_SOURCE
    document["observation_clock"] = "database_observation"
    if head is not None:
        document["reference"] = {
            "source": REFERENCE_SOURCE,
            "digest": head["reference"]["digest"],
            "rows": len(before or []),
            "history_unknown": bool(head.get("history_unknown")),
        }
        document["scope_notice"] = (
            "本批以 Profile 中已审核的本地采购价格参考为比较基线；"
            "含税价与不含税价分开比较，历史有效期若未记录保持未核验；"
            "以下表示记录报价变化，不等同于当前可执行采购价。"
        )
    else:
        document["scope_notice"] = "本批尚无已审核的本地采购价格参考；仅生成静默初始化候选，不把首次观察当作变价提醒。"
    changes = bridge.changes(document) if head is not None else []
    return {
        "rows": rows,
        "observed_at": observed_at,
        "observation": observation,
        "planned": planned,
        "document": document,
        "changes": changes,
        "after_reference": planned["after"],
        "queried_roles": False,
    }


def _prepare_adapter(
    profile: Path,
    out: Path,
    context: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    batch_id: str | None,
    initial: bool,
) -> Mapping[str, Any]:
    document = dict(context.get("document") or {})
    changes = list(context.get("changes") or [])
    if initial:
        # Initial observation is an explicit candidate only.  It deliberately
        # has no deliverable changes even when the pool is non-empty.
        changes = []
        return {
            "components": [],
            "attachments": {},
            "summary": {"part_count": 0, "initial_observation_only": True},
            "document": document,
            "changes": changes,
            "after_reference": context.get("after_reference") or [],
            "observation": context.get("observation") or {},
        }
    target_map = _validate_group_target(binding, required=bool(changes) or bool(binding.get("send_enabled")))
    components, attachments, summary = _prepare_components(
        profile,
        out,
        batch_id or "purchase",
        document,
        changes,
        target_map,
        str(context.get("observed_at")),
        disclosure=str(document.get("scope_notice") or ""),
    ) if changes else ([], {}, {"part_count": 0})
    return {
        "components": components,
        "attachments": attachments,
        "summary": summary,
        "document": document,
        "changes": changes,
        "after_reference": context.get("after_reference") or [],
        "observation": context.get("observation") or {},
    }


def _engine() -> price.PriceWorkflowEngine:
    return price.PriceWorkflowEngine(
        "purchase",
        reference_factory=_reference_factory,
        reader=_reader_adapter,
        prepare=_prepare_adapter,
        now=_now_local,
        save_manifest=_write_manifest,
        error_type=PurchasePriceRunnerError,
        job=JOB,
    )


def run(
    profile: Path,
    binding: Mapping[str, Any],
    out: Path,
    week: str,
    month: str,
    progress: Any,
    snapshots: Any,
    transport: Any,
) -> dict[str, Any]:
    """Prepare or deliver one hourly purchase-price group batch."""

    try:
        _validate_scope(binding)
        gated_transport = PurchaseReceiptGateTransport(transport) if binding.get("send_enabled") and transport is not None else transport
        return _engine().run(profile, binding, out, week, month, progress, snapshots, gated_transport)
    except (PurchasePriceRunnerError, io.IOErrorBoundary):
        raise
    except (PriceReferenceError, operations.OperationError, wf.WorkflowError, ValueError) as exc:
        raise PurchasePriceRunnerError(str(exc)) from exc


__all__ = [
    "JOB",
    "PurchasePriceRunnerError",
    "REFERENCE_SOURCE",
    "REGIONS",
    "PriceReferenceStore",
    "PurchaseReceiptGateTransport",
    "ROBOT_PLATFORM",
    "ROBOT_TARGET_KIND",
    "ROBOT_TARGET_REF_RE",
    "ROBOT_WEBHOOK_REF_RE",
    "_now_local",
    "_parse_time",
    "_prepare_components",
    "_source_rows",
    "run",
]
