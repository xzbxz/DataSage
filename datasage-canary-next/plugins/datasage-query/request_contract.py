"""Pure public request constraints shared by JSON Schema and runtime validation.

This module owns only transport-level request facts: bounded non-blank strings,
public batch sizes, and the query envelope/calculation reference contract.
Domain ownership and domain-specific enum values remain in
``capability_contract``; metric semantics remain in the versioned contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PUBLIC_REQUEST_LIMIT = 10
PUBLIC_CALCULATION_LIMIT = 10
PUBLIC_ROW_LIMIT_MIN = 1
PUBLIC_ROW_LIMIT_MAX = 100
MAX_GROUP_DIMENSIONS = 5
MAX_METRIC_FILTERS = 12
MAX_FILTER_VALUES = 50
ORDER_BY_FIELDS = ("field", "direction")
ORDER_BY_DIRECTIONS = ("asc", "desc")
ORDER_BY_FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


@dataclass(frozen=True)
class StringContract:
    min_length: int
    max_length: int
    require_nonblank: bool = True

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "string",
            "minLength": self.min_length,
            "maxLength": self.max_length,
        }
        if self.require_nonblank:
            # JSON Schema ``pattern`` uses search semantics. Requiring one
            # non-whitespace character matches the runtime ``strip`` check.
            result["pattern"] = r"\S"
        return result


REQUEST_ID = StringContract(1, 64)
METRIC_CODE = StringContract(1, 100)
DIMENSION_CODE = StringContract(1, 80)
CURRENCY_TOKEN = StringContract(1, 80)

QUERY_ENVELOPE_FIELDS = frozenset({"requests", "calculations"})
CALCULATION_FIELDS = frozenset(
    {
        "calculation_id",
        "operation",
        "left_request_id",
        "right_request_id",
    }
)
CALCULATION_OPERATIONS = ("difference", "ratio", "share")


class RequestContractError(ValueError):
    """Stable non-sensitive public request validation failure."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = "INVALID_INPUT"
        self.message = message
        self.path = path
        self.hint = hint


@dataclass(frozen=True)
class ValidatedQueryEnvelope:
    requests: tuple[dict[str, Any], ...]
    request_ids: tuple[str, ...]
    calculations: tuple[dict[str, str], ...]


def valid_string(value: Any, contract: StringContract) -> bool:
    if not isinstance(value, str):
        return False
    if not contract.min_length <= len(value) <= contract.max_length:
        return False
    return not contract.require_nonblank or bool(value.strip())


def valid_public_row_limit(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and PUBLIC_ROW_LIMIT_MIN <= value <= PUBLIC_ROW_LIMIT_MAX
    )


def validate_calculations(
    raw_calculations: Any,
    request_ids: tuple[str, ...],
    *,
    supplied: bool,
) -> tuple[dict[str, str], ...]:
    if not supplied:
        return ()
    if (
        not isinstance(raw_calculations, list)
        or not 1 <= len(raw_calculations) <= PUBLIC_CALCULATION_LIMIT
    ):
        raise RequestContractError(
            f"calculations 必须包含 1 到 {PUBLIC_CALCULATION_LIMIT} 个受治理计算。",
            path="calculations",
            hint="Place calculations once at the top level beside requests.",
        )

    known_request_ids = set(request_ids)
    calculation_ids: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(raw_calculations):
        calculation_path = f"calculations[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != CALCULATION_FIELDS:
            supplied_fields = set(raw) if isinstance(raw, Mapping) else set()
            unexpected = sorted(supplied_fields - CALCULATION_FIELDS)
            missing = sorted(CALCULATION_FIELDS - supplied_fields)
            field_path = (
                f"{calculation_path}.{unexpected[0]}"
                if unexpected
                else f"{calculation_path}.{missing[0]}"
                if missing
                else calculation_path
            )
            raise RequestContractError(
                "每个 calculation 只接受固定操作和两个 request_id 引用。",
                path=field_path,
                hint=(
                    f"Remove unsupported field '{unexpected[0]}'."
                    if unexpected
                    else f"Add required field '{missing[0]}'."
                    if missing
                    else "Use a calculation object with the four documented fields."
                ),
            )
        calculation = {key: raw.get(key) for key in CALCULATION_FIELDS}
        identifier_fields = (
            "calculation_id",
            "left_request_id",
            "right_request_id",
        )
        invalid_identifier = next(
            (
                field
                for field in identifier_fields
                if not valid_string(calculation.get(field), REQUEST_ID)
            ),
            None,
        )
        if invalid_identifier is not None:
            raise RequestContractError(
                "calculation 标识必须是 1 到 64 个字符的非空白字符串。",
                path=f"{calculation_path}.{invalid_identifier}",
                hint="Use a non-blank request identifier of at most 64 characters.",
            )
        operation = calculation.get("operation")
        if operation not in CALCULATION_OPERATIONS:
            raise RequestContractError(
                "不支持该受治理计算操作。",
                path=f"{calculation_path}.operation",
                hint=f"Use one of: {', '.join(CALCULATION_OPERATIONS)}.",
            )

        calculation_id = str(calculation["calculation_id"])
        if calculation_id in calculation_ids:
            raise RequestContractError(
                "calculation_id 必须唯一。",
                path=f"{calculation_path}.calculation_id",
                hint="Use a unique calculation_id within this tool call.",
            )
        invalid_reference = next(
            (
                field
                for field in ("left_request_id", "right_request_id")
                if calculation[field] not in known_request_ids
            ),
            None,
        )
        if invalid_reference is not None:
            raise RequestContractError(
                "计算操作数必须引用本批次中的 request_id。",
                path=f"{calculation_path}.{invalid_reference}",
                hint="Reference a request_id present in the top-level requests array.",
            )
        calculation_ids.add(calculation_id)
        normalized.append(
            {
                "calculation_id": calculation_id,
                "operation": str(operation),
                "left_request_id": str(calculation["left_request_id"]),
                "right_request_id": str(calculation["right_request_id"]),
            }
        )
    return tuple(normalized)


