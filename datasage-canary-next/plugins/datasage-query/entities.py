"""Deterministic entity normalization and bounded master-data candidates."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from functools import lru_cache
from typing import Any, Mapping, Sequence

from . import contract_store, contracts, db_runtime, sql_identifiers
from .capability_contract import (
    ATTRIBUTION_MODES,
    DOMAIN_SOURCES,
    ENTITY_NORMALIZATION_STEPS,
    ENTITY_RESOLVE_DEFAULT_LIMIT,
    ENTITY_RESOLVE_HARD_LIMIT,
    ENTITY_RUNTIME_POLICY,
    ENTITY_TYPES,
    SUPPORTED_DOMAINS,
)


_REGISTRY_PATH = "plugins/datasage-query/contracts/entity-registry.yaml"
_DOMAINS = set(SUPPORTED_DOMAINS)
_ARGUMENTS = {
    "token",
    "entity_types",
    "domain",
    "metric",
    "attribution_mode",
    "limit",
}
_SEMANTIC_PATHS = {
    domain: DOMAIN_SOURCES[domain]["semantics"] for domain in SUPPORTED_DOMAINS
}
_REGISTRY_DEPENDENCIES = (
    _REGISTRY_PATH,
    "plugins/datasage-query/contracts/datasets.yaml",
    *_SEMANTIC_PATHS.values(),
)

# Database-owned entity text is model-visible only through the public resolver
# projection.  Keep these limits local to the projection so the canonical
# identity values used by the governed query binder remain lossless.
PUBLIC_CANDIDATE_FIELD_MAX_CHARS = 128
PUBLIC_CANDIDATE_FIELD_MAX_BYTES = 512
PUBLIC_CANDIDATE_TOTAL_BYTES = 8192
_UNTRUSTED_CANDIDATE_MARKER = "untrusted_entity_metadata"
_PUBLIC_CANDIDATE_TEXT_FIELDS = (
    "entity_type",
    "canonical_id",
    "canonical_code",
    "display_name",
    "matched_value",
)
_PUBLIC_CANDIDATE_LIST_FIELDS = ("filter_values", "filter_role_candidates")

# Process-private provenance through the existing binder. JSON does not carry
# this type, and user-supplied trust flags cannot create it.
_DISPLAY_NAME_ISSUER = object()


class _GovernedDisplayName(str):
    __slots__ = ("_origin",)

    def __new__(cls, value: str, issuer=None, origin=None):
        if issuer is not _DISPLAY_NAME_ISSUER:
            raise ValueError("governed names are issued only by the entity reader")
        result = super().__new__(cls, value)
        object.__setattr__(result, "_origin", origin)
        return result

    def __setattr__(self, key, value):
        raise AttributeError("governed display provenance is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def is_governed_display_name(value: Any, resolved=None, definition=None) -> bool:
    """Only a non-fallback name from the governed master-data reader qualifies."""
    if type(value) is not _GovernedDisplayName:
        return False
    if resolved is None:
        return True
    if not isinstance(resolved, Mapping) or not isinstance(definition, Mapping):
        return False
    entity_type, canonical_id, canonical_code, source_table, display_column = value._origin
    source = _registry()["candidate_sources"].get(entity_type, {})
    identity = definition.get("identity_filter") or {}
    return (
        resolved.get("entity_type") == entity_type
        and identity.get("entity_type") == entity_type
        and source.get("table") == source_table
        and source.get("display_column") == display_column
        and display_column not in {source.get("id_column"), source.get("code_column")}
        and canonical_id in {str(v) for v in resolved.get("canonical_ids", []) if v is not None}
        and canonical_code in {str(v) for v in resolved.get("canonical_codes", []) if v is not None}
    )



class EntityFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _registry() -> dict[str, Any]:
    signature = []
    try:
        for relative_path in _REGISTRY_DEPENDENCIES:
            signature.append(contract_store.content_signature(relative_path))
    except (OSError, contract_store.ContractStoreError) as exc:
        raise EntityFailure("CONTRACT_UNAVAILABLE", "实体注册表依赖不可用。") from exc
    return _registry_cached(tuple(signature))


@lru_cache(maxsize=16)
def _registry_cached(_signature: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    try:
        registry = contracts._read_yaml(_REGISTRY_PATH)
    except contracts.ContractFailure as exc:
        raise EntityFailure(exc.code, exc.message) from exc
    if not isinstance(registry.get("entity_types"), Mapping):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "实体注册表格式无效。")
    if not isinstance(registry.get("known_entities"), list):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "实体别名注册表格式无效。")
    if not isinstance(registry.get("candidate_sources"), Mapping):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选源注册表格式无效。")
    _validate_registry(registry)
    return registry


def _validate_registry(registry: Mapping[str, Any]) -> None:
    entity_types = registry["entity_types"]
    candidate_sources = registry["candidate_sources"]
    policy = registry.get("policy")
    if set(entity_types) != set(ENTITY_TYPES):
        raise EntityFailure(
            "CONTRACT_UNAVAILABLE",
            "实体注册表类型与公共能力合同不一致。",
        )
    if tuple(registry.get("normalization") or ()) != ENTITY_NORMALIZATION_STEPS:
        raise EntityFailure(
            "CONTRACT_UNAVAILABLE",
            "实体标准化声明与运行时合同不一致。",
        )
    if not isinstance(policy, Mapping) or dict(policy) != ENTITY_RUNTIME_POLICY:
        raise EntityFailure("CONTRACT_UNAVAILABLE", "实体解析策略与运行时安全不变量不一致。")
    aliases: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    input_roles: dict[str, str] = {}
    for entity_type, raw in entity_types.items():
        if not isinstance(entity_type, str) or not isinstance(raw, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体类型定义无效。")
        for role in raw.get("input_filter_roles") or []:
            if not isinstance(role, str) or (
                role in input_roles and input_roles[role] != entity_type
            ):
                raise EntityFailure("CONTRACT_UNAVAILABLE", "实体输入角色存在冲突。")
            input_roles[role] = entity_type
        roles_by_domain = raw.get("roles_by_domain")
        if not isinstance(roles_by_domain, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体域角色定义无效。")
        for domain, roles in roles_by_domain.items():
            if domain not in _SEMANTIC_PATHS or not isinstance(roles, list):
                raise EntityFailure("CONTRACT_UNAVAILABLE", "实体域角色定义无效。")
            semantics = contracts._read_yaml(_SEMANTIC_PATHS[str(domain)])
            dimensions = semantics.get("dimensions")
            if not isinstance(dimensions, Mapping) or any(
                not isinstance(role, str) or role not in dimensions for role in roles
            ):
                raise EntityFailure("CONTRACT_UNAVAILABLE", "实体角色与业务语义不一致。")

            if entity_type in candidate_sources:
                for role in roles:
                    definition = dimensions[role]
                    if not isinstance(definition, Mapping):
                        raise EntityFailure("CONTRACT_UNAVAILABLE", "Entity dimension is invalid.")
                    identity_filter = definition.get("identity_filter")
                    if not isinstance(identity_filter, Mapping):
                        raise EntityFailure(
                            "CONTRACT_UNAVAILABLE",
                            "Master-data entity dimension lacks a stable identity filter.",
                        )
                    _validate_identity_filter(identity_filter, definition, entity_type)

            # Covers metric/component overrides in addition to base dimensions.
            pending: list[Any] = [semantics]
            while pending:
                value = pending.pop()
                if isinstance(value, Mapping):
                    identity_filter = value.get("identity_filter")
                    if identity_filter is not None:
                        if not isinstance(identity_filter, Mapping):
                            raise EntityFailure(
                                "CONTRACT_UNAVAILABLE",
                                "Stable identity filter is invalid.",
                            )
                        expected_type = identity_filter.get("entity_type")
                        if expected_type not in candidate_sources:
                            raise EntityFailure(
                                "CONTRACT_UNAVAILABLE",
                                "Stable identity filter references an unregistered entity type.",
                            )
                        _validate_identity_filter(
                            identity_filter,
                            value,
                            str(expected_type),
                        )
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

    for raw in registry["known_entities"]:
        if not isinstance(raw, Mapping) or raw.get("entity_type") not in entity_types:
            raise EntityFailure("CONTRACT_UNAVAILABLE", "已知实体类型无效。")
        entity_type = str(raw["entity_type"])
        display = str(raw.get("display_name") or "")
        values = raw.get("canonical_values")
        raw_aliases = raw.get("aliases")
        if not isinstance(values, list) or not values or not isinstance(raw_aliases, list):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "已知实体别名定义无效。")
        canonical = tuple(str(value) for value in values)
        for alias in [display, *raw_aliases]:
            if not isinstance(alias, str) or not alias.strip():
                raise EntityFailure("CONTRACT_UNAVAILABLE", "已知实体别名定义无效。")
            key = (entity_type, normalize_text(alias))
            value = (display, canonical)
            if key in aliases and aliases[key] != value:
                raise EntityFailure("CONTRACT_UNAVAILABLE", "同类型实体别名存在冲突。")
            aliases[key] = value

    dataset_contract = contracts._read_yaml(
        "plugins/datasage-query/contracts/datasets.yaml"
    )
    datasets = dataset_contract.get("datasets")
    if not isinstance(datasets, Mapping):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "数据集合同格式无效。")
    for entity_type, source in registry["candidate_sources"].items():
        if entity_type not in entity_types or not isinstance(source, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选源类型无效。")
        table = source.get("table")
        dataset = datasets.get(table) if isinstance(table, str) else None
        if not isinstance(dataset, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选源未在数据集合同登记。")
        allowed = set(dataset.get("allowed_columns") or [])
        fields = [source.get("id_column"), source.get("display_column")]
        if source.get("code_column") is not None:
            fields.append(source.get("code_column"))
        fields.extend(source.get("search_columns") or [])
        if any(not isinstance(field, str) or field not in allowed for field in fields):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选字段未在数据集合同放行。")
        required = source.get("required_filters")
        if not isinstance(required, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选过滤定义无效。")
        expected: dict[str, Any] = {}
        for item in dataset.get("required_filters") or []:
            if not isinstance(item, Mapping) or item.get("op") != "eq":
                raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选源包含不支持的固定过滤。")
            expected[str(item.get("column"))] = item.get("value")
        if dict(required) != expected:
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选过滤与数据集合同不一致。")


def _validate_identity_filter(
    identity_filter: Mapping[str, Any],
    definition: Mapping[str, Any],
    expected_entity_type: str,
) -> None:
    if (
        identity_filter.get("entity_type") != expected_entity_type
        or identity_filter.get("value_field") not in {"canonical_id", "canonical_code"}
        or not isinstance(identity_filter.get("column"), str)
    ):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "Stable identity filter is invalid.")
    columns = definition.get("columns")
    physical_columns: set[str] = set()
    if isinstance(columns, list):
        for item in columns:
            if isinstance(item, str):
                physical_columns.add(item)
            elif isinstance(item, Mapping) and isinstance(item.get("column"), str):
                physical_columns.add(str(item["column"]))
    if identity_filter["column"] not in physical_columns:
        raise EntityFailure(
            "CONTRACT_UNAVAILABLE",
            "Stable identity filter column is absent from its dimension columns.",
        )


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.strip().split()).casefold()


def _is_publicly_forbidden_character(character: str) -> bool:
    """Return whether a character can carry a control or formatting attack."""

    codepoint = ord(character)
    # C0, C1 and DEL are not useful in a user-facing candidate.  Unicode
    # ``Cf`` covers bidi controls and zero-width formatting characters, but
    # keep the explicit ranges documented because those are common injection
    # payloads and are easy to audit independently of the Unicode database.
    return (
        codepoint <= 0x1F
        or 0x7F <= codepoint <= 0x9F
        or unicodedata.category(character) in {"Cf", "Cs"}
    )


def _clean_public_text(value: Any) -> str:
    """Normalize untrusted database text without changing its meaning."""

    text = "" if value is None else str(value)
    text = "".join(
        character
        for character in text
        if not _is_publicly_forbidden_character(character)
    )
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        character
        for character in text
        if not _is_publicly_forbidden_character(character)
    )
    # Newlines and unusual whitespace are already removed by the control
    # filter where applicable; collapse the rest so a value cannot visually
    # impersonate a multi-line instruction or a structured field.
    return " ".join(text.split())


def _truncate_public_text(text: str, *, original: str) -> str:
    """Bound a public value while retaining a stable disambiguating suffix."""

    digest = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    suffix = f"…[{digest}]"
    char_limit = max(1, PUBLIC_CANDIDATE_FIELD_MAX_CHARS - len(suffix))
    byte_limit = max(1, PUBLIC_CANDIDATE_FIELD_MAX_BYTES - len(suffix.encode("utf-8")))
    prefix: list[str] = []
    used = 0
    for character in text:
        if len(prefix) >= char_limit:
            break
        encoded_length = len(character.encode("utf-8"))
        if used + encoded_length > byte_limit:
            break
        prefix.append(character)
        used += encoded_length
    return "".join(prefix) + suffix


def _public_text(value: Any, *, optional: bool = False) -> str | None:
    """Return a bounded, control-free projection of model-visible DB text."""

    if value is None and optional:
        return None
    original = "" if value is None else str(value)
    cleaned = _clean_public_text(original)
    if not cleaned:
        digest = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        cleaned = f"[untrusted-{digest}]"
    removed_forbidden = any(
        _is_publicly_forbidden_character(character) for character in original
    )
    if removed_forbidden or (
        len(cleaned) > PUBLIC_CANDIDATE_FIELD_MAX_CHARS
        or len(cleaned.encode("utf-8")) > PUBLIC_CANDIDATE_FIELD_MAX_BYTES
    ):
        cleaned = _truncate_public_text(cleaned, original=original)
    return cleaned


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Project one internal candidate into a bounded untrusted model value."""

    result = dict(candidate)
    for field in _PUBLIC_CANDIDATE_TEXT_FIELDS:
        if field in result:
            result[field] = _public_text(result[field], optional=True)
    for field in _PUBLIC_CANDIDATE_LIST_FIELDS:
        value = result.get(field)
        if not isinstance(value, list):
            continue
        bounded_values = [
            _public_text(item)
            for item in value[:8]
            if isinstance(item, (str, int, float, bool))
        ]
        result[field] = [item for item in bounded_values if item is not None]
    # This marker is intentionally boolean and independent of the text value:
    # downstream model instructions must treat every public candidate field as
    # data, including candidates originating from otherwise trusted aliases.
    result["untrusted"] = True
    result["untrusted_source"] = _UNTRUSTED_CANDIDATE_MARKER
    return result


