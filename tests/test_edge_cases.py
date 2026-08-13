import os
import tempfile
import unittest

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.exceptions import GgateBlockedError


class FakeTransport:
    def __init__(self, response=None):
        self.response = response or {"kind": "verdict", "verdict": "pass", "message": "allowed"}
        self.requests = []
        self.calls = []

    def request(self, request):
        import threading
        if request.get("op") != "collector_ready":
            self.requests.append(request)
        self.calls.append((threading.current_thread(), request))
        return self.response


class EdgeCaseTests(unittest.TestCase):
    def test_blocking_enforce_raises(self):
        transport = FakeTransport({"kind": "verdict", "verdict": "block", "message": "blocked"})
        client = Client(Config.from_values(mode="sync"), transport=transport)

        with self.assertRaises(GgateBlockedError):
            client.scan_prompt("blocked prompt", enforce=True)

    def test_async_mode_returns_without_transport_request_on_hot_path(self):
        import threading
        transport = FakeTransport()
        client = Client(Config.from_values(mode="async"), transport=transport)

        decision = client.scan_prompt("hello")

        self.assertTrue(decision.allowed)
        main_thread_scans = [
            req for thread, req in transport.calls
            if thread == threading.current_thread() and req.get("op") == "scan_text"
        ]
        self.assertEqual(main_thread_scans, [])

    def test_attachment_from_path_hashes_full_file_and_truncates_capture(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
            handle.write("secret=1234567890\n" * 10)
            path = handle.name
        try:
            client = Client(Config.from_values(mode="sync", capture_file_text=True, max_file_bytes=12), transport=FakeTransport())
            attachment = client.attachment_from_path(path)

            self.assertEqual(attachment.filename, os.path.basename(path))
            self.assertEqual(attachment.capture, "text")
            self.assertTrue(attachment.truncated)
            self.assertEqual(len(attachment.sha256), 64)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
