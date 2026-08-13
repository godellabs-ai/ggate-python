import os
import tempfile
import unittest
from unittest.mock import patch

from ggate.core.config import Config


class ConfigTests(unittest.TestCase):
    def test_reads_workstation_id_from_agent_config_yaml(self):
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


if __name__ == "__main__":
    unittest.main()
