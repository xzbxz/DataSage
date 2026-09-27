"""Pure Phase2 aggregate-detail pagination proposal.

This file intentionally lives beside the remediation notes rather than inside the
candidate plugin.  It has no candidate imports, database access, host access, secret
access, session state, or permission side effects.

The helper paginates a complete, already-authorized public aggregate collection.  A
cursor is a bounded continuation marker, not a permission credential.  A later read is
accepted only when its stable scope hash and complete public-result digest match the
cursor; equality means ``revalidated_current_observation`` and never an old snapshot.
Reconciliation is only a bounded public-collection subtotal; it is not an independent
source proof. Numeric subtotals use the displayed public decimal representation.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
import hashlib
import itertools
import json
import math
import re
from typing import Any


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 50
MAX_PAGES = 20
MAX_CURSOR_CHARS = 1024
MAX_ID_CHARS = 96
MAX_DIGEST_CHARS = 160

_DETAIL_CURSOR_VERSION = 1
_DETAIL_CURSOR_FIELDS = frozenset(
    {"v", "page", "page_size", "scope_digest", "data_digest"}
)
_DETAIL_REQUEST_FIELDS = frozenset({"id", "limit", "cursor"})
_VOLATILE_FIELDS = frozenset(
    {
        "read_at",
        "read_utc_at",
        # Exact inventory read-observation fields.  Do not generalize this to every
        # ``*_at`` field: baseline_frozen_at, snapshot_month and business event dates
        # remain part of the stable scope/public collection.
        "closing_read_at",
        "closing_utc_at",
        "monthly_read_at",
        "monthly_read_utc_at",
        "observed_clock_offset_seconds",
        "request_id",
        "request_ref",
        "purpose",
        "seal",
        "claim_seal",
        "disclosure_seal",
        "elapsed_ms",
        "read_latency_ms",
        "source_evidence_ref",
        "security_evidence",
        "cursor",
    }
)
_PUBLIC_VIEW_FIELDS = frozenset(
    {"dimensions", "facts", "states", "unit", "currency", "fact_units"}
)
_UNKNOWN_STATES = frozenset(
    {
        "unknown",
        "missing",
        "incomplete",
        "undefined",
        "unavailable",
        "not_set_for_future",
        "source_range_incomplete",
        "source_scope_unverifiable",
    }
)
_MATCHED_STATES = frozenset({"matched", "match", "included", "include", "true"})
_EXCLUDED_STATES = frozenset(
    {"excluded", "exclude", "not_matched", "not_match", "false"}
)
_NON_SUMMABLE_NAME = re.compile(
    r"(?:ratio|rate|share|percent|percentage|completion)", re.IGNORECASE
)
_SAFE_DIGEST = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_DIGEST_CHARS)
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,%d}$" % (MAX_ID_CHARS - 1))
NUMERIC_BASIS = "public_fact_decimal_representation"
_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

DETAIL_CONTRACT_VERSION = "registered-detail/v1"
DETAIL_IDS = (
    "inventory_product_groups",
    "receivable_customer_groups",
    "target_department_groups",
)
DETAIL_GRAINS = ("product_group", "customer_net_debt_group", "department_group")

# These are execution behaviors of the three fixed Phase2 grains, not a second
# registration store.  The metric-owned ``detail_contract`` supplies id, owner metric,
# required dimensions, public fields and summable_fields; only these safety semantics
# remain fixed here.
_DETAIL_BEHAVIORS = {
    "inventory_product_groups": {
        "requires_analysis_match": False,
        "currency_sensitive": False,
    },
    "receivable_customer_groups": {
        "requires_analysis_match": True,
        "currency_sensitive": True,
    },
    "target_department_groups": {
        "requires_analysis_match": True,
        "currency_sensitive": True,
    },
}

DETAIL_LIMIT_MIN = 1
DETAIL_LIMIT_DEFAULT = DEFAULT_PAGE_SIZE
DETAIL_LIMIT_MAX = MAX_PAGE_SIZE
DETAIL_CURSOR_MAX_LENGTH = MAX_CURSOR_CHARS


class DetailPagesError(ValueError):
    """Base error with a stable public code for a later adapter."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


class DetailValidationError(DetailPagesError):
    pass


class DetailCursorError(DetailPagesError):
    pass


