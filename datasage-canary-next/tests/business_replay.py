"""Pure offline business-transcript validation, independent of release machinery.

Parses supplied exports; does not capture sessions, call models, send messages,
write receipts, or grant eligibility. Git identifies the maintained Profile.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


TRANSCRIPT_ADAPTER_PATH = "plugins/datasage-query/e2e/canary_transcript_adapter.py"

SESSION_META_CONVERSATIONAL_FIELDS = (
    "content",
    "api_content",
    "tool_calls",
    "tool_call_id",
    "tool_name",
    "function_call",
    "name",
    "effect_disposition",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)

LIVE_ALLOWED_TOOL_NAMES = frozenset({
    "datasage_catalog",
    "datasage_entity_resolve",
    "datasage_query",
})

READONLY_SKILL_TRACE_SCHEMA = "datasage-readonly-skill-trace/v1"
READONLY_SKILL_TOOL_NAMES = frozenset({"skills_list", "skill_view"})
READONLY_SKILL_ALLOWED_TOOL_NAMES = LIVE_ALLOWED_TOOL_NAMES | READONLY_SKILL_TOOL_NAMES
READONLY_SKILL_REFERENCE_PATHS = frozenset({
    "references/query-rules.md",
    "references/entity-guidance.md",
    "references/answer-boundary.md",
    "references/delivery-analysis.md",
    "references/inventory-analysis.md",
    "references/receipt-analysis.md",
    "references/receivable-analysis.md",
    "references/target-analysis.md",
    "references/profit-analysis.md",
    "references/pattern-matching-analysis.md",
    "references/cross-domain-analysis.md",
})
READONLY_SKILL_SURFACE_FORBIDDEN = frozenset({
    "skill_manage",
    "terminal",
    "execute_code",
})
READONLY_SKILL_SNAPSHOT_SCOPES = frozenset({
    "readonly_surface",
    "existing_wecom_surface",
})

LIVE_SESSION_META_ALLOWED_FIELDS = frozenset({
    *SESSION_META_CONVERSATIONAL_FIELDS,
    "id",
    "role",
    "tools",
    "model",
    "timestamp",
    "platform",
})

FINAL_ANSWER_OBSERVATION_SCHEMA = "datasage-final-answer-observations/v1"
OFFICIAL_EXPORT_FORMAT = "hermes_sessions_export_jsonl"

def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

class _LiveTranscriptPolicyError(RuntimeError, ValueError):
    """Fail-closed live policy error shared with the capture runner."""

def _load_e2e_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load business evaluator {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _export_session(
    payload: bytes | str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    adapter = _load_e2e_module(
        "_datasage_official_export_parser",
        ROOT / TRANSCRIPT_ADAPTER_PATH,
    )
    try:
        return adapter.parse_official_session_export(payload)
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise _LiveTranscriptPolicyError(str(error)) from error

def _live_turn_completion_policies(
    completion_policy: object, prompt_count: int
) -> list[dict[str, object]]:
    if not isinstance(completion_policy, list) or len(completion_policy) != prompt_count:
        raise _LiveTranscriptPolicyError(
            "turn completion policy must declare every Golden turn exactly once"
        )
    policies: list[dict[str, object]] = []
    keys = {
        "case_id",
        "turn",
        "assistant_completion",
        "maximum_blocking_clarify_calls",
    }
    for index, value in enumerate(completion_policy, 1):
        if not isinstance(value, dict) or set(value) != keys:
            raise _LiveTranscriptPolicyError(
                "turn completion policy entry has an invalid shape"
            )
        if (
            not isinstance(value["case_id"], str)
            or not value["case_id"]
            or value["turn"] != index
        ):
            raise _LiveTranscriptPolicyError(
                "turn completion policy is missing or out of Golden turn order"
            )
        if value["assistant_completion"] != "ordinary_text":
            raise _LiveTranscriptPolicyError(
                "live turns must end with ordinary assistant text"
            )
        if (
            type(value["maximum_blocking_clarify_calls"]) is not int
            or value["maximum_blocking_clarify_calls"] != 0
        ):
            raise _LiveTranscriptPolicyError(
                "blocking clarify tool calls must be forbidden for every live turn"
            )
        policies.append(value)
    return policies

def _live_without_transport_metadata(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    conversation: list[dict[str, object]] = []
    for item in messages:
        if item.get("function_call") is not None:
            raise _LiveTranscriptPolicyError(
                "legacy function_call is forbidden in live evidence"
            )
        role = item.get("role")
        if role == "session_meta":
            if not set(item).issubset(LIVE_SESSION_META_ALLOWED_FIELDS):
                raise _LiveTranscriptPolicyError(
                    "session_meta contains unknown fields"
                )
            if any(
                item.get(field) not in (None, "", [], {})
                for field in SESSION_META_CONVERSATIONAL_FIELDS
            ):
                raise _LiveTranscriptPolicyError(
                    "session_meta contains conversational or tool-flow payload"
                )
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            raise _LiveTranscriptPolicyError(
                "official export contains an unsupported conversational role"
            )
        conversation.append(item)
    return conversation

def _strict_tool_calls_for_validation(raw: Any, message_id: Any) -> list[dict[str, Any]]:
    """Return a parsed copy of a tool-call list without mutating the export."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(
                raw,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (TypeError, ValueError) as exc:
            raise _LiveTranscriptPolicyError(
                f"message {message_id} tool_calls are not strict JSON"
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise _LiveTranscriptPolicyError(
            f"message {message_id} tool_calls must be a JSON list"
        )
    return list(parsed)


def _validate_live_turn_tool_flow(
    segment: list[dict[str, object]],
    *,
    allow_readonly_skills: bool = False,
) -> None:
    allowed_tool_names = (
        LIVE_ALLOWED_TOOL_NAMES | READONLY_SKILL_TOOL_NAMES
        if allow_readonly_skills is True
        else LIVE_ALLOWED_TOOL_NAMES
    )
    pending: list[tuple[str, str, int]] = []
    completed: set[str] = set()
    for relative_index, message in enumerate(segment):
        role = message.get("role")
        raw_tool_calls = message.get("tool_calls")
        tool_calls = []
        if raw_tool_calls not in (None, []):
            if role != "assistant":
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid tool-call carrier"
                )
            if allow_readonly_skills is True:
                tool_calls = _strict_tool_calls_for_validation(
                    raw_tool_calls, message.get("id")
                )
            elif not isinstance(raw_tool_calls, list):
                # Preserve the pre-existing default protocol: only a decoded
                # list is accepted.  The opt-in Skill path parses a strict
                # JSON-string copy because official exports may serialize it.
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid tool-call carrier"
                )
            else:
                tool_calls = raw_tool_calls
            if pending:
                raise _LiveTranscriptPolicyError(
                    "pending tool calls must close before a new assistant tool-call batch"
                )
        if role == "system":
            continue
        if pending:
            call_id, function_name, call_index = pending[0]
            if role != "tool":
                raise _LiveTranscriptPolicyError(
                    "pending tool calls must be closed by the next non-system tool results"
                )
            observed_id = message.get("tool_call_id")
            if observed_id != call_id:
                if any(observed_id == item[0] for item in pending[1:]):
                    raise _LiveTranscriptPolicyError(
                        "tool results must close pending calls in declaration order"
                    )
                raise _LiveTranscriptPolicyError(
                    "captured tool result is not bound to the next pending tool call"
                )
            tool_name = message.get("tool_name")
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or tool_name != function_name
            ):
                raise _LiveTranscriptPolicyError(
                    "captured tool result name does not match its tool call"
                )
            if call_index >= relative_index:
                raise _LiveTranscriptPolicyError(
                    "captured tool result precedes its tool call"
                )
            pending.pop(0)
            completed.add(call_id)
            continue
        if role == "tool":
            raise _LiveTranscriptPolicyError(
                "captured tool result is not bound to a prior tool call"
            )
        if not tool_calls:
            continue
        batch_ids: set[str] = set()
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            function = call.get("function") if isinstance(call, dict) else None
            function_name = function.get("name") if isinstance(function, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in batch_ids
                or call_id in completed
            ):
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid or duplicate tool-call ID"
                )
            if not isinstance(function_name, str) or not function_name:
                raise _LiveTranscriptPolicyError(
                    "captured tool call has no valid function name"
                )
            if function_name == "datasage_push":
                raise _LiveTranscriptPolicyError(
                    "datasage_push is forbidden in inbound live evidence"
                )
            if function_name == "clarify":
                raise _LiveTranscriptPolicyError(
                    "blocking clarify tool calls are forbidden in live evidence"
                )
            if function_name not in allowed_tool_names:
                raise _LiveTranscriptPolicyError(
                    "live evidence contains an unapproved tool call"
                )
            batch_ids.add(call_id)
            pending.append((call_id, function_name, relative_index))
    if pending:
        raise _LiveTranscriptPolicyError("captured turn has an unclosed tool flow")