def _bound_public_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Apply a total UTF-8 budget without returning a partial candidate."""

    bounded: list[dict[str, Any]] = []
    total_bytes = 0
    for candidate in candidates:
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "surrogatepass")
        if total_bytes + len(encoded) > PUBLIC_CANDIDATE_TOTAL_BYTES:
            return bounded, True
        bounded.append(dict(candidate))
        total_bytes += len(encoded)
    return bounded, False


def _public_payload_candidates(
    candidates: Sequence[Mapping[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return at most ``limit`` safe candidates and whether bytes truncated."""

    projected = [
        dict(candidate)
        if candidate.get("untrusted") is True
        else _public_candidate(candidate)
        for candidate in candidates[:limit]
    ]
    return _bound_public_candidates(projected)


def _exact_cache_text(value: str) -> str:
    """Preserve binary identity distinctions in exact-lookup cache keys."""

    # Exact SQL treats stable IDs/codes as binary values. A casefolded or NFKC
    # cache key could therefore reuse a result that the database would reject
    # for the next token. Only outer whitespace is ignored, matching the value
    # passed to the binary SQL comparison.
    return value.strip()


def _known_matches(
    token: str,
    *,
    entity_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_text(token)
    matches: list[dict[str, Any]] = []
    for raw in _registry()["known_entities"]:
        if not isinstance(raw, Mapping):
            continue
        entity_type = raw.get("entity_type")
        aliases = raw.get("aliases")
        values = raw.get("canonical_values")
        display_name = str(raw.get("display_name") or token)
        if not isinstance(entity_type, str) or (
            entity_types is not None and entity_type not in entity_types
        ):
            continue
        if not isinstance(aliases, list) or not isinstance(values, list) or not values:
            continue
        normalized_aliases = {
            normalize_text(alias) for alias in [display_name, *aliases]
            if isinstance(alias, str)
        }
        if normalized not in normalized_aliases:
            continue
        matches.append(
            {
                "entity_type": entity_type,
                "display_name": display_name,
                "filter_values": [str(value) for value in values],
                "match_kind": "registered_exact",
                "confidence": "exact",
            }
        )
    return matches


def _metric_allowed_dimensions(
    metric: str | None,
    semantics: Mapping[str, Any] | None,
    *,
    attribution_mode: str | None = None,
) -> set[str] | None:
    if metric is None:
        return None
    metrics = semantics.get("metrics") if isinstance(semantics, Mapping) else None
    definition = metrics.get(metric) if isinstance(metrics, Mapping) else None
    if not isinstance(definition, Mapping):
        raise EntityFailure("INVALID_INPUT", "metric 不属于所选业务域。")
    dimensions = definition.get("allowed_dimensions")
    if isinstance(dimensions, list):
        if any(not isinstance(item, str) for item in dimensions):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "指标实体角色定义无效。")
        return set(dimensions)

    paths = definition.get("paths")
    if isinstance(paths, Mapping):
        selected_paths: list[Mapping[str, Any]] = []
        if attribution_mode is not None:
            selected = paths.get(attribution_mode)
            if not isinstance(selected, Mapping):
                raise EntityFailure("INVALID_INPUT", "attribution_mode 不属于所选指标。")
            selected_paths.append(selected)
        else:
            if any(not isinstance(path, Mapping) for path in paths.values()):
                raise EntityFailure("CONTRACT_UNAVAILABLE", "指标归属路径定义无效。")
            selected_paths.extend(paths.values())
        allowed: set[str] = set()
        for path in selected_paths:
            path_dimensions = path.get("allowed_dimensions")
            if not isinstance(path_dimensions, list) or any(
                not isinstance(item, str) for item in path_dimensions
            ):
                raise EntityFailure("CONTRACT_UNAVAILABLE", "指标归属路径缺少实体角色定义。")
            allowed.update(path_dimensions)
        return allowed

    source_metric = definition.get("source_completion_metric")
    source_path = definition.get("source_path")
    if isinstance(source_metric, str) and isinstance(source_path, str):
        if attribution_mode is not None and attribution_mode != source_path:
            raise EntityFailure("INVALID_INPUT", "attribution_mode 不属于所选指标。")
        source_definition = metrics.get(source_metric)
        source_paths = (
            source_definition.get("paths")
            if isinstance(source_definition, Mapping)
            else None
        )
        selected = source_paths.get(source_path) if isinstance(source_paths, Mapping) else None
        path_dimensions = selected.get("allowed_dimensions") if isinstance(selected, Mapping) else None
        if not isinstance(path_dimensions, list) or any(
            not isinstance(item, str) for item in path_dimensions
        ):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "引用指标路径缺少实体角色定义。")
        return set(path_dimensions)

    raise EntityFailure("CONTRACT_UNAVAILABLE", "指标缺少实体角色定义。")


