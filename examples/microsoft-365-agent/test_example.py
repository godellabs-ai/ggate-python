import os
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from app import create_app


class HostingTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_public_but_messages_require_authentication(self):
        settings = {
            "GGATE_CONSOLE_URL": "http://127.0.0.1:9", "GGATE_API_KEY": "test-key",
            "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "00000000-0000-0000-0000-000000000001",
            "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "00000000-0000-0000-0000-000000000002",
            "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "test-secret",
        }
        with patch.dict(os.environ, settings):
            async with TestClient(TestServer(create_app())) as client:
                response = await client.get("/health")
                self.assertEqual(response.status, 200)
                response = await client.post("/api/messages", json={"type": "message", "text": "hello"})
                self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