def validate_query_envelope(args: Any) -> ValidatedQueryEnvelope:
    if not isinstance(args, dict):
        raise RequestContractError(
            "查询参数只接受 requests 和可选 calculations。",
            path="$",
            hint="Send one JSON object with a requests array.",
        )
    unexpected = sorted(set(args) - QUERY_ENVELOPE_FIELDS)
    if unexpected:
        raise RequestContractError(
            "查询参数只接受 requests 和可选 calculations。",
            path=unexpected[0],
            hint=f"Remove unsupported top-level field '{unexpected[0]}'.",
        )
    if "requests" not in args:
        raise RequestContractError(
            "查询参数缺少 requests。",
            path="requests",
            hint="Add a non-empty top-level requests array.",
        )

    raw_requests = args.get("requests")
    if (
        not isinstance(raw_requests, list)
        or not 1 <= len(raw_requests) <= PUBLIC_REQUEST_LIMIT
    ):
        raise RequestContractError(
            f"requests 必须包含 1 到 {PUBLIC_REQUEST_LIMIT} 个查询。",
            path="requests",
            hint=f"Provide between 1 and {PUBLIC_REQUEST_LIMIT} request objects.",
        )

    requests: list[dict[str, Any]] = []
    request_ids: list[str] = []
    for index, raw_request in enumerate(raw_requests):
        request_path = f"requests[{index}]"
        if not isinstance(raw_request, dict):
            raise RequestContractError(
                "每个查询请求必须是对象。",
                path=request_path,
                hint="Replace this value with a request object.",
            )
        request = dict(raw_request)
        request_id = request.get("request_id")
        if not valid_string(request_id, REQUEST_ID):
            raise RequestContractError(
                "每个 request_id 必须是 1 到 64 个字符的非空白字符串。",
                path=f"{request_path}.request_id",
                hint="Use a unique non-blank request_id of at most 64 characters.",
            )
        requests.append(request)
        request_ids.append(str(request_id))

    if len(set(request_ids)) != len(request_ids):
        duplicate = next(
            request_id
            for index, request_id in enumerate(request_ids)
            if request_id in request_ids[:index]
        )
        duplicate_index = request_ids.index(duplicate, request_ids.index(duplicate) + 1)
        raise RequestContractError(
            "同一次调用中的 request_id 必须唯一。",
            path=f"requests[{duplicate_index}].request_id",
            hint="Use a unique request_id within this tool call.",
        )

    calculations = validate_calculations(
        args.get("calculations"),
        tuple(request_ids),
        supplied="calculations" in args,
    )
    return ValidatedQueryEnvelope(
        requests=tuple(requests),
        request_ids=tuple(request_ids),
        calculations=calculations,
    )