def _roles_for(
    entity_type: str,
    domain: str | None,
    *,
    metric: str | None = None,
    attribution_mode: str | None = None,
    semantics: Mapping[str, Any] | None = None,
) -> list[str]:
    if domain is None:
        return []
    raw = _registry()["entity_types"].get(entity_type)
    roles_by_domain = raw.get("roles_by_domain") if isinstance(raw, Mapping) else None
    roles = roles_by_domain.get(domain) if isinstance(roles_by_domain, Mapping) else None
    if not isinstance(roles, list):
        return []
    allowed = _metric_allowed_dimensions(
        metric,
        semantics,
        attribution_mode=attribution_mode,
    )
    if allowed is None:
        return [str(role) for role in roles]
    return [str(role) for role in roles if role in allowed]


def _with_roles(
    candidate: Mapping[str, Any],
    domain: str | None,
    *,
    metric: str | None,
    attribution_mode: str | None,
    semantics: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = dict(candidate)
    roles = _roles_for(
        str(candidate["entity_type"]),
        domain,
        metric=metric,
        attribution_mode=attribution_mode,
        semantics=semantics,
    )
    if len(roles) == 1:
        result["filter_role"] = roles[0]
    elif roles:
        result["filter_role_candidates"] = roles
    return result


def _exact_resolution_status(
    candidates: Sequence[Mapping[str, Any]],
    *,
    metric: str | None,
) -> tuple[str, bool]:
    """Resolve only a unique identity with one executable metric filter role."""
    if len(candidates) != 1:
        return "ambiguous", True
    # A role ambiguity is still a hard stop when no metric was supplied.  The
    # caller must not infer which business dimension the entity represents
    # merely because its identity itself was unique.
    if candidates[0].get("filter_role_candidates"):
        return "ambiguous", True
    if metric is None:
        return "resolved", False
    candidate = candidates[0]
    if isinstance(candidate.get("filter_role"), str):
        return "resolved", False
    raise EntityFailure(
        "UNSUPPORTED_ENTITY_ROLE",
        "该实体类型不能用于所选指标，请调整实体类型、业务域或指标。",
    )


def _validate_args(
    args: Any,
) -> tuple[str, set[str] | None, str | None, str | None, str | None, int]:
    if not isinstance(args, Mapping) or set(args) - _ARGUMENTS:
        raise EntityFailure("INVALID_INPUT", "实体解析参数格式无效。")
    token = args.get("token")
    if not isinstance(token, str) or not 1 <= len(token.strip()) <= 128:
        raise EntityFailure("INVALID_INPUT", "token 必须是 1 到 128 个字符的字符串。")
    domain = args.get("domain")
    if domain is not None and domain not in _DOMAINS:
        raise EntityFailure("INVALID_INPUT", "domain 不受支持。")
    metric = args.get("metric")
    if metric is not None and (
        not isinstance(metric, str) or not 1 <= len(metric) <= 100 or domain is None
    ):
        raise EntityFailure("INVALID_INPUT", "metric 必须与 domain 一起提供。")
    attribution_mode = args.get("attribution_mode")
    if attribution_mode is not None and (
        attribution_mode not in ATTRIBUTION_MODES
        or metric is None
    ):
        raise EntityFailure("INVALID_INPUT", "attribution_mode 必须与 metric 一起提供。")
    registry_types = set(ENTITY_TYPES)
    raw_types = args.get("entity_types")
    entity_types: set[str] | None = None
    if raw_types is not None:
        if (
            not isinstance(raw_types, list)
            or not 1 <= len(raw_types) <= len(ENTITY_TYPES)
            or any(not isinstance(item, str) or item not in registry_types for item in raw_types)
            or len(raw_types) != len(set(raw_types))
        ):
            raise EntityFailure("INVALID_INPUT", "entity_types 包含不受支持的实体类型。")
        entity_types = set(raw_types)
    raw_limit = args.get("limit", ENTITY_RESOLVE_DEFAULT_LIMIT)
    if (
        not isinstance(raw_limit, int)
        or isinstance(raw_limit, bool)
        or not 1 <= raw_limit <= ENTITY_RESOLVE_HARD_LIMIT
    ):
        raise EntityFailure(
            "INVALID_INPUT",
            f"limit 必须是 1 到 {ENTITY_RESOLVE_HARD_LIMIT} 的整数。",
        )
    limit = raw_limit
    return token.strip(), entity_types, domain, metric, attribution_mode, limit


def _default_entity_types(domain: str | None) -> set[str]:
    registry = _registry()
    sources = set(registry["candidate_sources"])
    if domain is None:
        return sources
    selected = set()
    for entity_type, raw in registry["entity_types"].items():
        roles = raw.get("roles_by_domain") if isinstance(raw, Mapping) else None
        if entity_type in sources and isinstance(roles, Mapping) and domain in roles:
            selected.add(str(entity_type))
    return selected


def _fuzzy_search_allowed(token: str) -> bool:
    compact = "".join(normalize_text(token).split())
    if not compact:
        return False
    if all(ord(character) < 128 for character in compact):
        return len(compact) >= 3
    return len(compact) >= 2


def _build_candidate_query(
    token: str,
    entity_types: set[str],
    identifier_adapter: Any = sql_identifiers,
    *,
    exact_only: bool = False,
) -> tuple[str, list[Any]]:
    raw_quote_table = getattr(identifier_adapter, "quote_table", None) or getattr(
        identifier_adapter, "_quote_table"
    )
    raw_quote_identifier = getattr(
        identifier_adapter, "quote_identifier", None
    ) or getattr(identifier_adapter, "_quote_identifier")

    def quote_table(value: str) -> str:
        try:
            return raw_quote_table(value)
        except sql_identifiers.SqlIdentifierError as exc:
            raise EntityFailure("INVALID_PLAN", "查询包含无效数据表标识。") from exc

    def quote_identifier(value: str) -> str:
        try:
            return raw_quote_identifier(value)
        except sql_identifiers.SqlIdentifierError as exc:
            raise EntityFailure("INVALID_PLAN", "查询包含无效字段标识。") from exc

    branches: list[str] = []
    params: list[Any] = []
    sources = _registry()["candidate_sources"]
    for entity_type in sorted(entity_types):
        raw = sources.get(entity_type)
        if not isinstance(raw, Mapping):
            continue
        table = quote_table(str(raw.get("table") or ""))
        id_column = quote_identifier(str(raw.get("id_column") or ""))
        display_column = quote_identifier(str(raw.get("display_column") or ""))
        code_value = raw.get("code_column")
        code_sql = (
            f"CAST({quote_identifier(str(code_value))} AS CHAR)"
            if isinstance(code_value, str) and code_value
            else "NULL"
        )
        required = raw.get("required_filters")
        if not isinstance(required, Mapping):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选过滤定义无效。")
        required_sql = "".join(
            f" AND {quote_identifier(str(column))} = %s"
            for column in required
        )
        required_params = list(required.values())
        search_columns = raw.get("search_columns")
        if not isinstance(search_columns, list) or any(
            not isinstance(column, str) or not column for column in search_columns
        ):
            raise EntityFailure("CONTRACT_UNAVAILABLE", "实体候选字段定义无效。")
        search_specs: list[tuple[str, bool]] = []
        if exact_only:
            # Stable IDs/codes are valid exact re-entry tokens after a user
            # chooses among resolver candidates. They never participate in
            # prefix/contains search.
            search_specs.append((str(raw["id_column"]), True))
            if isinstance(code_value, str) and code_value:
                search_specs.append((code_value, True))
        search_specs.extend((str(column), False) for column in search_columns)
        for search_column, binary_identity in search_specs:
            quoted_search = quote_identifier(str(search_column))
            normalized_sql = f"LOWER(TRIM(CAST({quoted_search} AS CHAR)))"
            if exact_only:
                comparison_sql = (
                    f"CAST({quoted_search} AS BINARY) = CAST(%s AS BINARY)"
                    if binary_identity
                    else f"{normalized_sql} = %s"
                )
                branches.append(
                    "SELECT %s AS entity_type, "
                    f"CAST({id_column} AS CHAR) AS canonical_id, "
                    f"{code_sql} AS canonical_code, "
                    f"CAST({display_column} AS CHAR) AS display_name, "
                    f"CAST({quoted_search} AS CHAR) AS matched_value, "
                    "0 AS match_rank "
                    f"FROM {table} WHERE {quoted_search} IS NOT NULL"
                    f"{required_sql} AND {comparison_sql}"
                )
                params.append(entity_type)
                params.extend(required_params)
                params.append(token.strip() if binary_identity else normalize_text(token))
            else:
                branches.append(
                    "SELECT %s AS entity_type, "
                    f"CAST({id_column} AS CHAR) AS canonical_id, "
                    f"{code_sql} AS canonical_code, "
                    f"CAST({display_column} AS CHAR) AS display_name, "
                    f"CAST({quoted_search} AS CHAR) AS matched_value, "
                    f"CASE WHEN {normalized_sql} = %s THEN 0 "
                    f"WHEN LEFT({normalized_sql}, CHAR_LENGTH(%s)) = %s THEN 1 ELSE 2 END AS match_rank "
                    f"FROM {table} WHERE {quoted_search} IS NOT NULL"
                    f"{required_sql} AND LOCATE(%s, {normalized_sql}) > 0"
                )
                params.extend(
                    [entity_type, normalize_text(token), normalize_text(token), normalize_text(token)]
                )
                params.extend(required_params)
                params.append(normalize_text(token))
    if not branches:
        raise EntityFailure("UNSUPPORTED_ENTITY_TYPE", "当前实体类型没有候选数据源。")
    sql = (
        "SELECT DISTINCT entity_type, canonical_id, canonical_code, display_name, matched_value, match_rank "
        "FROM ("
        + " UNION ALL ".join(branches)
        + ") AS entity_candidates "
        "ORDER BY match_rank ASC, entity_type ASC, display_name ASC LIMIT %s"
    )
    params.append(51)
    return sql, params


def _effective_dimensions(semantics, metric=None):
    from .capability_contract import effective_dimension_definitions, CapabilityContractError
    try:
        return effective_dimension_definitions(semantics, metric)
    except CapabilityContractError as exc:
        raise EntityFailure(exc.code, exc.message) from exc


def _batchable_metric_lookup_keys(
    request: Mapping[str, Any],
    semantics: Mapping[str, Any],
    cache: Mapping[tuple[str, str], Any],
) -> list[tuple[str, str, str]]:
    """Return safe, uncached exact lookups as (type, normalized key, token)."""

    raw_filters = request.get("metric_filters")
    if not isinstance(raw_filters, Mapping) or not raw_filters:
        return []
    metric = str(request.get("metric") or "")
    attribution_mode = request.get("attribution_mode")
    allowed = _metric_allowed_dimensions(
        metric,
        semantics,
        attribution_mode=(
            str(attribution_mode) if isinstance(attribution_mode, str) else None
        ),
    ) or set()
    dimensions = _effective_dimensions(semantics, metric)
    if not isinstance(dimensions, Mapping):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "业务域缺少维度语义。")
    pending: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_role, raw_value in raw_filters.items():
        role = str(raw_role)
        definition = dimensions.get(role)
        value_contract = (
            definition.get("value_contract")
            if isinstance(definition, Mapping)
            else None
        )
        # Only prefetch a role already proven valid for the selected metric.
        # Remapped aliases and invalid roles stay on canonicalize's fail-closed
        # path so batching cannot create speculative database work.
        if (
            role not in allowed
            or not isinstance(value_contract, Mapping)
            or value_contract.get("kind") != "entity_exact"
        ):
            continue
        entity_type = _entity_type_for_filter_role(role)
        if entity_type is None:
            continue
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        if not values or any(not isinstance(value, str) for value in values):
            continue
        for value in values:
            if len(_known_matches(value, entity_types={entity_type})) == 1:
                continue
            key = (entity_type, _exact_cache_text(value))
            if key in cache or key in seen:
                continue
            seen.add(key)
            pending.append((entity_type, key[1], value))
    return pending


