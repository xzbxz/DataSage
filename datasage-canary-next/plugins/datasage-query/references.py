"""Bounded, read-only access to approved model-facing DataSage references."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


REGISTRY_VERSION = "datasage-reference-registry/v1"
RESPONSE_SCHEMA = "datasage-reference-response/v1"
MAX_REQUESTS = 3
MAX_SECTION_CHARS = 4_500
MAX_TOTAL_CONTENT_CHARS = 9_000
MAX_INDEX_RESPONSE_CHARS = 6_000
MAX_READ_RESPONSE_CHARS = 14_000
_REGISTRY_PATH = "plugins/datasage-query/contracts/reference-registry.yaml"

# Source paths and every readable section are code-owned constants.  The YAML
# registry pins versions and hashes for audit, but cannot authorize a new path,
# selector, or section by itself.
_SOURCE_PATHS = {
    "expert_playbooks": "skills/common-data-foundation/references/expert-playbooks.yaml",
    "answer_boundary": "skills/common-data-foundation/references/answer-boundary.md",
    "entity_guidance": "skills/common-data-foundation/references/entity-rules.md",
    "query_rules": "skills/common-data-foundation/references/query-rules.md",
}


_SECTION_SPECS: dict[str, dict[str, tuple[str, Any]]] = {
    "expert_playbooks": {
        "planning_semantics": ("yaml", ("planning_semantics",)),
        "evidence_roles": ("yaml", ("evidence_roles",)),
    },
    "answer_boundary": {
        "evidence_types": ("markdown_heading", "Five evidence types"),
        "product_boundary": ("markdown_heading", "Product boundary"),
        "response_shape": ("markdown_heading", "Response shape"),
        "business_language": ("markdown_heading", "Business-language boundary"),
        "units_numeric_scale": ("markdown_heading", "Units and numeric scale"),
        "local_failure": ("markdown_heading", "Local failure"),
    },
    # Only items 8, 9, and 12 are model-safe.  The remaining maintainer notes
    # contain physical joins, fields, and implementation details.
    "entity_guidance": {
        "identity_resolution": ("numbered_items", (8, 9, 12)),
    },
    "query_rules": {
        "intent_catalog": ("markdown_heading", "Intent and catalog discovery"),
        "time": ("markdown_heading", "Time"),
        "currency_units": ("markdown_heading", "Currency and units"),
        "typed_requests": ("markdown_heading", "Typed requests"),
        "comparisons_decomposition": ("markdown_heading", "Comparisons and decomposition"),
        "entities": ("markdown_heading", "Ledgers and entities"),
        "safety_cost": ("markdown_heading", "Safety and cost"),
        "failure_semantics": ("markdown_heading", "Failure semantics"),
    },
}

SOURCE_IDS = tuple(_SOURCE_PATHS)
SECTION_IDS = tuple(sorted({section for values in _SECTION_SPECS.values() for section in values}))
SECTION_IDS_BY_SOURCE = {
    source_id: tuple(sorted(section_specs))
    for source_id, section_specs in _SECTION_SPECS.items()
}


def _profile_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _trusted_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    path.relative_to(root.resolve(strict=True))
    if not path.is_file():
        raise ValueError("approved reference is not a regular file")
    return path


def _load_registry(root: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(_trusted_file(root, _REGISTRY_PATH).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or parsed.get("version") != REGISTRY_VERSION:
        raise ValueError("reference registry version is invalid")
    sources = parsed.get("sources")
    if not isinstance(sources, dict) or not set(_SOURCE_PATHS).issubset(sources):
        raise ValueError("reference registry source set is invalid")
    for source_id, expected_path in _SOURCE_PATHS.items():
        item = sources.get(source_id)
        if (
            not isinstance(item, dict)
            or item.get("path") != expected_path
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("source_version"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_sha256", "")))
            or not isinstance(item.get("content_format"), str)
        ):
            raise ValueError("reference registry source metadata is invalid")
    return parsed


def _markdown_heading(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = None
    level = None
    for index, line in enumerate(lines):
        match = re.fullmatch(r"(#{1,6})\s+(.+?)\s*", line)
        if match and match.group(2) == heading:
            start = index
            level = len(match.group(1))
            break
    if start is None or level is None:
        raise ValueError("approved markdown section is missing")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.fullmatch(r"(#{1,6})\s+(.+?)\s*", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip() + "\n"


def _numbered_items(text: str, numbers: tuple[int, ...]) -> str:
    items: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if match:
            current = int(match.group(1))
            items[current] = match.group(2).strip()
        elif current is not None and line.startswith((" ", "\t")):
            items[current] += " " + line.strip()
        else:
            current = None
    if any(number not in items for number in numbers):
        raise ValueError("approved entity guidance item is missing")
    # Normalize away implementation wording while preserving the pinned source
    # meaning.  This is the only projection that reads a maintainer-only file.
    normalized = {
        8: "Do not fuzzy-select an ambiguous business entity. Return bounded candidates or ask one concise clarification.",
        9: "Keep stable resolved identities in the governed request and show business names in the answer.",
        12: "Historical analysis must retain departed employees; current-employment status applies only to an explicit current-employee question.",
    }
    return "\n".join(f"- {normalized[number]}" for number in numbers) + "\n"


def _select_yaml_value(value: Any, selector: tuple[str, ...]) -> Any:
    for key in selector:
        if not isinstance(value, Mapping) or key not in value:
            raise ValueError("approved YAML section is missing")
        value = value[key]
    return value


def _yaml_value(text: str, selector: tuple[str, ...]) -> str:
    value = _select_yaml_value(yaml.safe_load(text), selector)
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip() + "\n"


def _available_sections(
    _root: Path, source_id: str, _raw: bytes
) -> list[str]:
    return sorted(_SECTION_SPECS[source_id])


def _section_content(
    _root: Path,
    _source_id: str,
    raw: bytes,
    kind: str,
    selector: Any,
) -> str:
    text = raw.decode("utf-8")
    if kind == "yaml":
        return _yaml_value(text, selector)
    if kind == "markdown_heading":
        return _markdown_heading(text, selector)
    if kind == "numbered_items":
        return _numbered_items(text, selector)
    raise ValueError("reference selector is invalid")


def _failure(code: str, message: str) -> str:
    return json.dumps(
        {
            "schema": RESPONSE_SCHEMA,
            "status": "failed",
            "error": {"code": code, "message": message},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _valid_args(args: Any) -> bool:
    if not isinstance(args, dict) or set(args) - {"mode", "requests"}:
        return False
    mode = args.get("mode")
    if mode == "index":
        return set(args) == {"mode"}
    requests = args.get("requests")
    if mode != "read" or not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        return False
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"source_id", "section_id"}:
            return False
        source_id = request.get("source_id")
        section_id = request.get("section_id")
        if source_id not in _SECTION_SPECS or section_id not in _SECTION_SPECS[source_id]:
            return False
    return True


def datasage_reference(args: dict[str, Any], **_kwargs: Any) -> str:
    """Return only enumerated, integrity-pinned planning reference sections."""
    if not _valid_args(args):
        return _failure("INVALID_INPUT", "参考资料请求必须使用已批准的 source_id 和 section_id。")
    try:
        root = _profile_root()
        registry = _load_registry(root)
        registered = registry["sources"]
        if args["mode"] == "index":
            indexed_sources: list[dict[str, Any]] = []
            for source_id in SOURCE_IDS:
                metadata = registered[source_id]
                raw = _trusted_file(root, _SOURCE_PATHS[source_id]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != metadata["source_sha256"]:
                    return _failure("REFERENCE_INTEGRITY_FAILED", "已批准参考资料与注册哈希不一致。")
                indexed_sources.append(
                    {
                        "source_id": source_id,
                        "title": metadata["title"],
                        "source_version": metadata["source_version"],
                        "source_sha256": metadata["source_sha256"],
                        "sections": _available_sections(root, source_id, raw),
                    }
                )
            payload = {
                "schema": RESPONSE_SCHEMA,
                "status": "success",
                "registry_version": REGISTRY_VERSION,
                "mode": "index",
                "limits": {
                    "max_requests": MAX_REQUESTS,
                    "max_section_chars": MAX_SECTION_CHARS,
                    "max_total_content_chars": MAX_TOTAL_CONTENT_CHARS,
                    "max_index_response_chars": MAX_INDEX_RESPONSE_CHARS,
                    "max_read_response_chars": MAX_READ_RESPONSE_CHARS,
                },
                "sources": indexed_sources,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > MAX_INDEX_RESPONSE_CHARS:
                return _failure("REFERENCE_LIMIT_EXCEEDED", "已批准参考资料索引超过输出上限。")
            return encoded

        results: list[dict[str, Any]] = []
        total = 0
        for request in args["requests"]:
            source_id = request["source_id"]
            section_id = request["section_id"]
            metadata = registered[source_id]
            raw = _trusted_file(root, _SOURCE_PATHS[source_id]).read_bytes()
            source_sha = hashlib.sha256(raw).hexdigest()
            if source_sha != metadata["source_sha256"]:
                return _failure("REFERENCE_INTEGRITY_FAILED", "已批准参考资料与注册哈希不一致。")
            kind, selector = _SECTION_SPECS[source_id][section_id]
            content = _section_content(
                root, source_id, raw, kind, selector
            )
            if len(content) > MAX_SECTION_CHARS:
                return _failure("REFERENCE_LIMIT_EXCEEDED", "已批准参考资料章节超过单节输出上限。")
            total += len(content)
            if total > MAX_TOTAL_CONTENT_CHARS:
                return _failure("REFERENCE_LIMIT_EXCEEDED", "参考资料总输出超过上限。")
            results.append(
                {
                    "source_id": source_id,
                    "source_version": metadata["source_version"],
                    "source_sha256": source_sha,
                    "section_id": section_id,
                    "section_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content_format": metadata["content_format"],
                    "content_chars": len(content),
                    "content": content,
                }
            )
        encoded = json.dumps(
            {
                "schema": RESPONSE_SCHEMA,
                "status": "success",
                "registry_version": REGISTRY_VERSION,
                "mode": "read",
                "total_content_chars": total,
                "results": results,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded) > MAX_READ_RESPONSE_CHARS:
            return _failure("REFERENCE_LIMIT_EXCEEDED", "参考资料响应超过输出上限。")
        return encoded
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return _failure("REFERENCE_UNAVAILABLE", "已批准参考资料当前不可用。")
