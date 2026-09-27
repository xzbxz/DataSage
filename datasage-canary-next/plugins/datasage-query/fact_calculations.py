"""Governed selection of public fact fields for top-level calculations.

The calculation transport is intentionally owned by ``request_contract`` and
the calculation orchestration remains in ``tools``.  This module contains the
small semantic boundary between those layers: a calculation may select one
declared public fact from one already verified claim, but it cannot select a
physical column, invent a formula, or silently turn an unknown value into
zero.

The module is dependency-light on purpose.  It does not import ``tools`` (the
composition root imports this module), so the existing evidence/seal checks
can call it without introducing an import cycle.  ``FactCalculationError``
has the same public ``code``/``message`` shape that the query layer can adapt
to its own ``QueryFailure`` type.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any


@dataclass(frozen=True)
class FactFieldSpec:
    """Public semantic registration for one selectable result fact."""

    name: str
    grain: str
    unit_family: str
    share_policy: str


# These are the only secondary facts this first calculation boundary accepts.
# ``metric_value`` is retained as the legacy default and is deliberately not
# given an expected unit: its contract unit is carried by the claim itself.
# The amount fields cover the target-completion contract's two currency modes.
FACT_FIELD_SPECS: dict[str, FactFieldSpec] = {
    "metric_value": FactFieldSpec("metric_value", "declared", "declared", "legacy"),
    "net_rolls": FactFieldSpec("net_rolls", "group", "roll", "never"),
    "gross_rolls": FactFieldSpec("gross_rolls", "group", "roll", "never"),
    "return_rolls": FactFieldSpec("return_rolls", "group", "roll", "never"),
    "high_net_rolls": FactFieldSpec("high_net_rolls", "group", "roll", "never"),
    "completion_rate": FactFieldSpec("completion_rate", "group", "ratio", "never"),
    "target_amount_rmb": FactFieldSpec("target_amount_rmb", "group", "rmb", "never"),
    "actual_amount_rmb": FactFieldSpec("actual_amount_rmb", "group", "rmb", "never"),
    "gap_amount_rmb": FactFieldSpec("gap_amount_rmb", "group", "rmb", "never"),
    "target_amount_original": FactFieldSpec("target_amount_original", "group", "original_amount", "never"),
    "actual_amount_original": FactFieldSpec("actual_amount_original", "group", "original_amount", "never"),
    "gap_amount_original": FactFieldSpec("gap_amount_original", "group", "original_amount", "never"),
}


# ``scope_*`` and ``unit_*`` inventory fields are deliberately absent.  They
# can be repeated on every group row and therefore cannot be safely combined
# with one row's group amount in this first release.  ``known_*`` fields are
# partial observations rather than complete facts and are also absent.
UNSUPPORTED_FACT_FIELD_PREFIXES = ("scope_", "unit_", "known_", "high_known_")
UNKNOWN_STATE_TOKENS = frozenset(
    {
        "unknown",
        "incomplete",
        "partial",
        "truncated",
        "unverified",
        "unavailable",
        "missing",
        "not_set_for_future",
        "not_assessable",
        "invalid",
    }
)
UNKNOWN_DISPLAY_TOKENS = frozenset(
    {
        "",
        "unknown",
        "null",
        "none",
        "n/a",
        "未知",
        "未知币种",
        "不明",
        "未识别",
        "未判定",
        "ambiguous",
        "unresolved",
    }
)

_PERIOD_DIMENSION_KEYS = frozenset(
    {"period", "time", "时间", "期间", "period_bucket"}
)
_INCOMPLETE_SCOPE_STATES = frozenset(
    {"source_range_incomplete", "source_scope_unverifiable"}
)
_VALID_TARGET_STATES = frozenset({"set", "zero"})
_VALID_ACTUAL_STATES = frozenset({"reported", "set"})

# Contract units are intentionally exact here.  Guessing from a suffix such
# as ``元`` would classify USD or another source currency as RMB.  ``metric_value``
# remains dynamically typed by its claim unit and is checked for exact equality
# by the existing calculation policy.
_EXACT_UNIT_FAMILIES = {
    "卷": "roll",
    "roll": "roll",
    "rolls": "roll",
    "比例": "ratio",
    "百分比": "ratio",
    "%": "ratio",
    "ratio": "ratio",
    "人民币": "rmb",
    "人民币元": "rmb",
    "rmb": "rmb",
    "rmb元": "rmb",
    "cny": "rmb",
    "cny元": "rmb",
    "原币金额（按币种分别计量）": "original_amount",
    "原币金额（按币种 分别计量）": "original_amount",
    "原币金额": "original_amount",
}


class FactCalculationError(ValueError):
    """Stable semantic failure that the query layer can expose safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FactOperand:
    """One finite, typed public fact selected from one verified claim."""

    request_id: str
    claim_id: str
    claim_seal: str
    metric_ref: Any
    field: str
    value: Decimal
    unit: str
    field_unit: str
    unit_family: str
    grain: str
    group_dimensions: tuple[str, ...]
    group_fingerprint: str
    scope_fingerprint: str
    projection_fingerprint: str
    metric_basis_fingerprint: str
    filter_fingerprint: str
    filter_scope: Mapping[str, tuple[str, ...]]
    share_partition_dimensions: frozenset[str]
    period: Mapping[str, Any]
    scope_entities: tuple[Mapping[str, Any], ...]
    currency: Any
    currency_state: Any

    def as_mapping(self) -> dict[str, Any]:
        """Return the public-safe operand facts consumed by ``tools``."""

        return {
            "request_id": self.request_id,
            "claim_id": self.claim_id,
            "claim_seal": self.claim_seal,
            "metric_ref": self.metric_ref,
            "field": self.field,
            "value": self.value,
            "unit": self.unit,
            "field_unit": self.field_unit,
            "unit_family": self.unit_family,
            "grain": self.grain,
            "group_dimensions": list(self.group_dimensions),
            "group_fingerprint": self.group_fingerprint,
            "scope_fingerprint": self.scope_fingerprint,
            "projection_fingerprint": self.projection_fingerprint,
            "metric_basis_fingerprint": self.metric_basis_fingerprint,
            "filter_fingerprint": self.filter_fingerprint,
            "filter_scope": dict(self.filter_scope),
            "share_partition_dimensions": set(self.share_partition_dimensions),
            "period": dict(self.period),
            "scope_entities": [dict(item) for item in self.scope_entities],
            "currency": self.currency,
            "currency_state": self.currency_state,
        }


