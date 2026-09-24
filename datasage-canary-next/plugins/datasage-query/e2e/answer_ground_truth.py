"""Deterministic final-answer checks backed by an independent fixture.

The Golden scorer owns plan/evidence contracts.  This module adds the small
answer layer required by H02 without trying to infer a business answer from a
plan or from a candidate's self-reported assertions.  A caller supplies an
answer text extracted from the official session export and a separately
reviewed Ground Truth fixture.  Numeric checks are deterministic; conclusion
and recommendation boundaries remain ``not_verified`` until an independent
review is supplied.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata
from typing import Any


GROUND_TRUTH_SCHEMA = "datasage-answer-ground-truth/v1"
REVIEW_SCHEMA = "datasage-answer-review/v1"
ANSWER_DIMENSIONS = (
    "numbers",
    "units",
    "arithmetic",
    "table_text_consistency",
    "conclusion_boundary",
)
# These aliases come from the Profile unit contracts.  They are deliberately
# grouped by a governed canonical unit; arbitrary English words are never
# treated as unit evidence.
UNIT_GROUPS = {
    "%": {"%", "％", "百分比", "百分率"},
    "元": {"元", "人民币元", "人民币", "rmb", "¥", "￥"},
    "万元": {"万元", "万人民币"},
    "m": {"米", "m", "ｍ"},
    "y": {"码", "y"},
    "kg": {"公斤", "kg"},
    "Pcs": {"件", "pcs"},
    "m2": {"平方米", "平方", "㎡", "m²", "m2"},
    "tao": {"tao"},
}
UNIT_ALIAS_TO_GROUP = {
    unicodedata.normalize("NFKC", alias).casefold(): group
    for group, aliases in UNIT_GROUPS.items()
    for alias in aliases
}
KNOWN_UNITS = set(UNIT_GROUPS)
UNKNOWN_MARKERS = (
    "未知",
    "缺失",
    "未提供",
    "无法确认",
    "无法核实",
    "不可得",
    "不确定",
    "n/a",
    "na",
    "null",
)
# These deterministic checks recognise bounded assertion forms, not arbitrary
# natural language. Unresolved scope is sent to the existing review path;
# matching a number is never a substitute for independent business review.
NON_ASSERTION_MARKERS = (
    "并不是",
    "不是",
    "并非",
    "非为",
    "不等于",
    "未达到",
    "未达",
    "尚未",
    "没有达到",
    # quoted, illustrative or hypothetical rather than asserted
    "参考",
    "参照",
    "参见",
    "例如",
    "比如",
    "示例",
    "假设",
    "假如",
    "如果",
    "举例",
    # the clause itself declares the value unknown
    "未知",
    "缺失",
    "未提供",
    "无法确认",
    "无法核实",
    "不可得",
    "不确定",
)
UNVERIFIED_SCOPE_RE = re.compile(
    r"假设|假如|如果|示例|举例|比如|例如|仅供参考|原问题|原文|引用|"
    r"可能|或许|大概|预计|估计|暂定|约为|"
    r"尚未(?:核实|确认|验证)|未经(?:核实|确认|验证)|待(?:核实|确认|验证)|"
    r"不正确|不属实|不成立|有误|(?:说法|结论|数据)(?:是)?错误|"
    r"\b(?:hypothetical|suppose|assuming|unverified|unconfirmed|quoted)\b",
    re.IGNORECASE,
)
TRAILING_QUALIFIER_RE = re.compile(
    r"^\s*(?:但|不过|然而)?\s*(?:尚未|未经|待核|待确|无法|不正确|不属实|"
    r"不成立|有误|这个说法|该说法|以上说法|仅供|仅为)"
)
QUOTED_NUMERIC_RE = re.compile(r'“[^”]*[0-9][^”]*”|「[^」]*[0-9][^」]*」|"[^"\n]*[0-9][^"\n]*"')

NUMBER_RE = re.compile(
    r"(?<![0-9.a-z])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![0-9])"
)
HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class AnswerGroundTruthError(ValueError):
    """Raised when a Ground Truth or review fixture is not well formed."""


def _sha256(value: Any) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        import json

        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and HEX_SHA256_RE.fullmatch(value) is not None


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\u00a0", " ").casefold()


def _decimal(value: Any, *, label: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise AnswerGroundTruthError(f"{label} must be a decimal string")
    try:
        result = Decimal(value.strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise AnswerGroundTruthError(f"{label} is not a finite decimal") from exc
    if not result.is_finite():
        raise AnswerGroundTruthError(f"{label} is not a finite decimal")
    return result


def _tolerance(value: Any, *, label: str) -> Decimal:
    result = _decimal(value, label=label)
    if result < 0:
        raise AnswerGroundTruthError(f"{label} must be non-negative")
    return result


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnswerGroundTruthError(f"{label} must be a non-empty string")
    return value.strip()


def _validate_source(source: Any, *, label: str, kinds: set[str]) -> None:
    if not isinstance(source, dict) or set(source) != {
        "kind",
        "reference",
        "artifact_sha256",
    }:
        raise AnswerGroundTruthError(
            f"{label} must contain kind, reference, and artifact_sha256"
        )
    if source.get("kind") not in kinds:
        raise AnswerGroundTruthError(f"{label}.kind is unsupported")
    _require_string(source.get("reference"), label=f"{label}.reference")
    if not _is_sha256(source.get("artifact_sha256")):
        raise AnswerGroundTruthError(f"{label}.artifact_sha256 is invalid")


def _validate_fact(value: Any, *, label: str) -> dict[str, Any]:
    required = {"id", "labels", "value", "unit", "state", "period", "tolerance"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"{label} must contain exactly {sorted(required)!r}"
        )
    fact_id = _require_string(value.get("id"), label=f"{label}.id")
    labels = value.get("labels")
    if (
        not isinstance(labels, list)
        or not labels
        or any(not isinstance(item, str) or not item.strip() for item in labels)
        or len(labels) != len(set(labels))
    ):
        raise AnswerGroundTruthError(f"{label}.labels is invalid")
    state = value.get("state")
    if state not in {"known", "unknown"}:
        raise AnswerGroundTruthError(f"{label}.state is invalid")
    unit = value.get("unit")
    if (
        not isinstance(unit, str)
        or not unit.strip()
        or _fold(unit) not in UNIT_ALIAS_TO_GROUP
    ):
        raise AnswerGroundTruthError(f"{label}.unit is unsupported")
    period = value.get("period")
    if period is not None and (not isinstance(period, str) or not period.strip()):
        raise AnswerGroundTruthError(f"{label}.period is invalid")
    tolerance = _tolerance(value.get("tolerance"), label=f"{label}.tolerance")
    if state == "known":
        numeric = _decimal(value.get("value"), label=f"{label}.value")
        normalized_value: str | None = format(numeric, "f")
    else:
        if value.get("value") is not None:
            raise AnswerGroundTruthError(f"{label}.value must be null for unknown facts")
        normalized_value = None
    return {
        "id": fact_id,
        "labels": [item.strip() for item in labels],
        "value": normalized_value,
        "unit": unit.strip(),
        "state": state,
        "period": period.strip() if isinstance(period, str) else None,
        "tolerance": format(tolerance, "f"),
    }


def _validate_arithmetic(value: Any, *, label: str) -> dict[str, Any]:
    required = {"id", "operation", "operands", "expected_fact", "tolerance"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"{label} must contain exactly {sorted(required)!r}"
        )
    operation = value.get("operation")
    if operation not in {"add", "subtract", "divide", "divide_percent"}:
        raise AnswerGroundTruthError(f"{label}.operation is unsupported")
    operands = value.get("operands")
    if (
        not isinstance(operands, list)
        or len(operands) != 2
        or any(not isinstance(item, str) or not item.strip() for item in operands)
    ):
        raise AnswerGroundTruthError(f"{label}.operands must contain two fact IDs")
    tolerance = _tolerance(value.get("tolerance"), label=f"{label}.tolerance")
    return {
        "id": _require_string(value.get("id"), label=f"{label}.id"),
        "operation": operation,
        "operands": [item.strip() for item in operands],
        "expected_fact": _require_string(
            value.get("expected_fact"), label=f"{label}.expected_fact"
        ),
        "tolerance": format(tolerance, "f"),
    }


def _validate_table_text(value: Any, *, label: str) -> dict[str, Any]:
    required = {"fact_id", "label", "require_table", "require_text"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"{label} must contain exactly {sorted(required)!r}"
        )
    return {
        "fact_id": _require_string(value.get("fact_id"), label=f"{label}.fact_id"),
        "label": _require_string(value.get("label"), label=f"{label}.label"),
        "require_table": value.get("require_table") is True,
        "require_text": value.get("require_text") is True,
    }


def _validate_review_requirements(value: Any, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {"required_labels", "forbidden_labels", "advice_boundary"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"{label} must contain exactly {sorted(required)!r}"
        )
    result: dict[str, Any] = {}
    for key in ("required_labels", "forbidden_labels"):
        values = value.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            raise AnswerGroundTruthError(f"{label}.{key} is invalid")
        result[key] = [item.strip() for item in values]
    advice_boundary = value.get("advice_boundary")
    if advice_boundary is not None and advice_boundary not in {
        "bounded",
        "no_advice",
        "review_required",
    }:
        raise AnswerGroundTruthError(f"{label}.advice_boundary is invalid")
    result["advice_boundary"] = advice_boundary
    return result


def validate_ground_truth(value: Any) -> dict[str, Any]:
    """Validate and normalize an independently supplied Ground Truth fixture."""

    required = {"schema", "fixture_id", "source", "cases"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"ground truth must contain exactly {sorted(required)!r}"
        )
    if value.get("schema") != GROUND_TRUTH_SCHEMA:
        raise AnswerGroundTruthError(f"ground truth schema must be {GROUND_TRUTH_SCHEMA}")
    fixture_id = _require_string(value.get("fixture_id"), label="fixture_id")
    _validate_source(
        value.get("source"),
        label="source",
        kinds={"independent_sql", "manual_fixture", "manual_review"},
    )
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AnswerGroundTruthError("ground truth cases must be a non-empty list")
    normalized_cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(cases):
        label = f"cases[{index}]"
        required_case_keys = {
            "case_id",
            "facts",
            "arithmetic",
            "table_text",
            "review_requirements",
        }
        if not isinstance(item, dict) or set(item) != required_case_keys:
            raise AnswerGroundTruthError(
                f"{label} must contain exactly {sorted(required_case_keys)!r}"
            )
        case_id = _require_string(item.get("case_id"), label=f"{label}.case_id")
        if case_id in ids:
            raise AnswerGroundTruthError(f"duplicate Ground Truth case ID {case_id!r}")
        ids.add(case_id)
        facts = item.get("facts")
        if not isinstance(facts, list) or not facts:
            raise AnswerGroundTruthError(f"{label}.facts must be a non-empty list")
        normalized_facts = [
            _validate_fact(fact, label=f"{label}.facts[{fact_index}]")
            for fact_index, fact in enumerate(facts)
        ]
        fact_ids = {fact["id"] for fact in normalized_facts}
        # F02: a duplicate normalized ID used to collapse two different facts
        # into one entry, so an omitted value could still score "passed".  The
        # normalized ID (already stripped) is what uniqueness is judged on.
        if len(fact_ids) != len(normalized_facts):
            raise AnswerGroundTruthError(f"{label}.facts IDs must be unique")
        arithmetic = item.get("arithmetic")
        if not isinstance(arithmetic, list):
            raise AnswerGroundTruthError(f"{label}.arithmetic must be a list")
        normalized_arithmetic = [
            _validate_arithmetic(entry, label=f"{label}.arithmetic[{entry_index}]")
            for entry_index, entry in enumerate(arithmetic)
        ]
        arithmetic_ids = {entry["id"] for entry in normalized_arithmetic}
        if len(arithmetic_ids) != len(normalized_arithmetic):
            raise AnswerGroundTruthError(f"{label}.arithmetic IDs must be unique")
        for entry in normalized_arithmetic:
            if any(operand not in fact_ids for operand in entry["operands"]):
                raise AnswerGroundTruthError(
                    f"{label}.arithmetic references an unknown operand"
                )
            if entry["expected_fact"] not in fact_ids:
                raise AnswerGroundTruthError(
                    f"{label}.arithmetic references an unknown expected fact"
                )
        table_text = item.get("table_text")
        if not isinstance(table_text, list):
            raise AnswerGroundTruthError(f"{label}.table_text must be a list")
        normalized_table_text = [
            _validate_table_text(entry, label=f"{label}.table_text[{entry_index}]")
            for entry_index, entry in enumerate(table_text)
        ]
        for entry in normalized_table_text:
            if entry["fact_id"] not in fact_ids:
                raise AnswerGroundTruthError(
                    f"{label}.table_text references an unknown fact"
                )
        normalized_cases.append(
            {
                "case_id": case_id,
                "facts": normalized_facts,
                "arithmetic": normalized_arithmetic,
                "table_text": normalized_table_text,
                "review_requirements": _validate_review_requirements(
                    item.get("review_requirements"),
                    label=f"{label}.review_requirements",
                ),
            }
        )
    return {
        "schema": GROUND_TRUTH_SCHEMA,
        "fixture_id": fixture_id,
        "source": dict(value["source"]),
        "cases": normalized_cases,
    }


def validate_answer_review(value: Any) -> dict[str, Any]:
    """Validate a manually reviewed conclusion/advice sidecar."""

    required = {"schema", "source", "session_export_sha256", "cases"}
    if not isinstance(value, dict) or set(value) != required:
        raise AnswerGroundTruthError(
            f"answer review must contain exactly {sorted(required)!r}"
        )
    if value.get("schema") != REVIEW_SCHEMA:
        raise AnswerGroundTruthError(f"answer review schema must be {REVIEW_SCHEMA}")
    _validate_source(
        value.get("source"),
        label="answer review source",
        kinds={"manual_review"},
    )
    if not _is_sha256(value.get("session_export_sha256")):
        raise AnswerGroundTruthError("answer review session_export_sha256 is invalid")
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise AnswerGroundTruthError("answer review cases must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(cases):
        label = f"answer review cases[{index}]"
        required_case_keys = {
            "case_id",
            "status",
            "reviewer_id",
            "labels",
            "advice_boundary",
            "final_answer_sha256",
        }
        if not isinstance(item, dict) or set(item) != required_case_keys:
            raise AnswerGroundTruthError(
                f"{label} must contain exactly {sorted(required_case_keys)!r}"
            )
        case_id = _require_string(item.get("case_id"), label=f"{label}.case_id")
        if case_id in seen:
            raise AnswerGroundTruthError(f"duplicate answer review case ID {case_id!r}")
        seen.add(case_id)
        if item.get("status") not in {"reviewed", "not_reviewed"}:
            raise AnswerGroundTruthError(f"{label}.status is invalid")
        reviewer_id = _require_string(item.get("reviewer_id"), label=f"{label}.reviewer_id")
        labels = item.get("labels")
        if (
            not isinstance(labels, list)
            or any(not isinstance(entry, str) or not entry.strip() for entry in labels)
            or len(labels) != len(set(labels))
        ):
            raise AnswerGroundTruthError(f"{label}.labels is invalid")
        advice_boundary = item.get("advice_boundary")
        if advice_boundary not in {None, "bounded", "no_advice", "review_required", "auto_execute"}:
            raise AnswerGroundTruthError(f"{label}.advice_boundary is invalid")
        if not _is_sha256(item.get("final_answer_sha256")):
            raise AnswerGroundTruthError(f"{label}.final_answer_sha256 is invalid")
        normalized.append(
            {
                "case_id": case_id,
                "status": item["status"],
                "reviewer_id": reviewer_id,
                "labels": [entry.strip() for entry in labels],
                "advice_boundary": advice_boundary,
                "final_answer_sha256": item["final_answer_sha256"],
            }
        )
    return {
        "schema": REVIEW_SCHEMA,
        "source": dict(value["source"]),
        "session_export_sha256": value["session_export_sha256"],
        "cases": normalized,
    }


def _unit_group(unit: str) -> str | None:
    return UNIT_ALIAS_TO_GROUP.get(_fold(unit))


def _parse_numbers(text: str) -> list[Decimal]:
    result: list[Decimal] = []
    for match in NUMBER_RE.finditer(_fold(text)):
        try:
            value = Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        if value.is_finite():
            result.append(value)
    return result


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def _lines_for_fact(
    text: str, fact: dict[str, Any], *, table: bool | None = None
) -> list[str]:
    labels = [_fold(label) for label in fact["labels"]]
    lines = text.splitlines() or [text]
    return [
        line
        for line in lines
        if (table is None or _is_table_line(line) is table)
        and any(label in _fold(line) for label in labels)
    ]


def _period_tokens(text: str) -> set[str]:
    folded = _fold(text)
    tokens: set[str] = set()
    for year, month in re.findall(r"(\d{4})-(\d{1,2})", folded):
        tokens.add(f"{year}-{int(month):02d}")
    for year, month in re.findall(r"(\d{4})年(\d{1,2})月", folded):
        tokens.add(f"{year}-{int(month):02d}")
    for year, month in re.findall(r"(\d{4})[/.](\d{1,2})", folded):
        tokens.add(f"{year}-{int(month):02d}")
    return tokens


def _period_status(
    fact: dict[str, Any], occurrence: dict[str, Any]
) -> str:
    period = fact.get("period")
    if period is None:
        return "passed"
    expected = _period_tokens(period)
    if len(expected) != 1:
        raise AnswerGroundTruthError(
            f"fact {fact.get('id')!r} period is not a supported YYYY-MM value"
        )
    expected_period = next(iter(expected))
    observed = set().union(*(atom["periods"] for atom in occurrence["atoms"]))
    if not observed:
        return "not_verified"
    if expected_period not in observed:
        return "failed"
    return "passed" if observed == {expected_period} else "not_verified"


def _line_unit_ok(line: str, expected: str) -> tuple[bool, bool]:
    folded = _fold(line)
    aliases = UNIT_ALIAS_TO_GROUP
    # Match longer aliases first so ``万元`` is not misread as the ``元``
    # suffix.  Unit aliases are intentionally a small governed vocabulary.
    found: set[str] = set()
    spans: list[tuple[int, int]] = []
    for token, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.fullmatch(r"[a-z0-9]+", token):
            # Numeric adjacency is a valid unit spelling (``100y``, ``2m2``),
            # while a letter boundary prevents matches inside ordinary words.
            pattern = rf"(?<![a-z]){re.escape(token)}(?![a-z0-9])"
            matches = list(re.finditer(pattern, folded))
        else:
            matches = list(re.finditer(re.escape(token), folded))
        for match in matches:
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in spans):
                continue
            spans.append(span)
            found.add(canonical)
    expected_group = _unit_group(expected)
    return expected_group in found, bool(found.difference({expected_group}))


def _label_atoms(
    line: str, fact: dict[str, Any], *, table: bool
) -> list[dict[str, Any]]:
    """Return only the table cell or prose clause attached to a fact label."""

    folded = _fold(line)
    labels = sorted((_fold(label) for label in fact["labels"]), key=len, reverse=True)
    atoms: list[dict[str, Any]] = []
    if table:
        cells = line.split("|")
        for index, cell in enumerate(cells):
            folded_cell = _fold(cell)
            if not any(label in folded_cell for label in labels):
                continue
            # A normal Markdown row puts the value in the next cell.  Keeping
            # one adjacent cell also supports ``目标: 100 万元`` rows without
            # swallowing numbers from another metric.
            value_cell = cells[index + 1] if index + 1 < len(cells) else ""
            atom = f"{cell} {value_cell}"
            atoms.append({"text": atom, "scope": atom, "periods": _period_tokens(atom)})
        return atoms

    delimiters_after = "，,；;。！？!?|\n"
    delimiters_before = "，,；;。！？!?|：:\n"
    sentence_boundaries = "；;。！？!?\n"

    def next_delimiter(start: int) -> int:
        for index in range(start, len(folded)):
            token = folded[index]
            if token not in delimiters_after:
                continue
            # An ASCII comma between digits is a thousands separator, not a
            # prose boundary (``1,000`` must stay one atomic value).
            if (
                token == ","
                and index > 0
                and index + 1 < len(folded)
                and folded[index - 1].isdigit()
                and folded[index + 1].isdigit()
            ):
                continue
            return index
        return len(folded)

    for label in labels:
        start = 0
        while True:
            position = folded.find(label, start)
            if position < 0:
                break
            before_candidates = [
                folded.rfind(token, 0, position) for token in delimiters_before
            ]
            before = max(before_candidates, default=-1)
            after = next_delimiter(position + len(label))
            scope = folded[before + 1 : after]
            sentence_before = max(
                (folded.rfind(token, 0, position) for token in sentence_boundaries),
                default=-1,
            )
            sentence_after_candidates = [
                folded.find(token, position + len(label))
                for token in sentence_boundaries
            ]
            sentence_after_candidates = [
                value for value in sentence_after_candidates if value >= 0
            ]
            sentence_after = (
                min(sentence_after_candidates)
                if sentence_after_candidates
                else len(folded)
            )
            period_scope = folded[sentence_before + 1 : sentence_after]
            # Start at the label itself.  A period prefix such as
            # ``2026年9月`` belongs to scope checking, not to the value tied
            # to this label.
            atoms.append(
                {
                    # NFKC/case folding can change string length. Keep offsets
                    # and slices in the same buffer so leading digits survive.
                    "text": folded[position:after],
                    # Retain the surrounding proposition for assertion scope;
                    # numbers still come only from the label-attached clause.
                    "scope": scope,
                    "question": sentence_after < len(folded) and folded[sentence_after] in "？?",
                    "conditional_prefix": (
                        folded[sentence_before + 1:position]
                        if UNVERIFIED_SCOPE_RE.search(folded[sentence_before + 1:position])
                        else ""
                    ),
                    "qualifier": (
                        folded[after + 1:sentence_after]
                        if after < sentence_after and TRAILING_QUALIFIER_RE.match(
                            folded[after + 1:sentence_after]
                        ) else ""
                    ),
                    "periods": _period_tokens(period_scope),
                }
            )
            start = position + len(label)
    return atoms


def _assertion_connector(text: str) -> str:
    """Return the clause text between the fact label and its first number."""

    folded = _fold(text)
    match = NUMBER_RE.search(folded)
    return folded if match is None else folded[: match.start()]


def _asserts_its_value(text: str, scope: str = "") -> bool:
    """Recognise a bounded positive assertion; never claim full NLP coverage."""

    folded = _fold(scope or text)
    connector = _assertion_connector(text)
    if re.search(r"至少|至多|超过|大于|小于|不低于|不高于|[<>≥≤≠]", connector):
        return False
    if any(marker in connector for marker in NON_ASSERTION_MARKERS):
        return False
    if "?" in folded or UNVERIFIED_SCOPE_RE.search(folded) or QUOTED_NUMERIC_RE.search(folded):
        return False
    return True


def _fact_bindings(text: str, fact: dict[str, Any], table: bool | None):
    """Keep the heading of a contiguous Markdown table as assertion context.

    A preceding hypothetical/quoted heading is not a verified table result.
    This is contextual evidence only; it never supplies numeric cell values.
    """
    previous_nonempty = ""
    table_context = ""
    in_table = False
    for line in text.splitlines() or [text]:
        is_table = _is_table_line(line)
        if is_table and not in_table:
            table_context = previous_nonempty
        if not is_table and line.strip():
            previous_nonempty = line
        in_table = is_table or (in_table and not line.strip())
        if table is not None and is_table is not table:
            continue
        for binding in _label_atoms(line, fact, table=is_table):
            scope = binding.get("scope", binding["text"])
            if binding.get("conditional_prefix"):
                scope = binding["conditional_prefix"] + " " + scope
            if binding.get("qualifier"):
                scope += " " + binding["qualifier"]
            if binding.get("question"):
                scope += "?"
            if is_table and (
                UNVERIFIED_SCOPE_RE.search(_fold(table_context))
                or any(marker in _fold(table_context) for marker in UNKNOWN_MARKERS)
            ):
                scope = table_context + " " + scope
            binding["scope"] = scope
            yield binding


def _fact_occurrence(
    text: str, fact: dict[str, Any], *, table: bool | None = None
) -> dict[str, Any]:
    lines = _lines_for_fact(text, fact, table=table)
    atoms: list[dict[str, Any]] = []
    for atom_binding in _fact_bindings(text, fact, table):
        atom = atom_binding["text"]
        scope = atom_binding["scope"]
        numbers = _parse_numbers(atom)
        expected_unit, other_unit = _line_unit_ok(atom, fact["unit"])
        atoms.append(
            {
                "text": atom,
                "periods": set(atom_binding["periods"]),
                "numbers": numbers,
                "unit_ok": expected_unit,
                "wrong_unit": other_unit,
                "unknown": any(marker in _fold(atom) for marker in UNKNOWN_MARKERS),
                "asserted": _asserts_its_value(atom, scope),
                "uncertain": "?" in _fold(scope) or bool(UNVERIFIED_SCOPE_RE.search(_fold(scope)))
                    or bool(QUOTED_NUMERIC_RE.search(_fold(scope)))
                    or any(marker in _fold(scope) for marker in UNKNOWN_MARKERS),
            }
        )
    # A heading such as ``2026年9月目标完成情况`` contains date digits but no
    # unit and is not an answer value.  If a fact has a unit-bearing value (or
    # an explicitly wrong unit) elsewhere, discard such context atoms while
    # keeping unit-less answers detectable when they are the only occurrence.
    if any(
        atom["numbers"] and (atom["unit_ok"] or atom["wrong_unit"])
        for atom in atoms
    ):
        atoms = [
            atom
            for atom in atoms
            if not (
                atom["numbers"]
                and not atom["unit_ok"]
                and not atom["wrong_unit"]
            )
        ]
    numbers = [number for atom in atoms for number in atom["numbers"]]
    numeric_atoms = [atom for atom in atoms if atom["numbers"]]
    asserted_atoms = [atom for atom in numeric_atoms if atom["asserted"]]
    checked_atoms = asserted_atoms or numeric_atoms
    asserted_numbers = [
        number for atom in atoms if atom["asserted"] for number in atom["numbers"]
    ]
    return {
        "lines": lines,
        "atoms": atoms,
        "numbers": numbers,
        "numeric_atoms": numeric_atoms,
        "unasserted": bool(numbers) and not asserted_numbers,
        "asserted_numbers": asserted_numbers,
        "needs_review": any(atom["uncertain"] for atom in atoms),
        "ambiguous": any(len(atom["numbers"]) != 1 for atom in checked_atoms),
        "unit_missing": any(not atom["unit_ok"] for atom in checked_atoms),
        "wrong_unit": any(atom["wrong_unit"] for atom in checked_atoms),
        "unknown": any(atom["unknown"] for atom in atoms),
    }


def _near(value: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    return abs(value - expected) <= tolerance


def _dimension(status: str, errors: list[str], *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "passed": status == "passed",
        "errors": errors,
        **({"details": details} if details else {}),
    }


def _compute_arithmetic(
    operation: str, left: Decimal, right: Decimal
) -> Decimal | None:
    if operation == "add":
        return left + right
    if operation == "subtract":
        return left - right
    if operation == "divide":
        if right == 0:
            return None
        return left / right
    if operation == "divide_percent":
        if right == 0:
            return None
        return left / right * Decimal("100")
    return None


def score_answer_case(
    case: dict[str, Any],
    answer_text: str,
    *,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one answer against normalized Ground Truth and optional review."""

    if not isinstance(answer_text, str) or not answer_text.strip():
        raise AnswerGroundTruthError("answer text must be non-empty")
    facts = {fact["id"]: fact for fact in case["facts"]}
    number_errors: list[str] = []
    number_unverified: list[str] = []
    unit_errors: list[str] = []
    occurrences: dict[str, dict[str, Any]] = {}
    for fact_id, fact in facts.items():
        occurrence = _fact_occurrence(answer_text, fact)
        occurrences[fact_id] = occurrence
        if occurrence["ambiguous"]:
            number_unverified.append(
                f"fact {fact_id!r} has multiple numeric values in one attached statement"
            )
            continue
        period_status = _period_status(fact, occurrence)
        if period_status == "failed":
            number_errors.append(
                f"fact {fact_id!r} is attached to a different period than {fact['period']!r}"
            )
        elif period_status == "not_verified":
            number_unverified.append(
                f"fact {fact_id!r} period {fact['period']!r} is not uniquely bound to its statement"
            )
        if fact["state"] == "unknown":
            if not occurrence["lines"]:
                number_errors.append(f"fact {fact_id!r} label is missing")
            elif any(_near(value, Decimal("0"), fact_tolerance(fact)) for value in occurrence["numbers"]):
                number_errors.append(f"fact {fact_id!r} is unknown but answer renders zero")
            elif occurrence["numbers"]:
                number_errors.append(f"fact {fact_id!r} has an unexplained numeric value")
            elif not occurrence["unknown"]:
                number_errors.append(f"fact {fact_id!r} is unknown but answer has no unknown marker")
            if occurrence["wrong_unit"] or occurrence["unit_missing"]:
                unit_errors.append(f"fact {fact_id!r} uses an unexpected unit")
            continue
        expected = _decimal(fact["value"], label=f"fact {fact_id}.value")
        tolerance = fact_tolerance(fact)
        if not occurrence["lines"]:
            number_errors.append(f"fact {fact_id!r} label is missing")
        elif occurrence["unasserted"] or occurrence["needs_review"]:
            # F04: ``目标并不是100万元`` / ``目标未知（参考100万元）`` must not
            # pass the numeric dimension.  Denial, quotation and an explicitly
            # unknown statement are ambiguous for a scorer, so they go to the
            # existing human-review path instead of being reported as answered.
            number_unverified.append(
                f"fact {fact_id!r} value is only denied, quoted or declared unknown"
            )
        elif not occurrence["asserted_numbers"]:
            number_errors.append(f"fact {fact_id!r} numeric value is missing")
        elif not all(_near(value, expected, tolerance) for value in occurrence["asserted_numbers"]):
            number_errors.append(
                f"fact {fact_id!r} has a value outside tolerance of {format(expected, 'f')}"
            )
        if occurrence["unit_missing"]:
            unit_errors.append(f"fact {fact_id!r} is missing unit {fact['unit']!r}")
        if occurrence["wrong_unit"]:
            unit_errors.append(f"fact {fact_id!r} uses an unexpected or mixed unit")

    arithmetic_errors: list[str] = []
    arithmetic_unverified: list[str] = []
    for entry in case["arithmetic"]:
        left_fact, right_fact = (facts[item] for item in entry["operands"])
        expected_fact = facts[entry["expected_fact"]]
        if any(fact["state"] != "known" for fact in (left_fact, right_fact, expected_fact)):
            arithmetic_unverified.append(
                f"arithmetic {entry['id']!r} uses an unknown Ground Truth fact"
            )
            continue
        left = _decimal(left_fact["value"], label=f"fact {left_fact['id']}.value")
        right = _decimal(right_fact["value"], label=f"fact {right_fact['id']}.value")
        computed = _compute_arithmetic(entry["operation"], left, right)
        if computed is None:
            arithmetic_unverified.append(
                f"arithmetic {entry['id']!r} cannot be computed from Ground Truth"
            )
            continue
        expected = _decimal(expected_fact["value"], label=f"fact {expected_fact['id']}.value")
        tolerance = _tolerance(entry["tolerance"], label=f"arithmetic {entry['id']}.tolerance")
        if not _near(computed, expected, tolerance):
            raise AnswerGroundTruthError(
                f"arithmetic {entry['id']!r} does not agree with its Ground Truth facts"
            )
        observed_occurrence = occurrences[entry["expected_fact"]]
        observed = observed_occurrence["asserted_numbers"]
        if any(
            occurrences[fact["id"]]["ambiguous"]
            or occurrences[fact["id"]]["unasserted"]
            or occurrences[fact["id"]]["needs_review"]
            for fact in (left_fact, right_fact, expected_fact)
        ):
            arithmetic_unverified.append(
                f"arithmetic {entry['id']!r} has an ambiguous answer value"
            )
        elif not observed or not all(_near(value, computed, tolerance) for value in observed):
            arithmetic_errors.append(
                f"arithmetic {entry['id']!r} does not match {format(computed, 'f')}"
            )

    table_errors: list[str] = []
    table_unverified: list[str] = []
    for entry in case["table_text"]:
        fact = facts[entry["fact_id"]]
        table_occurrence = _fact_occurrence(answer_text, fact, table=True)
        text_occurrence = _fact_occurrence(answer_text, fact, table=False)
        if any(
            occurrence["ambiguous"] or occurrence["unasserted"] or occurrence["needs_review"]
            for occurrence in (table_occurrence, text_occurrence)
        ):
            table_unverified.append(
                f"fact {entry['fact_id']!r} has an ambiguous table or prose value"
            )
            continue
        if entry["require_table"] and not table_occurrence["asserted_numbers"]:
            table_errors.append(f"fact {entry['fact_id']!r} is missing from the table")
        if entry["require_text"] and not text_occurrence["asserted_numbers"]:
            table_errors.append(f"fact {entry['fact_id']!r} is missing from prose")
        if table_occurrence["asserted_numbers"] and text_occurrence["asserted_numbers"]:
            tolerance = fact_tolerance(fact)
            if not all(
                _near(table_value, text_value, tolerance)
                for table_value in table_occurrence["asserted_numbers"]
                for text_value in text_occurrence["asserted_numbers"]
            ):
                table_errors.append(
                    f"fact {entry['fact_id']!r} differs between table and prose"
                )

    if case.get("review_requirements") is None or review is None:
        conclusion = _dimension(
            "not_verified",
            ["independent conclusion/advice review is not supplied"],
        )
    else:
        requirements = case["review_requirements"]
        if review.get("status") != "reviewed":
            conclusion = _dimension(
                "not_verified",
                ["independent conclusion/advice review is not marked reviewed"],
            )
        else:
            labels = set(review.get("labels", []))
            errors: list[str] = []
            missing = set(requirements["required_labels"]).difference(labels)
            forbidden = labels.intersection(requirements["forbidden_labels"])
            if missing:
                errors.append(f"review is missing required conclusion labels {sorted(missing)!r}")
            if forbidden:
                errors.append(f"review contains forbidden conclusion labels {sorted(forbidden)!r}")
            expected_boundary = requirements.get("advice_boundary")
            if expected_boundary is not None and review.get("advice_boundary") != expected_boundary:
                errors.append(
                    f"review advice boundary={review.get('advice_boundary')!r}, expected {expected_boundary!r}"
                )
            conclusion = _dimension("failed" if errors else "passed", errors)

    if not case["arithmetic"]:
        arithmetic_status = "not_verified"
        arithmetic_messages = ["Ground Truth contains no arithmetic assertions"]
    elif arithmetic_errors:
        arithmetic_status = "failed"
        arithmetic_messages = arithmetic_errors
    elif arithmetic_unverified:
        arithmetic_status = "not_verified"
        arithmetic_messages = arithmetic_unverified
    else:
        arithmetic_status = "passed"
        arithmetic_messages = []
    if not case["table_text"]:
        table_status = "not_verified"
        table_messages = ["Ground Truth contains no table/text assertions"]
    elif table_errors:
        table_status = "failed"
        table_messages = table_errors
    elif table_unverified:
        table_status = "not_verified"
        table_messages = table_unverified
    else:
        table_status = "passed"
        table_messages = []
    if number_errors:
        number_status = "failed"
        number_messages = number_errors
    elif number_unverified:
        number_status = "not_verified"
        number_messages = number_unverified
    else:
        number_status = "passed"
        number_messages = []
    return {
        "schema": "datasage-final-answer-score/v1",
        "case_id": case["case_id"],
        "dimensions": {
            "numbers": _dimension(number_status, number_messages),
            "units": _dimension("failed" if unit_errors else "passed", unit_errors),
            "arithmetic": _dimension(arithmetic_status, arithmetic_messages),
            "table_text_consistency": _dimension(table_status, table_messages),
            "conclusion_boundary": conclusion,
        },
    }


def fact_tolerance(fact: dict[str, Any]) -> Decimal:
    return _tolerance(fact["tolerance"], label=f"fact {fact.get('id')}.tolerance")


def answer_result_status(result: dict[str, Any]) -> str:
    dimensions = result.get("dimensions", {})
    statuses = [dimension.get("status") for dimension in dimensions.values()]
    if "failed" in statuses:
        return "failed"
    if any(status == "not_verified" for status in statuses):
        return "not_verified"
    return "passed"


__all__ = [
    "ANSWER_DIMENSIONS",
    "AnswerGroundTruthError",
    "GROUND_TRUTH_SCHEMA",
    "REVIEW_SCHEMA",
    "answer_result_status",
    "score_answer_case",
    "validate_answer_review",
    "validate_ground_truth",
]
