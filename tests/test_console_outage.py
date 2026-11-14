"""The Console's "behaviour when the Console is down" setting (``console_outage_action``)."""

import io
import json
import unittest
import urllib.error

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport
from ggate.core.decision import CONSOLE_OUTAGE_MESSAGE
from ggate.exceptions import (
    GgateBlockedError,
    GgateConsoleUnreachableError,
    GgateTransportError,
)

PASS = {"kind": "verdict", "verdict": "pass", "message": "allowed"}
# The Console's answer when the rule blocks this user and every other user of this agent.
BLOCK_ALL = {**PASS, "console_outage_action": "block", "console_outage_fallback": "block"}


class ScriptedTransport:
    """Answers each scan with the next scripted value; exceptions are raised."""

    def __init__(self, *replies):
        self.replies = list(replies)

    def request(self, request):
        if request.get("op") != "scan_text":
            return {"kind": "ok"}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(*replies, cooldown=0):
    return Client(Config.from_values(mode="sync", cooldown=cooldown), transport=ScriptedTransport(*replies))


def refused():
    return GgateConsoleUnreachableError("refused")


class OutageActionTests(unittest.TestCase):
    def test_blocks_when_the_console_said_so_and_is_unreachable(self):
        sdk = client(BLOCK_ALL, refused())
        self.assertFalse(sdk.scan_prompt("hello").blocked)
        self.assertEqual(sdk.console_outage_action(), "block")

        decision = sdk.scan_prompt("hello")
        self.assertTrue(decision.blocked)
        self.assertFalse(decision.fail_open)
        self.assertEqual(decision.reason_codes, ["console_unreachable"])
        self.assertEqual(decision.message, CONSOLE_OUTAGE_MESSAGE)

    def test_answers_are_remembered_per_user(self):
        # The Console blocks Alice only: other users of this agent keep failing open.
        alice_only = {**PASS, "console_outage_action": "block", "console_outage_fallback": "allow"}
        sdk = client(alice_only, refused(), refused())
        sdk.scan_prompt("hello", user="Alice@Acme.com")
        self.assertEqual(sdk.console_outage_action("alice@acme.com"), "block")
        self.assertTrue(sdk.scan_prompt("hello", user="alice@acme.com").blocked)
        self.assertTrue(sdk.scan_prompt("hello", user="bob@acme.com").fail_open)

    def test_fail_closed_default_lets_a_listed_user_through(self):
        alice_exempt = {**PASS, "console_outage_action": "allow", "console_outage_fallback": "block"}
        sdk = client(alice_exempt, refused(), refused())
        sdk.scan_prompt("hello", user="alice@acme.com")
        self.assertTrue(sdk.scan_prompt("hello", user="alice@acme.com").fail_open)
        self.assertTrue(sdk.scan_prompt("hello", user="bob@acme.com").blocked)

    def test_enforcing_scan_raises_during_an_outage(self):
        sdk = client(BLOCK_ALL, refused())
        sdk.scan_prompt("hello")
        with self.assertRaises(GgateBlockedError):
            sdk.scan_prompt("hello", enforce=True)

    def test_cooldown_keeps_blocking_without_calling_the_console(self):
        sdk = client(BLOCK_ALL, refused(), cooldown=60)
        sdk.scan_prompt("hello")
        self.assertTrue(sdk.scan_prompt("hello").blocked)
        # The scripted transport has nothing left: this answer comes from the breaker.
        self.assertTrue(sdk.scan_prompt("hello").blocked)

    def test_a_console_that_answered_keeps_failing_open(self):
        sdk = client(BLOCK_ALL, GgateTransportError("HTTP 401"))
        sdk.scan_prompt("hello")
        decision = sdk.scan_prompt("hello")
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.fail_open)

    def test_allow_and_unknown_settings_fail_open(self):
        for first in (
            {**PASS, "console_outage_action": "allow", "console_outage_fallback": "allow"},
            PASS,
            {**PASS, "console_outage_action": "closed", "console_outage_fallback": "closed"},
        ):
            sdk = client(first, refused())
            sdk.scan_prompt("hello")
            self.assertTrue(sdk.scan_prompt("hello").fail_open)
        # Never answered: the setting is unknown, so the historical fail-open applies.
        self.assertTrue(client(refused()).scan_prompt("hello").fail_open)

    def test_operator_switching_back_to_allow_is_followed(self):
        allow_all = {**PASS, "console_outage_action": "allow", "console_outage_fallback": "allow"}
        sdk = client(BLOCK_ALL, allow_all, refused())
        sdk.scan_prompt("hello")
        sdk.scan_prompt("hello")
        self.assertFalse(sdk.scan_prompt("hello").blocked)


class _FailingTransport(ConsoleTransport):
    def __init__(self, failure):
        super().__init__(Config.from_values(console_url="https://console.example", api_key="godel_test"))
        self._access_token = "token"
        self._token_expires_at = float("inf")
        self.failure = failure

    def _urlopen(self, request):
        raise self.failure()


def http_error(status, body):
    return lambda: urllib.error.HTTPError(
        "https://console.example/api/v1/scan", status, "error", {}, io.BytesIO(body.encode())
    )


class TransportClassificationTests(unittest.TestCase):
    def scan(self, failure):
        with self.assertRaises(GgateTransportError) as raised:
            _FailingTransport(failure).request({"op": "scan_text", "event": {}, "redaction": {}})
        return raised.exception

    def test_no_connection_and_gateway_pages_are_unreachable(self):
        for failure in (
            lambda: urllib.error.URLError(ConnectionRefusedError("refused")),
            lambda: ConnectionResetError("reset"),
            http_error(502, "<html>Bad Gateway</html>"),
            http_error(503, ""),
        ):
            self.assertIsInstance(self.scan(failure), GgateConsoleUnreachableError)

    def test_console_answers_and_slow_scans_are_not_unreachable(self):
        for failure in (
            http_error(503, json.dumps({"error": "model deadline exceeded", "policy_resolved": True})),
            http_error(401, ""),
            lambda: TimeoutError("timed out"),
        ):
            self.assertNotIsInstance(self.scan(failure), GgateConsoleUnreachableError)


if __name__ == "__main__":
    unittest.main()
