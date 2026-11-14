"""Shared audit and verification helpers for cloud application examples."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport
from ggate.core.event import SDK_VERSION
from ggate.exceptions import GgateTransportError


class AuditTransport(ConsoleTransport):
    """Record actual Console replies without changing their verdicts."""

    def __init__(self, config):
        super().__init__(config)
        self.receipts = []

    def request(self, request):
        try:
            result = super().request(request)
        except GgateTransportError as error:
            if request.get("op") == "scan_text":
                self.receipts.append(
                    {
                        "event": request["event"],
                        "decision": {
                            "fail_open": True,
                            "message": str(error),
                        },
                    }
                )
            raise
        if request.get("op") == "scan_text":
            event = request["event"]
            payload = event["payload"]
            self.receipts.append({"event": event, "decision": result})
            verdict = result.get("verdict", "unknown").upper()
            label = (
                payload.get("tool")
                or payload.get("tool_name")
                or payload.get("surface")
            )
            print(f"  {verdict:7} {payload.get('surface', ''):13} {label}", flush=True)
        return result


def client(name, platform):
    config = Config.from_values(
        agent_name=os.environ.get("GGATE_AGENT_NAME", name),
        team=os.environ.get("GGATE_TEAM", "Platform Engineering"),
        mode="sync",
        timeout=30,
        cooldown=0,
        labels={"cloud_platform": platform},
    )
    transport = AuditTransport(config)
    return Client(config, transport=transport), transport


def require_scan(decision):
    if decision.fail_open:
        raise RuntimeError("Scan delivery failed: " + decision.message)
    if decision.blocked:
        raise RuntimeError("Action blocked: " + decision.message)
    return decision


def api(path, *, method="GET", body=None):
    headers = {
        "x-api-key": os.environ["GGATE_ADMIN_API_KEY"],
        "User-Agent": f"ggate-python-sdk/{SDK_VERSION}",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(
        os.environ["GGATE_CONSOLE_URL"].rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    ca = os.environ.get("GGATE_CONSOLE_CA_CERT")
    with urllib.request.urlopen(
        request, timeout=60, context=ssl.create_default_context(cafile=ca)
    ) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


async def verify(sdk, transport, session_id, name, expected_tools=()):
    if not await asyncio.to_thread(sdk.flush, 30):
        raise RuntimeError("Delivery queue did not drain")
    receipts = list(transport.receipts)
    if not receipts or any(r["decision"].get("fail_open") for r in receipts):
        raise RuntimeError("Missing or failed scans")
    payloads = [r["event"]["payload"] for r in receipts]
    missing_surfaces = {"prompt", "response"} - {p.get("surface") for p in payloads}
    if missing_surfaces:
        raise RuntimeError(f"Missing scan boundaries: {missing_surfaces}")
    for surface in ("mcp_tool", "tool_result"):
        names = {
            p.get("tool") or p.get("tool_name")
            for p in payloads
            if p.get("surface") == surface
        }
        missing = set(expected_tools) - names
        if missing:
            raise RuntimeError(f"Missing {surface} scans: {missing}")
    ids = {r["event"]["id"] for r in receipts}
    if not os.environ.get("GGATE_ADMIN_API_KEY"):
        print(
            f"Submitted {len(ids)} scan events. Review Activity for session {session_id}."
        )
        print(
            "Automatic reconciliation requires a separate trusted operator credential."
        )
        return {"session_id": session_id, "submitted": len(ids), "reconciled": False}
    stored = None
    for attempt in range(10):
        data = await asyncio.to_thread(
            api,
            "/api/v1/events?"
            + urllib.parse.urlencode({"session": session_id, "limit": 200}),
        )
        rows = data.get("events", []) if isinstance(data, dict) else data
        found = {r.get("event_id", r.get("id")) for r in rows}
        if ids <= found:
            stored = rows
            break
        await asyncio.sleep(2)
    if stored is None:
        raise RuntimeError(
            f"Console event reconciliation failed: {len(ids)} expected, {len(found)} visible"
        )
    result = {
        "name": name,
        "session_id": session_id,
        "passed": True,
        "scans": len(receipts),
        "receipts": receipts,
        "stored_events": stored,
    }
    destination = Path(os.environ.get("CLOUD_EVIDENCE_DIR", "evidence"))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f"{session_id}.json").write_text(
        json.dumps(result, indent=2, default=str)
    )
    print(
        f"\nVERIFIED  {len(receipts)} scans recorded in Activity; no missing events.",
        flush=True,
    )
    print(
        "Activity: "
        + os.environ["GGATE_CONSOLE_URL"].rstrip("/")
        + "/activities/"
        + session_id
    )
    return result


def session_id():
    return str(uuid.uuid4())
