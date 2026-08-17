"""Final model-wire constraints shared by every public DataSage tool."""

from __future__ import annotations

from functools import wraps
import json
from typing import Any, Callable

from . import settings


DEFAULT_TOOL_RESULT_CHAR_LIMIT = 90_000
HARD_TOOL_RESULT_CHAR_LIMIT = 95_000


def tool_result_char_limit() -> int:
    """Stay below Hermes' approximately 100k-character executor ceiling."""

    return settings.get_int(
        "max_tool_result_chars",
        DEFAULT_TOOL_RESULT_CHAR_LIMIT,
        4_096,
        HARD_TOOL_RESULT_CHAR_LIMIT,
    )


def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def enforce_tool_result_budget(tool_name: str, result: Any) -> str:
    """Return valid JSON within budget, replacing invalid/oversized output."""

    limit = tool_result_char_limit()
    if isinstance(result, str):
        rendered = result
    else:
        try:
            rendered = _compact_json(result)
        except (TypeError, ValueError):
            rendered = ""
    try:
        decoded = json.loads(rendered)
    except (TypeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict) and len(rendered) <= limit:
        return rendered

    reason = "OUTPUT_TOO_LARGE" if isinstance(decoded, dict) else "INVALID_TOOL_RESULT"
    replacement = _compact_json(
        {
            "status": "failed",
            "results": [],
            "error": {
                "code": reason,
                "message": (
                    "Tool result exceeds the model-safe context budget; "
                    "narrow the request or reduce detail."
                    if reason == "OUTPUT_TOO_LARGE"
                    else "The tool did not produce a valid JSON object."
                ),
                "retryable": False,
            },
            "tool": tool_name,
            "result_char_limit": limit,
        }
    )
    if len(replacement) > limit:  # Defensive for an operator-set tiny limit.
        replacement = '{"status":"failed","error":{"code":"OUTPUT_TOO_LARGE"}}'
    return replacement


def bounded_json_handler(tool_name: str, handler: Callable[..., Any]):
    """Wrap a public handler at its last boundary before Hermes dispatch."""

    @wraps(handler)
    def invoke(args: dict[str, Any], **kwargs: Any) -> str:
        return enforce_tool_result_budget(tool_name, handler(args, **kwargs))

    invoke.__datasage_result_budget__ = True
    return invoke
