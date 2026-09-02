import json
import unittest
from unittest.mock import patch

from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class _RecordingTransport(ConsoleTransport):
    def __init__(self, config):
        super().__init__(config)
        self.urls = []

    def _urlopen(self, request):
        self.urls.append(request.full_url)
        if request.full_url.endswith("/api/v1/detection-engine/token"):
            return _Response(
                {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 1}
            )
        if request.full_url.endswith("/api/v1/detection-engine/token/refresh"):
            return _Response(
                {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
            )
        return _Response({"verdict": "pass", "message": "allowed"})


class ConsoleTransportTests(unittest.TestCase):
    def test_private_ca_builds_a_verified_ssl_context(self):
        sentinel = object()
        with patch(
            "ggate.core.console_transport.ssl.create_default_context", return_value=sentinel
        ) as create_context:
            transport = ConsoleTransport(
                Config.from_values(
                    console_url="https://godels-gate.example.com",
                    api_key="godel_test",
                    console_ca_cert="/opt/ggate/console-ca.pem",
                )
            )
        create_context.assert_called_once_with(cafile="/opt/ggate/console-ca.pem")
        self.assertIs(transport._ssl_context, sentinel)

    def test_uses_canonical_detection_engine_auth_and_refresh(self):
        transport = _RecordingTransport(
            Config.from_values(
                console_url="https://godels-gate.example.com", api_key="godel_test"
            )
        )
        request = {"op": "scan_text", "event": {}, "redaction": {}}

        transport.request(request)
        transport.request(request)

        self.assertEqual(
            transport.urls.count("https://godels-gate.example.com/api/v1/detection-engine/token"),
            1,
        )
        self.assertEqual(
            transport.urls.count(
                "https://godels-gate.example.com/api/v1/detection-engine/token/refresh"
            ),
            1,
        )
        self.assertFalse(any("/api/v1/agent/token" in url for url in transport.urls))


if __name__ == "__main__":
    unittest.main()
