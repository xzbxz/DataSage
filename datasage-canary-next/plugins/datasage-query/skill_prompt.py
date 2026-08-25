"""Bounded, read-only access to the governed DataSage main Skill."""

from __future__ import annotations

from pathlib import Path
import stat


MAIN_SKILL_PATH = "skills/datasage/datasage/SKILL.md"
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
            root / "skills" / "datasage" / "datasage",
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