def fact_field_metadata(field: Any) -> FactFieldSpec | None:
    """Return a registered public fact spec, or ``None`` for unknown fields."""

    if not isinstance(field, str):
        return None
    return FACT_FIELD_SPECS.get(field)


def normalized_fact_field(field: Any) -> str:
    """Normalize omitted selectors while keeping arbitrary names invalid."""

    if field is None:
        return "metric_value"
    if not isinstance(field, str) or not field.strip():
        raise FactCalculationError(
            "CALCULATION_FACT_FIELD_UNSUPPORTED",
            "计算事实字段必须是已登记的非空字段名。",
        )
    normalized = field.strip()
    if normalized not in FACT_FIELD_SPECS:
        if normalized.startswith(UNSUPPORTED_FACT_FIELD_PREFIXES):
            message = "该计算事实是已知部分或重复展示的范围值，不能作为独立操作数。"
        else:
            message = "计算事实字段未登记或未公开。"
        raise FactCalculationError("CALCULATION_FACT_FIELD_UNSUPPORTED", message)
    return normalized


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _display_token(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().casefold()
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip().casefold()


def _unit_family(unit: str) -> str:
    """Map only exact registered unit text to an arithmetic family."""

    if not isinstance(unit, str) or not unit.strip():
        return "unknown"
    token = unit.strip().casefold()
    return _EXACT_UNIT_FAMILIES.get(token, "declared")


def _require_text(value: Any, *, code: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactCalculationError(code, message)
    return value.strip()


def _state_is_unknown(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    token = value.strip().casefold().replace("-", "_")
    return token in UNKNOWN_STATE_TOKENS or any(
        marker in token
        for marker in ("unknown", "incomplete", "truncat", "unverif", "missing")
    )


def _require_inventory_coverage(
    facts: Mapping[str, Any],
    field: str,
) -> None:
    """Require the counters that make one inventory fact complete.

    A finite value alone is not a coverage proof.  The query builders publish
    these counters beside the roll facts; each selected field has a narrower
    evidence set so an unknown return blocks net/high-net while a known gross
    slice can remain usable.
    """

    analysis_slice = any(
        key in facts
        for key in (
            "analysis_missing_roll_rows",
            "analysis_unknown_rows",
            "analysis_return_rows",
        )
    )
    if analysis_slice and field in {
        "gross_rolls",
        "net_rolls",
        "return_rolls",
        "high_net_rolls",
    }:
        # Analysis output has its own roll coverage.  In particular, a
        # non-matching row with a missing roll must not poison the selected
        # gross slice; analysis_missing_roll_rows counts only matched rows.
        coverage_fields = {
            "gross_rolls": (
                "outbound_unknown_rows",
                "analysis_unknown_rows",
                "analysis_missing_roll_rows",
            ),
            "net_rolls": (
                "outbound_unknown_rows",
                "returns_unknown_rows",
                "analysis_unknown_rows",
                "analysis_missing_roll_rows",
                "analysis_return_rows",
            ),
            "return_rolls": (
                "returns_unknown_rows",
                "analysis_return_rows",
            ),
            "high_net_rolls": (
                "outbound_unknown_rows",
                "returns_unknown_rows",
                "analysis_unknown_rows",
                "analysis_missing_roll_rows",
                "analysis_return_rows",
                "high_missing_price_rows",
                "high_missing_roll_rows",
            ),
        }
    else:
        coverage_fields = {
            "net_rolls": (
                "outbound_unknown_rows",
                "returns_unknown_rows",
                "gross_missing_roll_rows",
                "return_missing_roll_rows",
            ),
            "gross_rolls": (
                "outbound_unknown_rows",
                "gross_missing_roll_rows",
            ),
            "return_rolls": (
                "returns_unknown_rows",
                "return_missing_roll_rows",
            ),
            "high_net_rolls": (
                "outbound_unknown_rows",
                "returns_unknown_rows",
                "high_missing_price_rows",
                "high_missing_roll_rows",
                "return_missing_roll_rows",
            ),
        }
    required = coverage_fields.get(field)
    if required is None:
        return
    for counter in required:
        if counter not in facts:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 缺少字段级覆盖证据。",
            )
        value = _finite_decimal(facts.get(counter))
        if value is None or value < 0:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的覆盖计数无效。",
            )
        if value > 0:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的来源覆盖仍有未知或缺口。",
            )


