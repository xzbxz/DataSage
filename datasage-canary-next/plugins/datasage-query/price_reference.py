"""Shared local sales and purchase price reference storage.

The store keeps an explicitly reviewed local reference and sealed pending
content. It does not query a database, read current roles, send messages, or
interpret business changes.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import uuid
from decimal import InvalidOperation

from .legacy_price_bridge import SPECS as _PRICE_SPECS


SCHEMA = "datasage-sales-reference/v1"
_HEAD_SCHEMA = "datasage-sales-reference-head/v1"
_BATCH_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")

_SIDE_CONFIG = {
    "sales": {
        "directory": "sales_reference",
        "schema": "datasage-sales-reference/v1",
        "head_schema": "datasage-sales-reference-head/v1",
        "proof_schema": "datasage-sales-reference-proof/v1",
        "initialization_schema": "datasage-sales-reference-initialization/v1",
    },
    "purchase": {
        "directory": "purchase_reference",
        "schema": "datasage-purchase-reference/v1",
        "head_schema": "datasage-purchase-reference-head/v1",
        "proof_schema": "datasage-purchase-reference-proof/v1",
        "initialization_schema": "datasage-purchase-reference-initialization/v1",
    },
}


class PriceReferenceError(ValueError):
    pass


SalesReferenceError = PriceReferenceError


def _canonical(value: Any) -> str:
    from .legacy_workflow import canonical

    return canonical(value)


def _digest(value: Any) -> str:
    from .legacy_workflow import digest

    return digest(value)


def _json(value: Any, code: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PriceReferenceError(code) from exc


def _time(value: Any) -> str:
    try:
        if 'T' not in str(value) and ' ' not in str(value):raise ValueError()
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PriceReferenceError("SALES_REFERENCE_TIME_INVALID") from exc
    return str(value)


def _origin_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PriceReferenceError("SALES_REFERENCE_PROVENANCE_INVALID")
    origin = value.get("initial") if isinstance(value.get("initial"), dict) else value
    if not isinstance(origin, dict) or not isinstance(origin.get("source"), str) or not origin["source"].strip():
        raise PriceReferenceError("SALES_REFERENCE_PROVENANCE_SOURCE_INVALID")
    if "observed_at" not in origin:
        raise PriceReferenceError("SALES_REFERENCE_PROVENANCE_OBSERVED_AT_MISSING")
    if origin.get('history_unknown',True) is not True:
        raise PriceReferenceError('SALES_REFERENCE_HISTORICAL_DELIVERY_NOT_PROVEN')
    _time(origin["observed_at"])
    _json(value, "SALES_REFERENCE_PROVENANCE_INVALID")
    return origin


def _side_config(side: str) -> dict[str, str]:
    if not isinstance(side, str) or side not in _SIDE_CONFIG:
        raise PriceReferenceError("SALES_REFERENCE_SIDE_INVALID")
    return _SIDE_CONFIG[side]


def _legacy_spec(side: str) -> Mapping[str, Any]:
    _side_config(side)
    return _PRICE_SPECS[side]


def _validate_decimal_string(value: str) -> None:
    try:
        if not Decimal(value).is_finite():
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise PriceReferenceError("SALES_REFERENCE_DECIMAL_STRING_INVALID") from None


def _validate_reference_rows(rows: Any, observed_at: str, side: str = "sales") -> None:
    if not isinstance(rows, list):
        raise PriceReferenceError("SALES_REFERENCE_REFERENCE_ROWS_INVALID")
    spec = _legacy_spec(side)
    for row in rows:
        if not isinstance(row, dict):
            raise PriceReferenceError("SALES_REFERENCE_REFERENCE_ROW_INVALID")
        for field in spec["prices"]:
            value = row.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise PriceReferenceError("SALES_REFERENCE_DECIMAL_STRING_REQUIRED")
                _validate_decimal_string(value)
    try:
        from .legacy_price_bridge import reference
        at = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if at.tzinfo is not None:
            at = at.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        # The legacy bridge compares Python datetimes directly. Validate a
        # normalized copy so old heads with explicit offsets remain readable;
        # the stored rows and their exact decimal strings are untouched.
        normalized_rows = []
        for row in rows:
            normalized = dict(row)
            stamp = datetime.fromisoformat(str(row["snapshot_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
                normalized["snapshot_at"] = stamp.isoformat()
            normalized_rows.append(normalized)
        reference(side, normalized_rows, at)
    except PriceReferenceError:
        raise
    except Exception as exc:
        raise PriceReferenceError("SALES_REFERENCE_REFERENCE_VALIDATION_FAILED") from exc

def _batch_id(value: Any) -> str:
    if not isinstance(value, str) or not _BATCH_RE.fullmatch(value):
        raise PriceReferenceError("SALES_REFERENCE_BATCH_ID_INVALID")
    return value


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PriceReferenceError(code)
    return value


def _component_keys(components: list[Any]) -> list[str]:
    keys = []
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("key"), str) or not component["key"].strip():
            raise PriceReferenceError("SALES_REFERENCE_COMPONENT_KEY_INVALID")
        keys.append(component["key"])
    if len(keys) != len(set(keys)):
        raise PriceReferenceError("SALES_REFERENCE_COMPONENT_KEY_DUPLICATE")
    return keys


def _validate_decimal_paths(value: Any) -> None:
    decimal_keys = {"ddp_price", "tax_inclue_price", "tax_exclue_price", "old_ddp_price", "new_ddp_price", "old_inc", "new_inc", "old_exc", "new_exc"}
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in decimal_keys:
                if child is not None and not isinstance(child, str):
                    raise PriceReferenceError("SALES_REFERENCE_DECIMAL_STRING_REQUIRED")
                if isinstance(child, str):
                    try:
                        if not __import__("decimal").Decimal(child).is_finite():
                            raise ValueError()
                    except (ValueError, __import__("decimal").InvalidOperation):
                        raise PriceReferenceError("SALES_REFERENCE_DECIMAL_STRING_INVALID") from None
            _validate_decimal_paths(child)
    elif isinstance(value, list):
        for child in value:
            _validate_decimal_paths(child)


def _seal(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key != "head_seal"})


def _head_root(profile: Path, side: str = "sales") -> Path:
    from .workflow_io import private_root

    root = private_root(profile) / _side_config(side)["directory"]
    if root.is_symlink() or not root.resolve().is_relative_to(profile.resolve()):
        raise PriceReferenceError("SALES_REFERENCE_ROOT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    return root


class PriceReferenceStore:
    def __init__(self, profile: Path, mode: str = "local", *, side: str = "sales"):
        if mode != "local":
            raise PriceReferenceError("SALES_REFERENCE_MODE_INVALID")
        self._config = _side_config(side)
        self.side = side
        self.profile = Path(profile)
        self.root = _head_root(self.profile, side)
        self.path = self.root / "head.json"
        self.proof_root = self.root / "batches"

    def _proof_path(self, batch_id: str) -> Path:
        _batch_id(batch_id)
        if self.proof_root.is_symlink() or (hasattr(self.proof_root,'is_junction') and self.proof_root.is_junction()) or not self.proof_root.resolve().is_relative_to(self.root.resolve()):
            raise PriceReferenceError("SALES_REFERENCE_PROOF_PATH_INVALID")
        self.proof_root.mkdir(parents=True, exist_ok=True)
        path = self.proof_root / (batch_id + ".json")
        if path.is_symlink() or not path.resolve().is_relative_to(self.proof_root.resolve()):
            raise PriceReferenceError("SALES_REFERENCE_PROOF_PATH_INVALID")
        return path

    def _proof_load(self, batch_id: str) -> dict[str, Any]:
        path = self._proof_path(batch_id)
        if not path.exists() or path.is_symlink():
            raise PriceReferenceError("SALES_REFERENCE_PROOF_MISSING")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PriceReferenceError("SALES_REFERENCE_PROOF_INVALID") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "batch_id", "pending", "receipt_evidence", "proof_seal"} or value.get("schema") != self._config["proof_schema"] or value.get("batch_id") != batch_id:
            raise PriceReferenceError("SALES_REFERENCE_PROOF_INVALID")
        if value["proof_seal"] != _digest({key: value[key] for key in ("schema", "batch_id", "pending", "receipt_evidence")}):
            raise PriceReferenceError("SALES_REFERENCE_PROOF_SEAL_MISMATCH")
        self._validate_pending(value["pending"], value["pending"]["before_digest"])
        _json(value["receipt_evidence"], "SALES_REFERENCE_RECEIPT_INVALID")
        return value

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise PriceReferenceError("SALES_REFERENCE_HEAD_INVALID")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PriceReferenceError("SALES_REFERENCE_HEAD_INVALID") from exc
        self._validate_head(value)
        return value

    def _validate_head(self, value: Any) -> None:
        required = {"schema", "mode", "reference", "pending", "last_committed", "history_unknown", "head_seal"}
        if not isinstance(value, dict) or set(value) != required or value.get("schema") != self._config["head_schema"] or value.get("mode") != "local":
            raise PriceReferenceError("SALES_REFERENCE_HEAD_SHAPE_INVALID")
        if value.get("history_unknown") is not True:
            raise PriceReferenceError("SALES_REFERENCE_HISTORY_UNKNOWN_INVALID")
        if value.get("head_seal") != _seal(value):
            raise PriceReferenceError("SALES_REFERENCE_HEAD_SEAL_MISMATCH")
        reference = value.get("reference")
        if not isinstance(reference, dict) or set(reference) != {"rows", "digest", "provenance", "initialized_at", "initialized_at_kind"}:
            raise PriceReferenceError("SALES_REFERENCE_REFERENCE_INVALID")
        if not isinstance(reference["rows"], list):
            raise PriceReferenceError("SALES_REFERENCE_REFERENCE_ROWS_INVALID")
        if _digest(reference["rows"]) != _hex(reference["digest"], "SALES_REFERENCE_REFERENCE_DIGEST_INVALID"):
            raise PriceReferenceError("SALES_REFERENCE_REFERENCE_DIGEST_MISMATCH")
        _time(reference["initialized_at"])
        if reference["initialized_at_kind"] != "source_observed_at":
            raise PriceReferenceError("SALES_REFERENCE_INITIALIZED_AT_SEMANTICS_INVALID")
        _origin_provenance(reference["provenance"])
        if value.get("pending") is not None:
            self._validate_pending(value["pending"], reference["digest"])
        if value.get("last_committed") is not None:
            committed = value["last_committed"]
            if not isinstance(committed, dict) or set(committed) != {"batch_id", "before_digest", "after_digest", "observed_at", "receipt_evidence", "committed_at", "proof_path", "proof_digest"}:
                raise PriceReferenceError("SALES_REFERENCE_COMMITTED_INVALID")
            _batch_id(committed["batch_id"]); _hex(committed["before_digest"], "SALES_REFERENCE_BEFORE_DIGEST_INVALID"); _hex(committed["after_digest"], "SALES_REFERENCE_AFTER_DIGEST_INVALID"); _time(committed["observed_at"]); _time(committed["committed_at"]); _hex(committed["proof_digest"], "SALES_REFERENCE_PROOF_DIGEST_INVALID"); _json(committed["receipt_evidence"], "SALES_REFERENCE_RECEIPT_INVALID")
            if committed["after_digest"] != reference["digest"]:
                raise PriceReferenceError("SALES_REFERENCE_COMMITTED_REFERENCE_MISMATCH")
            if committed['proof_path'] != 'batches/'+committed['batch_id']+'.json':
                raise PriceReferenceError('SALES_REFERENCE_COMMITTED_PROOF_PATH_MISMATCH')
            proof = self._proof_load(committed["batch_id"])
            if _digest(proof) != committed["proof_digest"] or proof.get("receipt_evidence") != committed["receipt_evidence"]:
                raise PriceReferenceError("SALES_REFERENCE_COMMITTED_PROOF_MISMATCH")
            if _digest(proof['pending']['after_reference'])!=reference['digest'] or proof['pending']['before_digest']!=committed['before_digest'] or proof['pending']['observed_at']!=committed['observed_at']:
                raise PriceReferenceError('SALES_REFERENCE_COMMITTED_PLAN_MISMATCH')

    def _validate_pending(self, pending: Any, reference_digest: str) -> None:
        required = {"batch_id", "before_digest", "content_seal", "observed_at", "observation", "components", "target_map", "attachments", "after_reference"}
        if not isinstance(pending, dict) or set(pending) != required:
            raise PriceReferenceError("SALES_REFERENCE_PENDING_SHAPE_INVALID")
        _batch_id(pending["batch_id"]); _hex(pending["before_digest"], "SALES_REFERENCE_BEFORE_DIGEST_INVALID"); _hex(pending["content_seal"], "SALES_REFERENCE_CONTENT_SEAL_INVALID"); _time(pending["observed_at"])
        if pending["before_digest"] != reference_digest:
            raise PriceReferenceError("SALES_REFERENCE_PENDING_CAS_MISMATCH")
        if not isinstance(pending["components"], list) or not isinstance(pending["attachments"], dict) or not isinstance(pending["target_map"], dict) or not isinstance(pending["after_reference"], list):
            raise PriceReferenceError("SALES_REFERENCE_PENDING_FIELDS_INVALID")
        _component_keys(pending["components"])
        _json({key: pending[key] for key in ("observation", "components", "target_map", "attachments", "after_reference")}, "SALES_REFERENCE_PENDING_NOT_JSON_SERIALIZABLE")
        _validate_reference_rows(pending["after_reference"], pending["observed_at"], self.side)
        expected = _digest({key: pending[key] for key in ("batch_id", "before_digest", "observed_at", "observation", "components", "target_map", "attachments", "after_reference")})
        if expected != pending["content_seal"]:
            raise PriceReferenceError("SALES_REFERENCE_CONTENT_SEAL_MISMATCH")

    def initialization_plan(self, rows: list[Mapping[str, Any]], provenance: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(rows, list) or not isinstance(provenance, Mapping):
            raise PriceReferenceError("SALES_REFERENCE_INITIALIZATION_INPUT_INVALID")
        _json(rows, "SALES_REFERENCE_ROWS_NOT_JSON_SERIALIZABLE"); _json(provenance, "SALES_REFERENCE_PROVENANCE_INVALID")
        origin=_origin_provenance(dict(provenance));_validate_reference_rows(rows,origin['observed_at'],self.side)
        return {"schema": self._config["initialization_schema"], "mode": "local", "rows": rows, "provenance": dict(provenance), "rows_digest": _digest(rows), "review_required": True}

    def dryrun_initialize(self, reviewed_plan: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_initialization_plan(reviewed_plan, require_reason=False)
        return {"status": "dryrun", "would_write": False, "rows_digest": reviewed_plan["rows_digest"], "provenance": reviewed_plan["provenance"]}

    def _validate_initialization_plan(self, reviewed_plan: Mapping[str, Any], *, require_reason: bool) -> None:
        required = {"schema", "mode", "rows", "provenance", "rows_digest", "review_required"} | ({"approval_reason"} if require_reason else set())
        allowed = required | ({"approval_reason"} if not require_reason else set())
        if not isinstance(reviewed_plan, Mapping) or set(reviewed_plan) - allowed or not required.issubset(set(reviewed_plan)) or reviewed_plan.get("schema") != self._config["initialization_schema"] or reviewed_plan.get("mode") != "local" or reviewed_plan.get("review_required") is not True:
            raise PriceReferenceError("SALES_REFERENCE_INITIALIZATION_REVIEW_REQUIRED")
        if not isinstance(reviewed_plan["rows"], list) or reviewed_plan["rows_digest"] != _digest(reviewed_plan["rows"]):
            raise PriceReferenceError("SALES_REFERENCE_INITIALIZATION_ROWS_CHANGED")
        if require_reason and (not isinstance(reviewed_plan.get("approval_reason"), str) or not 5 <= len(reviewed_plan["approval_reason"].strip()) <= 120):
            raise PriceReferenceError("SALES_REFERENCE_INITIALIZATION_REASON_REQUIRED")
        _json(reviewed_plan["provenance"], "SALES_REFERENCE_PROVENANCE_INVALID")
        origin=_origin_provenance(reviewed_plan['provenance']);_validate_reference_rows(reviewed_plan['rows'],origin['observed_at'],self.side)

    def initialize(self, reviewed_plan: Mapping[str, Any]) -> dict[str, Any]:
        if self.load() is not None:
            raise PriceReferenceError("SALES_REFERENCE_ALREADY_INITIALIZED")
        self._validate_initialization_plan(reviewed_plan, require_reason=True)
        origin = _origin_provenance(reviewed_plan["provenance"])
        _validate_reference_rows(reviewed_plan["rows"], origin["observed_at"], self.side)
        reference = {"rows": reviewed_plan["rows"], "digest": reviewed_plan["rows_digest"], "provenance": dict(origin), "initialized_at": origin["observed_at"], "initialized_at_kind": "source_observed_at"}
        _time(reference["initialized_at"])
        value = {"schema": self._config["head_schema"], "mode": "local", "reference": reference, "pending": None, "last_committed": None, "history_unknown": bool(reviewed_plan["provenance"].get("history_unknown", True))}
        value["head_seal"] = _seal(value)
        from . import operations
        operations._atomic(self.path, value)
        return self.load() or value

    def prepare(self, payload: Mapping[str, Any], before_digest: str) -> dict[str, Any]:
        head = self.load()
        if head is None:
            raise PriceReferenceError("SALES_REFERENCE_HEAD_REQUIRED")
        _hex(before_digest, "SALES_REFERENCE_BEFORE_DIGEST_INVALID")
        if before_digest != head["reference"]["digest"]:
            raise PriceReferenceError("SALES_REFERENCE_CAS_MISMATCH")
        if not isinstance(payload, Mapping):
            raise PriceReferenceError("SALES_REFERENCE_PAYLOAD_INVALID")
        required = {"batch_id", "observed_at", "observation", "components", "target_map", "attachments", "after_reference"}
        if set(payload) != required:
            raise PriceReferenceError("SALES_REFERENCE_PAYLOAD_SHAPE_INVALID")
        _batch_id(payload["batch_id"]); _time(payload["observed_at"])
        if not isinstance(payload["components"], list) or not isinstance(payload["attachments"], dict) or not isinstance(payload["target_map"], dict) or not isinstance(payload["after_reference"], list):
            raise PriceReferenceError("SALES_REFERENCE_PAYLOAD_FIELDS_INVALID")
        _json(payload, "SALES_REFERENCE_PAYLOAD_NOT_JSON_SERIALIZABLE")
        _component_keys(payload["components"])
        _validate_decimal_paths(payload["observation"])
        _validate_reference_rows(payload['after_reference'],payload['observed_at'],self.side)
        pending_payload = {"batch_id": payload["batch_id"], "before_digest": before_digest, "observed_at": payload["observed_at"], "observation": payload["observation"], "components": payload["components"], "target_map": payload["target_map"], "attachments": payload["attachments"], "after_reference": payload["after_reference"]}
        pending = {**pending_payload, "content_seal": _digest(pending_payload)}
        if head["pending"] is not None:
            existing = head["pending"]
            if existing.get("batch_id") == payload.get("batch_id"):
                if existing.get("content_seal") != pending["content_seal"]:
                    raise PriceReferenceError("SALES_REFERENCE_PENDING_CONTENT_CHANGED")
                return {"status": "reused", "head": head}
            raise PriceReferenceError("SALES_REFERENCE_PENDING_EXISTS")
        proof_path = self._proof_path(payload["batch_id"])
        if proof_path.exists():
            proof = self._proof_load(payload["batch_id"])
            if proof["pending"].get("content_seal") != pending["content_seal"]:
                raise PriceReferenceError("SALES_REFERENCE_PENDING_CONTENT_CHANGED")
            if (head.get("last_committed") or {}).get("batch_id") == payload["batch_id"]:
                raise PriceReferenceError("SALES_REFERENCE_BATCH_ALREADY_COMMITTED")
        else:
            proof = {"schema": self._config["proof_schema"], "batch_id": payload["batch_id"], "pending": pending, "receipt_evidence": None}
            proof["proof_seal"] = _digest({key: proof[key] for key in ("schema", "batch_id", "pending", "receipt_evidence")})
            from . import operations
            operations._atomic(proof_path, proof)
        value = dict(head); value["pending"] = pending; value["head_seal"] = _seal(value)
        from . import operations
        operations._atomic(self.path, value)
        return {"status": "prepared", "head": self.load() or value}

    def commit(self, batch_id: str, receipt_evidence: Mapping[str, Any]) -> dict[str, Any]:
        head = self.load()
        if head is None:
            raise PriceReferenceError("SALES_REFERENCE_HEAD_REQUIRED")
        pending = head.get("pending")
        if not isinstance(pending, dict) or pending.get("batch_id") != batch_id:
            if (head.get("last_committed") or {}).get("batch_id") == batch_id:
                if head["last_committed"].get("receipt_evidence") == receipt_evidence:
                    return {"status": "reused", "head": head}
            raise PriceReferenceError("SALES_REFERENCE_PENDING_BATCH_MISMATCH")
        _json(receipt_evidence, "SALES_REFERENCE_RECEIPT_INVALID")
        receipt_required = {"batch_id", "content_seal", "status", "component_keys", "accepted_fingerprints"}
        if not isinstance(receipt_evidence, dict) or set(receipt_evidence) != receipt_required:
            raise PriceReferenceError("SALES_REFERENCE_RECEIPT_INVALID")
        expected_keys = _component_keys(pending["components"])
        actual_keys = receipt_evidence["component_keys"]
        if not isinstance(actual_keys, list) or len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != set(expected_keys):
            raise PriceReferenceError("SALES_REFERENCE_RECEIPT_COMPONENTS_MISMATCH")
        if receipt_evidence["batch_id"] != batch_id or receipt_evidence["content_seal"] != pending["content_seal"]:
            raise PriceReferenceError("SALES_REFERENCE_RECEIPT_BINDING_MISMATCH")
        if not isinstance(receipt_evidence["accepted_fingerprints"], dict) or set(receipt_evidence["accepted_fingerprints"]) != set(expected_keys) or any(not isinstance(v, str) or not v for v in receipt_evidence["accepted_fingerprints"].values()):
            raise PriceReferenceError("SALES_REFERENCE_RECEIPT_FINGERPRINTS_INVALID")
        if len(expected_keys) == 0:
            if receipt_evidence["status"] != "silent_no_notifications":
                raise PriceReferenceError("SALES_REFERENCE_SILENT_RECEIPT_INVALID")
        elif receipt_evidence["status"] not in ("provider_accepted", "accepted"):
            raise PriceReferenceError("SALES_REFERENCE_RECEIPT_NOT_ACCEPTED")
        if pending["before_digest"] != head["reference"]["digest"]:
            raise PriceReferenceError("SALES_REFERENCE_COMMIT_CAS_MISMATCH")
        proof = self._proof_load(batch_id)
        if proof["pending"].get("content_seal") != pending["content_seal"]:
            raise PriceReferenceError("SALES_REFERENCE_PROOF_PENDING_MISMATCH")
        if proof['receipt_evidence'] is not None and proof['receipt_evidence'] != receipt_evidence:
            raise PriceReferenceError('SALES_REFERENCE_PROOF_RECEIPT_CHANGED')
        after_digest = _digest(pending["after_reference"])
        origin = _origin_provenance(head["reference"]["provenance"])
        _validate_reference_rows(pending["after_reference"], pending["observed_at"], self.side)
        reference = {"rows": pending["after_reference"], "digest": after_digest, "provenance": {"initial": dict(origin), "last_commit": {"batch_id": batch_id, "previous_digest": pending["before_digest"]}}, "initialized_at": head["reference"]["initialized_at"], "initialized_at_kind": head["reference"]["initialized_at_kind"]}
        proof["receipt_evidence"] = dict(receipt_evidence)
        proof["proof_seal"] = _digest({key: proof[key] for key in ("schema", "batch_id", "pending", "receipt_evidence")})
        from . import operations
        operations._atomic(self._proof_path(batch_id), proof)
        proof_digest = _digest(proof)
        value = dict(head); value["reference"] = reference; value["pending"] = None; value["last_committed"] = {"batch_id": batch_id, "before_digest": pending["before_digest"], "after_digest": after_digest, "observed_at": pending["observed_at"], "receipt_evidence": dict(receipt_evidence), "committed_at": datetime.now(timezone.utc).isoformat(), "proof_path": self._proof_path(batch_id).relative_to(self.root).as_posix(), "proof_digest": proof_digest}; value["head_seal"] = _seal(value)
        operations._atomic(self.path, value)
        return {"status": "committed", "head": self.load() or value}


SalesReferenceStore = PriceReferenceStore

__all__ = ["PriceReferenceError", "SalesReferenceError", "PriceReferenceStore", "SalesReferenceStore"]


