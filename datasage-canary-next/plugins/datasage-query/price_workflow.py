"""Shared local price workflow controls.

The sales and purchase reminders differ in how they read and render a price
observation.  Their durable delivery contract is the same, though: a pending
batch is immutable, delivery evidence is bound to the exact component bytes,
and a reference head advances only after every required component is accepted
by the provider.  This module owns those controls so the purchase reminder
does not grow a second copy of the sales recovery logic.

The helpers deliberately accept the small interfaces already exposed by
``workflow_io`` and the reference stores.  That keeps them useful while the
sales reference store is being replaced by the side-aware shared store.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from . import legacy_workflow as wf
from . import workflow_io as io
from . import operations


class PriceWorkflowError(io.IOErrorBoundary):
    """Fail-closed local price workflow error."""


def json_safe(value: Any) -> Any:
    """Convert database values to deterministic JSON without float coercion."""

    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def digest(value: Any) -> str:
    return wf.digest(json_safe(value))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: Any, *, error_type: type[Exception] = PriceWorkflowError, code: str = "PRICE_OBSERVATION_CLOCK_INVALID") -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise error_type(code) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return parsed


def same_hour(head: Mapping[str, Any] | None, now: datetime) -> bool:
    committed = head.get("last_committed") if isinstance(head, Mapping) else None
    if not isinstance(committed, Mapping):
        return False
    value = committed.get("observed_at") or committed.get("committed_at")
    if not value:
        return False
    try:
        return parse_time(value).strftime("%Y%m%d%H") == now.strftime("%Y%m%d%H")
    except PriceWorkflowError:
        return False


def last_observed_at(head: Mapping[str, Any] | None) -> datetime | None:
    committed = head.get("last_committed") if isinstance(head, Mapping) else None
    if not isinstance(committed, Mapping):
        return None
    value = committed.get("observed_at") or committed.get("committed_at")
    if not value:
        return None
    try:
        return parse_time(value)
    except PriceWorkflowError:
        return None


def _error(error_type: type[Exception], code: str) -> Exception:
    return error_type(code)


def validate_pending(
    profile: Path,
    pending: Mapping[str, Any],
    target_map: Mapping[str, Any],
    *,
    error_type: type[Exception] = PriceWorkflowError,
) -> list[dict[str, Any]]:
    """Validate sealed pending content and attachment bytes.

    The pending payload is the recovery source of truth.  This function never
    consults the database, roles, or current target configuration beyond the
    durable target map binding.
    """

    if not isinstance(pending, Mapping):
        raise _error(error_type, "PRICE_PENDING_STATE_INVALID")
    if dict(pending.get("target_map") or {}) != dict(target_map or {}):
        raise _error(error_type, "PRICE_PENDING_TARGET_BINDING_CHANGED")
    components = pending.get("components")
    if not isinstance(components, list):
        raise _error(error_type, "PRICE_PENDING_COMPONENTS_INVALID")
    attachments = pending.get("attachments")
    if not isinstance(attachments, dict):
        raise _error(error_type, "PRICE_PENDING_ATTACHMENTS_INVALID")
    profile = Path(profile).resolve()
    for component in components:
        if not isinstance(component, Mapping):
            raise _error(error_type, "PRICE_PENDING_COMPONENTS_INVALID")
        if component.get("kind") != "file":
            continue
        path = Path(str(component.get("path") or ""))
        try:
            inside = path.resolve().is_relative_to(profile)
        except (OSError, RuntimeError):
            inside = False
        if path.is_symlink() or not path.is_file() or not inside:
            raise _error(error_type, "PRICE_PENDING_ATTACHMENT_MISSING")
        expected = attachments.get(str(path))
        if expected != sha256(path):
            raise _error(error_type, "PRICE_PENDING_ATTACHMENT_CHANGED")
    return [dict(component) for component in components]


def record_batch_binding(
    progress: Any,
    batch_id: str,
    content_seal: str,
    *,
    side: str,
    error_type: type[Exception] = PriceWorkflowError,
) -> None:
    """Persist one immutable batch-to-content binding in Progress."""

    if side not in ("sales", "purchase"):
        raise _error(error_type, "PRICE_SIDE_INVALID")
    bindings = progress.data.setdefault(f"{side}_batch_bindings", {})
    old = bindings.get(batch_id)
    if old is not None and old != content_seal:
        raise _error(error_type, "PRICE_BATCH_CONTENT_BINDING_CHANGED")
    if old is None:
        bindings[batch_id] = content_seal
        path = getattr(progress, "path", None)
        if path is not None:
            operations._atomic(path, progress.data)


def validate_existing_delivery_binding(
    progress: Any,
    components: list[dict[str, Any]],
    transport: Any,
    batch_id: str,
    content_seal: str,
    *,
    side: str,
    error_type: type[Exception] = PriceWorkflowError,
) -> None:
    """Verify already-attempted components still match the sealed batch."""

    bindings = progress.data.get(f"{side}_batch_bindings", {})
    attempted_any = any(progress.status(item["key"]) != "not_attempted" for item in components)
    if attempted_any and batch_id not in bindings:
        raise _error(error_type, "PRICE_BATCH_BINDING_MISSING")
    if batch_id in bindings and bindings[batch_id] != content_seal:
        raise _error(error_type, "PRICE_BATCH_CONTENT_BINDING_CHANGED")
    record_batch_binding(progress, batch_id, content_seal, side=side, error_type=error_type)
    if not components:
        return
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in components:
        groups[item.get("notification_key") or wf.digest([item["account"]])].append(item)
    manifests = progress.data.get("notification_manifests", {})
    for notification, items in groups.items():
        expected_fingerprints: dict[str, Any] = {}
        for item in items:
            if not hasattr(transport, "fingerprint"):
                raise _error(error_type, "PRICE_DELIVERY_FINGERPRINT_UNAVAILABLE")
            expected_fingerprints[item["key"]] = transport.fingerprint(item)
        attempted = [item for item in items if progress.status(item["key"]) != "not_attempted"]
        if not attempted:
            continue
        manifest = manifests.get(notification)
        if not isinstance(manifest, str):
            raise _error(error_type, "PRICE_DELIVERY_MANIFEST_MISSING")
        ordered = sorted(items, key=lambda value: value["kind"] != "text")
        expected_manifest = wf.digest([[item["key"], expected_fingerprints[item["key"]]] for item in ordered])
        if manifest != expected_manifest:
            raise _error(error_type, "PRICE_DELIVERY_MANIFEST_CHANGED")
        for item in attempted:
            recorded = progress.data.get("components", {}).get(item["key"], {}).get("fingerprint")
            if recorded != expected_fingerprints[item["key"]]:
                raise _error(error_type, "PRICE_DELIVERY_FINGERPRINT_CHANGED")


def assert_pending_normalized(
    components: list[dict[str, Any]],
    transport: Any,
    progress: Any,
    *,
    error_type: type[Exception] = PriceWorkflowError,
) -> None:
    if not hasattr(transport, "normalize"):
        return
    normalized = transport.normalize([dict(component) for component in components], progress)
    if json_safe(normalized) != json_safe(components):
        raise _error(error_type, "PRICE_PENDING_COMPONENTS_NORMALIZATION_CHANGED")


def preview_validate(
    profile: Path,
    components: list[dict[str, Any]],
    *,
    error_type: type[Exception] = PriceWorkflowError,
) -> dict[str, Any]:
    """Validate preview component paths without invoking a transport."""

    profile = Path(profile).resolve()
    for item in components:
        if not item.get("account") or item.get("kind") not in ("text", "file"):
            raise _error(error_type, "PRICE_COMPONENT_INVALID")
        if item["kind"] == "file":
            path = Path(str(item.get("path") or ""))
            try:
                inside = path.resolve().is_relative_to(profile)
            except (OSError, RuntimeError):
                inside = False
            if path.is_symlink() or not path.is_file() or not inside:
                raise _error(error_type, "PRICE_ATTACHMENT_PATH_INVALID")
    return {
        "verified": True,
        "status": "accepted",
        "mode": "preview",
        "component_keys": [item["key"] for item in components],
    }


def receipt_evidence(
    progress: Any,
    components: list[dict[str, Any]],
    batch_id: str,
    content_seal: str,
    *,
    error_type: type[Exception] = PriceWorkflowError,
    status: str = "provider_accepted",
) -> dict[str, Any]:
    """Build the receipt proof accepted by side-aware reference stores."""

    statuses = {item["key"]: progress.status(item["key"]) for item in components}
    if any(value != "provider_accepted" for value in statuses.values()):
        raise _error(error_type, "PRICE_DELIVERY_NOT_FULLY_ACCEPTED")
    fingerprints: dict[str, str] = {}
    for key in statuses:
        fingerprint = progress.data.get("components", {}).get(key, {}).get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise _error(error_type, "PRICE_DELIVERY_FINGERPRINT_MISSING")
        fingerprints[key] = fingerprint
    manifests = progress.data.get("notification_manifests", {})
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in components:
        groups[item.get("notification_key") or wf.digest([item["account"]])].append(item)
    for notification, items in groups.items():
        manifest = manifests.get(notification)
        if not isinstance(manifest, str):
            raise _error(error_type, "PRICE_DELIVERY_MANIFEST_MISSING")
        ordered = sorted(items, key=lambda value: value["kind"] != "text")
        expected = wf.digest([[item["key"], fingerprints[item["key"]]] for item in ordered])
        if manifest != expected:
            raise _error(error_type, "PRICE_DELIVERY_MANIFEST_CHANGED")
    return {
        "batch_id": batch_id,
        "content_seal": content_seal,
        "status": status,
        "component_keys": sorted(statuses),
        "accepted_fingerprints": fingerprints,
    }


def write_manifest(out: Path, value: Mapping[str, Any], *, filename: str = "price-manifest.json") -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    operations._atomic(out / filename, json_safe(value))


def recover_pending(
    profile: Path,
    binding: Mapping[str, Any],
    out: Path,
    progress: Any,
    transport: Any,
    store: Any,
    head: Mapping[str, Any],
    *,
    side: str,
    job: str,
    error_type: type[Exception] = PriceWorkflowError,
    save_manifest: Callable[[Path, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Recover one sealed pending batch for either price side.

    This is the one implementation of pending validation, partial delivery
    recovery, receipt proof construction, and reference commit used by both
    sales and purchase runners.
    """

    pending = head.get("pending")
    if not isinstance(pending, Mapping):
        raise error_type("PRICE_PENDING_STATE_INVALID")
    components = validate_pending(profile, pending, binding.get("target_map") or {}, error_type=error_type)
    base = {
        "status": "success",
        "execution_state": "pending_recovery_preview",
        "job": job,
        "batch_id": pending.get("batch_id"),
        "observed_at": pending.get("observed_at"),
        "source_rows": pending.get("observation", {}).get("source_rows") if isinstance(pending.get("observation"), Mapping) else None,
        "pending_recovery": True,
        "queried_source": False,
        "queried_roles": False,
        "head_advanced": False,
        "prepared_only": True,
        "components": len(components),
    }
    save = save_manifest or (lambda path, value: write_manifest(path, value))
    if not binding.get("send_enabled"):
        preview_validate(profile, components, error_type=error_type)
        save(Path(out), base)
        return base
    if binding.get("price_accept_enabled") is not True:
        raise error_type("PROFILE_PRICE_REFERENCE_WRITE_NOT_ENABLED")
    if transport is None:
        raise error_type("PRICE_OFFICIAL_TRANSPORT_REQUIRED")
    assert_pending_normalized(components, transport, progress, error_type=error_type)
    validate_existing_delivery_binding(
        progress,
        components,
        transport,
        str(pending["batch_id"]),
        str(pending["content_seal"]),
        side=side,
        error_type=error_type,
    )
    io.deliver_components(components, transport, progress, enabled=True, force=False)
    receipt = receipt_evidence(
        progress,
        components,
        str(pending["batch_id"]),
        str(pending["content_seal"]),
        status="provider_accepted" if components else "silent_no_notifications",
        error_type=error_type,
    )
    committed = store.commit(str(pending["batch_id"]), receipt)
    result = {
        **base,
        "execution_state": "pending_recovery_committed",
        "prepared_only": False,
        "delivery": "provider_accepted_not_human_read" if components else "silent_no_deliverable_change",
        "head_advanced": True,
        "commit": {"status": committed.get("status")},
    }
    save(Path(out), result)
    return result


