"""R21: one shared profile must not merge two members' private sessions.

The gateway keys a conversation per source; these tests pin the isolation the profile
relies on (per-member DM and group keys, profile namespacing), assert the profile never
turns per-user group sessions off, and keep the Skill's rules for corrections, fact time
and shared knowledge honest.
"""

from __future__ import annotations

from pathlib import Path
import unittest

import yaml

from gateway.config import Platform
from gateway.session import (
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "references"
)
OWNER = "wo-owner-0001"
COLLEAGUE = "wo-colleague-0002"
GROUP = "wo-group-chat-01"


def source(*, chat_type="dm", chat_id="", user_id=OWNER, **changes):
    return SessionSource(
        platform=Platform.WECOM,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        **changes,
    )


class SessionIsolationTests(unittest.TestCase):
    def test_each_member_gets_their_own_dm_session(self) -> None:
        owner_key = build_session_key(source())
        colleague_key = build_session_key(source(user_id=COLLEAGUE))

        # Matches the keys the running gateway writes (agent:main:wecom:dm:<member>).
        self.assertEqual(f"agent:main:wecom:dm:{OWNER}", owner_key)
        self.assertNotEqual(owner_key, colleague_key)
        self.assertNotIn(COLLEAGUE, owner_key)
        self.assertNotIn(OWNER, colleague_key)
        self.assertFalse(is_shared_multi_user_session(source()))

    def test_a_group_does_not_merge_its_members(self) -> None:
        owner_key = build_session_key(source(chat_type="group", chat_id=GROUP))
        colleague_key = build_session_key(
            source(chat_type="group", chat_id=GROUP, user_id=COLLEAGUE)
        )

        self.assertNotEqual(owner_key, colleague_key)
        for key, member in ((owner_key, OWNER), (colleague_key, COLLEAGUE)):
            with self.subTest(key=key):
                self.assertIn(GROUP, key)
                self.assertIn(member, key)
        self.assertFalse(
            is_shared_multi_user_session(source(chat_type="group", chat_id=GROUP))
        )

    def test_two_profiles_serving_one_chat_never_collide(self) -> None:
        self.assertEqual(
            f"agent:datasage-canary-next:wecom:dm:{OWNER}",
            build_session_key(source(), profile="datasage-canary-next"),
        )
        self.assertNotEqual(
            build_session_key(source(), profile="datasage-canary-next"),
            build_session_key(source(), profile="another-profile"),
        )

    def test_profile_never_disables_per_member_group_sessions(self) -> None:
        config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        for section in ("agent", "gateway"):
            value = (config.get(section) or {}).get("group_sessions_per_user")
            with self.subTest(section=section):
                self.assertIsNot(
                    False,
                    value,
                    "members of one group must not share one session on this profile",
                )

    def test_answer_boundary_keeps_the_correction_and_shared_state_rules(self) -> None:
        boundary = " ".join(
            (REFERENCES / "answer-boundary.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("## Shared profile, sessions and corrections", boundary)
        for rule in (
            "the newer statement replaces the earlier one",
            "the returned period or snapshot for every number",
            "do not carry one member's content into another member's answer",
            "not by itself a fact to remember",
            "survive compression",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, boundary)


if __name__ == "__main__":
    unittest.main()
