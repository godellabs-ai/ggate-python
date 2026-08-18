"""Shared test setup.

Every ``Config`` requires a declared agent name and owning team. The suite supplies them the way a
containerized deployment does — through the environment — so each test can keep naming only the
setting it is actually exercising. ``test_config.py`` removes them again where the requirement
itself is under test.
"""

import os

os.environ.setdefault("GGATE_AGENT_NAME", "Test Agent")
os.environ.setdefault("GGATE_TEAM", "Test Team")
