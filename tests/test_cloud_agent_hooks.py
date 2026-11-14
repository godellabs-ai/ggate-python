"""Run with examples/cloud-agents/requirements-aws.txt installed."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.exceptions import GgateTransportError

pytest.importorskip("strands")


@pytest.mark.parametrize("verdict", ["pass", "block", "unavailable"])
def test_strands_tool_output_waits_for_scan_before_delivery(monkeypatch, verdict):
    monkeypatch.syspath_prepend(
        str(Path(__file__).parents[1] / "examples/cloud-agents")
    )
    hooks_module = importlib.import_module("aws_strands_agent")
    scanned = []

    class Transport:
        def request(self, request):
            if request["op"] == "collector_ready":
                return {"kind": "ok"}
            scanned.append(request["event"])
            if verdict == "unavailable":
                raise GgateTransportError("test outage")
            return {"kind": "verdict", "verdict": verdict, "message": verdict}

    sdk = Client(Config.from_values(mode="sync", cooldown=0), transport=Transport())
    original = {
        "toolUseId": "tool-1",
        "status": "success",
        "content": [{"text": "order data"}],
    }
    event = SimpleNamespace(
        tool_use={"name": "read_order", "toolUseId": "tool-1"}, result=original
    )
    hooks_module.GateHooks(sdk, {"framework": "strands"}).after_tool(event)
    assert len(scanned) == 1
    assert scanned[0]["phase"] == "pre"
    if verdict == "pass":
        assert event.result is original
    else:
        assert event.result["status"] == "error"
        assert event.result["content"] == [
            {"text": "Tool output withheld by Godel Gate"}
        ]