def _build_exact_lookup_batch_query(
    lookups: Sequence[tuple[str, str, str]],
    identifier_adapter: Any = sql_identifiers,
) -> tuple[str, list[Any]]:
    """Combine bounded exact-only token probes into one database round trip."""

    branches: list[str] = []
    params: list[Any] = []
    for lookup_index, (entity_type, _normalized, token) in enumerate(lookups):
        inner_sql, inner_params = _build_candidate_query(
            token,
            {entity_type},
            identifier_adapter,
            exact_only=True,
        )
        branches.append(
            "SELECT %s AS lookup_index, exact_candidates.* "
            f"FROM ({inner_sql}) AS exact_candidates"
        )
        params.append(lookup_index)
        params.extend(inner_params)
    if not branches:
        raise EntityFailure("INTERNAL_ERROR", "实体批量预检缺少查询项。")
    return " UNION ALL ".join(branches), params


def prefetch_metric_entities(
    request: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    exact_lookup: Any,
    resolution_cache: dict[tuple[str, str], Any],
    max_unique_lookups: int,
) -> int:
    """Prefetch two or more exact identities with one connection and query.

    A single token continues through the established lookup path. This keeps
    compatibility while collapsing the expensive multi-token case. Returned
    rows are still rechecked independently by ``_candidate_rows`` before any
    stable identity can reach business SQL.
    """

    lookups = _batchable_metric_lookup_keys(request, semantics, resolution_cache)
    if len(lookups) <= 1:
        return 0
    if len(resolution_cache) + len(lookups) > max_unique_lookups:
        raise EntityFailure(
            "ENTITY_PREFLIGHT_LIMIT_EXCEEDED",
            "本次请求包含过多不同实体，请缩小范围。",
        )
    sql, params = _build_exact_lookup_batch_query(lookups)
    keys = [(entity_type, normalized) for entity_type, normalized, _token in lookups]
    try:
        rows, globally_truncated = exact_lookup(sql, params, 51 * len(lookups))
    except Exception as exc:
        for key in keys:
            resolution_cache[key] = exc
        raise

    try:
        grouped: dict[int, list[dict[str, Any]]] = {
            index: [] for index in range(len(lookups))
        }
        for raw_row in rows:
            row = dict(raw_row)
            raw_index = row.pop("lookup_index", None)
            if raw_index is None and len(lookups) == 1:
                raw_index = 0
            try:
                lookup_index = int(raw_index)
            except (TypeError, ValueError, OverflowError) as exc:
                raise EntityFailure(
                    "CONTRACT_UNAVAILABLE",
                    "实体批量预检返回了无法归属的候选。",
                ) from exc
            if lookup_index not in grouped:
                raise EntityFailure(
                    "CONTRACT_UNAVAILABLE",
                    "实体批量预检返回了越界候选。",
                )
            grouped[lookup_index].append(row)

        for index, key in enumerate(keys):
            candidates = grouped[index]
            truncated = bool(globally_truncated or len(candidates) > 50)
            resolution_cache[key] = (candidates[:50], truncated)
    except Exception as exc:
        # Cache malformed/unsafe batch outcomes as well as transport failures;
        # a repeated token must never create a failure retry storm.
        for key in keys:
            resolution_cache[key] = exc
        raise
    return 1


