"""A short-lived agent's blocked admission path, with explicit audit flushing."""

import json
import os
import time
import uuid

import boto3

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport


class Receipts(ConsoleTransport):
    def __init__(self, config):
        super().__init__(config)
        self.events = []

    def request(self, request):
        result = super().request(request)
        if request.get("op") == "scan_text":
            self.events.append(
                {
                    "id": request["event"]["id"],
                    "surface": request["event"]["payload"]["surface"],
                    "verdict": result.get("verdict"),
                    "fail_open": result.get("fail_open", False),
                }
            )
        return result


def handler(event, context):
    session = event.get("session_id") or str(uuid.uuid4())
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["GGATE_SECRET_ARN"]
    )["SecretString"]
    config = Config.from_values(
        agent_name="Payroll Review Worker",
        team="Platform Engineering",
        user="payroll-operations",
        host="aws-lambda",
        workstation_id="aws-payroll-worker",
        api_key=secret,
        mode="sync",
        timeout=15,
        cooldown=0,
    )
    transport = Receipts(config)
    sdk = Client(config, transport=transport)
    meta = {"session_id": session, "framework": "aws-lambda", "provider": "aws-bedrock"}
    prompt = event["prompt"]
    decision = sdk.scan_prompt(prompt, **meta)
    if decision.fail_open:
        raise RuntimeError("Prompt scan failed; no model was called")
    if not decision.blocked:
        # This workload is intentionally restricted to its blocked-admission qualification path.
        # The caller must not mistake an allowed prompt for a successful Bedrock invocation.
        raise RuntimeError("Expected an admission block; no model was called")
    sdk.scan_response(
        "The payroll request was stopped before model invocation. No downstream action was performed.",
        **meta,
    )
    started = time.monotonic()
    flushed = sdk.flush(
        timeout=min(15, max(0, context.get_remaining_time_in_millis() / 1000 - 1))
    )
    if (
        not flushed
        or len(transport.events) != 2
        or any(r["fail_open"] for r in transport.events)
    ):
        raise RuntimeError("Audit delivery did not complete before exit")
    result = {
        "session_id": session,
        "blocked": True,
        "model_called": False,
        "flush_completed": flushed,
        "flush_ms": round((time.monotonic() - started) * 1000),
        "events": transport.events,
    }
    print(json.dumps(result))
    return result
