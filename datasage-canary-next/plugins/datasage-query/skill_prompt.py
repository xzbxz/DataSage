"""Hash-pinned, read-only DataSage guidance for WeCom turns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


MAIN_SKILL_PATH = "skills/datasage/SKILL.md"
_WECOM_PLATFORMS = frozenset({"wecom", "hermes-wecom"})


class SkillPromptIntegrityError(RuntimeError):
    """Raised when the governed main skill is absent or not release-pinned."""


def _profile_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _release_digest(profile_root: Path, relative_path: str) -> str:
    release_path = profile_root / ".release" / "RELEASE.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillPromptIntegrityError("release manifest is unavailable") from exc
    entries = release.get("payload_files") if isinstance(release, Mapping) else None
    if not isinstance(entries, list):
        raise SkillPromptIntegrityError("release manifest files are unavailable")
    matches = [
        entry.get("sha256")
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("path") == relative_path
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0], str)
        or len(matches[0]) != 64
    ):
        raise SkillPromptIntegrityError("main skill is not uniquely release-pinned")
    return matches[0].lower()


def load_pinned_main_skill(profile_root: Path | None = None) -> str:
    """Read and verify the release-owned main skill before freezing it."""

    root = (profile_root or _profile_root()).resolve(strict=True)
    skill_path = root / MAIN_SKILL_PATH
    try:
        if skill_path.is_symlink() or not skill_path.is_file():
            raise SkillPromptIntegrityError("main skill is not a regular file")
        raw = skill_path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise SkillPromptIntegrityError("main skill is unavailable") from exc
    expected = _release_digest(root, MAIN_SKILL_PATH)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise SkillPromptIntegrityError("main skill does not match its release digest")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPromptIntegrityError("main skill is not valid UTF-8") from exc
    if not content.strip():
        raise SkillPromptIntegrityError("main skill is empty")
    return content


def build_wecom_skill_hook(main_skill: str):
    """Return a hook whose captured guidance cannot change during the process."""

    frozen = str(main_skill)
    context = (
        "<datasage_main_skill immutable=\"true\" source=\"release-manifest\">\n"
        + frozen
        + "\n</datasage_main_skill>"
    )

    def history_contains_frozen_context(
        conversation_history: Any,
        user_message: Any,
    ) -> bool:
        if not isinstance(conversation_history, (list, tuple)):
            return False
        messages = list(conversation_history)
        # Hermes invokes pre_llm_call after appending the current clean user
        # message. Exclude that message so only replayed prompt state decides
        # whether the frozen skill survived an ordinary turn or compaction.
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, Mapping)
                and message.get("role") == "user"
                and message.get("content") == user_message
            ):
                messages = messages[:index]
                break
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            for field in ("api_content", "content"):
                value = message.get(field)
                if isinstance(value, str) and context in value:
                    return True
        return False

    def inject_main_skill(
        *,
        platform: str = "",
        conversation_history: Any = None,
        is_first_turn: bool = False,
        user_message: Any = None,
        **_kwargs: Any,
    ):
        if str(platform).strip().casefold() not in _WECOM_PLATFORMS:
            return None
        if not is_first_turn and history_contains_frozen_context(
            conversation_history,
            user_message,
        ):
            return None
        return {"context": context}

    return inject_main_skill


def frozen_wecom_skill_hook():
    """Load once at plugin registration, then serve only the frozen value."""

    return build_wecom_skill_hook(load_pinned_main_skill())