def _reference_store(factory: Callable[..., Any], profile: Path, side: str) -> Any:
    """Call the explicit side-aware reference store factory."""

    return factory(profile, side=side)


def _field(value: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Prefer an explicitly supplied field, including an explicit empty value."""

    return value[key] if key in value else default


class PriceWorkflowEngine:
    """Run one side's local price workflow with injected business callbacks.

    ``reader`` and ``prepare`` own all side-specific source and content rules.
    The reader returns a mapping containing ``rows``, ``observed_at``,
    ``observation``, and optionally ``plan``/``document``.  The prepare callback
    receives that mapping and returns ``components``, ``attachments``,
    ``after_reference``, ``document`` and ``observation``.  This is intentionally
    a small protocol rather than a second sales runner.
    """

    def __init__(
        self,
        side: str,
        *,
        reference_factory: Callable[..., Any],
        reader: Callable[..., Mapping[str, Any]],
        prepare: Callable[..., Mapping[str, Any]],
        now: Callable[[], datetime],
        save_manifest: Callable[[Path, Mapping[str, Any]], None] | None = None,
        error_type: type[Exception] = PriceWorkflowError,
        job: str | None = None,
    ) -> None:
        if side not in ("sales", "purchase"):
            raise error_type("PRICE_SIDE_INVALID")
        self.side = side
        self.job = job or f"{side}_price"
        self.reference_factory = reference_factory
        self.reader = reader
        self.prepare = prepare
        self.now = now
        self.save_manifest = save_manifest or (lambda out, value: write_manifest(out, value))
        self.error_type = error_type

    def _raise(self, code: str) -> None:
        raise self.error_type(code)

    def _save(self, out: Path, value: Mapping[str, Any]) -> None:
        self.save_manifest(Path(out), value)

    def _pending_run(
        self,
        profile: Path,
        binding: Mapping[str, Any],
        out: Path,
        progress: Any,
        transport: Any,
        store: Any,
        head: Mapping[str, Any],
    ) -> dict[str, Any]:
        return recover_pending(
            profile,
            binding,
            out,
            progress,
            transport,
            store,
            head,
            side=self.side,
            job=self.job,
            error_type=self.error_type,
            save_manifest=self.save_manifest,
        )

    def run(
        self,
        profile: Path,
        binding: Mapping[str, Any],
        out: Path,
        week: str,
        month: str,
        progress: Any,
        snapshots: Any,
        transport: Any,
    ) -> dict[str, Any]:
        profile, out = Path(profile), Path(out)
        store = _reference_store(self.reference_factory, profile, self.side)
        head = store.load()
        if head is None and binding.get("send_enabled"):
            self._raise("PRICE_LOCAL_REFERENCE_REQUIRED_FOR_SEND")
        if head is not None and head.get("pending") is not None:
            return self._pending_run(profile, binding, out, progress, transport, store, head)
        if binding.get("send_enabled"):
            prior_states = {value.get("status") for value in progress.data.get("components", {}).values() if isinstance(value, dict)}
            if prior_states & {"failed", "unknown", "in_flight", "unverified_success", "not_delivered"}:
                self._raise("PRICE_UNBOUND_PRIOR_DELIVERY_REQUIRES_REVIEW")
        now = self.now()
        if head is not None and same_hour(head, now):
            result = {
                "status": "success",
                "execution_state": "same_hour_already_committed",
                "job": self.job,
                "observed_at": head.get("last_committed", {}).get("observed_at") or head.get("last_committed", {}).get("committed_at"),
                "queried_source": False,
                "queried_roles": False,
                "head_advanced": False,
                "delivery": "not_requested",
            }
            self._save(out, result)
            return result
        before = head.get("reference", {}).get("rows") if head is not None else None
        context = dict(self.reader(
            snapshots,
            binding.get("operation") or {},
            profile=profile,
            before=before,
            head=head,
            week=week,
            month=month,
            binding=binding,
        ))
        observed_at = context.get("observed_at")
        if not isinstance(observed_at, datetime):
            observed_at = parse_time(observed_at, error_type=self.error_type)
        observed_text = observed_at.isoformat(sep=" ")
        last_observed = last_observed_at(head)
        if last_observed is not None:
            if observed_at < last_observed:
                self._raise("PRICE_LOCAL_OBSERVATION_CLOCK_REGRESSION")
            if observed_at.strftime("%Y%m%d%H") == last_observed.strftime("%Y%m%d%H"):
                result = {
                    "status": "success",
                    "execution_state": "same_source_hour_already_committed",
                    "job": self.job,
                    "observed_at": observed_text,
                    "queried_source": True,
                    "queried_roles": bool(context.get("queried_roles")),
                    "head_advanced": False,
                    "delivery": "not_requested",
                    "prepared_only": True,
                }
                self._save(out, result)
                return result
        context["observed_at"] = observed_at
        if head is None:
            candidate = dict(self.prepare(
                profile,
                out,
                context,
                binding=binding,
                batch_id=None,
                initial=True,
            ))
            document = dict(candidate.get("document") or {})
            document["baseline_source"] = "profile_local_reference_initialization_candidate"
            document.setdefault("reference_source", "profile_local")
            document.setdefault("scope_notice", "本批尚无已审核的本地价格参考；仅生成静默初始化候选，不把首次观察当作变价提醒。")
            rows = candidate.get("after_reference")
            if rows is None:
                rows = []
            provenance = {
                "source": f"{self.side}_price_observation",
                "observed_at": observed_text,
                "snapshot_marker": (context.get("observation") or {}).get("snapshot_marker"),
                "history_unknown": True,
            }
            init = store.initialization_plan(json_safe(rows), provenance)
            result = {
                "status": "success",
                "execution_state": "dryrun_reference_initialization_required",
                "prepared_only": True,
                "job": self.job,
                "observed_at": observed_text,
                "source_rows": len(context.get("rows") or []),
                "current_records": json_safe(context.get("rows") or []),
                "candidate_document": json_safe(document),
                "event_counts": document.get("event_counts", {}),
                "candidate_reference_digest": init.get("rows_digest"),
                "candidate_reference_rows": len(init.get("rows") or []),
                "queried_source": True,
                "queried_roles": False,
                "head_advanced": False,
                "delivery": "not_requested",
                "reference_initialized": False,
            }
            self._save(out, result)
            return result
        prepared = dict(self.prepare(profile, out, context, binding=binding, batch_id=None, initial=False))
        document = dict(_field(prepared, "document", _field(context, "document", {})) or {})
        changes = list(_field(prepared, "changes", _field(context, "changes", [])) or [])
        after_reference = json_safe(_field(prepared, "after_reference", _field(context, "after_reference", [])))
        observation = json_safe(_field(prepared, "observation", _field(context, "observation", {})) or {})
        observation.setdefault("observed_at", observed_text)
        observation["document"] = json_safe(document)
        observation["before_records"] = json_safe(before or [])
        observation["current_records"] = json_safe(context.get("rows") or [])
        observation["after_reference"] = after_reference
        observation["event_counts"] = document.get("event_counts", {})
        observation["deliverable_events"] = len(changes)
        components = [dict(item) for item in prepared.get("components") or []]
        attachments = dict(prepared.get("attachments") or {})
        if not changes:
            if not binding.get("send_enabled"):
                result = {
                    "status": "success",
                    "execution_state": "preview_no_change",
                    "prepared_only": True,
                    "job": self.job,
                    "observed_at": observed_text,
                    "source_rows": len(context.get("rows") or []),
                    "event_counts": document.get("event_counts", {}),
                    "document": json_safe(document),
                    "head_advanced": False,
                    "delivery": "not_requested",
                }
                self._save(out, result)
                return result
            if binding.get("price_accept_enabled") is not True:
                self._raise("PROFILE_PRICE_REFERENCE_WRITE_NOT_ENABLED")
            batch_prefix = "sp" if self.side == "sales" else "pp"
            batch_id = batch_prefix + "-" + digest([head["reference"]["digest"], observed_text, after_reference])[:40]
            payload = {
                "batch_id": batch_id,
                "observed_at": observed_text,
                "observation": observation,
                "components": [],
                "target_map": dict(binding.get("target_map") or {}),
                "attachments": {},
                "after_reference": after_reference,
            }
            store.prepare(payload, head["reference"]["digest"])
            pending = (store.load() or {}).get("pending") or {}
            receipt = receipt_evidence(progress, [], batch_id, str(pending.get("content_seal") or ""), status="silent_no_notifications", error_type=self.error_type)
            committed = store.commit(batch_id, receipt)
            result = {
                "status": "success",
                "execution_state": "committed_no_deliverable_change",
                "job": self.job,
                "observed_at": observed_text,
                "source_rows": len(context.get("rows") or []),
                "event_counts": document.get("event_counts", {}),
                "document": json_safe(document),
                "head_advanced": True,
                "delivery": "silent_no_deliverable_change",
                "commit": {"status": committed.get("status")},
            }
            self._save(out, result)
            return result
        if not components:
            self._raise("PRICE_REQUIRED_RECIPIENT_PLAN_EMPTY")
        batch_prefix = "sp" if self.side == "sales" else "pp"
        batch_id = batch_prefix + "-" + digest([head["reference"]["digest"], observed_text, after_reference, [(item.get("key"), item.get("text"), item.get("path")) for item in components]])[:40]
        for item in components:
            old_key = item.get("key")
            old_notification_key = item.get("notification_key")
            item["key"] = wf.digest([batch_id, old_key])
            item["notification_key"] = wf.digest([batch_id, old_notification_key])
        if binding.get("send_enabled"):
            if binding.get("price_accept_enabled") is not True:
                self._raise("PROFILE_PRICE_REFERENCE_WRITE_NOT_ENABLED")
            if transport is None:
                self._raise("PRICE_OFFICIAL_TRANSPORT_REQUIRED")
            components = io.preflight_components(components, transport, progress, force=False, check_receipts=True)
        else:
            preview_validate(profile, components, error_type=self.error_type)
        prepared_summary = _field(prepared, "summary", {})
        observation["prepared"] = json_safe(prepared_summary if prepared_summary is not None else {})
        payload = {
            "batch_id": batch_id,
            "observed_at": observed_text,
            "observation": observation,
            "components": json_safe(components),
            "target_map": json_safe(binding.get("target_map") or {}),
            "attachments": attachments,
            "after_reference": after_reference,
        }
        if not binding.get("send_enabled"):
            result = {
                "status": "success",
                "execution_state": "prepared_only",
                "prepared_only": True,
                "job": self.job,
                "batch_id": batch_id,
                "observed_at": observed_text,
                "source_rows": len(context.get("rows") or []),
                "event_counts": document.get("event_counts", {}),
                "document": json_safe(document),
                "components": len(components),
                "attachments": len(attachments),
                "head_advanced": False,
                "delivery": "not_requested",
                "summary": json_safe(prepared_summary if prepared_summary is not None else {}),
            }
            if isinstance(prepared_summary, Mapping) and "target_plan" in prepared_summary:
                result["target_plan"] = json_safe(prepared_summary["target_plan"])
            self._save(out, result)
            return result
        store.prepare(payload, head["reference"]["digest"])
        pending = (store.load() or {}).get("pending") or {}
        record_batch_binding(progress, batch_id, str(pending.get("content_seal") or ""), side=self.side, error_type=self.error_type)
        try:
            io.deliver_components(components, transport, progress, enabled=True, force=False)
            pending = (store.load() or {}).get("pending") or {}
            receipt = receipt_evidence(progress, components, batch_id, str(pending.get("content_seal") or ""), error_type=self.error_type)
            committed = store.commit(batch_id, receipt)
        except Exception:
            # Durable pending content intentionally survives all delivery or
            # commit failures; later runs recover these exact bytes.
            raise
        result = {
            "status": "success",
            "execution_state": "delivered_and_committed",
            "job": self.job,
            "batch_id": batch_id,
            "observed_at": observed_text,
            "source_rows": len(context.get("rows") or []),
            "event_counts": document.get("event_counts", {}),
            "components": len(components),
            "attachments": len(attachments),
            "head_advanced": True,
            "delivery": "provider_accepted_not_human_read",
            "summary": json_safe(prepared_summary if prepared_summary is not None else {}),
            "commit": {"status": committed.get("status")},
        }
        if isinstance(prepared_summary, Mapping) and "target_plan" in prepared_summary:
            result["target_plan"] = json_safe(prepared_summary["target_plan"])
        self._save(out, result)
        return result


PriceEngine = PriceWorkflowEngine
_json_safe = json_safe
_digest = digest
_sha256 = sha256


__all__ = [
    "PriceWorkflowError",
    "PriceEngine",
    "PriceWorkflowEngine",
    "assert_pending_normalized",
    "digest",
    "json_safe",
    "last_observed_at",
    "parse_time",
    "preview_validate",
    "receipt_evidence",
    "record_batch_binding",
    "recover_pending",
    "same_hour",
    "sha256",
    "validate_existing_delivery_binding",
    "validate_pending",
    "write_manifest",
]
