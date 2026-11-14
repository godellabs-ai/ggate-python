"""Offline checks for the cloud examples' delivery and coverage verification."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport
from ggate.exceptions import GgateTransportError

spec = importlib.util.spec_from_file_location(
    "cloud_example_common",
    Path(__file__).parents[1] / "examples/cloud-agents/common.py",
)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def receipts():
    return [
        {
            "event": {
                "id": str(i),
                "payload": {"surface": surface, "tool": "read_order"},
            },
            "decision": {"verdict": "pass"},
        }
        for i, surface in enumerate(("prompt", "mcp_tool", "tool_result", "response"))
    ]


def verify(rows):
    return asyncio.run(
        common.verify(
            SimpleNamespace(flush=lambda timeout: True),
            SimpleNamespace(receipts=rows),
            "session",
            "example",
            ["read_order"],
        )
    )


def test_local_verification_does_not_claim_reconciliation(monkeypatch):
    monkeypatch.delenv("GGATE_ADMIN_API_KEY", raising=False)
    result = verify(receipts())
    assert result == {"session_id": "session", "submitted": 4, "reconciled": False}


@pytest.mark.parametrize("missing", range(4))
def test_missing_boundaries_rejected_without_admin_key(monkeypatch, missing):
    monkeypatch.delenv("GGATE_ADMIN_API_KEY", raising=False)
    rows = receipts()
    del rows[missing]
    with pytest.raises(RuntimeError, match="Missing"):
        verify(rows)


def test_failed_scan_is_retained_even_when_sdk_fails_open(monkeypatch):
    def request(self, request):
        if request["op"] == "collector_ready":
            return {"kind": "ok"}
        raise GgateTransportError("test transport failure")

    monkeypatch.setattr(ConsoleTransport, "request", request)
    config = Config.from_values(mode="sync", cooldown=0)
    transport = common.AuditTransport(config)
    sdk = Client(config, transport=transport)
    assert sdk.scan_prompt("hello").fail_open
    assert len(transport.receipts) == 1
    assert transport.receipts[0]["decision"]["fail_open"]
    with pytest.raises(RuntimeError, match="failed scans"):
        verify(receipts() + transport.receipts)


def test_workload_identity_overrides_are_preserved(monkeypatch):
    monkeypatch.setenv("GGATE_USER", "authenticated-caller")
    monkeypatch.setenv("GGATE_WORKSTATION_ID", "worker-17")
    monkeypatch.setenv("GGATE_TEAM", "Operations")
    monkeypatch.setattr(ConsoleTransport, "request", lambda *args: {"kind": "ok"})
    sdk, _ = common.client("Order assistant", "aws")
    assert sdk.config.user == "authenticated-caller"
    assert sdk.config.workstation_id == "worker-17"
    assert sdk.config.team == "Operations"
    assert sdk.flush(1)


def test_reconciliation_checks_submitted_event_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("GGATE_ADMIN_API_KEY", "test-only")
    monkeypatch.setenv("GGATE_CONSOLE_URL", "https://console.example")
    monkeypatch.setenv("CLOUD_EVIDENCE_DIR", str(tmp_path))
    rows = receipts()
    monkeypatch.setattr(
        common,
        "api",
        lambda path: {
            "events": [{"event_id": r["event"]["id"]} for r in rows],
        },
    )
    result = verify(rows)
    assert result["passed"]
    assert (tmp_path / "session.json").is_file()
