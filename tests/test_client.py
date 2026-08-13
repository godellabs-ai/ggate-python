import unittest

from ggate.core.client import Client
from ggate.core.config import Config


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.all_requests = []

    def request(self, request):
        self.all_requests.append(request)
        if request.get("op") != "collector_ready":
            self.requests.append(request)
        return self.response


class ClientTests(unittest.TestCase):
    def test_sync_prompt_sends_scan_text(self):
        transport = FakeTransport({"kind": "verdict", "verdict": "pass", "message": "allowed"})
        client = Client(Config.from_values(mode="sync"), transport=transport)

        decision = client.scan_prompt("hello", framework="openai", model="gpt-4o")

        self.assertFalse(decision.blocked)
        self.assertEqual(transport.requests[0]["op"], "scan_text")
        event = transport.requests[0]["event"]
        self.assertEqual(event["payload"]["surface"], "prompt")
        self.assertEqual(event["collector"]["labels"]["framework"], "openai")
        self.assertEqual(event["source"]["model"], "gpt-4o")

    def test_reports_collector_ready_in_background_on_init(self):
        transport = FakeTransport({"kind": "verdict", "verdict": "pass", "message": "allowed"})

        client = Client(Config.from_values(mode="async"), transport=transport)
        self.assertTrue(client.flush(timeout=5.0))

        ready = transport.all_requests[0]
        self.assertEqual(ready["op"], "collector_ready")
        self.assertEqual(ready["collector_type"], "sdk")
        self.assertEqual(ready["name"], "ggate-python-sdk")

    def test_fail_open_on_transport_error(self):
        class BrokenTransport:
            def request(self, request):
                from ggate.exceptions import GgateTransportError

                raise GgateTransportError("down")

        client = Client(Config.from_values(mode="sync"), transport=BrokenTransport())

        decision = client.scan_prompt("hello")

        self.assertTrue(decision.fail_open)
        self.assertTrue(decision.allowed)

    def test_breaker_fails_open_without_redialing(self):
        class CountingBrokenTransport:
            calls = 0

            def request(self, request):
                if request.get("op") == "collector_ready":
                    return {"kind": "ok"}
                CountingBrokenTransport.calls += 1
                from ggate.exceptions import GgateTransportError

                raise GgateTransportError("down")

        client = Client(Config.from_values(mode="sync"), transport=CountingBrokenTransport())
        client.scan_prompt("first")  # trips the breaker
        tripped = CountingBrokenTransport.calls
        decision = client.scan_prompt("second")  # inside the cooldown: no dial

        self.assertEqual(CountingBrokenTransport.calls, tripped)
        self.assertTrue(decision.fail_open)

    def test_decision_parses_detection_headline(self):
        transport = FakeTransport(
            {
                "kind": "verdict",
                "verdict": "block",
                "message": "blocked",
                "detection": {"source": "sensitive_data", "detail": "aws_access_key_id"},
            }
        )
        client = Client(Config.from_values(mode="sync"), transport=transport)

        decision = client.scan_prompt("hello")

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.detection["source"], "sensitive_data")

    def test_without_a_console_every_scan_fails_open(self):
        # The SDK has exactly one destination. With no Console configured there is nowhere to
        # scan, and the contract is to allow the call rather than break the application.
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            for key in ("GGATE_CONSOLE_URL", "GGATE_API_KEY"):
                os.environ.pop(key, None)
            client = Client(Config.from_values(mode="sync"))

        decision = client.scan_prompt("hello")

        self.assertTrue(decision.fail_open)
        self.assertTrue(decision.allowed)
        self.assertIn("GGATE_CONSOLE_URL", decision.message)

    def test_console_transport_is_the_only_transport(self):
        from ggate.core.console_transport import ConsoleTransport

        client = Client(
            Config.from_values(
                mode="sync", console_url="https://godels-gate.example.com", api_key="godel_x"
            )
        )

        self.assertIsInstance(client.transport, ConsoleTransport)


if __name__ == "__main__":
    unittest.main()
