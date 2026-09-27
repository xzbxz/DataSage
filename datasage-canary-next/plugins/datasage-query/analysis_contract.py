"""Pure public contract for the finite metric-analysis filter language.

``analysis`` deliberately describes a small, typed predicate surface.  It is
not an expression language and never carries a SQL column, table, function,
or formula.  Metric semantics register the business aliases that an
execution builder may implement; this module only validates and projects that
registration and the request shape.

The execution layer may use the normalized request produced here, but it must
still bind every registered alias to its own public fact and source evidence.
Unknown values remain an execution/evidence concern: callers cannot opt into
an ``unknown == false`` policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from types import MappingProxyType
from typing import Any


ANALYSIS_FIELDS = frozenset({"row_filters", "group_filters"})
_ANALYSIS_METADATA_STAGES = frozenset({"row", "group"})
ANALYSIS_FILTER_FIELDS = frozenset({"field", "op", "value"})
ANALYSIS_OPERATORS = ("eq", "gt", "gte", "lt", "lte")
ANALYSIS_OPERATOR_SET = frozenset(ANALYSIS_OPERATORS)
ANALYSIS_MAX_ROW_FILTERS = 6
ANALYSIS_MAX_GROUP_FILTERS = 6
ANALYSIS_MAX_FILTERS = 8
ANALYSIS_FIELD_MAX_LENGTH = 80
ANALYSIS_LABEL_MAX_LENGTH = 160
ANALYSIS_UNIT_MAX_LENGTH = 80
ANALYSIS_DECIMAL_MAX_LENGTH = 64
# Match the backend's governed decimal binding.  At most 26 integer digits
# and 12 fractional digits fit DECIMAL(38,12); rejecting excess scale here
# prevents a database cast from silently rounding a threshold.
ANALYSIS_DECIMAL_PRECISION = 38
ANALYSIS_DECIMAL_SCALE = 12
ANALYSIS_DECIMAL_INTEGER_DIGITS = ANALYSIS_DECIMAL_PRECISION - ANALYSIS_DECIMAL_SCALE

# Decimal strings are intentionally finite base-10 values.  Exponents are not
# part of the public input language; this avoids accepting an unbounded value
# through forms such as ``1e999999`` and makes the wire contract predictable.
# Accept ordinary finite decimal spellings, including ``+.500``.  The
# normalized form below canonicalizes equivalent spellings before a scope or
# SQL binding is built.
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))$")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


class AnalysisContractError(ValueError):
    """Stable public analysis-contract failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.hint = hint


@dataclass(frozen=True)
class AnalysisField:
    """One registered business fact accepted by a filter stage."""

    alias: str
    label: str
    unit: str
    operators: tuple[str, ...]
    # Optional descriptive facts are retained for the backend/evidence layer;
    # none of them are interpreted as SQL or as planner instructions here.
    public_fact: str | None = None
    phase: str | None = None
    nullable: bool | None = None
    currency: str | None = None
    population: str | None = None
    source_scope: str | None = None
    unknown_policy: str | None = None
    operation: str | None = None

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "label": self.label,
            "unit": self.unit,
            "operators": list(self.operators),
        }
        for key, value in (
            ("public_fact", self.public_fact),
            ("phase", self.phase),
            ("nullable", self.nullable),
            ("currency", self.currency),
            ("population", self.population),
            ("source_scope", self.source_scope),
            ("unknown_policy", self.unknown_policy),
            ("operation", self.operation),
        ):
            if value is not None:
                result[key] = value
        if self.phase is not None:
            # ``stage`` is the semantic-file spelling; ``phase`` is the
            # public contract spelling.  Publishing both keeps the catalog
            # self-describing during the migration without exposing a source
            # identifier.
            result["stage"] = self.phase
        return result