def _analysis_has_unknown_member(
    claim: Mapping[str, Any],
    field: str,
    *extra_containers: Mapping[str, Any] | None,
) -> bool:
    """Return true for unknown members relevant to the selected fact.

    ``analysis_net_state`` describes net-flow attribution.  It must not make a
    complete selected gross slice unusable; gross coverage is checked through
    its own analysis counters instead.
    """

    if field == "gross_rolls":
        return False
    inspect_net_state = field in {"metric_value", "net_rolls", "high_net_rolls"}
    inspect_match_state = field in {
        "metric_value",
        "target_amount_rmb",
        "target_amount_original",
        "actual_amount_rmb",
        "actual_amount_original",
        "gap_amount_rmb",
        "gap_amount_original",
        "completion_rate",
    }

    containers: list[tuple[Any, bool]] = [
        (claim.get("facts"), False),
        (claim.get("states"), False),
    ]
    for extra in extra_containers:
        if isinstance(extra, Mapping):
            containers.extend(
                [
                    (extra, False),
                    (extra.get("facts"), False),
                    (extra.get("states"), False),
                    (extra.get("analysis"), True),
                    (
                        extra.get("_calculation_scope", {}).get("analysis"),
                        True,
                    )
                    if isinstance(extra.get("_calculation_scope"), Mapping)
                    else (None, True),
                ]
            )
    for container, analysis_context in containers:
        if not isinstance(container, Mapping):
            continue
        for raw_key, raw_value in container.items():
            key = str(raw_key).casefold()
            if not analysis_context and "analysis" not in key:
                continue
            if key == "unknown_policy":
                continue
            if key == "analysis_net_state" and not inspect_net_state:
                continue
            if key == "analysis_match_state" and not inspect_match_state:
                continue
            if key.endswith("_state") and key not in {
                "analysis_net_state",
                "analysis_match_state",
                "analysis_price_state",
            }:
                continue
            if key == "analysis_price_state" and not inspect_match_state:
                continue
            if field == "metric_value" and key in {
                "analysis_missing_quantity_rows",
                "analysis_missing_roll_rows",
                "analysis_return_rows",
            }:
                count = _finite_decimal(raw_value)
                if count is None or count > 0:
                    return True
            if _state_is_unknown(raw_value):
                return True
            if key.endswith(("_unknown_count", "_unknown_rows")):
                count = _finite_decimal(raw_value)
                if count is None or count > 0:
                    return True
            if key in {"analysis_net_state", "analysis_match_state", "analysis_price_state"} and raw_value is None:
                return True
    return False


def _target_states_for_field(
    states: Mapping[str, Any],
    field: str,
    value: Decimal,
) -> None:
    """Apply target/actual/source coverage semantics per selected fact."""

    source_scope_state = states.get("source_scope_state")
    source_scope_incomplete = (
        source_scope_state in _INCOMPLETE_SCOPE_STATES
        or _state_is_unknown(source_scope_state)
    )
    target_state = states.get("target_data_state")
    actual_state = states.get("actual_data_state")
    target_fields = {
        "target_amount_rmb",
        "target_amount_original",
    }
    actual_fields = {
        "actual_amount_rmb",
        "actual_amount_original",
    }
    gap_fields = {
        "gap_amount_rmb",
        "gap_amount_original",
    }
    completion_fields = {"completion_rate", "metric_value"}
    if field in target_fields:
        if target_state not in _VALID_TARGET_STATES:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的目标完整性状态不可用。",
            )
        if target_state == "zero" and value != 0:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 与目标为零状态不一致。",
            )
        # A complete target ledger can remain useful even when the actual
        # source scope is incomplete; actual/gap/completion are checked below.
        return
    if field in actual_fields:
        if actual_state not in _VALID_ACTUAL_STATES or source_scope_incomplete:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的实际覆盖范围未知或不完整。",
            )
        return
    if field in gap_fields:
        if (
            target_state not in _VALID_TARGET_STATES
            or actual_state not in _VALID_ACTUAL_STATES
            or source_scope_incomplete
        ):
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 缺少完整目标、实际或范围证据。",
            )
        return
    if field in completion_fields and (
        "target_data_state" in states or "actual_data_state" in states
    ):
        if (
            target_state not in _VALID_TARGET_STATES
            or actual_state not in _VALID_ACTUAL_STATES
            or source_scope_incomplete
            or target_state == "zero"
        ):
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的目标完成范围未知或不完整。",
            )


