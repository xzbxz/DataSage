import hashlib
import importlib
import json
import httpx
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_business_contracts as base


io = importlib.import_module(base.TEST_PACKAGE + ".workflow_io")
transport_mod = importlib.import_module(base.TEST_PACKAGE + ".wecom_app_transport")
wf = importlib.import_module(base.TEST_PACKAGE + ".legacy_workflow")


class PurchaseWebhookTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name)
        self.url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=production-secret"
        self.target_ref = hashlib.sha256(self.url.encode()).hexdigest()
        self.binding = {
            "send_enabled": True,
            "target_map": {
                "purchase-group": {
                    "platform": "wecom_webhook",
                    "target_kind": "robot_group",
                    "target_ref": self.target_ref,
                    "webhook_ref": "purchase_price",
                }
            },
        }
        self.doc = {
            "version": 1,
            "mode": "acceptance_only",
            "enabled": True,
            "private_target": "synthetic",
            "application": {
                "name": "synthetic",
                "corp_id": "synthetic",
                "agent_id": "1000043",
                "corp_secret": "synthetic-secret",
            },
            "test_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "production_webhooks": {
                "purchase_price": {
                    "enabled": True,
                    "url": self.url,
                    "sha256": self.target_ref,
                }
            },
        }
        (self.profile / "acceptance-delivery.secrets.json").write_text(
            json.dumps(self.doc), encoding="utf-8"
        )
        self.gate = patch.object(io, "require_action", return_value=self.binding)
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.clock = [100.0]
        self.sleeps = []
        self.events = []

    def factory(self, handler):
        return lambda: httpx.Client(
            transport=httpx.MockTransport(handler), timeout=1, follow_redirects=False
        )

    def make(self, handler):
        return transport_mod.ProductionWebhookTransport(
            "purchase_price",
            self.profile,
            client_factory=self.factory(handler),
            clock=lambda: self.clock[0],
            sleeper=self.sleeps.append,
        )

    def component(self, text="**采购价变更**"):
        return {
            "account": "purchase-group",
            "kind": "text",
            "message_format": "markdown",
            "text": text,
            "key": wf.digest(["purchase", text]),
            "notification_key": wf.digest(["purchase", "group"]),
        }

    def handler(self, request):
        self.events.append(request)
        self.assertEqual("/cgi-bin/webhook/send", request.url.path)
        self.assertEqual("production-secret", request.url.params["key"])
        body = json.loads(request.content)
        self.assertEqual("markdown", body["msgtype"])
        self.assertEqual("**采购价变更**", body["markdown"]["content"])
        return httpx.Response(200, json={"errcode": 0, "msgid": "synthetic-message"})

    def test_disabled_field_is_separate_from_acceptance_enabled_and_makes_no_http(self):
        self.doc["production_webhooks"]["purchase_price"]["enabled"] = False
        (self.profile / "acceptance-delivery.secrets.json").write_text(
            json.dumps(self.doc), encoding="utf-8"
        )
        with self.assertRaisesRegex(io.IOErrorBoundary, "PRODUCTION_WEBHOOK_DISABLED"):
            self.make(self.handler)
        self.assertEqual([], self.events)

    def test_markdown_send_has_explicit_business_code_and_no_secret_in_state(self):
        sender = self.make(self.handler)
        item = self.component()
        sender.preflight([item])
        sender.prepare([item], io.Progress(self.profile, "purchase_price", "price-events"))
        result = sender.send(item)
        self.assertTrue(result["success"])
        self.assertEqual({"errcode": 0}, result["raw_response"])
        self.assertEqual("synthetic-message", result["message_id"])
        self.assertEqual(1, len(self.events))
        self.assertEqual(0, len(self.sleeps))
        state = "".join(
            path.read_text(encoding="utf-8")
            for path in (self.profile / "report_runs").rglob("*.json")
        )
        self.assertNotIn(self.url, state)
        self.assertNotIn("production-secret", state)

    def test_nonzero_business_code_is_failed_and_missing_code_is_unknown(self):
        def failed(request):
            self.assertEqual("production-secret", request.url.params["key"])
            return httpx.Response(200, json={"errcode": 40058, "errmsg": "secret must not persist"})

        sender = self.make(failed)
        item = self.component()
        sender.preflight([item])
        result = sender.send(item)
        self.assertFalse(result["success"])
        self.assertEqual(40058, result["raw_response"]["errcode"])

        def missing(request):
            return httpx.Response(200, json={"msgid": "untrusted"})

        sender = self.make(missing)
        sender.preflight([item])
        result = sender.send(item)
        self.assertFalse(result["success"])
        self.assertIsNone(result["raw_response"]["errcode"])

    def test_http_timeout_is_unknown_without_url_or_token_in_error(self):
        def timeout(request):
            raise httpx.ReadTimeout("production-secret " + self.url, request=request)

        sender = self.make(timeout)
        item = self.component()
        sender.preflight([item])
        with self.assertRaisesRegex(io.IOErrorBoundary, "APPLICATION_HTTP_OUTCOME_UNKNOWN") as caught:
            sender.send(item)
        self.assertNotIn(self.url, str(caught.exception))
        self.assertNotIn("production-secret", str(caught.exception))

    def test_only_complete_markdown_text_is_accepted(self):
        sender = self.make(self.handler)
        complete = "采购🙂" * 350
        pieces = sender.normalize([self.component(complete)], io.Progress(self.profile, "purchase_price", "complete"))
        self.assertEqual([complete], [item["text"] for item in pieces])
        with self.assertRaisesRegex(io.IOErrorBoundary, "MESSAGE_TOO_LARGE"):
            sender.normalize([self.component("采购🙂" * 2500)], io.Progress(self.profile, "purchase_price", "long"))
        with self.assertRaisesRegex(io.IOErrorBoundary, "MARKDOWN_ONLY"):
            sender.normalize([{**self.component(), "message_format": "text"}], io.Progress(self.profile, "purchase_price", "format"))
        with self.assertRaisesRegex(io.IOErrorBoundary, "MARKDOWN_ONLY"):
            sender.normalize([{"account": "purchase-group", "kind": "file", "path": "x"}], io.Progress(self.profile, "purchase_price", "format-file"))

    def test_target_digest_and_credential_shape_are_required(self):
        wrong = dict(self.binding["target_map"]["purchase-group"])
        wrong["target_ref"] = "0" * 64
        with patch.object(io, "require_action", return_value={"send_enabled": True, "target_map": {"purchase-group": wrong}}):
            with self.assertRaisesRegex(io.IOErrorBoundary, "TARGET_INVALID"):
                self.make(self.handler)
        self.doc["production_webhooks"]["purchase_price"]["sha256"] = "0" * 64
        (self.profile / "acceptance-delivery.secrets.json").write_text(
            json.dumps(self.doc), encoding="utf-8"
        )
        with self.assertRaisesRegex(io.IOErrorBoundary, "CONFIGURATION_INVALID"):
            self.make(self.handler)

    def test_rate_clock_is_persisted_and_future_clock_fails_closed(self):
        sender = self.make(self.handler)
        sender._rate_limit()
        self.clock[0] = 100.5
        sender._rate_limit()
        self.assertEqual([2.7], self.sleeps)
        path = io.private_root(self.profile) / "webhook-send-clock-purchase_price.json"
        path.write_text(json.dumps({"last_attempt": 1000.0}), encoding="utf-8")
        with self.assertRaisesRegex(io.IOErrorBoundary, "RATE_CLOCK_REVIEW_REQUIRED"):
            sender._rate_limit()


if __name__ == "__main__":
    unittest.main()