@dataclass(frozen=True)
class NormalizedAnalysis:
    """Normalized finite analysis predicates."""

    row_filters: tuple[dict[str, Any], ...] = ()
    group_filters: tuple[dict[str, Any], ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.row_filters:
            result["row_filters"] = [dict(item) for item in self.row_filters]
        if self.group_filters:
            result["group_filters"] = [dict(item) for item in self.group_filters]
        return result


def _error(
    code: str,
    message: str,
    path: str | None,
    hint: str | None = None,
) -> AnalysisContractError:
    return AnalysisContractError(code, message, path=path, hint=hint)


def parse_decimal(value: Any, *, path: str | None = None) -> Decimal:
    """Return one finite, bounded decimal value.

    JSON booleans are a subclass of ``int`` in Python, so the explicit bool
    guard is required.  Floats are accepted as JSON numbers only after a
    finite check; conversion through ``str`` keeps the public decimal value
    stable and avoids binary expansion in the normalized request.
    """

    if isinstance(value, bool) or value is None:
        raise _error(
            "INVALID_INPUT",
            "分析条件值必须是有限数字或有界十进制字符串，不能是布尔值。",
            path,
            "Use a finite JSON number or a decimal string without an exponent.",
        )
    if isinstance(value, str):
        if len(value) > ANALYSIS_DECIMAL_MAX_LENGTH or not _DECIMAL_TEXT.fullmatch(value):
            raise _error(
                "INVALID_INPUT",
                "分析条件值必须是有界十进制字符串。",
                path,
                f"Use at most {ANALYSIS_DECIMAL_MAX_LENGTH} characters such as 0.5.",
            )
        text = value
    elif isinstance(value, int):
        try:
            text = str(value)
        except ValueError:
            raise _error(
                "INVALID_INPUT",
                "分析条件值超出十进制长度上限。",
                path,
            ) from None
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _error(
                "INVALID_INPUT",
                "分析条件值必须是有限数字。",
                path,
                "NaN and Infinity are not accepted.",
            )
        text = str(value)
    else:
        raise _error(
            "INVALID_INPUT",
            "分析条件值必须是有限数字或有界十进制字符串。",
            path,
            "Use a finite JSON number or decimal string.",
        )
    if len(text) > ANALYSIS_DECIMAL_MAX_LENGTH:
        raise _error(
            "INVALID_INPUT",
            "分析条件值超出长度上限。",
            path,
            f"Use at most {ANALYSIS_DECIMAL_MAX_LENGTH} characters.",
        )
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise _error(
            "INVALID_INPUT",
            "分析条件值不是有效十进制数。",
            path,
        ) from None
    if not parsed.is_finite():
        raise _error(
            "INVALID_INPUT",
            "分析条件值必须是有限数字。",
            path,
        )
    digits = parsed.as_tuple().digits
    exponent = parsed.as_tuple().exponent
    fractional_digits = max(0, -exponent)
    integer_digits = max(0, len(digits) + exponent) if exponent >= 0 else max(0, len(digits) + exponent)
    significant_digits = len(digits)
    if (
        fractional_digits > ANALYSIS_DECIMAL_SCALE
        or integer_digits > ANALYSIS_DECIMAL_INTEGER_DIGITS
        or significant_digits > ANALYSIS_DECIMAL_PRECISION
    ):
        raise _error(
            "INVALID_INPUT",
            "分析条件值超出受治理十进制精度或小数位上限。",
            path,
            f"Use a value representable as DECIMAL({ANALYSIS_DECIMAL_PRECISION},{ANALYSIS_DECIMAL_SCALE}).",
        )
    return parsed


def normalize_decimal(value: Any, *, path: str | None = None) -> str:
    """Validate and canonicalize one decimal for stable scope fingerprints."""

    parsed = parse_decimal(value, path=path)
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0", "+0"} else text


def _check_text(
    value: Any,
    *,
    name: str,
    max_length: int,
    path: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise _error(
            "CONTRACT_UNAVAILABLE",
            f"分析合同的 {name} 无效。",
            path,
        )
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _error(
            "CONTRACT_UNAVAILABLE",
            f"分析合同的 {name} 无效。",
            path,
        )
    return value.strip()


def _field_spec(raw: Any, *, field: str, path: str) -> AnalysisField:
    if not isinstance(raw, Mapping):
        raise _error("CONTRACT_UNAVAILABLE", "分析字段登记格式无效。", path)
    allowed_keys = {
        "label",
        "unit",
        "operators",
        "public_fact",
        "phase",
        "nullable",
        "currency",
        "population",
        "stage",
        "source_scope",
        "unknown_policy",
        "operation",
    }
    unexpected = set(raw) - allowed_keys
    if unexpected:
        key = sorted(str(item) for item in unexpected)[0]
        raise _error(
            "CONTRACT_UNAVAILABLE",
            "分析字段登记包含未支持属性。",
            f"{path}.{key}",
        )
    alias = _check_text(
        field,
        name="字段别名",
        max_length=ANALYSIS_FIELD_MAX_LENGTH,
        path=f"{path}.__alias__",
        pattern=_FIELD_NAME,
    )
    label = _check_text(
        raw.get("label", alias),
        name="字段标签",
        max_length=ANALYSIS_LABEL_MAX_LENGTH,
        path=f"{path}.label",
    )
    unit = _check_text(
        raw.get("unit"),
        name="字段单位",
        max_length=ANALYSIS_UNIT_MAX_LENGTH,
        path=f"{path}.unit",
    )
    # Existing semantic files use ``operation`` for the backend operation and
    # omit the public comparison operator list.  Omission means every closed
    # comparison is supported; a future registration may narrow it with the
    # explicit ``operators`` list.
    operators = raw.get("operators", list(ANALYSIS_OPERATORS))
    if (
        not isinstance(operators, list)
        or not operators
        or len(operators) > len(ANALYSIS_OPERATORS)
        or any(not isinstance(item, str) for item in operators)
        or len(set(operators)) != len(operators)
        or any(item not in ANALYSIS_OPERATOR_SET for item in operators)
    ):
        raise _error(
            "CONTRACT_UNAVAILABLE",
            "分析字段登记的运算符无效。",
            f"{path}.operators",
        )
    optional_text: dict[str, str | None] = {}
    for key in (
        "public_fact",
        "phase",
        "currency",
        "population",
        "source_scope",
        "unknown_policy",
        "operation",
    ):
        value = raw.get(key)
        if key == "phase" and value is None:
            value = raw.get("stage")
        if value is not None:
            optional_text[key] = _check_text(
                value,
                name=key,
                max_length=ANALYSIS_FIELD_MAX_LENGTH,
                path=f"{path}.{key}",
                pattern=(
                    _FIELD_NAME
                    if key in {
                        "public_fact",
                        "phase",
                        "currency",
                        "population",
                        "source_scope",
                        "unknown_policy",
                        "operation",
                    }
                    else None
                ),
            )
        else:
            optional_text[key] = None
    nullable = raw.get("nullable")
    if nullable is not None and not isinstance(nullable, bool):
        raise _error(
            "CONTRACT_UNAVAILABLE",
            "分析字段登记的 nullable 必须是布尔值。",
            f"{path}.nullable",
        )
    return AnalysisField(
        alias=alias,
        label=label,
        unit=unit,
        operators=tuple(operators),
        public_fact=optional_text["public_fact"],
        phase=optional_text["phase"],
        nullable=nullable,
        currency=optional_text["currency"],
        population=optional_text["population"],
        source_scope=optional_text["source_scope"],
        unknown_policy=optional_text["unknown_policy"],
        operation=optional_text["operation"],
    )


def validate_analysis_fields(
    raw: Any,
    *,
    path: str = "analysis_fields",
    supported_combinations: Any = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate metric-owned ``analysis_fields`` metadata.

    Metadata is intentionally small.  Missing stages are allowed, while an
    explicitly supplied stage must be a mapping of public business aliases to
    ``label``/``unit``/``operators`` facts.  This lets a metric reject an
    unsupported row/group combination before any source execution.
    """

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _error("CONTRACT_UNAVAILABLE", "分析字段登记格式无效。", path)
    # ``row``/``group`` are the compact semantic-file spelling.  Public
    # catalog and request paths use the explicit ``row_filters``/
    # ``group_filters`` spelling; both resolve to the same internal stages.
    accepted_stages = ANALYSIS_FIELDS | _ANALYSIS_METADATA_STAGES
    unexpected = set(raw) - accepted_stages
    if unexpected:
        key = sorted(str(item) for item in unexpected)[0]
        raise _error("CONTRACT_UNAVAILABLE", "分析字段登记包含未支持阶段。", f"{path}.{key}")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for public_stage, semantic_stage in (
        ("row_filters", "row"),
        ("group_filters", "group"),
    ):
        raw_stage = raw.get(public_stage)
        if raw_stage is None:
            raw_stage = raw.get(semantic_stage)
        if raw_stage is None:
            continue
        if not isinstance(raw_stage, Mapping):
            raise _error("CONTRACT_UNAVAILABLE", "分析字段登记阶段格式无效。", f"{path}.{semantic_stage}")
        stage_result: dict[str, dict[str, Any]] = {}
        for raw_alias, spec in raw_stage.items():
            alias = str(raw_alias)
            field = _field_spec(spec, field=alias, path=f"{path}.{semantic_stage}.{alias}")
            declared_stage = field.as_mapping().get("stage")
            if declared_stage is not None and declared_stage != semantic_stage:
                raise _error(
                    "CONTRACT_UNAVAILABLE",
                    "分析字段登记的阶段与所在分组不一致。",
                    f"{path}.{semantic_stage}.{alias}.stage",
                )
            stage_result[field.alias] = field.as_mapping()
        result[public_stage] = stage_result
    _validate_supported_combinations(
        supported_combinations,
        result,
        path=f"{path.rsplit('.', 1)[0]}.analysis_supported_combinations"
        if "." in path
        else "analysis_supported_combinations",
    )
    return result


def _validate_supported_combinations(
    raw: Any,
    fields: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    path: str,
) -> list[dict[str, Any]]:
    """Validate optional metric-level stage combinations."""

    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise _error("CONTRACT_UNAVAILABLE", "分析支持组合登记格式无效。", path)
    known = {
        stage: set(values)
        for stage, values in fields.items()
    }
    combinations: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            raise _error("CONTRACT_UNAVAILABLE", "分析支持组合登记格式无效。", item_path)
        allowed = {"row_fields", "group_fields", "stages", "preserves", "source_scope"}
        unexpected = set(item) - allowed
        if unexpected:
            key = sorted(str(value) for value in unexpected)[0]
            raise _error("CONTRACT_UNAVAILABLE", "分析支持组合包含未支持属性。", f"{item_path}.{key}")
        normalized: dict[str, Any] = {}
        for key, stage in (("row_fields", "row_filters"), ("group_fields", "group_filters")):
            values = item.get(key, [])
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values)
                or any(value not in known.get(stage, set()) for value in values)
            ):
                raise _error("CONTRACT_UNAVAILABLE", "分析支持组合引用了未登记字段。", f"{item_path}.{key}")
            normalized[key] = list(values)
        stages = item.get("stages")
        expected_stages = [
            stage
            for key, stage in (("row_fields", "row"), ("group_fields", "group"))
            if normalized[key]
        ]
        if stages is None:
            stages = expected_stages
        if (
            not isinstance(stages, list)
            or any(not isinstance(stage, str) for stage in stages)
            or len(set(stages)) != len(stages)
            or any(stage not in _ANALYSIS_METADATA_STAGES for stage in stages)
            or set(stages) != set(expected_stages)
        ):
            raise _error("CONTRACT_UNAVAILABLE", "分析支持组合的阶段声明无效。", f"{item_path}.stages")
        normalized["stages"] = list(stages)
        for key in ("preserves", "source_scope"):
            if key not in item:
                continue
            value = item[key]
            if key == "preserves":
                if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                    raise _error("CONTRACT_UNAVAILABLE", "分析支持组合的 preserves 无效。", f"{item_path}.{key}")
                normalized[key] = [v.strip() for v in value]
            else:
                normalized[key] = _check_text(value, name=key, max_length=ANALYSIS_FIELD_MAX_LENGTH, path=f"{item_path}.{key}")
        combinations.append(normalized)
    return combinations


def project_analysis_fields(raw: Any) -> dict[str, dict[str, dict[str, Any]]] | None:
    """Return safe model metadata, or ``None`` when a metric has no analysis."""

    if raw is None:
        return None
    return validate_analysis_fields(raw)


def _normalize_filter(
    raw: Any,
    *,
    stage: str,
    index: int,
    registered: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    path = f"analysis.{stage}[{index}]"
    if not isinstance(raw, Mapping) or set(raw) != ANALYSIS_FILTER_FIELDS:
        raise _error(
            "INVALID_INPUT",
            "每个分析条件必须且只能包含 field、op、value。",
            path,
            "Use {field, op, value} with a registered business field.",
        )
    field = raw.get("field")
    if not isinstance(field, str) or not _FIELD_NAME.fullmatch(field):
        raise _error("INVALID_INPUT", "分析字段别名无效。", f"{path}.field")
    if registered is not None and field not in registered:
        raise _error(
            "ANALYSIS_UNSUPPORTED",
            "该分析字段未在指标合同中登记。",
            f"{path}.field",
            "Use an analysis field published by datasage_catalog.",
        )
    op = raw.get("op")
    if op not in ANALYSIS_OPERATOR_SET:
        raise _error(
            "INVALID_INPUT",
            "分析条件运算符不受支持。",
            f"{path}.op",
            f"Use one of: {', '.join(ANALYSIS_OPERATORS)}.",
        )
    if registered is not None:
        spec = registered.get(field) or {}
        operators = spec.get("operators")
        if isinstance(operators, (list, tuple)) and op not in operators:
            raise _error(
                "ANALYSIS_UNSUPPORTED_COMBINATION",
                "该指标不支持此分析字段运算。",
                f"{path}.op",
                "Use an operator listed for this field in datasage_catalog.",
            )
    return {
        "field": field,
        "op": op,
        "value": normalize_decimal(raw.get("value"), path=f"{path}.value"),
    }


def normalize_analysis(raw: Any, *, path: str = "analysis") -> dict[str, Any]:
    """Validate only the request shape and return deterministic predicates."""

    if not isinstance(raw, Mapping):
        raise _error("INVALID_INPUT", "analysis 必须是对象。", path)
    unexpected = set(raw) - ANALYSIS_FIELDS
    if unexpected:
        key = sorted(str(item) for item in unexpected)[0]
        raise _error("INVALID_INPUT", "analysis 只接受 row_filters 和 group_filters。", f"{path}.{key}")
    if not raw:
        raise _error("INVALID_INPUT", "analysis 至少需要一类筛选条件。", path)
    result: dict[str, Any] = {}
    total = 0
    for stage, maximum in (
        ("row_filters", ANALYSIS_MAX_ROW_FILTERS),
        ("group_filters", ANALYSIS_MAX_GROUP_FILTERS),
    ):
        if stage not in raw:
            continue
        values = raw.get(stage)
        if not isinstance(values, list) or len(values) > maximum:
            raise _error(
                "INVALID_INPUT",
                f"{stage} 必须包含 0 到 {maximum} 个条件。",
                f"{path}.{stage}",
            )
        total += len(values)
        if total > ANALYSIS_MAX_FILTERS:
            raise _error(
                "INVALID_INPUT",
                f"analysis 两类条件合计最多 {ANALYSIS_MAX_FILTERS} 项。",
                path,
            )
        normalized_stage = [
            _normalize_filter(item, stage=stage, index=index)
            for index, item in enumerate(values)
        ]
        # Canonicalize commutative AND predicates only after all per-stage
        # and total limits have been counted.  Keep the outer row->group stage
        # order stable for source/evidence fingerprints.
        normalized_stage.sort(
            key=lambda item: (item["field"], item["op"], item["value"])
        )
        if normalized_stage:
            result[stage] = normalized_stage
    if total < 1:
        raise _error("INVALID_INPUT", "analysis 至少需要一个过滤条件。", path)
    return result


def validate_analysis_for_metric(
    raw: Any,
    metric: Mapping[str, Any] | None,
    *,
    metric_code: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Validate request analysis against one metric's registered aliases.

    ``metric`` is the already loaded semantic definition.  A missing
    ``analysis_fields`` entry means this metric has no analysis capability and
    therefore rejects a supplied analysis request explicitly.
    """

    normalized = normalize_analysis(raw)
    if not isinstance(metric, Mapping):
        raise _error(
            "ANALYSIS_UNSUPPORTED",
            "该指标不支持有限分析筛选。",
            "analysis",
            "Use a metric whose catalog entry publishes analysis_fields.",
        )
    registered = validate_analysis_fields(
        metric.get("analysis_fields"),
        supported_combinations=metric.get("analysis_supported_combinations"),
    )
    if not registered:
        label = metric_code or "该指标"
        raise _error(
            "ANALYSIS_UNSUPPORTED",
            f"{label} 未登记有限分析能力。",
            "analysis",
            "Use the row/group fields published by datasage_catalog.",
        )
    validated: dict[str, Any] = {}
    for stage in ("row_filters", "group_filters"):
        if stage not in normalized:
            continue
        stage_fields = registered.get(stage, {})
        if not stage_fields:
            raise _error(
                "ANALYSIS_UNSUPPORTED_COMBINATION",
                f"该指标不支持 {stage}。",
                f"analysis.{stage}",
                "Use the analysis stage published by datasage_catalog.",
            )
        validated[stage] = [
            _normalize_filter(
                item,
                stage=stage,
                index=index,
                registered=stage_fields,
            )
            for index, item in enumerate(normalized[stage])
        ]
    supported = _validate_supported_combinations(
        metric.get("analysis_supported_combinations"),
        registered,
        path="analysis_supported_combinations",
    )
    if supported:
        requested_rows = {item["field"] for item in validated.get("row_filters", ())}
        requested_groups = {item["field"] for item in validated.get("group_filters", ())}
        if not any(
            requested_rows <= set(item["row_fields"])
            and requested_groups <= set(item["group_fields"])
            for item in supported
        ):
            raise _error(
                "ANALYSIS_UNSUPPORTED_COMBINATION",
                "该指标不支持所请求的 row/group 分析组合。",
                "analysis",
                "Use a combination published by datasage_catalog.",
            )
    return validated


def analysis_capability_projection(
    raw: Any,
    supported_combinations: Any = None,
) -> dict[str, Any] | None:
    """Publish registry facts plus fixed execution semantics in catalog/wire."""

    if raw is None:
        return None
    projected = validate_analysis_fields(
        raw,
        supported_combinations=supported_combinations,
    )
    if projected is None:
        return None
    result: dict[str, Any] = {
        "row_filters": projected.get("row_filters", {}),
        "group_filters": projected.get("group_filters", {}),
        "combination": "AND",
        "unknown_policy": "preserve_and_report",
        "limits": {
            "row_filters": ANALYSIS_MAX_ROW_FILTERS,
            "group_filters": ANALYSIS_MAX_GROUP_FILTERS,
            "total_filters": ANALYSIS_MAX_FILTERS,
        },
    }
    combinations = _validate_supported_combinations(
        supported_combinations,
        projected,
        path="analysis_supported_combinations",
    )
    if combinations:
        result["supported_combinations"] = combinations
    return result


# A read-only alias is useful to callers that build catalog summaries without
# mutating the module-level contract.  It intentionally starts empty: metric
# semantics remain the authoritative registration source.
ANALYSIS_FIELD_REGISTRY = MappingProxyType({})