def _fact_is_known(
    claim: Mapping[str, Any],
    field: str,
    value: Any,
    *,
    result: Mapping[str, Any] | None = None,
) -> None:
    """Reject an explicitly unknown field without coupling to metric_value.

    Inventory SQL can legitimately return a known gross slice while the net
    metric is unknown because a return could not be attributed.  Therefore the
    generic metric data state is not used as a proxy for every selected fact;
    only a field-specific state (when a contract supplies one) blocks the
    selected fact.  A NULL/non-finite selected value remains unknown.
    """

    if _finite_decimal(value) is None:
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            f"计算事实 {field} 没有有限且完整的数值。",
        )
    facts = claim.get("facts")
    if not isinstance(facts, Mapping):
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算操作数缺少事实完整性证据。",
        )
    if _analysis_has_unknown_member(claim, field, result):
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            "分析条件仍有未知成员，不能把选中事实当作完整操作数。",
        )
    states = claim.get("states")
    if states is None:
        states = {}
    elif not isinstance(states, Mapping):
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算事实的状态证据格式无效。",
        )
    if isinstance(states, Mapping):
        _target_states_for_field(states, field, _finite_decimal(value))
    typed_fields = {
        "target_amount_rmb",
        "target_amount_original",
        "actual_amount_rmb",
        "actual_amount_original",
        "gap_amount_rmb",
        "gap_amount_original",
        "completion_rate",
    }
    if field in typed_fields:
        if not states:
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 缺少目标/实际完整性状态。",
            )
    candidates = (
        f"{field}_state",
        f"{field}_data_state",
        f"{field}_completeness",
        f"{field}_coverage_state",
    )
    for key in candidates:
        if key in states and _state_is_unknown(states.get(key)):
            raise FactCalculationError(
                "CALCULATION_VALUE_UNAVAILABLE",
                f"计算事实 {field} 的完整性状态未知。",
            )
    _require_inventory_coverage(facts, field)


def _period_dimension(label: Any, code: Any) -> bool:
    token = _display_token(code or label)
    return token in {"period", "time", "时间", "期间", "period_bucket"} or "期间" in token