def _live_endpoints(
    messages: list[dict[str, object]],
    prompts: list[str],
    completion_policy: object,
    *,
    allow_readonly_skills: bool = False,
) -> list[tuple[int, int, str]]:
    _live_turn_completion_policies(completion_policy, len(prompts))
    conversation = _live_without_transport_metadata(messages)
    if [
        item.get("content") for item in conversation if item.get("role") == "user"
    ] != prompts:
        raise _LiveTranscriptPolicyError(
            "official export user turns are not exactly the two ordered Golden prompts"
        )
    try:
        first_user = next(
            index
            for index, item in enumerate(conversation)
            if item.get("role") == "user"
        )
    except StopIteration as error:
        raise _LiveTranscriptPolicyError(
            "official export contains no Golden user prompt"
        ) from error
    if any(item.get("role") != "system" for item in conversation[:first_user]):
        raise _LiveTranscriptPolicyError(
            "captured transcript has a non-system message before the first Golden prompt"
        )
    result: list[tuple[int, int, str]] = []
    start = 0
    for prompt in prompts:
        users = [
            index
            for index in range(start, len(conversation))
            if conversation[index].get("role") == "user"
            and conversation[index].get("content") == prompt
        ]
        if len(users) != 1:
            raise _LiveTranscriptPolicyError(
                "captured prompt is not unique in the official export"
            )
        user_index = users[0]
        next_user = next(
            (
                index
                for index in range(user_index + 1, len(conversation))
                if conversation[index].get("role") == "user"
            ),
            len(conversation),
        )
        segment = conversation[user_index + 1 : next_user]
        visible = [item for item in segment if item.get("role") != "system"]
        if not visible:
            raise _LiveTranscriptPolicyError(
                "captured turn has no terminal assistant answer"
            )
        final = visible[-1]
        if (
            final.get("role") != "assistant"
            or not isinstance(final.get("content"), str)
            or not final["content"]
            or final.get("tool_calls") not in (None, [])
        ):
            raise _LiveTranscriptPolicyError(
                "captured turn does not end in a terminal assistant answer"
            )
        _validate_live_turn_tool_flow(
            segment,
            allow_readonly_skills=allow_readonly_skills,
        )
        user_id = conversation[user_index].get("id")
        final_id = final.get("id")
        if type(user_id) is not int or type(final_id) is not int or user_id >= final_id:
            raise _LiveTranscriptPolicyError("captured turn endpoints are invalid")
        result.append(
            (user_id, final_id, _sha256_bytes(final["content"].encode("utf-8")))
        )
        start = next_user
    return result

