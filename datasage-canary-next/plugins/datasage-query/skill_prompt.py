"""Bounded, read-only DataSage guidance for WeCom turns."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Any, Mapping


MAIN_SKILL_PATH = "skills/datasage/SKILL.md"
_WECOM_PLATFORMS = frozenset({"wecom", "hermes-wecom"})
_MAX_MAIN_SKILL_BYTES = 64 * 1024


class SkillPromptIntegrityError(RuntimeError):
    """Raised when the governed main skill is not a safe profile file."""


def _profile_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        raise SkillPromptIntegrityError("main skill path is unavailable") from exc
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def load_main_skill(profile_root: Path | None = None) -> str:
    """Read the Git-governed main skill through its fixed profile path."""

    try:
        candidate_root = Path(profile_root) if profile_root is not None else _profile_root()
        root = candidate_root.resolve(strict=True)
        if not root.is_dir() or _is_reparse(candidate_root):
            raise SkillPromptIntegrityError("profile root is not a regular directory")
        skill_path = root.joinpath(*MAIN_SKILL_PATH.split("/"))
        for candidate in (
            root / "skills",
            root / "skills" / "datasage",
            skill_path,
        ):
            if _is_reparse(candidate):
                raise SkillPromptIntegrityError("main skill path contains a reparse point")
        resolved_skill = skill_path.resolve(strict=True)
        if not resolved_skill.is_relative_to(root) or resolved_skill != skill_path:
            raise SkillPromptIntegrityError("main skill escapes the profile root")
        if not skill_path.is_file():
            raise SkillPromptIntegrityError("main skill is not a regular file")
        raw = skill_path.read_bytes()
    except SkillPromptIntegrityError:
        raise
    except OSError as exc:
        raise SkillPromptIntegrityError("main skill is unavailable") from exc
    if not 0 < len(raw) <= _MAX_MAIN_SKILL_BYTES:
        raise SkillPromptIntegrityError("main skill size is outside the allowed range")
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
        "<datasage_main_skill immutable=\"process\" source=\"profile-file\" "
        "authority=\"git\">\n"
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

    return build_wecom_skill_hook(load_main_skill())