def _canonical_dimension(item: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a public dimension while preserving identity bytes exactly."""

    code = item.get("code") if isinstance(item.get("code"), str) else None
    label = item.get("label") if isinstance(item.get("label"), str) else None
    key = code or label
    value = item.get("value")
    if not isinstance(key, str) or not key or value is None:
        raise FactCalculationError(
            "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
            "分组操作数缺少可验证的同组身份。",
        )
    if _display_token(value) in UNKNOWN_DISPLAY_TOKENS:
        raise FactCalculationError(
            "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
            "分组操作数的身份值未知。",
        )
    identity_state = item.get("identity_state")
    if _state_is_unknown(identity_state) or _display_token(identity_state) in {
        "ambiguous",
        "unresolved",
        "identity_missing",
    }:
        raise FactCalculationError(
            "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
            "分组操作数的身份状态未知或有歧义。",
        )
    if item.get("display_only") is True:
        raise FactCalculationError(
            "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
            "分组操作数只有显示标签，不能证明同组身份。",
        )
    # Keep raw identity values and case.  Source-exact codes such as ``A`` and
    # ``a`` are distinct unless their contract explicitly normalizes them.
    allowed_keys = (
        "code",
        "label",
        "value",
        "display_only",
        "display_name_missing",
        "identity_state",
        "entity_ref",
        "source_group_identity",
        "source_group_ref",
        "source_group_ref_field",
    )
    canonical = {key: item[key] for key in allowed_keys if key in item}
    for identity_key in ("entity_ref", "source_group_ref"):
        if identity_key in canonical and (
            canonical[identity_key] is None
            or _display_token(canonical[identity_key]) in UNKNOWN_DISPLAY_TOKENS
        ):
            raise FactCalculationError(
                "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
                "分组操作数的来源身份引用未知。",
            )
    return canonical


def _group_signature(
    claim: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[tuple[str, ...], str]:
    raw_scope = result.get("_calculation_scope")
    if not isinstance(raw_scope, Mapping):
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数没有受治理的范围证明。",
        )
    raw_dimensions = raw_scope.get("group_dimensions", ())
    if raw_dimensions is None:
        raw_dimensions = ()
    if not isinstance(raw_dimensions, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_dimensions
    ):
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数的分组维度证明无效。",
        )
    group_dimensions = tuple(str(item).strip() for item in raw_dimensions)
    if len(set(group_dimensions)) != len(group_dimensions):
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数的分组维度证明重复。",
        )

    dimensions = claim.get("dimensions")
    if not isinstance(dimensions, list):
        raise FactCalculationError(
            "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
            "计算操作数缺少可验证的分组身份。",
        )
    canonical: list[dict[str, Any]] = []
    for raw_item in dimensions:
        if not isinstance(raw_item, Mapping):
            raise FactCalculationError(
                "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
                "计算操作数的分组身份格式无效。",
            )
        code = raw_item.get("code")
        label = raw_item.get("label")
        if _period_dimension(label, code):
            continue
        canonical.append(_canonical_dimension(raw_item))

    # A scalar claim must have no non-temporal dimensions.  A grouped claim is
    # safe only when it carries one fully identified claim row.  The first
    # release explicitly supports a default inventory unit group; the same
    # exact identity rule also works for other registered one-row groups.
    if not group_dimensions:
        if canonical:
            raise FactCalculationError(
                "CALCULATION_REQUIRES_SCALAR",
                "带分组维度的结果不能作为标量计算操作数。",
            )
    else:
        if not canonical:
            raise FactCalculationError(
                "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
                "分组计算必须有明确的单行分组身份。",
            )
        if len(canonical) != len(group_dimensions):
            raise FactCalculationError(
                "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
                "结果行与分组合同的维度数量不一致。",
            )
        # The same verified claim is enough to bind a non-unit display value;
        # no cross-query entity inference is attempted. Preserve the claim's
        # dimension order and identity bytes in the fingerprint.

    payload = {
        "group_dimensions": list(group_dimensions),
        "dimensions": canonical,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return group_dimensions, f"group_{digest[:24]}"


def group_identity_fingerprint(
    claim: Mapping[str, Any],
    group_dimensions: Sequence[str],
) -> str:
    """Recompute a public claim's group identity for projection integrity.

    The model-wire result intentionally omits the private calculation scope.
    Projection can still verify the operand's group binding because the
    operand carries the already validated group-dimension list.
    """

    dimensions = list(group_dimensions)
    return _group_signature(
        claim,
        {"_calculation_scope": {"group_dimensions": dimensions}},
    )[1]


def _normalized_filter_scope(value: Any) -> dict[str, tuple[str, ...]]:
    """Normalize existing calculation filter evidence without importing tools."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数缺少可验证的筛选范围。",
        )
    normalized: dict[str, tuple[str, ...]] = {}
    for key, raw_values in value.items():
        if not isinstance(key, str) or not key:
            raise FactCalculationError(
                "CALCULATION_SCOPE_UNVERIFIED",
                "计算操作数包含无法验证的筛选字段。",
            )
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if not values:
            raise FactCalculationError(
                "CALCULATION_SCOPE_UNVERIFIED",
                "计算操作数包含空筛选范围。",
            )
        tokens: set[str] = set()
        for item in values:
            if isinstance(item, (Mapping, list, tuple, set)) or item is None:
                raise FactCalculationError(
                    "CALCULATION_SCOPE_UNVERIFIED",
                    "计算操作数包含无法验证的筛选值。",
                )
            if isinstance(item, bool):
                item_type = "bool"
            else:
                item_type = type(item).__name__
            tokens.add(
                json.dumps(
                    {"type": item_type, "value": item},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
        normalized[key] = tuple(sorted(tokens))
    return dict(sorted(normalized.items()))


def _filter_fingerprint(filter_scope: Mapping[str, Sequence[str]]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            filter_scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"filter_{digest[:24]}"


def _resolve_fact_unit(
    claim: Mapping[str, Any],
    selected_field: str,
) -> tuple[str, str]:
    """Resolve and validate the unit evidence for one public fact field."""

    fact_units = claim.get("fact_units")
    if selected_field == "metric_value":
        field_unit = claim.get("unit")
    else:
        if not isinstance(fact_units, Mapping) or selected_field not in fact_units:
            raise FactCalculationError(
                "CALCULATION_FACT_UNIT_UNVERIFIED",
                f"计算事实 {selected_field} 缺少合同登记的单位证据。",
            )
        field_unit = fact_units.get(selected_field)
    field_unit = _require_text(
        field_unit,
        code="CALCULATION_FACT_UNIT_UNVERIFIED",
        message=f"计算事实 {selected_field} 的单位证据无效。",
    )
    actual_unit_family = _unit_family(field_unit)
    if actual_unit_family == "unknown":
        raise FactCalculationError(
            "CALCULATION_FACT_UNIT_UNVERIFIED",
            f"计算事实 {selected_field} 的单位无法解释。",
        )
    spec = FACT_FIELD_SPECS[selected_field]
    if spec.unit_family not in {"declared", actual_unit_family}:
        raise FactCalculationError(
            "CALCULATION_FACT_UNIT_MISMATCH",
            f"计算事实 {selected_field} 的登记单位与字段语义不兼容。",
        )
    return field_unit, actual_unit_family


def resolve_fact_operand(
    *,
    request_id: Any,
    result: Mapping[str, Any],
    claim: Mapping[str, Any],
    field: Any = None,
) -> FactOperand:
    """Resolve one calculation operand from one already verified claim.

    ``tools`` must perform claim sealing and model-wire integrity validation
    before calling this function.  This function repeats the structural facts
    that are needed to keep explicit secondary fields fail-closed.
    """

    selected_field = normalized_fact_field(field)
    spec = FACT_FIELD_SPECS[selected_field]
    if not isinstance(result, Mapping) or result.get("status") != "success":
        raise FactCalculationError(
            "CALCULATION_SOURCE_UNAVAILABLE",
            "计算引用的查询没有成功返回。",
        )
    if result.get("truncated") is True or claim.get("source_truncated") is not False:
        raise FactCalculationError(
            "CALCULATION_REQUIRES_SCALAR",
            "计算只接受一个未截断的完整 claim。",
        )
    claims = result.get("claim_ledger")
    if not isinstance(claims, list) or len(claims) != 1 or claims[0] is not claim:
        # Identity is intentionally strict here.  The caller may pass an
        # equivalent mapping from a projected result, but it must be the one
        # claim actually bound to this result, not an arbitrary row.
        if not isinstance(claims, list) or len(claims) != 1 or claims[0] != claim:
            raise FactCalculationError(
                "CALCULATION_REQUIRES_SCALAR",
                "计算只接受一个未截断的标量或已验证单行 claim。",
            )
    if "row_count" in result and result.get("row_count") != 1:
        raise FactCalculationError(
            "CALCULATION_REQUIRES_SCALAR",
            "计算只接受一个结果行。",
        )

    request_text = _require_text(
        request_id,
        code="CALCULATION_SCOPE_UNVERIFIED",
        message="计算操作数缺少 request_id。",
    )
    claim_id = _require_text(
        claim.get("claim_id"),
        code="CALCULATION_VALUE_UNAVAILABLE",
        message="计算操作数缺少 claim 身份。",
    )
    claim_seal = _require_text(
        claim.get("claim_seal"),
        code="CALCULATION_SOURCE_INTEGRITY_INVALID",
        message="计算操作数缺少 claim 完整性证明。",
    )
    metric_ref = claim.get("metric_ref")
    scope_fingerprint = _require_text(
        claim.get("scope_fingerprint"),
        code="CALCULATION_SCOPE_UNVERIFIED",
        message="计算操作数缺少范围指纹。",
    )
    projection_fingerprint = _require_text(
        claim.get("projection_fingerprint"),
        code="CALCULATION_SCOPE_UNVERIFIED",
        message="计算操作数缺少投影指纹。",
    )
    period = claim.get("period")
    if not isinstance(period, Mapping):
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算操作数缺少期间证据。",
        )
    scope_entities = claim.get("scope_entities")
    if not isinstance(scope_entities, list) or any(
        not isinstance(item, Mapping) for item in scope_entities
    ):
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算操作数缺少范围实体证据。",
        )
    facts = claim.get("facts")
    if not isinstance(facts, Mapping) or selected_field not in facts:
        raise FactCalculationError(
            "CALCULATION_FACT_UNAVAILABLE",
            f"结果没有返回已登记事实 {selected_field}。",
        )
    value = _finite_decimal(facts.get(selected_field))
    _fact_is_known(
        claim,
        selected_field,
        facts.get(selected_field),
        result=result,
    )
    if value is None:  # defensive; _fact_is_known raises above
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            f"计算事实 {selected_field} 没有有限数值。",
        )

    field_unit, actual_unit_family = _resolve_fact_unit(claim, selected_field)

    raw_scope = result.get("_calculation_scope")
    if not isinstance(raw_scope, Mapping):  # defensive after _group_signature
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数没有受治理的范围证明。",
        )
    raw_group_dimensions = raw_scope.get("group_dimensions") or []
    if selected_field == "metric_value" and raw_group_dimensions:
        raise FactCalculationError(
            "CALCULATION_REQUIRES_SCALAR",
            "省略事实字段的旧 metric_value 分组结果不能作为计算操作数。",
        )
    group_dimensions, group_fingerprint = _group_signature(claim, result)
    metric_basis_fingerprint = _require_text(
        raw_scope.get("metric_basis_fingerprint"),
        code="CALCULATION_SCOPE_UNVERIFIED",
        message="计算操作数没有指标母集指纹。",
    )
    raw_share_dimensions = raw_scope.get("share_partition_dimensions", ())
    if not isinstance(raw_share_dimensions, list) or any(
        not isinstance(item, str) for item in raw_share_dimensions
    ):
        raise FactCalculationError(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数的占比分区证明无效。",
        )
    filter_scope = _normalized_filter_scope(raw_scope.get("filter_scope"))
    return FactOperand(
        request_id=request_text,
        claim_id=claim_id,
        claim_seal=claim_seal,
        metric_ref=metric_ref,
        field=selected_field,
        value=value,
        unit=field_unit,
        field_unit=field_unit,
        unit_family=actual_unit_family,
        grain=spec.grain,
        group_dimensions=group_dimensions,
        group_fingerprint=group_fingerprint,
        scope_fingerprint=scope_fingerprint,
        projection_fingerprint=projection_fingerprint,
        metric_basis_fingerprint=metric_basis_fingerprint,
        filter_fingerprint=_filter_fingerprint(filter_scope),
        filter_scope=filter_scope,
        share_partition_dimensions=frozenset(raw_share_dimensions),
        period=dict(period),
        scope_entities=tuple(dict(item) for item in scope_entities),
        currency=claim.get("currency"),
        currency_state=claim.get("currency_state"),
    )