def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON is forbidden: {value}")

def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property: {key}")
        result[key] = value
    return result

def _fail_on_nonretryable_tool_result(messages: list[dict[str, object]]) -> None:
    public_tools = {
        "datasage_catalog",
        "datasage_entity_resolve",
        "datasage_query",
        "datasage_push",
    }
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_name") not in public_tools:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError("persisted DataSage tool result is not JSON text")
        try:
            payload = json.loads(
                content,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("persisted DataSage tool result is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("persisted DataSage tool result is not an object")
        codes: set[str] = set()
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.add(error["code"])
        for result in payload.get("results") or []:
            if isinstance(result, dict):
                error = result.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    codes.add(error["code"])
        bundle = payload.get("evidence_bundle")
        for gap in bundle.get("evidence_gaps") or [] if isinstance(bundle, dict) else []:
            if isinstance(gap, dict) and isinstance(gap.get("reason"), str):
                codes.add(gap["reason"])
        if "DATA_ENTITLEMENT_DENIED" in codes:
            raise RuntimeError(
                "nonretryable DATA_ENTITLEMENT_DENIED in persisted tool result"
            )

def _review_assertion(review: dict[str, object], *, adapter: Any, profile: dict[str, str], case_id: str, session_id: str, user_id: int, prompt_sha: str, session_export_sha256: str, watermark: str, final_sha: str, fixture_sha: str, database_ref_sha: str) -> dict[str, object]:
    assertion = {
        "schema": adapter.REVIEW_SCHEMA, "status": "reviewed", "test_id": case_id,
        "artifact_id": profile["artifact_id"], "payload_sha256": profile["payload_sha256"],
        "session_id": session_id, "user_message_id": user_id, "canonical_prompt_sha256": prompt_sha,
        "session_export_sha256": session_export_sha256, "watermark_sha256": watermark,
        "final_answer_sha256": final_sha, "reviewer_id": review["reviewer_id"], "labels": review["labels"],
        "fixture_attestation_sha256": fixture_sha, "business_database_ref_sha256": database_ref_sha,
    }
    quality = review.get("decision_quality")
    if isinstance(quality, dict):
        assertion["decision_quality"] = dict(quality)
    return assertion


def _readonly_skill_json_value(raw: Any, label: str) -> Any:
    """Parse an exported JSON value with the same duplicate/non-finite rules as the adapter."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw:
        raise _LiveTranscriptPolicyError(f"{label} is missing or not JSON")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise _LiveTranscriptPolicyError(f"{label} is not strict JSON") from exc


def _readonly_skill_json_object(raw: Any, label: str) -> dict[str, Any]:
    value = _readonly_skill_json_value(raw, label)
    if not isinstance(value, dict):
        raise _LiveTranscriptPolicyError(f"{label} must be a JSON object")
    return value


def _readonly_skill_sha256(raw: Any) -> str:
    if isinstance(raw, str):
        payload = raw.encode("utf-8")
    else:
        payload = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _readonly_skill_argument_metadata(tool_name: str, arguments: Any, message_id: Any) -> tuple[dict[str, Any], str]:
    """Validate the official skills schemas without accepting internal handler arguments."""
    parsed = _readonly_skill_json_object(arguments, f"message {message_id} {tool_name} arguments")
    if tool_name == "skills_list":
        if set(parsed) - {"category"}:
            raise _LiveTranscriptPolicyError(
                f"message {message_id} skills_list arguments contain unknown fields"
            )
        if "category" in parsed and not isinstance(parsed["category"], str):
            raise _LiveTranscriptPolicyError(
                f"message {message_id} skills_list category must be a string"
            )
        return parsed, "skills_list"

    if set(parsed) - {"name", "file_path"}:
        raise _LiveTranscriptPolicyError(
            f"message {message_id} skill_view arguments contain unknown fields"
        )
    if parsed.get("name") != "datasage":
        raise _LiveTranscriptPolicyError(
            f"message {message_id} skill_view name must be exactly 'datasage'"
        )
    file_path = parsed.get("file_path")
    if file_path is None:
        canonical_path = "SKILL.md"
    elif isinstance(file_path, str) and file_path == "SKILL.md":
        canonical_path = "SKILL.md"
    elif isinstance(file_path, str) and file_path in READONLY_SKILL_REFERENCE_PATHS:
        canonical_path = file_path
    else:
        raise _LiveTranscriptPolicyError(
            f"message {message_id} skill_view file_path is outside the reviewed DataSage allowlist"
        )
    return dict(parsed), canonical_path


def _readonly_skill_result_metadata(
    tool_name: str,
    arguments: dict[str, Any],
    raw_result: Any,
    message_id: Any,
) -> tuple[str, str | None, bool]:
    """Validate the JSON envelopes emitted by skills_list/skill_view.

    ``success: false`` is still a valid observed read attempt.  The trace validator
    records that failure and never upgrades it to a successful method load.
    """
    result = _readonly_skill_json_object(raw_result, f"message {message_id} {tool_name} result")
    success = result.get("success")
    if type(success) is not bool:
        raise _LiveTranscriptPolicyError(
            f"message {message_id} {tool_name} result must declare boolean success"
        )
    if not success:
        if not isinstance(result.get("error"), str) or not result["error"]:
            raise _LiveTranscriptPolicyError(
                f"message {message_id} {tool_name} failed result has no error text"
            )
        return "failed", None, False

    if tool_name == "skills_list":
        if not isinstance(result.get("skills"), list):
            raise _LiveTranscriptPolicyError(
                f"message {message_id} skills_list success result has no skills list"
            )
        discovered = any(
            isinstance(item, dict) and item.get("name") == "datasage"
            for item in result["skills"]
        )
        return "listed", None, discovered

    expected_path = arguments.get("file_path", "SKILL.md")
    explicit_file_path = "file_path" in arguments and arguments.get("file_path") is not None
    observed_path = result.get("file")
    if (
        result.get("status") == "unchanged"
        and result.get("dedup") is True
        and result.get("content_returned") is False
    ):
        if (
            result.get("name") != "datasage"
            or not isinstance(result.get("message"), str)
            or not result["message"]
            or observed_path != expected_path
            or "content" in result
        ):
            raise _LiveTranscriptPolicyError(
                f"message {message_id} skill_view dedup result has an invalid envelope"
            )
        return "cached_not_reobserved", None, False

    if result.get("is_binary") is True:
        if (
            result.get("name") != "datasage"
            or not isinstance(result.get("content"), str)
            or not result["content"]
            or observed_path != expected_path
        ):
            raise _LiveTranscriptPolicyError(
                f"message {message_id} binary skill_view result has an invalid envelope"
            )
        return "binary_placeholder", None, False

    content = result.get("content")
    if result.get("name") != "datasage" or not isinstance(content, str) or not content.strip():
        raise _LiveTranscriptPolicyError(
            f"message {message_id} skill_view success result is missing datasage content"
        )
    if expected_path != "SKILL.md" or explicit_file_path:
        if observed_path != expected_path:
            raise _LiveTranscriptPolicyError(
                f"message {message_id} skill_view result path does not match its request"
            )
    elif observed_path is not None:
        raise _LiveTranscriptPolicyError(
            f"message {message_id} main skill_view result has unexpected file metadata"
        )
    return "read_success", hashlib.sha256(content.encode("utf-8")).hexdigest(), True


def _readonly_skill_tool_surface(
    tool_snapshot: Any,
    *,
    snapshot_scope: str = "readonly_surface",
) -> dict[str, Any]:
    """Classify an optional tool snapshot without claiming OS sandboxing.

    ``readonly_surface`` is the original strict mode.  The explicit
    ``existing_wecom_surface`` mode audits the already-configured WeCom
    ``execute_code`` exposure while keeping it forbidden in the observed
    transcript; it is therefore a surface classification, not a read-only
    or execution-isolation claim.
    """
    if snapshot_scope not in READONLY_SKILL_SNAPSHOT_SCOPES:
        raise _LiveTranscriptPolicyError(
            "snapshot_scope must be one of: existing_wecom_surface, readonly_surface"
        )
    if tool_snapshot is None:
        return {
            "status": "not_observed",
            "tools": None,
            "sandbox_claim": "not_observed",
            "execution_tools_exposed": [],
            "whole_surface_readonly": "not_certified",
            "execution_isolation": "not_observed",
            "snapshot_scope": snapshot_scope,
        }
    if not isinstance(tool_snapshot, (list, tuple, set, frozenset)):
        raise _LiveTranscriptPolicyError("tool_snapshot must be a list or set of names")
    tools = list(tool_snapshot)
    if any(not isinstance(name, str) or not name for name in tools):
        raise _LiveTranscriptPolicyError("tool_snapshot contains an invalid tool name")
    if len(tools) != len(set(tools)):
        raise _LiveTranscriptPolicyError("tool_snapshot contains duplicate tool names")
    observed = set(tools)
    forbidden_snapshot_tools = READONLY_SKILL_SURFACE_FORBIDDEN - (
        {"execute_code"} if snapshot_scope == "existing_wecom_surface" else set()
    )
    if observed & forbidden_snapshot_tools:
        raise _LiveTranscriptPolicyError(
            "tool_snapshot contains a forbidden mutating or execution tool"
        )
    # ``clarify`` is part of the existing WeCom surface.  Its presence in a
    # snapshot does not prove a write or execution capability, but an actual
    # blocking clarify call remains forbidden by the replay flow validator.
    allowed_snapshot_tools = READONLY_SKILL_ALLOWED_TOOL_NAMES | {"clarify"}
    if snapshot_scope == "existing_wecom_surface":
        allowed_snapshot_tools |= {"execute_code"}
    unexpected = observed - allowed_snapshot_tools
    if unexpected:
        raise _LiveTranscriptPolicyError(
            f"tool_snapshot contains tools outside the {snapshot_scope} surface: {sorted(unexpected)!r}"
        )
    has_skill_tools = bool(observed & READONLY_SKILL_TOOL_NAMES)
    execution_tools_exposed = sorted(observed & {"execute_code"})
    return {
        "status": (
            "provided_unbound_existing_wecom_surface"
            if snapshot_scope == "existing_wecom_surface"
            else "provided_unbound_no_skill_edit_or_exec"
        ),
        "tools": sorted(observed),
        "skill_tools_observed": has_skill_tools,
        "source_binding": "not_provided",
        "sandbox_claim": "not_observed",
        "execution_tools_exposed": execution_tools_exposed,
        "whole_surface_readonly": "not_certified",
        "execution_isolation": "not_observed",
        "snapshot_scope": snapshot_scope,
    }


def validate_readonly_skill_trace(
    payload: bytes | str,
    *,
    allow_readonly_skills: bool = False,
    case_id: str | None = None,
    tool_snapshot: Any = None,
    snapshot_scope: str = "readonly_surface",
) -> dict[str, Any]:
    """Validate an explicitly opted-in read-only Skill trace from an official export.

    The default replay path remains unchanged and continues to reject every tool
    outside ``LIVE_ALLOWED_TOOL_NAMES``.  Callers must pass
    ``allow_readonly_skills=True`` to accept ``skills_list``/``skill_view`` here.
    This function only validates transcript identity, tool-call pairing, Skill
    arguments/envelopes, and hashes.  It does not claim the model used the
    reference, prove an OS sandbox, or replace the existing DataSage business
    replay validator.
    """
    if allow_readonly_skills is not True:
        raise _LiveTranscriptPolicyError(
            "read-only Skill trace validation requires explicit allow_readonly_skills=True"
        )
    if snapshot_scope not in READONLY_SKILL_SNAPSHOT_SCOPES:
        raise _LiveTranscriptPolicyError(
            "snapshot_scope must be one of: existing_wecom_surface, readonly_surface"
        )
    if case_id is not None and (not isinstance(case_id, str) or not case_id):
        raise _LiveTranscriptPolicyError("case_id must be a non-empty string when supplied")

    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    session, messages = _export_session(raw)
    export_sha = _sha256_bytes(raw)
    session_id = session.get("id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise _LiveTranscriptPolicyError("official export session ID is missing")

    positions: dict[int, int] = {}
    previous_id: int | None = None
    for position, row in enumerate(messages):
        if not isinstance(row, dict):
            raise _LiveTranscriptPolicyError("official export messages must be objects")
        message_id = row.get("id")
        if type(message_id) is not int or message_id <= 0:
            raise _LiveTranscriptPolicyError("official export message IDs are invalid")
        if previous_id is not None and message_id <= previous_id:
            raise _LiveTranscriptPolicyError("official export message IDs are not strictly increasing")
        if message_id in positions:
            raise _LiveTranscriptPolicyError("official export has duplicate message IDs")
        if row.get("session_id") != session_id:
            raise _LiveTranscriptPolicyError("official export contains a cross-session message")
        if "active" in row and row["active"] != 1:
            raise _LiveTranscriptPolicyError("official export contains an inactive message")
        positions[message_id] = position
        previous_id = message_id

    # Reuse the existing transport/session hygiene checks.  In particular,
    # session_meta content and legacy function_call fields must not be used to
    # smuggle a hidden tool flow into this opt-in path.
    conversation = _live_without_transport_metadata(messages)
    _validate_live_turn_tool_flow(conversation, allow_readonly_skills=True)

    result_rows: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, row in enumerate(conversation):
        if row.get("role") != "tool":
            continue
        call_id = row.get("tool_call_id")
        tool_name = row.get("tool_name")
        if not isinstance(call_id, str) or not call_id:
            raise _LiveTranscriptPolicyError("Skill trace tool result has no call ID")
        if call_id in result_rows:
            raise _LiveTranscriptPolicyError(f"duplicate tool result for call {call_id}")
        if not isinstance(tool_name, str) or tool_name not in READONLY_SKILL_ALLOWED_TOOL_NAMES:
            raise _LiveTranscriptPolicyError(
                f"live evidence contains an unapproved tool result: {tool_name!r}"
            )
        result_rows[call_id] = (position, row)

    calls: list[dict[str, Any]] = []
    used_results: set[str] = set()
    declared_calls: set[str] = set()
    for position, row in enumerate(conversation):
        role = row.get("role")
        tool_calls_raw = row.get("tool_calls")
        if role == "tool":
            continue
        if role != "assistant" or tool_calls_raw in (None, [], ""):
            continue
        tool_calls = _strict_tool_calls_for_validation(tool_calls_raw, row.get("id"))
        for item in tool_calls:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise _LiveTranscriptPolicyError("assistant tool call is invalid")
            call_id = item.get("id") or item.get("call_id")
            function = item["function"]
            function_name = function.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise _LiveTranscriptPolicyError("assistant tool call has no valid ID")
            if call_id in declared_calls:
                raise _LiveTranscriptPolicyError(f"duplicate tool call ID: {call_id}")
            if not isinstance(function_name, str) or function_name not in READONLY_SKILL_ALLOWED_TOOL_NAMES:
                raise _LiveTranscriptPolicyError(
                    f"live evidence contains an unapproved tool call: {function_name!r}"
                )
            arguments = _readonly_skill_json_object(
                function.get("arguments"),
                f"message {row.get('id')} {function_name} arguments",
            )
            if function_name in READONLY_SKILL_TOOL_NAMES:
                arguments, _ = _readonly_skill_argument_metadata(
                    function_name, arguments, row.get("id")
                )
            if call_id not in result_rows:
                raise _LiveTranscriptPolicyError(
                    f"approved tool call {call_id} has no persisted result"
                )
            result_position, result_row = result_rows[call_id]
            if result_position <= position:
                raise _LiveTranscriptPolicyError(
                    f"tool result {call_id} precedes its declaration"
                )
            if result_row.get("tool_name") != function_name:
                raise _LiveTranscriptPolicyError(
                    f"tool result {call_id} name does not match its declaration"
                )
            raw_result = result_row.get("content")
            if function_name in READONLY_SKILL_TOOL_NAMES:
                read_status, content_sha, datasage_discovered = _readonly_skill_result_metadata(
                    function_name, arguments, raw_result, result_row.get("id")
                )
            else:
                _readonly_skill_json_object(raw_result, f"call {call_id} DataSage result")
                read_status, content_sha, datasage_discovered = "observed", None, False
            declared_calls.add(call_id)
            used_results.add(call_id)
            calls.append({
                "call_id": call_id,
                "tool_name": function_name,
                "assistant_message_id": row.get("id"),
                "tool_message_id": result_row.get("id"),
                "arguments_sha256": _readonly_skill_sha256(arguments),
                "result_sha256": _readonly_skill_sha256(raw_result),
                "read_status": read_status,
                "content_sha256": content_sha,
                "datasage_discovered": datasage_discovered,
                "skill_name": (
                    arguments.get("name")
                    if function_name == "skill_view"
                    else None
                ),
                "skill_file_path": (
                    arguments.get("file_path", "SKILL.md")
                    if function_name == "skill_view"
                    else None
                ),
                "skill_category": (
                    arguments.get("category")
                    if function_name == "skills_list"
                    else None
                ),
                "skill_method_use_observed": False,
            })
    orphaned = sorted(call_id for call_id in result_rows if call_id not in used_results)
    if orphaned:
        raise _LiveTranscriptPolicyError(f"orphaned approved tool results: {orphaned!r}")

    tool_surface = _readonly_skill_tool_surface(
        tool_snapshot,
        snapshot_scope=snapshot_scope,
    )
    if tool_snapshot is not None:
        surface_tools = set(tool_surface["tools"] or [])
        missing_calls = {
            call["tool_name"] for call in calls if call["tool_name"] not in surface_tools
        }
        if missing_calls:
            raise _LiveTranscriptPolicyError(
                f"tool_snapshot is missing observed call names: {sorted(missing_calls)!r}"
            )

    return {
        "schema": READONLY_SKILL_TRACE_SCHEMA,
        "case_id": case_id,
        "case_binding": (
            "caller_label_unbound" if case_id is not None else "not_supplied"
        ),
        "source": {
            "format": OFFICIAL_EXPORT_FORMAT,
            "session_id": session_id,
            "session_export_sha256": export_sha,
        },
        "tool_surface": tool_surface,
        "calls": calls,
        "call_count": len(calls),
        "skill_call_count": sum(call["tool_name"] in READONLY_SKILL_TOOL_NAMES for call in calls),
        "skill_discovery_observed": any(call["datasage_discovered"] for call in calls),
        "calls_only_allowed": True,
        "skill_method_use_observed": False,
        "runtime_side_effects_observed": "not_observed",
        "business_replay_required": True,
    }


def extract_final_answer_observations(
    payload: bytes | str,
    bindings: dict[str, object],
) -> dict[str, object]:
    """Extract final answer text from an official export with bound endpoints.

    The result is a sidecar for the Golden scorer.  It deliberately requires
    the official export bytes and the same turn bindings used by the existing
    replay adapter, so callers cannot manufacture answer text from a candidate
    case's self-reported fields.  No model, database, network, or session
    state is read.
    """

    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    session, messages = _export_session(raw)
    export_sha = _sha256_bytes(raw)
    if not isinstance(bindings, dict):
        raise _LiveTranscriptPolicyError("answer bindings must be an object")
    if bindings.get("schema") not in {
        "datasage-canary-bindings/v2",
        "datasage-canary-bindings/v3-live-fixture",
    }:
        raise _LiveTranscriptPolicyError(
            "answer bindings schema is not an official canary binding schema"
        )
    if bindings.get("session_export_sha256") != export_sha:
        raise _LiveTranscriptPolicyError(
            "answer bindings session export identity does not match official export"
        )
    turns = bindings.get("turns")
    if not isinstance(turns, list) or not turns:
        raise _LiveTranscriptPolicyError("answer bindings must contain non-empty turns")

    exported_session_id = session.get("id") or session.get("session_id")
    if not isinstance(exported_session_id, str) or not exported_session_id:
        raise _LiveTranscriptPolicyError("official export session ID is missing")
    positions: dict[int, int] = {}
    previous_id: int | None = None
    for position, row in enumerate(messages):
        if not isinstance(row, dict):
            raise _LiveTranscriptPolicyError("official export messages must be objects")
        message_id = row.get("id")
        if type(message_id) is not int or message_id <= 0:
            raise _LiveTranscriptPolicyError("official export message IDs are invalid")
        if previous_id is not None and message_id <= previous_id:
            raise _LiveTranscriptPolicyError(
                "official export message IDs are not strictly increasing"
            )
        if message_id in positions:
            raise _LiveTranscriptPolicyError("official export has duplicate message IDs")
        if row.get("session_id") != exported_session_id:
            raise _LiveTranscriptPolicyError("official export contains a cross-session message")
        if type(row.get("active")) is not int or row["active"] != 1:
            raise _LiveTranscriptPolicyError("official export contains an inactive message")
        positions[message_id] = position
        previous_id = message_id

    observations: list[dict[str, object]] = []
    seen_case_ids: set[str] = set()
    for index, binding in enumerate(turns):
        if not isinstance(binding, dict):
            raise _LiveTranscriptPolicyError(f"answer turn {index} must be an object")
        required = {
            "test_id",
            "conversation_id",
            "turn",
            "session_id",
            "user_message_id",
            "final_message_id",
        }
        if not required.issubset(binding) or set(binding).difference(
            required | {"canonical_prompt_sha256"}
        ):
            raise _LiveTranscriptPolicyError(
                f"answer turn {binding.get('test_id')!r} has invalid binding keys"
            )
        case_id = binding.get("test_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise _LiveTranscriptPolicyError("answer turn IDs must be unique non-empty strings")
        seen_case_ids.add(case_id)
        if binding.get("session_id") != exported_session_id:
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} session binding does not match export"
            )
        user_id = binding.get("user_message_id")
        final_id = binding.get("final_message_id")
        if type(user_id) is not int or type(final_id) is not int:
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} message IDs are invalid"
            )
        if user_id not in positions or final_id not in positions:
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} endpoints are not in the export"
            )
        user_position, final_position = positions[user_id], positions[final_id]
        if user_position >= final_position:
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} endpoint order is invalid"
            )
        user_row = messages[user_position]
        final_row = messages[final_position]
        if user_row.get("role") != "user" or final_row.get("role") != "assistant":
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} endpoints must be user then assistant"
            )
        if not isinstance(final_row.get("content"), str) or not final_row["content"]:
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} final assistant text is missing"
            )
        if final_row.get("tool_calls") not in (None, []):
            raise _LiveTranscriptPolicyError(
                f"answer turn {case_id!r} does not end in terminal assistant text"
            )
        if "canonical_prompt_sha256" in binding:
            prompt = user_row.get("content")
            if (
                not isinstance(prompt, str)
                or binding["canonical_prompt_sha256"]
                != _sha256_bytes(prompt.encode("utf-8"))
            ):
                raise _LiveTranscriptPolicyError(
                    f"answer turn {case_id!r} prompt hash does not match export"
                )
        final_text = final_row["content"]
        observations.append(
            {
                "case_id": case_id,
                "conversation_id": binding.get("conversation_id"),
                "turn": binding.get("turn"),
                "session_id": exported_session_id,
                "user_message_id": user_id,
                "final_message_id": final_id,
                "final_answer_sha256": _sha256_bytes(final_text.encode("utf-8")),
                "final_answer_text": final_text,
            }
        )
    return {
        "schema": FINAL_ANSWER_OBSERVATION_SCHEMA,
        "source": {
            "format": OFFICIAL_EXPORT_FORMAT,
            "session_export_sha256": export_sha,
        },
        "answers": observations,
    }

_export = _export_session
_turn_completion_policies = _live_turn_completion_policies
_endpoints = _live_endpoints
