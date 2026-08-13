import os
import tempfile
import unittest
from unittest.mock import patch

from ggate.core.config import Config


class ConfigTests(unittest.TestCase):
    def test_reads_workstation_id_from_device_config_yaml(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("workstation_id: ws-installer-123\norg_id: org-from-config\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"GGATE_CONFIG": path}, clear=False):
                os.environ.pop("GGATE_WORKSTATION_ID", None)
                os.environ.pop("GGATE_ORG_ID", None)
                cfg = Config.from_values()

            self.assertEqual(cfg.workstation_id, "ws-installer-123")
            self.assertEqual(cfg.org_id, "org-from-config")
            self.assertEqual(cfg.collector_id, "ws-installer-123:ggate-python-sdk")
        finally:
            os.unlink(path)

    def test_env_workstation_id_overrides_config_yaml(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("workstation_id: ws-from-config\n")
            path = handle.name
        try:
            with patch.dict(
                os.environ,
                {"GGATE_CONFIG": path, "GGATE_WORKSTATION_ID": "ws-from-env"},
                clear=False,
            ):
                cfg = Config.from_values()

            self.assertEqual(cfg.workstation_id, "ws-from-env")
        finally:
            os.unlink(path)

    def test_console_settings_are_what_make_it_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("GGATE_CONSOLE_URL", "GGATE_API_KEY"):
                os.environ.pop(key, None)
            self.assertFalse(Config.from_values().configured)
            self.assertTrue(
                Config.from_values(
                    console_url="https://godels-gate.example.com", api_key="godel_x"
                ).configured
            )
            # A URL without a key (or the reverse) is not a usable Console.
            self.assertFalse(Config.from_values(console_url="https://godels-gate.example.com").configured)

    def test_redaction_is_off_by_default(self):
        # The Console is the detection engine; masking client-side would hide what it exists
        # to catch. An explicit choice still wins.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GGATE_REDACT", None)
            self.assertFalse(Config.from_values().redact)
        self.assertTrue(Config.from_values(redact=True).redact)


if __name__ == "__main__":
    unittest.main()