class DetailReconciliationError(DetailPagesError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_safe_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_ID_CHARS:
        raise DetailValidationError("DETAIL_ID_INVALID", "detail id 无效。", path=path)
    if _SAFE_ID.fullmatch(value) is None:
        raise DetailValidationError("DETAIL_ID_INVALID", "detail id 无效。", path=path)
    return value


def _require_digest(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or _SAFE_DIGEST.fullmatch(value) is None:
        raise DetailCursorError("CURSOR_INVALID", "cursor digest 无效。", path=path)
    return value


def _canonical_value(value: Any) -> Any:
    """Convert supported public values to deterministic JSON-safe values."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DetailReconciliationError(
                "NONFINITE_VALUE", "公开事实包含非有限 Decimal。"
            )
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DetailReconciliationError(
                "NONFINITE_VALUE", "公开事实包含非有限浮点数。"
            )
        # Floating values are canonicalized for digesting; reconciliation uses
        # Decimal(str(value)) so the displayed public decimal representation is explicit.
        return repr(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DetailValidationError(
                    "PUBLIC_VIEW_INVALID", "公开 view 的对象键必须是字符串。"
                )
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise DetailValidationError(
        "PUBLIC_VIEW_INVALID", "公开 view 包含不支持的值类型。"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def public_collection_digest(views: Sequence[Mapping[str, Any]]) -> str:
    """Digest complete ordered public views, excluding volatile metadata."""

    if isinstance(views, (str, bytes, Mapping)):
        raise DetailValidationError("PUBLIC_VIEW_INVALID", "公开集合必须是有限 view 序列。")
    sanitized = [_sanitize_view(row, index=index) for index, row in enumerate(views)]
    return _digest(
        "data_v1_", {"version": "public-detail-collection/v1", "rows": sanitized}
    )


def encode_cursor(
    *,
    page: int,
    page_size: int,
    scope_digest: str,
    data_digest: str,
) -> str:
    """Encode the deliberately non-secret five-field cursor payload."""

    if not _is_int(page) or page < 0 or page >= MAX_PAGES:
        raise DetailCursorError("CURSOR_INVALID", "cursor page 无效。", path="page")
    if not _is_int(page_size) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise DetailCursorError("CURSOR_INVALID", "cursor page_size 无效.", path="page_size")
    scope_digest = _require_digest(scope_digest, path="scope_digest")
    data_digest = _require_digest(data_digest, path="data_digest")
    payload = {
        "v": _DETAIL_CURSOR_VERSION,
        "page": page,
        "page_size": page_size,
        "scope_digest": scope_digest,
        "data_digest": data_digest,
    }
    encoded = base64.urlsafe_b64encode(
        _canonical_json(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    if len(encoded) > MAX_CURSOR_CHARS:
        raise DetailCursorError("CURSOR_INVALID", "cursor 超出长度上限。")
    return encoded


def decode_cursor(raw: Any) -> dict[str, Any]:
    """Decode and strictly validate a cursor without treating it as authorization."""

    if not isinstance(raw, str) or not raw or len(raw) > MAX_CURSOR_CHARS:
        raise DetailCursorError("CURSOR_INVALID", "cursor 格式无效。", path="cursor")
    if _CURSOR_PATTERN.fullmatch(raw) is None:
        raise DetailCursorError("CURSOR_INVALID", "cursor 编码无效。", path="cursor")
    padding = "=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DetailCursorError("CURSOR_INVALID", "cursor 内容无效.", path="cursor") from exc
    if not isinstance(payload, dict) or set(payload) != _DETAIL_CURSOR_FIELDS:
        raise DetailCursorError("CURSOR_INVALID", "cursor 字段不受支持。", path="cursor")
    if payload.get("v") != _DETAIL_CURSOR_VERSION:
        raise DetailCursorError("CURSOR_INVALID", "cursor 版本不受支持。", path="cursor.v")
    page = payload.get("page")
    page_size = payload.get("page_size")
    if not _is_int(page) or not 0 <= page < MAX_PAGES:
        raise DetailCursorError("CURSOR_INVALID", "cursor page 无效。", path="cursor.page")
    if not _is_int(page_size) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise DetailCursorError(
            "CURSOR_INVALID", "cursor page_size 无效。", path="cursor.page_size"
        )
    scope_digest = _require_digest(payload.get("scope_digest"), path="cursor.scope_digest")
    data_digest = _require_digest(payload.get("data_digest"), path="cursor.data_digest")
    canonical = encode_cursor(
        page=page,
        page_size=page_size,
        scope_digest=scope_digest,
        data_digest=data_digest,
    )
    if canonical != raw.rstrip("="):
        raise DetailCursorError("CURSOR_INVALID", "cursor 不是规范编码。", path="cursor")
    return {
        "v": _DETAIL_CURSOR_VERSION,
        "page": page,
        "page_size": page_size,
        "scope_digest": scope_digest,
        "data_digest": data_digest,
    }


def normalize_detail(raw: Any) -> dict[str, Any] | None:
    """Validate and idempotently normalize the public detail object.

    Cursor decoding is deliberately deferred to ``paginate``.  The public
    validator must always return only ``id``, ``limit`` and optional ``cursor``.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise DetailValidationError("DETAIL_INVALID", "detail 必须是对象。", path="detail")
    supplied = set(raw)
    unexpected = supplied - _DETAIL_REQUEST_FIELDS
    if unexpected:
        field = sorted(unexpected)[0]
        raise DetailValidationError(
            "DETAIL_INVALID", "detail 包含不支持字段。", path=f"detail.{field}"
        )
    detail_id = _require_safe_id(raw.get("id"), path="detail.id")
    if detail_id not in DETAIL_IDS:
        raise DetailValidationError("DETAIL_ID_UNSUPPORTED", "detail id 未登记。", path="detail.id")
    page_size = raw.get("limit", DEFAULT_PAGE_SIZE)
    if not _is_int(page_size) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise DetailValidationError(
            "DETAIL_PAGE_SIZE_INVALID",
            f"detail.limit 必须是 1 到 {MAX_PAGE_SIZE} 的整数。",
            path="detail.limit",
        )
    cursor_present = "cursor" in raw
    cursor = raw.get("cursor")
    if cursor_present:
        if cursor is None:
            raise DetailValidationError("CURSOR_INVALID", "cursor 不能为 null。", path="detail.cursor")
        if (
            not isinstance(cursor, str)
            or not 1 <= len(cursor) <= MAX_CURSOR_CHARS
            or _CURSOR_PATTERN.fullmatch(cursor) is None
        ):
            raise DetailValidationError("CURSOR_INVALID", "cursor 编码无效。", path="detail.cursor")
        # Preflight format/version/canonical encoding before database work.  The decoded
        # payload is deliberately discarded; paginate decodes again to compare scope/data.
        decode_cursor(cursor)
    result = {"id": detail_id, "limit": page_size}
    if cursor_present:
        result["cursor"] = cursor
    return result


def validate_detail_contract(raw: Any) -> dict[str, Any]:
    """Validate the backend's metric-owned aggregate-detail registration."""

    if not isinstance(raw, Mapping):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail contract 无效。")
    detail_id = _require_safe_id(raw.get("id"), path="detail_contract.id")
    if detail_id not in DETAIL_IDS:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail contract id 未登记。", path="detail_contract.id")
    owner_metric = raw.get("owner_metric")
    if owner_metric is not None and (not isinstance(owner_metric, str) or not owner_metric.strip()):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail owner_metric 无效。", path="detail_contract.owner_metric")
    if raw.get("requires_analysis") is not True:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail requires_analysis 必须为 true。", path="detail_contract.requires_analysis")
    required_dimensions = raw.get("required_dimensions")
    if not isinstance(required_dimensions, (list, tuple)) or any(not isinstance(item, str) for item in required_dimensions) or len(required_dimensions) != len(set(required_dimensions)):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail required_dimensions 无效。", path="detail_contract.required_dimensions")
    summable_fields = raw.get("summable_fields")
    if not isinstance(summable_fields, (list, tuple)) or any(not isinstance(field, str) for field in summable_fields) or len(summable_fields) != len(set(summable_fields)):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail summable_fields 无效。", path="detail_contract.summable_fields")
    for key in ("allowed_analysis_fields", "public_fields", "allowed_dimensions"):
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, (list, tuple))
            or any(not isinstance(item, str) for item in value)
            or len(value) != len(set(value))
            or any(not item.strip() for item in value)
        ):
            raise DetailValidationError("DETAIL_CONTRACT_INVALID", f"detail {key} 无效。", path=f"detail_contract.{key}")
    default_page_size = raw.get("default_page_size", DEFAULT_PAGE_SIZE)
    max_page_size = raw.get("max_page_size", MAX_PAGE_SIZE)
    max_pages = raw.get("max_pages", MAX_PAGES)
    max_collection = raw.get("max_collection", 500)
    if not _is_int(default_page_size) or not 1 <= default_page_size <= MAX_PAGE_SIZE:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail default_page_size 无效。", path="detail_contract.default_page_size")
    if not _is_int(max_page_size) or not 1 <= max_page_size <= MAX_PAGE_SIZE or default_page_size > max_page_size:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail max_page_size 无效。", path="detail_contract.max_page_size")
    if not _is_int(max_pages) or not 1 <= max_pages <= MAX_PAGES:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail max_pages 无效。", path="detail_contract.max_pages")
    if not _is_int(max_collection) or not 1 <= max_collection <= 500:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail max_collection 无效。", path="detail_contract.max_collection")
    result = copy.deepcopy(dict(raw))
    result["id"] = detail_id
    result["required_dimensions"] = tuple(required_dimensions)
    result["summable_fields"] = tuple(summable_fields)
    result["default_page_size"] = default_page_size
    result["max_page_size"] = max_page_size
    result["max_pages"] = max_pages
    result["max_collection"] = max_collection
    return result