def _fact_values_equal(expected: Any, actual: Any) -> bool:
    """Compare wire values without allowing numeric spelling to bypass proof."""

    expected_decimal = _finite_decimal(expected)
    actual_decimal = _finite_decimal(actual)
    if expected_decimal is not None and actual_decimal is not None:
        return expected_decimal == actual_decimal
    return expected == actual


def resolve_visible_fact(
    claim: Mapping[str, Any],
    field: Any = None,
    *,
    public_result: Mapping[str, Any] | None = None,
    group_dimensions: Sequence[str] | None = None,
    expected_group_fingerprint: Any = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Resolve one field from a model-visible claim for projection checks.

    This entry point deliberately does not need private ``_calculation_scope``
    data.  It reuses the same field completeness and unit guards as operand
    construction, and can recompute a group fingerprint from the public claim
    plus the already sealed operand's governed dimension list.
    """

    if not isinstance(claim, Mapping):
        raise FactCalculationError(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算操作数的公开 claim 无效。",
        )
    if claim.get("source_truncated") is not False:
        raise FactCalculationError(
            "CALCULATION_REQUIRES_SCALAR",
            "计算只接受一个未截断的公开 claim。",
        )
    selected_field = normalized_fact_field(field)
    facts = claim.get("facts")
    if not isinstance(facts, Mapping) or selected_field not in facts:
        raise FactCalculationError(
            "CALCULATION_FACT_UNAVAILABLE",
            f"公开 claim 没有事实 {selected_field}。",
        )
    raw_value = facts.get(selected_field)
    if strict:
        _fact_is_known(
            claim,
            selected_field,
            raw_value,
            result=public_result,
        )
    elif _finite_decimal(raw_value) is None:
        raise FactCalculationError(
            "CALCULATION_VALUE_UNAVAILABLE",
            f"公开 claim 的事实 {selected_field} 没有有限数值。",
        )
    field_unit, unit_family = _resolve_fact_unit(claim, selected_field)
    dimensions: list[str] | None = None
    group_fingerprint: str | None = None
    if group_dimensions is not None:
        if not isinstance(group_dimensions, Sequence) or isinstance(
            group_dimensions, (str, bytes)
        ) or any(
            not isinstance(item, str) for item in group_dimensions
        ):
            raise FactCalculationError(
                "CALCULATION_GROUP_IDENTITY_UNVERIFIED",
                "公开 claim 的分组维度证明无效。",
            )
        dimensions = list(group_dimensions)
        group_fingerprint = group_identity_fingerprint(claim, dimensions)
        if (
            expected_group_fingerprint is not None
            and expected_group_fingerprint != group_fingerprint
        ):
            raise FactCalculationError(
                "CALCULATION_SOURCE_INTEGRITY_INVALID",
                "公开 claim 的分组身份与计算操作数不一致。",
            )
    return {
        "field": selected_field,
        "value": raw_value,
        "unit": field_unit,
        "field_unit": field_unit,
        "unit_family": unit_family,
        "group_dimensions": dimensions,
        "group_fingerprint": group_fingerprint,
    }


def validate_visible_fact(
    operand: Mapping[str, Any],
    claim: Mapping[str, Any],
    public_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a sealed calculation operand against its visible claim.

    Legacy calculation operands omit ``field``/group metadata; those retain the
    old finite ``metric_value`` check.  New selector operands carry the field
    and group metadata, so this function checks value, unit, completeness and
    exact group identity before the projection can re-seal them.
    """

    if not isinstance(operand, Mapping):
        raise FactCalculationError(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算操作数格式无效。",
        )
    has_selector = "field" in operand
    selected_field = operand.get("field") if has_selector else None
    dimensions = operand.get("group_dimensions") if has_selector else None
    expected_group_fingerprint = (
        operand.get("group_fingerprint")
        if has_selector and "group_fingerprint" in operand
        else None
    )
    visible = resolve_visible_fact(
        claim,
        selected_field,
        public_result=public_result,
        group_dimensions=dimensions,
        expected_group_fingerprint=expected_group_fingerprint,
        strict=has_selector,
    )
    if "value" in operand and not _fact_values_equal(
        operand.get("value"), visible["value"]
    ):
        raise FactCalculationError(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算操作数的事实值与公开 claim 不一致。",
        )
    for key in ("unit", "field_unit"):
        if key in operand and operand.get(key) != visible[key]:
            raise FactCalculationError(
                "CALCULATION_SOURCE_INTEGRITY_INVALID",
                "计算操作数的单位与公开 claim 不一致。",
            )
    if has_selector and visible["group_dimensions"] != dimensions:
        raise FactCalculationError(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算操作数的分组维度与公开 claim 不一致。",
        )
    return visible


def validate_fact_operation(
    operation: Any,
    left: FactOperand,
    right: FactOperand,
) -> dict[str, Any]:
    """Validate field grain/units before the existing scope rules run.

    The result is a small evidence fragment suitable for ``scope_compatibility``.
    This does not decide metric basis, period, filter subset, or currency;
    those checks remain in ``tools`` where the existing calculation policy is
    authoritative.
    """

    if operation not in {"difference", "ratio", "share"}:
        raise FactCalculationError(
            "CALCULATION_OPERATION_UNSUPPORTED",
            "不支持该受治理计算操作。",
        )
    if left.group_dimensions != right.group_dimensions:
        raise FactCalculationError(
            "CALCULATION_SCOPE_MISMATCH",
            "计算操作数的分组维度不同，不能混合母集。",
        )
    if left.group_fingerprint != right.group_fingerprint:
        raise FactCalculationError(
            "CALCULATION_SCOPE_MISMATCH",
            "计算操作数不是同一已验证分组或同一标量范围。",
        )
    if left.group_dimensions:
        if left.request_id != right.request_id or left.claim_id != right.claim_id:
            raise FactCalculationError(
                "CALCULATION_SCOPE_MISMATCH",
                "分组次级事实只支持同一 request 的同一已验证 claim。",
            )
        if left.field == "metric_value" or right.field == "metric_value":
            raise FactCalculationError(
                "CALCULATION_REQUIRES_SCALAR",
                "省略事实字段的旧 metric_value 分组结果不能作为计算操作数。",
            )
    if left.grain != right.grain and "declared" not in {left.grain, right.grain}:
        raise FactCalculationError(
            "CALCULATION_SCOPE_MISMATCH",
            "计算事实的分组粒度不兼容。",
        )
    if left.unit_family != right.unit_family:
        raise FactCalculationError(
            "CALCULATION_UNIT_MISMATCH",
            "计算事实的单位类别不兼容。",
        )
    if operation == "share" and (
        left.field != "metric_value"
        or right.field != "metric_value"
        or FACT_FIELD_SPECS[left.field].share_policy != "legacy"
        or FACT_FIELD_SPECS[right.field].share_policy != "legacy"
    ):
        raise FactCalculationError(
            "CALCULATION_SUBSET_NOT_PROVEN",
            "已登记次级事实没有可证明的子集关系，不能作为 share。",
        )
    return {
        "left_field": left.field,
        "right_field": right.field,
        "same_group_identity": True,
        "same_group_dimensions": True,
        "same_fact_unit_family": True,
        "fact_grain": left.grain if left.grain == right.grain else "declared_group",
        "share_policy": "legacy_metric_value_only" if operation == "share" else "not_applicable",
    }


def fields_are_compatible(operation: Any, left: Any, right: Any) -> dict[str, Any]:
    """Small mapping-oriented adapter for callers that do not keep dataclasses."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_field = normalized_fact_field(left.get("field"))
        right_field = normalized_fact_field(right.get("field"))
        if operation == "share" and (left_field != "metric_value" or right_field != "metric_value"):
            raise FactCalculationError(
                "CALCULATION_SUBSET_NOT_PROVEN",
                "已登记次级事实没有可证明的子集关系，不能作为 share。",
            )
        left_group_dimensions = left.get("group_dimensions")
        right_group_dimensions = right.get("group_dimensions")
        if left_group_dimensions:
            if (
                left.get("request_id") != right.get("request_id")
                or left.get("claim_id") != right.get("claim_id")
            ):
                raise FactCalculationError(
                    "CALCULATION_SCOPE_MISMATCH",
                    "分组次级事实只支持同一 request 的同一已验证 claim。",
                )
            if left_field == "metric_value" or right_field == "metric_value":
                raise FactCalculationError(
                    "CALCULATION_REQUIRES_SCALAR",
                    "省略事实字段的旧 metric_value 分组结果不能作为计算操作数。",
                )
        if (
            not isinstance(left.get("group_fingerprint"), str)
            or not isinstance(right.get("group_fingerprint"), str)
            or left.get("group_fingerprint") != right.get("group_fingerprint")
        ):
            raise FactCalculationError(
                "CALCULATION_SCOPE_MISMATCH",
                "计算操作数不是同一已验证分组或同一标量范围。",
            )
        if left_group_dimensions != right_group_dimensions:
            raise FactCalculationError(
                "CALCULATION_SCOPE_MISMATCH",
                "计算操作数的分组维度不同，不能混合母集。",
            )
        if left.get("unit_family") != right.get("unit_family"):
            raise FactCalculationError(
                "CALCULATION_UNIT_MISMATCH",
                "计算事实的单位类别不兼容。",
            )
        if left.get("grain") != right.get("grain") and "declared" not in {
            left.get("grain"), right.get("grain")
        }:
            raise FactCalculationError(
                "CALCULATION_SCOPE_MISMATCH",
                "计算事实的分组粒度不兼容。",
            )
        return {
            "left_field": left_field,
            "right_field": right_field,
            "same_group_identity": True,
            "same_group_dimensions": left_group_dimensions == right_group_dimensions,
            "same_fact_unit_family": True,
            "fact_grain": left.get("grain"),
            "share_policy": "legacy_metric_value_only" if operation == "share" else "not_applicable",
        }
    raise FactCalculationError(
        "CALCULATION_SCOPE_UNVERIFIED",
        "计算事实操作数格式无效。",
    )


__all__ = [
    "FACT_FIELD_SPECS",
    "FactCalculationError",
    "FactFieldSpec",
    "FactOperand",
    "fact_field_metadata",
    "fields_are_compatible",
    "group_identity_fingerprint",
    "normalized_fact_field",
    "resolve_fact_operand",
    "resolve_visible_fact",
    "validate_visible_fact",
    "validate_fact_operation",
]