def _candidate_rows(
    rows: Sequence[Mapping[str, Any]],
    domain: str | None,
    *,
    metric: str | None,
    attribution_mode: str | None,
    semantics: Mapping[str, Any] | None,
    token: str | None = None,
    public: bool = True,
) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        entity_type = str(row.get("entity_type") or "")
        canonical_id = str(row.get("canonical_id") or "")
        canonical_code = str(row.get("canonical_code") or "")
        display_name = str(row.get("display_name") or canonical_code or canonical_id)
        if not public:
            source = _registry()["candidate_sources"].get(entity_type, {})
            raw_name = row.get("display_name")
            if (
                isinstance(raw_name, str) and raw_name.strip()
                and isinstance(source.get("display_column"), str)
                and source["display_column"] not in {source.get("id_column"), source.get("code_column")}
            ):
                display_name = _GovernedDisplayName(
                    raw_name, _DISPLAY_NAME_ISSUER,
                    (entity_type, canonical_id, canonical_code, source["table"], source["display_column"]),
                )

        try:
            rank = int(row.get("match_rank", 2))
        except (TypeError, ValueError, OverflowError):
            rank = 2
        if token is not None:
            normalized_token = normalize_text(token)
            possible_matches = [
                row.get("matched_value"),
                canonical_id,
                canonical_code,
                display_name,
            ]
            verified_ranks: list[int] = []
            for raw_match in possible_matches:
                if not isinstance(raw_match, str) or not raw_match:
                    continue
                normalized_match = normalize_text(raw_match)
                if normalized_match == normalized_token:
                    verified_ranks.append(0)
                elif normalized_match.startswith(normalized_token):
                    verified_ranks.append(1)
                elif normalized_token in normalized_match:
                    verified_ranks.append(2)
            if not verified_ranks:
                # A permissive database collation may recall values that are
                # not exact under the registry's NFKC/casefold contract.
                continue
            rank = min(verified_ranks)
        key = (entity_type, canonical_id or canonical_code, display_name)
        candidate = {
            "entity_type": entity_type,
            "canonical_id": canonical_id or None,
            "canonical_code": canonical_code or None,
            "display_name": display_name,
            "filter_values": [display_name],
            "match_kind": "exact" if rank == 0 else "prefix" if rank == 1 else "contains",
            "confidence": "exact" if rank == 0 else "candidate",
            "_rank": rank,
        }
        previous = deduplicated.get(key)
        if previous is None or rank < int(previous["_rank"]):
            deduplicated[key] = candidate
    ordered = sorted(
        deduplicated.values(),
        key=lambda item: (int(item["_rank"]), str(item["entity_type"]), str(item["display_name"])),
    )
    result = []
    for candidate in ordered:
        candidate.pop("_rank", None)
        resolved = _with_roles(
            candidate,
            domain,
            metric=metric,
            attribution_mode=attribution_mode,
            semantics=semantics,
        )
        result.append(_public_candidate(resolved) if public else resolved)
    return result


