import hashlib
import importlib
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_business_contracts as base

a = importlib.import_module(base.TEST_PACKAGE + ".acceptance_delivery")
r = importlib.import_module(base.TEST_PACKAGE + ".workflow_delivery_review")
io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")


class DeliveryBindingTests(unittest.TestCase):
    def binding(
        self,
        *,
        cycle_id="ls-hcm-2026-w38-g0-weekly",
        case_id="profile-v1-ls-hcm-2026-w38-g0-weekly",
        period="2026-W38",
        generation=0,
        phase="weekly",
        target="1" * 64,
        content="2" * 64,
        keys=("a" * 64,),
        business_scope=None,
    ):
        return a.make_delivery_binding(
            cycle_id=cycle_id,
            case_id=case_id,
            period=period,
            generation=generation,
            phase=phase,
            target_binding_digest=target,
            content_digest=content,
            component_keys=list(keys),
            progress_scope={"job": "acceptance-" + case_id, "period": case_id},
            business_scope=business_scope,
        )

    def receipt_with_state(self, binding, home):
        root = io.private_root(home)
        path = root / (
            r.base.digest(
                [
                    binding["progress_scope"]["job"],
                    binding["progress_scope"]["period"],
                ]
            )
            + ".json"
        )
        state = {
            "scope": binding["progress_scope"],
            "delivery_binding": binding,
            "components": {
                key: {"status": "provider_accepted"}
                for key in binding["component_keys"]
            },
        }
        path.write_text(json.dumps(state), encoding="utf-8")
        receipt = {
            "status": "provider_accepted_not_human_read",
            "case_id": binding["case_id"],
            "components": len(binding["component_keys"]),
            "progress_file": str(path),
            "delivery_binding": binding,
        }
        return receipt, path

    def test_verified_receipt_requires_binding_and_self_consistent_progress(self):
        binding = self.binding()
        with TemporaryDirectory() as directory:
            home = Path(directory)
            receipt, _ = self.receipt_with_state(binding, home)
            with patch.object(a, "runtime_home", return_value=home):
                self.assertTrue(r.accepted(receipt, binding))
                state = a.classify_receipt(receipt, binding)
            self.assertEqual("verified_for_reuse", state["classification"])

    def test_old_accepted_receipt_is_historical_but_not_reusable(self):
        old = {"status": "provider_accepted_not_human_read", "components": 1}
        self.assertEqual(
            "historical_provider_accepted",
            a.classify_receipt(old)["classification"],
        )
        self.assertFalse(r.accepted(old))

    def test_mixed_receipt_and_progress_binding_is_rejected(self):
        binding = self.binding()
        with TemporaryDirectory() as directory:
            home = Path(directory)
            receipt, path = self.receipt_with_state(binding, home)
            state = json.loads(path.read_text(encoding="utf-8"))
            state["delivery_binding"] = self.binding(content="3" * 64)
            path.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(a, "runtime_home", return_value=home):
                self.assertFalse(r.accepted(receipt, binding))

    def test_period_generation_and_target_are_reuse_bound_but_logical_id_is_not_target(self):
        original = self.binding()
        self.assertFalse(
            a.bindings_compatible(original, self.binding(target="4" * 64))
        )
        self.assertFalse(
            a.cross_entry_compatible(
                original,
                self.binding(
                    cycle_id="ls-hn-2026-w38-g0-weekly",
                    case_id="profile-v1-ls-hn-2026-w38-g0-weekly",
                    business_scope="hn",
                ),
            )
        )
        unknown_scope = self.binding(
            cycle_id="manual-2026-w38-g0-weekly",
            case_id="profile-v1-manual-2026-w38-g0-weekly",
            business_scope=None,
        )
        self.assertFalse(a.cross_entry_compatible(original, unknown_scope))
        self.assertFalse(
            a.bindings_compatible(
                original,
                self.binding(
                    cycle_id="ls-hcm-2026-w39-g0-weekly",
                    case_id="profile-v1-ls-hcm-2026-w39-g0-weekly",
                    period="2026-W39",
                ),
            )
        )
        self.assertFalse(
            a.bindings_compatible(
                original,
                self.binding(
                    cycle_id="ls-hcm-2026-w38-g1-weekly",
                    case_id="profile-v1-ls-hcm-2026-w38-g1-weekly",
                    generation=1,
                ),
            )
        )
        self.assertEqual(
            a.approved_target_binding_digest(
                [{"channel": "private", "logical_id": "logical-a"}]
            ),
            a.approved_target_binding_digest(
                [{"channel": "private", "logical_id": "logical-b"}]
            ),
        )

    def test_missing_component_rerun_sends_only_missing_component(self):
        sent = []

        class Transport:
            def normalize(self, items, progress):
                del progress
                return items

            def preflight(self, items):
                del items

            @contextmanager
            def delivery_lock(self, progress):
                del progress
                yield

            def fingerprint(self, item):
                return "fp-" + item["key"]

            def send(self, item):
                sent.append(item["key"])
                return {
                    "success": True,
                    "raw_response": {"errcode": 0},
                    "message_id": "synthetic-" + item["key"],
                }

        with TemporaryDirectory() as directory:
            profile = Path(directory)
            progress = io.Progress(profile, "job", "period")
            progress.run_id = "rerun"
            accepted_key = "a" * 64
            missing_key = "b" * 64
            notification = "n" * 64
            progress.data["components"][accepted_key] = {
                "status": "provider_accepted",
                "run_id": "previous",
                "fingerprint": "fp-" + accepted_key,
            }
            items = [
                {
                    "account": "private:logical",
                    "kind": "text",
                    "stage": "text",
                    "key": accepted_key,
                    "notification_key": notification,
                    "text": "already accepted",
                },
                {
                    "account": "private:logical",
                    "kind": "text",
                    "stage": "file",
                    "key": missing_key,
                    "notification_key": notification,
                    "text": "missing component",
                },
            ]
            io.deliver_components(items, Transport(), progress, enabled=True)
            self.assertEqual([missing_key], sent)
            self.assertEqual(
                "provider_accepted", progress.status(accepted_key)
            )
            self.assertEqual("provider_accepted", progress.status(missing_key))

    def test_deliver_batch_persists_binding_in_receipt_and_progress(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory)
            webhook = (
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                "00000000-0000-0000-0000-000000000000"
            )
            document = {
                "version": 1,
                "mode": "acceptance_only",
                "enabled": True,
                "private_target": "zhangzhengwei",
                "application": {
                    "name": "synthetic-app",
                    "corp_id": "synthetic-corp",
                    "agent_id": "1000043",
                    "corp_secret": "synthetic-secret",
                },
                "test_webhook": webhook,
                "approved_webhook_sha256": hashlib.sha256(
                    webhook.encode()
                ).hexdigest(),
                "max_logical_notifications_per_batch": 2,
                "max_messages_per_batch": 12,
            }
            (profile / a.SECRET_FILE).write_text(
                json.dumps(document), encoding="utf-8"
            )
            notice = {
                "channel": "private",
                "logical_id": "logical-a",
                "role": "role",
                "body": "body",
                "attachments": [],
            }
            with (
                patch.object(
                    a, "APPROVED_WEBHOOK_SHA256", document["approved_webhook_sha256"]
                ),
                patch.object(
                    a,
                    "APPROVED_CORP_SHA256",
                    hashlib.sha256(b"synthetic-corp").hexdigest(),
                ),
                patch.object(a.workflow, "deliver_components"),
            ):
                receipt = a.deliver_batch(
                    profile,
                    "profile-v1-ls-hcm-2026-w38-g0-weekly",
                    [notice],
                    cycle_id="ls-hcm-2026-w38-g0-weekly",
                )
            binding = receipt["delivery_binding"]
            self.assertTrue(a.validate_delivery_binding(binding))
            self.assertEqual("2026-W38", binding["period"])
            self.assertEqual(0, binding["generation"])
            self.assertNotIn("component_keys", receipt)
            self.assertTrue(binding["component_keys"])
            state = json.loads(Path(receipt["progress_file"]).read_text())
            self.assertEqual(binding, state["delivery_binding"])
            state.pop("delivery_binding")
            state["components"] = {"a" * 64: {"status": "failed"}}
            Path(receipt["progress_file"]).write_text(
                json.dumps(state), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                io.IOErrorBoundary, "DELIVERY_BINDING_LEGACY_REVIEW_REQUIRED"
            ):
                with (
                    patch.object(
                        a, "APPROVED_WEBHOOK_SHA256", document["approved_webhook_sha256"]
                    ),
                    patch.object(
                        a,
                        "APPROVED_CORP_SHA256",
                        hashlib.sha256(b"synthetic-corp").hexdigest(),
                    ),
                ):
                    a.deliver_batch(
                        profile,
                        "profile-v1-ls-hcm-2026-w38-g0-weekly",
                        [notice],
                        cycle_id="ls-hcm-2026-w38-g0-weekly",
                    )

    def test_review_reuses_same_business_content_without_equating_logical_ids(self):
        key = "ls-hcm-2026-w38-g0"
        current_notice = {
            "channel": "private",
            "logical_id": key + "-weekly",
            "role": "role",
            "body": "same business body",
            "attachments": [],
        }
        prior_notice = {
            **current_notice,
            "logical_id": "prior-logical",
            "body": "【真实来源验收，非生产派发】\nsame business body",
        }
        current = {
            "notices": [current_notice],
            "notice_digest": r.base.digest([current_notice]),
            "files": {},
            "evidence": {},
        }
        prior_cycle = key + "-weekly"
        prior = {
            "notices": [prior_notice],
            "notice_digest": r.base.digest([prior_notice]),
            "files": {},
            "evidence": {},
        }
        prior_binding = a.binding_for_manifest(
            Path("."),
            prior_cycle,
            prior["notices"],
            manifest=prior,
            case_id=a.case_id_for_cycle(prior_cycle),
        )

        class Store:
            def rows(self, *_args):
                return []

        with TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(r.delivery, "runtime_home", return_value=home), patch.object(
                r.io, "private_root", return_value=home
            ):
                progress = home / (
                    r.base.digest(
                        [
                            prior_binding["progress_scope"]["job"],
                            prior_binding["progress_scope"]["period"],
                        ]
                    )
                    + ".json"
                )
                progress.write_text(
                    json.dumps(
                        {
                            "scope": prior_binding["progress_scope"],
                            "delivery_binding": prior_binding,
                            "components": {
                                component: {"status": "provider_accepted"}
                                for component in prior_binding["component_keys"]
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                receipt = {
                    "status": "provider_accepted_not_human_read",
                    "case_id": prior_binding["case_id"],
                    "components": len(prior_binding["component_keys"]),
                    "progress_file": str(progress),
                    "delivery_binding": prior_binding,
                }
                manifest_path = home / "prior-manifest.json"
                manifest_path.write_text(json.dumps(prior), encoding="utf-8")
                (home / "send-result.json").write_text(
                    json.dumps(receipt), encoding="utf-8"
                )
                result = r.review(Store(), key, current)
            self.assertEqual(1, len(result["prior_matches"]))
            self.assertEqual([], result["legacy_unverified"])

    def test_review_retains_old_accepted_receipt_as_historical_only(self):
        key = "ls-hcm-2026-w38-g0"
        notice = {
            "channel": "private",
            "logical_id": key + "-weekly",
            "role": "role",
            "body": "same body",
            "attachments": [],
        }
        manifest = {
            "notices": [notice],
            "notice_digest": r.base.digest([notice]),
            "files": {},
            "evidence": {},
        }

        class Store:
            def rows(self, *_args):
                return []

        with TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(r.delivery, "runtime_home", return_value=home), patch.object(
                r.io, "private_root", return_value=home
            ):
                (home / "legacy-manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                (home / "send-result.json").write_text(
                    json.dumps(
                        {
                            "status": "provider_accepted_not_human_read",
                            "components": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                result = r.review(Store(), key, manifest)
            self.assertEqual([], result["prior_matches"])
            self.assertEqual(1, len(result["legacy_unverified"]))
            self.assertEqual(
                "historical_provider_accepted",
                result["legacy_unverified"][0]["classification"],
            )

    def test_status_projection_preserves_provider_counts_without_claiming_binding(self):
        summary = r.summarize_receipts(
            [
                (
                    {
                        "status": "provider_accepted_not_human_read",
                        "logical_notifications": 10,
                        "components": 9,
                    },
                    {"notices": []},
                )
            ]
        )
        self.assertEqual(10, summary["provider_logical_notifications"])
        self.assertEqual(9, summary["provider_components"])
        self.assertEqual(10, summary["historical_logical_notifications"])
        self.assertEqual(0, summary["verified_logical_notifications"])
        self.assertFalse(summary["all_component_receipts_verified"])

        failed = r.summarize_receipts(
            [
                (
                    {
                        "status": "failed",
                        "logical_notifications": 7,
                        "components": 6,
                    },
                    {"notices": []},
                )
            ]
        )
        self.assertEqual(0, failed["provider_components"])
        self.assertEqual(6, failed["recorded_components"])
        self.assertEqual(6, failed["unverified_components"])
        self.assertFalse(failed["all_component_receipts_verified"])


if __name__ == "__main__":
    unittest.main()
