"""Offline reference identity/link checks, not an instruction router.

Formatting and peer-reference cycles do not prove or disprove semantic
consistency. Business definitions still need contract and human review.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

OWNER_RE = re.compile(r"^Rule ID: `([^`]+)`$", re.MULTILINE)
LINK_RE = re.compile(r"\[`(datasage\.[^`]+/v\d+)`\]\(([^)#]+\.md)(?:#[^)]*)?\)")


def _prose(text: str) -> str:
    """Example fences are content, not new authority declarations or links."""
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
                continue
            if run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
                continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def rule_link_errors(documents: Mapping[Path, str]) -> list[str]:
    """Verify unique declared owners and the identity of each local rule link.

    The input is an explicit document set. No filesystem, network, model call,
    or executable content is loaded by this checker.
    """
    texts = {path.resolve(): _prose(text) for path, text in documents.items()}
    owner_by_path: dict[Path, str] = {}
    path_by_owner: dict[str, Path] = {}
    errors: list[str] = []
    for path, text in texts.items():
        owners = OWNER_RE.findall(text)
        if not owners and path.name == "SKILL.md":
            # The entry Skill is a discovery index, not another rule authority.
            continue
        if len(owners) != 1:
            errors.append(f"{path.name}: expected one Rule ID, found {len(owners)}")
            continue
        owner = owners[0]
        if owner in path_by_owner:
            errors.append(f"{path.name}: duplicate Rule ID {owner!r}")
        owner_by_path[path] = owner
        path_by_owner[owner] = path
    for path, text in texts.items():
        for claimed_owner, link in LINK_RE.findall(text):
            target = (path.parent / link).resolve()
            if target not in texts:
                errors.append(f"{path.name}: rule target {link!r} is not inventoried")
            elif owner_by_path.get(target) != claimed_owner:
                errors.append(f"{path.name}: link label {claimed_owner!r} disagrees with {link!r}")
    return errors