def datasage_entity_resolve(args: dict[str, Any], **_kwargs: Any) -> str:
    """Resolve registered aliases or return bounded, non-binding candidates."""

    started = time.monotonic()
    try:
        token, entity_types, domain, metric, attribution_mode, limit = _validate_args(args)
        semantics = None
        if metric is not None:
            _datasets, semantics = contracts.execution_contracts(str(domain))
            _metric_allowed_dimensions(
                metric,
                semantics,
                attribution_mode=attribution_mode,
            )
            if entity_types is not None:
                unsupported_types = [
                    entity_type
                    for entity_type in sorted(entity_types)
                    if not _roles_for(
                        entity_type,
                        domain,
                        metric=metric,
                        attribution_mode=attribution_mode,
                        semantics=semantics,
                    )
                ]
                if unsupported_types:
                    raise EntityFailure(
                        "UNSUPPORTED_ENTITY_ROLE",
                        "该实体类型不能用于所选指标，请调整实体类型、业务域或指标。",
                    )
        exact = _known_matches(token, entity_types=entity_types)
        registered_types = {str(item["entity_type"]) for item in exact}
        considered_types = set(entity_types) if entity_types is not None else {
            str(kind) for kind in _registry()["entity_types"]
            if domain is None or _roles_for(
                str(kind), domain, metric=metric,
                attribution_mode=attribution_mode, semantics=semantics,
            )
        }
        searched_types: set[str] = set()

        def with_resolution_scope(payload):
            # A unique match among searchable masters cannot rule out a
            # considered source-exact type that has no discovery source.
            unsearched = considered_types - registered_types - searched_types
            payload["resolution_scope"] = {
                "considered_entity_types": sorted(considered_types),
                "registered_exact_entity_types": sorted(registered_types),
                "master_searched_entity_types": sorted(searched_types),
                "unsearched_entity_types": sorted(unsearched),
                "complete": not unsearched,
            }
            if unsearched:
                payload["candidate_count_is_lower_bound"] = True
                if payload["status"] == "resolved":
                    payload["status"] = "ambiguous"
                    payload["must_clarify"] = True
                    payload["must_stop_business_query"] = True
            return payload

        # An explicit list of several types is still a search across types,
        # not a choice of the first registered match.
        if exact and entity_types is not None and len(entity_types) == 1:
            candidates = [
                _with_roles(
                    item,
                    domain,
                    metric=metric,
                    attribution_mode=attribution_mode,
                    semantics=semantics,
                )
                for item in exact
            ]
            public_candidates, public_bytes_truncated = _public_payload_candidates(
                candidates,
                limit,
            )
            count_truncated = len(candidates) > limit
            status, must_clarify = _exact_resolution_status(
                candidates,
                metric=metric,
            )
            payload = {
                "status": status,
                "resolution_path": "registered_exact",
                "token": _public_text(token),
                "candidates": public_candidates,
                "must_clarify": must_clarify,
                "must_stop_business_query": status != "resolved",
                "candidate_count": len(candidates),
                "candidate_count_is_lower_bound": False,
                "truncated": bool(count_truncated or public_bytes_truncated),
            }
        else:
            selected_types = entity_types or _default_entity_types(domain)
            source_types = selected_types.intersection(_registry()["candidate_sources"])
            if not source_types:
                if exact:
                    candidates = [
                        _with_roles(
                            item,
                            domain,
                            metric=metric,
                            attribution_mode=attribution_mode,
                            semantics=semantics,
                        )
                        for item in exact
                    ]
                    public_candidates, public_bytes_truncated = _public_payload_candidates(
                        candidates,
                        limit,
                    )
                    count_truncated = len(candidates) > limit
                    status, must_clarify = _exact_resolution_status(
                        candidates,
                        metric=metric,
                    )
                    payload = {
                        "status": status,
                        "resolution_path": "registered_exact",
                        "token": _public_text(token),
                        "candidates": public_candidates,
                        "must_clarify": must_clarify,
                        "must_stop_business_query": status != "resolved",
                        "candidate_count": len(candidates),
                        "candidate_count_is_lower_bound": False,
                        "truncated": bool(count_truncated or public_bytes_truncated),
                    }
                else:
                    payload = {
                        "status": "not_found",
                        "resolution_path": "no_candidate_source",
                        "token": _public_text(token),
                        "candidates": [],
                        "must_clarify": False,
                        "must_stop_business_query": True,
                        "candidate_count": 0,
                        "candidate_count_is_lower_bound": False,
                        "truncated": False,
                    }
                payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                return json.dumps(with_resolution_scope(payload), ensure_ascii=False, separators=(",", ":"))
            fuzzy_allowed = _fuzzy_search_allowed(token)
            sql, params = _build_candidate_query(
                token,
                source_types,
                exact_only=bool(exact) or not fuzzy_allowed,
            )
            deadline_at = _kwargs.get("deadline_at")
            if deadline_at is not None and time.monotonic() >= deadline_at:
                raise EntityFailure("BATCH_DEADLINE_EXCEEDED", "实体解析已超过调用总时限。")
            execution_kwargs = {"deadline_at": deadline_at} if deadline_at is not None else {}
            rows, truncated = db_runtime.execute(sql, params, 50, **execution_kwargs)
            searched_types.update(source_types)
            candidates = _candidate_rows(
                rows,
                domain,
                metric=metric,
                attribution_mode=attribution_mode,
                semantics=semantics,
                token=token,
                public=False,
            )
            master_exact = [
                item for item in candidates if item.get("confidence") == "exact"
            ]
            registered_exact = [
                _with_roles(
                    item,
                    domain,
                    metric=metric,
                    attribution_mode=attribution_mode,
                    semantics=semantics,
                )
                for item in exact
            ]
            exact_candidates = [*registered_exact, *master_exact]
            if exact_candidates:
                candidates = exact_candidates
                status, must_clarify = _exact_resolution_status(
                    candidates,
                    metric=metric,
                )
            elif candidates:
                status = "ambiguous"
                must_clarify = True
            else:
                status = "not_found"
                must_clarify = False
            candidate_count = len(candidates)
            public_candidates, public_bytes_truncated = _public_payload_candidates(
                candidates,
                limit,
            )
            payload = {
                "status": status,
                "resolution_path": (
                    "registered_and_master_exact"
                    if exact
                    else "master_candidate_query" if fuzzy_allowed else "master_exact_short_token"
                ),
                "token": _public_text(token),
                "candidates": public_candidates,
                "must_clarify": must_clarify,
                "must_stop_business_query": status != "resolved",
                "candidate_count": candidate_count,
                "candidate_count_is_lower_bound": bool(truncated),
                "truncated": bool(
                    truncated or candidate_count > limit or public_bytes_truncated
                ),
                "fuzzy_search_skipped": not fuzzy_allowed,
            }
        payload = with_resolution_scope(payload)
    except EntityFailure as failure:
        payload = {
            "status": "failed",
            "error": {"code": failure.code, "message": failure.message},
            "must_stop_business_query": True,
        }
    except (contracts.ContractFailure, db_runtime.DatabaseRuntimeError) as failure:
        payload = {
            "status": "failed",
            "error": {"code": failure.code, "message": failure.message},
            "must_stop_business_query": True,
        }
    except Exception as failure:
        # The injected governed executor retains its public QueryFailure type
        # in the orchestration layer.  Preserve that stable taxonomy without
        # importing the higher-level module solely to name the exception.
        code = getattr(failure, "code", None)
        message = getattr(failure, "message", None)
        payload = {
            "status": "failed",
            "error": (
                {"code": code, "message": message}
                if isinstance(code, str) and isinstance(message, str)
                else {"code": "INTERNAL_ERROR", "message": "实体解析工具暂时不可用。"}
            ),
            "must_stop_business_query": True,
        }
    payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _entity_type_for_filter_role(role: str) -> str | None:
    for entity_type, raw in _registry()["entity_types"].items():
        input_roles = raw.get("input_filter_roles") if isinstance(raw, Mapping) else None
        if isinstance(input_roles, list) and role in input_roles:
            return str(entity_type)
    return None