def project_detail_contract(raw: Any) -> dict[str, Any] | None:
    """Project safe registration metadata; private tie/sum keys stay internal."""

    if raw is None:
        return None
    contract = validate_detail_contract(raw)
    return {
        key: contract[key]
        for key in (
            "id",
            "owner_metric",
            "requires_analysis",
            "required_dimensions",
            "allowed_dimensions",
            "allowed_analysis_fields",
            "public_fields",
            "summable_fields",
            "default_page_size",
            "max_page_size",
            "max_pages",
            "max_collection",
        )
        if key in contract
    }


def validate_request_detail_shape(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate public detail dependency/conflicts before metric I/O."""

    if "detail" not in request:
        return dict(request)
    detail = normalize_detail(request.get("detail"))
    if detail is None:
        raise DetailValidationError("DETAIL_INVALID", "detail 必须是对象。", path="detail")
    analysis = request.get("analysis")
    if not isinstance(analysis, Mapping) or not any(
        analysis.get(key) for key in ("row_filters", "group_filters")
    ):
        raise DetailValidationError(
            "ANALYSIS_REQUIRED", "该 detail 至少需要一个 analysis 条件。", path="analysis"
        )
    conflicts = {
        key
        for key in (
            "limit",
            "comparison",
            "complete_change_decomposition",
            "complete_target_gap_decomposition",
            "decomposition_of_request_id",
            "_target_gap_of_request_id",
            "time_bucket",
        )
        if key in request
    }
    if conflicts:
        key = sorted(conflicts)[0]
        raise DetailValidationError(
            "DETAIL_REQUEST_CONFLICT", "detail 不能与该顶层字段并用。", path=key
        )
    result = dict(request)
    result["detail"] = detail
    return result


def validate_metric_detail(
    request: Mapping[str, Any],
    metric: Mapping[str, Any] | None,
    *,
    metric_code: str | None = None,
) -> dict[str, Any]:
    """Bind one normalized detail to its metric-owned contract.

    Upstream analysis validation owns filter syntax, operators, values and combinations.
    This helper only requires a non-empty analysis dependency and checks registered field
    membership and required-dimension set equality.
    """

    if not isinstance(request, Mapping):
        raise DetailValidationError("DETAIL_INVALID", "request 结构无效。")
    detail = normalize_detail(request.get("detail"))
    if detail is None:
        return {"detail": None, "contract": None}
    if not isinstance(metric, Mapping):
        raise DetailValidationError("DETAIL_UNSUPPORTED", "当前指标不支持 detail。", path="detail.id")
    raw_contract = metric.get("detail_contract")
    if raw_contract is None:
        raise DetailValidationError("DETAIL_UNSUPPORTED", "当前指标不支持 detail。", path="detail.id")
    contract = validate_detail_contract(raw_contract)
    contract_id = _require_safe_id(contract.get("id"), path="metric.detail_contract.id")
    if detail["id"] != contract_id:
        raise DetailValidationError("DETAIL_ID_UNSUPPORTED", "detail id 与 metric 不匹配。", path="detail.id")
    owner_metric = contract.get("owner_metric")
    if metric_code is None:
        metric_code = request.get("metric")
    if owner_metric is not None and metric_code is not None and owner_metric != metric_code:
        raise DetailValidationError("DETAIL_ID_UNSUPPORTED", "detail 不属于当前 metric。", path="detail.id")
    forbidden = {
        "limit",
        "comparison",
        "complete_change_decomposition",
        "complete_target_gap_decomposition",
        "decomposition_of_request_id",
        "_target_gap_of_request_id",
        "time_bucket",
    }
    present_forbidden = sorted(field for field in forbidden if field in request)
    if present_forbidden:
        field = present_forbidden[0]
        raise DetailValidationError(
            "DETAIL_REQUEST_CONFLICT", "detail 不接受该顶层字段。", path=field
        )
    analysis = request.get("analysis")
    if not isinstance(analysis, Mapping) or not any(
        analysis.get(key) for key in ("row_filters", "group_filters")
    ):
        raise DetailValidationError("DETAIL_ANALYSIS_REQUIRED", "detail 至少需要一个 analysis 条件。", path="analysis")
    required_dimensions = tuple(contract.get("required_dimensions") or ())
    dimensions = request.get("dimensions")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != len(set(dimensions))
        or set(dimensions) != set(required_dimensions)
    ):
        raise DetailValidationError(
            "DETAIL_DIMENSIONS_MISMATCH", "detail 需要精确的登记分组维度。", path="dimensions"
        )
    if detail["limit"] > int(contract.get("max_page_size", MAX_PAGE_SIZE)):
        raise DetailValidationError("DETAIL_PAGE_SIZE_INVALID", "detail page size 超出合同上限。", path="detail.limit")
    return {"detail": detail, "contract": copy.deepcopy(dict(contract))}


def _sanitize_view(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DetailValidationError("PUBLIC_VIEW_INVALID", "detail view 必须是对象。", path=f"views[{index}]")
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise DetailValidationError("PUBLIC_VIEW_INVALID", "detail view 键必须是字符串。", path=f"views[{index}]")
        if key in _VOLATILE_FIELDS:
            continue
        if key not in _PUBLIC_VIEW_FIELDS:
            raise DetailValidationError(
                "PUBLIC_VIEW_PRIVATE_FIELD",
                "detail view 含未登记公开字段。",
                path=f"views[{index}].{key}",
            )
        result[key] = copy.deepcopy(value)
    for key in ("dimensions", "facts", "states", "fact_units"):
        if key in result and not isinstance(result[key], (Mapping, list, tuple)):
            raise DetailValidationError(
                "PUBLIC_VIEW_INVALID", "detail view 的公开容器无效。", path=f"views[{index}].{key}"
            )
    # Known volatile names are forbidden even when nested in a public container.
    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                if child_key in _VOLATILE_FIELDS:
                    raise DetailValidationError(
                        "PUBLIC_VIEW_PRIVATE_FIELD", "detail view 含 volatile 字段。", path=path
                    )
                walk(child_value, f"{path}.{child_key}")
        elif isinstance(value, (list, tuple)):
            for child_index, child_value in enumerate(value):
                walk(child_value, f"{path}[{child_index}]")

    walk(result, f"views[{index}]")
    return result


def _bounded_views(views: Iterable[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    if isinstance(views, (str, bytes, Mapping)):
        raise DetailValidationError("PUBLIC_VIEW_INVALID", "views 必须是有限可迭代集合。")
    iterator = iter(views)
    raw_rows = list(itertools.islice(iterator, cap + 1))
    if len(raw_rows) > cap:
        raise DetailValidationError(
            "DETAIL_SCOPE_TOO_LARGE", "detail group 集合超过受控上限。"
        )
    return [_sanitize_view(row, index=index) for index, row in enumerate(raw_rows)]


def _scope_digest(scope_binding: Mapping[str, Any]) -> str:
    if not isinstance(scope_binding, Mapping):
        raise DetailValidationError("SCOPE_BINDING_INVALID", "root scope binding 无效。")
    value = scope_binding.get("resolved_scope_hash", scope_binding.get("scope_digest"))
    if value is None and isinstance(scope_binding.get("stable_context"), Mapping):
        stable = scope_binding["stable_context"]

        def check_stable(node: Any) -> None:
            if isinstance(node, Mapping):
                for key, child in node.items():
                    if key in _VOLATILE_FIELDS or key in {"page", "page_size", "cursor"}:
                        raise DetailValidationError(
                            "SCOPE_BINDING_INVALID",
                            "stable scope context 含 volatile/cursor 字段。",
                        )
                    check_stable(child)
            elif isinstance(node, (list, tuple)):
                for child in node:
                    check_stable(child)

        check_stable(stable)
        return _digest("scope_v1_", stable)
    return _require_digest(value, path="scope_binding.scope_digest")


def _mapping_value(view: Mapping[str, Any], container: str, field: str) -> Any:
    raw = view.get(container)
    if not isinstance(raw, Mapping):
        return None
    return raw.get(field)


def _field_state(view: Mapping[str, Any], field: str) -> str | None:
    state = _mapping_value(view, "states", field)
    if state is None:
        state = _mapping_value(view, "states", f"{field}_state")
    if isinstance(state, Mapping):
        state = state.get("state") or state.get("status")
    return str(state).casefold() if state is not None else None


def _exact_decimal(value: Any, *, field: str) -> Decimal:
    # JSON number/DOUBLE values are interpreted through their displayed decimal text.
    # This does not recover precision hidden in the source binary representation.
    if isinstance(value, bool):
        raise DetailReconciliationError(
            "INEXACT_NUMERIC", f"字段 {field} 不是可精确求和的 Decimal 输入。"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise DetailReconciliationError("NONFINITE_VALUE", f"字段 {field} 不是有限数值。")
    if not isinstance(value, (str, int, float, Decimal)):
        raise DetailReconciliationError(
            "INEXACT_NUMERIC", f"字段 {field} 不是可精确求和的 Decimal 输入。"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DetailReconciliationError(
            "NUMERIC_INVALID", f"字段 {field} 的数值无效。"
        ) from exc
    if not parsed.is_finite():
        raise DetailReconciliationError("NONFINITE_VALUE", f"字段 {field} 不是有限数值。")
    return parsed


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _sum_exact(values: Sequence[Decimal], *, field: str) -> Decimal:
    """Sum Decimal values with a precision sized for the complete exact result."""

    if not values:
        return Decimal(0)
    max_digits = max(len(value.as_tuple().digits) for value in values)
    max_adjusted = max(value.adjusted() for value in values)
    min_adjusted = min(value.adjusted() for value in values)
    precision = max(
        28,
        max_digits + (max_adjusted - min_adjusted) + len(str(len(values))) + 8,
    )
    if precision > 100_000:
        raise DetailReconciliationError(
            "INEXACT_NUMERIC", f"字段 {field} 的精确求和超出精度边界。"
        )
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            total = Decimal(0)
            for value in values:
                total += value
            return total
    except (Inexact, Rounded) as exc:
        raise DetailReconciliationError(
            "INEXACT_NUMERIC", f"字段 {field} 的求和发生不精确舍入。"
        ) from exc


def _unknown_field(view: Mapping[str, Any], field: str) -> bool:
    value = _mapping_value(view, "facts", field)
    if value is None:
        return True
    state = _field_state(view, field)
    return state in _UNKNOWN_STATES if state is not None else False


def _analysis_match(view: Mapping[str, Any]) -> str | None:
    value = _mapping_value(view, "states", "analysis_match_state")
    if value is None:
        value = _mapping_value(view, "states", "analysis_match")
    if value is None:
        value = _mapping_value(view, "facts", "analysis_match_state")
    if value is None:
        value = _mapping_value(view, "facts", "analysis_match")
    if isinstance(value, Mapping):
        value = value.get("state") or value.get("status") or value.get("value")
    normalized = str(value).casefold() if value is not None else ""
    if normalized in _MATCHED_STATES:
        return "matched"
    if normalized in _EXCLUDED_STATES:
        return "excluded"
    return None


def _reconcile(rows: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> dict[str, Any]:
    fields = tuple(contract.get("summable_fields") or ())
    behavior = _DETAIL_BEHAVIORS.get(str(contract.get("id")))
    if behavior is None:
        raise DetailReconciliationError("DETAIL_CONTRACT_INVALID", "detail 行为未登记。")
    if not fields:
        return {
            "status": "not_applicable",
            "full_subtotals": {},
            "unknown_fields": [],
            "numeric_basis": NUMERIC_BASIS,
        }
    for field in fields:
        if not isinstance(field, str) or _NON_SUMMABLE_NAME.search(field):
            raise DetailReconciliationError(
                "NON_SUMMABLE_FIELD", f"字段 {field!r} 不能作为聚合 subtotal。"
            )
    if not rows:
        return {
            "status": "empty",
            "full_subtotals": {field: None for field in fields},
            "known_matched_subtotals": {field: None for field in fields},
            "matched_subtotals": {field: None for field in fields},
            "unknown_fields": list(fields),
            "known_matched_member_count": {field: 0 for field in fields},
            "analysis_match_unknown_count": 0,
            "numeric_basis": NUMERIC_BASIS,
        }
    errors: list[str] = []
    invalid_fields: set[str] = set()
    for field in fields:
        prior_errors = len(errors)
        units = {
            str(_mapping_value(row, "fact_units", field) or (row.get("unit") if field == "metric_value" else ""))
            for row in rows
            if _mapping_value(row, "facts", field) is not None
        }
        if "" in units:
            errors.append(f"FACT_UNIT_UNKNOWN:{field}")
        if len(units) > 1:
            errors.append(f"CROSS_UNIT_RECONCILIATION:{field}")
        if behavior["currency_sensitive"] and any(
            "原币" in unit or "按币种" in unit for unit in units if unit
        ):
            currencies = {str(row.get("currency") or "") for row in rows}
            if "" in currencies:
                errors.append(f"CURRENCY_UNKNOWN:{field}")
            if len(currencies) > 1:
                errors.append(f"CROSS_CURRENCY_RECONCILIATION:{field}")
        if len(errors) != prior_errors:
            invalid_fields.add(field)
    full: dict[str, str | None] = {}
    unknown_fields: list[str] = []
    for field in fields:
        if field in invalid_fields or any(_unknown_field(row, field) for row in rows):
            full[field] = None
            unknown_fields.append(field)
            continue
        values = [
            _exact_decimal(_mapping_value(row, "facts", field), field=field)
            for row in rows
        ]
        full[field] = _format_decimal(_sum_exact(values, field=field))
    requires_match = bool(behavior["requires_analysis_match"])
    match_states = [_analysis_match(row) for row in rows] if requires_match else ["matched"] * len(rows)
    match_unknown = sum(state is None for state in match_states)
    matched: dict[str, str | None] = {}
    known_matched: dict[str, str | None] = {}
    known_member_count: dict[str, int] = {}
    for field in fields:
        if field in invalid_fields:
            known_member_count[field] = 0
            known_matched[field] = matched[field] = None
            continue
        matched_rows = [row for row, state in zip(rows, match_states) if state == "matched"]
        known_rows = [row for row in matched_rows if not _unknown_field(row, field)]
        known_member_count[field] = len(known_rows)
        if not known_rows:
            known_matched[field] = None
        else:
            known_matched[field] = _format_decimal(
                _sum_exact(
                    [_exact_decimal(_mapping_value(row, "facts", field), field=field) for row in known_rows],
                    field=field,
                )
            )
        field_unknown = len(known_rows) != len(matched_rows)
        matched[field] = None if match_unknown or field_unknown else known_matched[field]
    status = "unknown" if invalid_fields else "complete" if not unknown_fields and not match_unknown else "incomplete"
    result: dict[str, Any] = {
        "status": status,
        "full_subtotals": full,
        "known_matched_subtotals": known_matched,
        "matched_subtotals": matched,
        "known_matched_member_count": known_member_count,
        "analysis_match_unknown_count": match_unknown,
        "unknown_fields": sorted(set(unknown_fields)),
        "numeric_basis": NUMERIC_BASIS,
        **({"errors": sorted(set(errors))} if errors else {}),
    }
    return result


def _contract_limits(contract: Mapping[str, Any], collection_cap: int) -> tuple[int, int, tuple[str, ...]]:
    if not isinstance(contract, Mapping):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail contract 无效。")
    max_page_size = contract.get("max_page_size", MAX_PAGE_SIZE)
    max_pages = contract.get("max_pages", MAX_PAGES)
    max_collection = contract.get("max_collection", 500)
    if not _is_int(max_page_size) or not 1 <= max_page_size <= MAX_PAGE_SIZE:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail page cap 无效。")
    if not _is_int(max_pages) or not 1 <= max_pages <= MAX_PAGES:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail page count cap 无效。")
    if not _is_int(max_collection) or not 1 <= max_collection <= 500:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail collection cap 无效。")
    if not _is_int(collection_cap) or collection_cap < 1:
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "collection cap 无效。")
    effective_cap = min(collection_cap, max_collection, 500)
    fields = tuple(contract.get("summable_fields") or ())
    return effective_cap, max_page_size, fields


def _coerce_detail_for_pagination(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the public shape to private pagination state only at paginate."""

    normalized = normalize_detail(detail)
    if normalized is None:
        raise DetailValidationError("DETAIL_REQUIRED", "paginate 需要 detail。")
    cursor = normalized.get("cursor")
    payload = decode_cursor(cursor) if cursor is not None else None
    return {"id": normalized["id"], "requested_page_size": normalized["limit"], "cursor_payload": payload}


def paginate(
    views: Iterable[Mapping[str, Any]],
    detail: Mapping[str, Any],
    scope_binding: Mapping[str, Any],
    contract: Mapping[str, Any],
    collection_cap: int,
    *,
    collection_truncated: bool = False,
) -> dict[str, Any]:
    """Paginate a bounded complete public aggregate collection.

    ``views`` must already be produced from the same fully authorized metric scope.  The
    helper re-sanitizes its public shape, never turns unknown into zero, and returns full
    versus page reconciliation separately.
    """

    if not isinstance(detail, Mapping):
        raise DetailValidationError("DETAIL_REQUIRED", "paginate 需要 detail 对象。")
    normalized = _coerce_detail_for_pagination(detail)
    if not isinstance(contract, Mapping):
        raise DetailValidationError("DETAIL_CONTRACT_INVALID", "detail contract 无效。")
    contract_id = _require_safe_id(contract.get("id"), path="contract.id")
    if normalized["id"] != contract_id:
        raise DetailValidationError("DETAIL_ID_UNSUPPORTED", "detail 与 contract 不匹配。")
    effective_cap, max_page_size, _fields = _contract_limits(contract, collection_cap)
    if collection_truncated:
        raise DetailValidationError(
            "DETAIL_COLLECTION_TRUNCATED",
            "root 未提供完整 detail group 集合，不能生成 digest 或分页。",
        )
    requested_page_size = normalized["requested_page_size"]
    effective_page_size = min(requested_page_size, max_page_size, effective_cap)
    if effective_page_size < 1:
        raise DetailValidationError("DETAIL_PAGE_SIZE_INVALID", "detail page size 超出上限。")
    rows = _bounded_views(views, effective_cap)
    scope_digest = _scope_digest(scope_binding)
    data_digest = public_collection_digest(rows)
    payload = normalized.get("cursor_payload")
    if payload is not None:
        if payload["scope_digest"] != scope_digest:
            raise DetailCursorError("CURSOR_SCOPE_CHANGED", "cursor scope 已变化。")
        if payload["data_digest"] != data_digest:
            raise DetailCursorError("CURSOR_STALE", "公开聚合集合已变化。")
        if payload["page_size"] != effective_page_size:
            raise DetailCursorError("CURSOR_PAGE_SIZE_CHANGED", "cursor effective page size 已变化。")
        page_index = payload["page"]
        cursor_consistency = "revalidated_current_observation"
    else:
        page_index = 0
        cursor_consistency = "initial_current_observation"
    page_count = (len(rows) + effective_page_size - 1) // effective_page_size if rows else 0
    max_pages = min(int(contract.get("max_pages", MAX_PAGES)), MAX_PAGES)
    if page_count > max_pages:
        raise DetailValidationError(
            "DETAIL_PAGE_COUNT_TOO_LARGE", "detail 页数超过受控上限，请提高粒度或缩小范围。"
        )
    if not rows:
        if payload is not None:
            raise DetailCursorError("CURSOR_OUT_OF_RANGE", "空集合没有可继续的页。")
        page_index = 0
    elif page_index >= page_count:
        raise DetailCursorError("CURSOR_OUT_OF_RANGE", "cursor page 超出集合范围。")
    start = page_index * effective_page_size
    end = min(start + effective_page_size, len(rows))
    page_rows = rows[start:end]
    has_more = end < len(rows)
    next_cursor = (
        encode_cursor(
            page=page_index + 1,
            page_size=effective_page_size,
            scope_digest=scope_digest,
            data_digest=data_digest,
        )
        if has_more
        else None
    )
    return {
        "rows": page_rows,
        "page_index": page_index,
        "page_count": page_count,
        "start_index": start,
        "end_index_exclusive": end,
        "returned_count": len(page_rows),
        "full_count": len(rows),
        "requested_page_size": requested_page_size,
        "effective_page_size": effective_page_size,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "scope_digest": scope_digest,
        "data_digest": data_digest,
        "numeric_basis": NUMERIC_BASIS,
        "reconciliation_basis": "public_collection_only_not_independent_source_proof",
        "cursor_consistency": cursor_consistency,
        "snapshot_continuity": "not_guaranteed_old_snapshot",
        "reconciliation": {
            "full": _reconcile(rows, contract),
            "page": _reconcile(page_rows, contract),
        },
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_PAGES",
    "DETAIL_IDS",
    "DETAIL_LIMIT_MIN",
    "DETAIL_LIMIT_DEFAULT",
    "DETAIL_LIMIT_MAX",
    "DETAIL_CURSOR_MAX_LENGTH",
    "NUMERIC_BASIS",
    "DetailCursorError",
    "DetailPagesError",
    "DetailReconciliationError",
    "DetailValidationError",
    "decode_cursor",
    "encode_cursor",
    "normalize_detail",
    "validate_detail_contract",
    "project_detail_contract",
    "validate_request_detail_shape",
    "paginate",
    "public_collection_digest",
    "validate_metric_detail",
]
