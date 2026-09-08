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

LIVE_SESSION_META_ALLOWED_FIELDS = frozenset({
    *SESSION_META_CONVERSATIONAL_FIELDS,
    "id",
    "role",
    "tools",
    "model",
    "timestamp",
    "platform",
})

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

def _validate_live_turn_tool_flow(segment: list[dict[str, object]]) -> None:
    pending: list[tuple[str, str, int]] = []
    completed: set[str] = set()
    for relative_index, message in enumerate(segment):
        role = message.get("role")
        tool_calls = message.get("tool_calls")
        if tool_calls not in (None, []):
            if role != "assistant" or not isinstance(tool_calls, list):
                raise _LiveTranscriptPolicyError(
                    "captured tool flow has an invalid tool-call carrier"
                )
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
        if tool_calls in (None, []):
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
            if function_name not in LIVE_ALLOWED_TOOL_NAMES:
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
        _validate_live_turn_tool_flow(segment)
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

_export = _export_session
_turn_completion_policies = _live_turn_completion_policies
_endpoints = _live_endpoints