def _identity_columns_for(entity_type: str, value_field: str) -> list[str]:
    columns: set[str] = set()
    for path in _SEMANTIC_PATHS.values():
        pending: list[Any] = [contracts._read_yaml(path)]
        while pending:
            value = pending.pop()
            if isinstance(value, Mapping):
                identity_filter = value.get("identity_filter")
                if (
                    isinstance(identity_filter, Mapping)
                    and identity_filter.get("entity_type") == entity_type
                    and identity_filter.get("value_field") == value_field
                    and isinstance(identity_filter.get("column"), str)
                ):
                    columns.add(str(identity_filter["column"]))
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    return sorted(columns)


def canonicalize_metric_request(
    request: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    exact_lookup: Any | None = None,
    resolution_cache: dict[tuple[str, str], Any] | None = None,
    max_unique_lookups: int = 100,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind entity filters to registered codes or exact master identities."""

    normalized_request = dict(request)
    raw_filters = request.get("metric_filters")
    if not isinstance(raw_filters, Mapping) or not raw_filters:
        return normalized_request, []
    domain = str(request.get("domain") or "")
    metric = str(request.get("metric") or "")
    attribution_mode = request.get("attribution_mode")
    allowed = _metric_allowed_dimensions(
        metric,
        semantics,
        attribution_mode=str(attribution_mode) if isinstance(attribution_mode, str) else None,
    ) or set()
    normalized_filters: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    entity_bindings: dict[str, dict[str, Any]] = {}
    dimensions = _effective_dimensions(semantics, metric)
    if not isinstance(dimensions, Mapping):
        raise EntityFailure("CONTRACT_UNAVAILABLE", "业务域缺少维度语义。")
    cache = resolution_cache if resolution_cache is not None else {}

    for input_role, raw_value in raw_filters.items():
        role = str(input_role)
        # A role can be listed in the entity registry as a convenient alias
        # (notably department/warehouse_department) while the selected domain
        # still declares its values as source_exact.  Read that contract before
        # routing to a master-data resolver: source-exact values must pass
        # through byte-for-byte unless every supplied value is an explicitly
        # registered alias.  This keeps controlled department expansions while
        # preventing an unregistered fact value from becoming ENTITY_NOT_FOUND.
        definition = dimensions.get(role)
        value_contract = (
            definition.get("value_contract")
            if isinstance(definition, Mapping)
            else None
        )
        source_exact_role = (
            isinstance(value_contract, Mapping)
            and value_contract.get("kind") == "source_exact"
        )
        entity_type = _entity_type_for_filter_role(role)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        resolved_values: list[str] = []
        registered_display_names: list[str] = []
        all_registered = bool(entity_type) and bool(values)
        for value in values:
            if not isinstance(value, str) or entity_type is None:
                all_registered = False
                break
            matches = _known_matches(value, entity_types={entity_type})
            if len(matches) != 1:
                all_registered = False
                break
            display_name = matches[0].get("display_name")
            if (
                isinstance(display_name, str)
                and display_name
                and display_name not in registered_display_names
            ):
                registered_display_names.append(display_name)
            for canonical in matches[0]["filter_values"]:
                if canonical not in resolved_values:
                    resolved_values.append(canonical)

        output_role = role
        output_value: Any = raw_value
        if all_registered:
            roles = _roles_for(
                entity_type,
                domain,
                metric=metric,
                attribution_mode=(
                    str(attribution_mode) if isinstance(attribution_mode, str) else None
                ),
                semantics=semantics,
            )
            if role not in allowed and len(roles) == 1:
                output_role = roles[0]
            output_value = (
                resolved_values[0]
                if not isinstance(raw_value, list) and len(resolved_values) == 1
                else resolved_values
            )
            evidence.append(
                {
                    "entity_type": entity_type,
                    "input_role": role,
                    "filter_role": output_role,
                    "input_values": list(values),
                    "filter_values": list(resolved_values),
                    "display_names": registered_display_names,
                    "resolution_path": "registered_exact",
                    "lookup_db_call_count": 0,
                }
            )
        elif source_exact_role:
            # Source values are already the governed filter vocabulary.  A
            # source-exact role may share an input alias with an entity type,
            # but it has no master identity to resolve.  Do not call the
            # database and do not normalize/expand a partial value list.
            output_value = raw_value
        elif entity_type is not None and exact_lookup is not None:
            if not values or any(not isinstance(value, str) for value in values):
                raise EntityFailure("INVALID_INPUT", "实体筛选值必须是名称或编码字符串。")
            roles = _roles_for(
                entity_type,
                domain,
                metric=metric,
                attribution_mode=(
                    str(attribution_mode) if isinstance(attribution_mode, str) else None
                ),
                semantics=semantics,
            )
            if role in allowed and role not in roles:
                raise EntityFailure(
                    "UNSUPPORTED_ENTITY_ROLE",
                    "该实体类型没有可安全用于所选指标的稳定过滤角色。",
                )
            if role not in allowed:
                if len(roles) == 1:
                    output_role = roles[0]
                elif roles:
                    raise EntityFailure(
                        "ENTITY_ROLE_AMBIGUOUS",
                        "该实体在所选指标中对应多个筛选角色，请使用 datasage_entity_resolve 或明确业务口径。",
                    )
                else:
                    raise EntityFailure(
                        "UNSUPPORTED_ENTITY_ROLE",
                        "该实体类型不能用于所选指标。",
                    )
            source = _registry()["candidate_sources"].get(entity_type)
            binding_policy = source.get("query_binding") if isinstance(source, Mapping) else None
            if isinstance(binding_policy, Mapping) and binding_policy.get("status") == "blocked":
                raise EntityFailure(
                    "ENTITY_IDENTITY_UNAVAILABLE",
                    "该实体类型尚无可安全用于业务过滤的唯一身份键。",
                )
            resolved_candidates: list[dict[str, Any]] = []
            lookup_db_calls = 0
            for value in values:
                key = (entity_type, _exact_cache_text(value))
                if key not in cache:
                    if len(cache) >= max_unique_lookups:
                        raise EntityFailure(
                            "ENTITY_PREFLIGHT_LIMIT_EXCEEDED",
                            "本次请求包含过多不同实体，请缩小范围。",
                        )
                    if not isinstance(source, Mapping):
                        raise EntityFailure(
                            "ENTITY_NOT_FOUND",
                            "未找到与输入完全一致的受控实体，请使用 datasage_entity_resolve 澄清实体后重试。",
                        )
                    sql, params = _build_candidate_query(
                        value,
                        {entity_type},
                        exact_only=True,
                    )
                    try:
                        rows, truncated = exact_lookup(sql, params, 50)
                    except Exception as exc:
                        # A repeated token in the same batch must not amplify
                        # one operational failure into repeated DB pressure.
                        cache[key] = exc
                        raise
                    else:
                        cache[key] = (list(rows), bool(truncated))
                        lookup_db_calls += 1
                cached = cache[key]
                if isinstance(cached, Exception):
                    raise cached
                rows, truncated = cached
                candidates = _candidate_rows(
                    rows,
                    domain,
                    metric=metric,
                    attribution_mode=(
                        str(attribution_mode) if isinstance(attribution_mode, str) else None
                    ),
                    semantics=semantics,
                    token=value,
                    public=False,
                )
                exact_candidates = [
                    candidate for candidate in candidates
                    if candidate.get("confidence") == "exact"
                ]
                if truncated or len(exact_candidates) > 1:
                    raise EntityFailure(
                        "ENTITY_AMBIGUOUS",
                        "存在多个完全匹配的实体，请使用 datasage_entity_resolve 或唯一编码进一步明确。",
                    )
                if not exact_candidates:
                    raise EntityFailure(
                        "ENTITY_NOT_FOUND",
                        "未找到与输入完全一致的受控实体，请使用 datasage_entity_resolve 澄清实体后重试。",
                    )
                _exact_resolution_status(exact_candidates, metric=metric)
                resolved_candidates.append(exact_candidates[0])

            definition = dimensions.get(output_role)
            identity_filter = (
                definition.get("identity_filter")
                if isinstance(definition, Mapping)
                else None
            )
            if (
                not isinstance(identity_filter, Mapping)
                or identity_filter.get("entity_type") != entity_type
                or identity_filter.get("value_field") not in {"canonical_id", "canonical_code"}
                or not isinstance(identity_filter.get("column"), str)
            ):
                raise EntityFailure(
                    "CONTRACT_UNAVAILABLE",
                    "实体维度缺少稳定身份过滤定义。",
                )
            value_field = str(identity_filter["value_field"])
            stable_values = [candidate.get(value_field) for candidate in resolved_candidates]
            if any(value is None or value == "" for value in stable_values):
                raise EntityFailure(
                    "ENTITY_IDENTITY_UNAVAILABLE",
                    "完全匹配的实体缺少可安全执行的稳定身份值。",
                )
            stable_values = list(dict.fromkeys(str(value) for value in stable_values))
            output_value = (
                stable_values[0]
                if not isinstance(raw_value, list) and len(stable_values) == 1
                else stable_values
            )
            entity_bindings[output_role] = {
                "entity_type": entity_type,
                "input_role": role,
                "filter_role": output_role,
                "value_field": value_field,
                "identity_columns": _identity_columns_for(entity_type, value_field),
                "filter_values": stable_values,
            }
            evidence.append(
                {
                    "entity_type": entity_type,
                    "input_role": role,
                    "filter_role": output_role,
                    "input_values": list(values),
                    "canonical_ids": [candidate.get("canonical_id") for candidate in resolved_candidates],
                    "canonical_codes": [candidate.get("canonical_code") for candidate in resolved_candidates],
                    "display_names": [candidate.get("display_name") for candidate in resolved_candidates],
                    "filter_values": stable_values,
                    "resolution_path": "master_exact_preflight",
                    "lookup_db_call_count": lookup_db_calls,
                }
            )

        if output_role in normalized_filters and normalized_filters[output_role] != output_value:
            raise EntityFailure(
                "ENTITY_FILTER_CONFLICT",
                "实体归一化后出现相互冲突的筛选条件，请明确保留哪一个。",
            )
        normalized_filters[output_role] = output_value

    normalized_request["metric_filters"] = normalized_filters
    if entity_bindings:
        normalized_request["_entity_bindings"] = entity_bindings
    return normalized_request, evidence
