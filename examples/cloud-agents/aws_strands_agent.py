"""Strands on Bedrock with manual Gate prompt, tool and buffered-response scans."""

import asyncio
import json
import os

from common import client, require_scan, session_id, verify
from order_tools import TOOLS
from strands import Agent, tool
from strands.hooks import (
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
)
from strands.models import BedrockModel


class GateHooks(HookProvider):
    def __init__(self, sdk, metadata):
        self.sdk = sdk
        self.metadata = metadata

    def register_hooks(self, registry):
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_model(self, event):
        # Inspect the actual message history before each model request.
        require_scan(
            self.sdk.scan_prompt(
                json.dumps(event.agent.messages), enforce=True, **self.metadata
            )
        )

    def before_tool(self, event):
        decision = self.sdk.scan_tool_call(
            event.tool_use["name"],
            input_summary=json.dumps(event.tool_use["input"]),
            enforce=False,
            **self.metadata,
        )
        if decision.fail_open or decision.blocked:
            event.cancel_tool = "Action stopped by Godel Gate"

    def after_tool(self, event):
        decision = self.sdk.scan_tool_result(
            event.tool_use["name"],
            json.dumps(event.result, default=str),
            wait=True,
            before_delivery=True,
            enforce=False,
            **self.metadata,
        )
        if decision.fail_open or decision.blocked:
            event.result = {
                "toolUseId": event.tool_use["toolUseId"],
                "status": "error",
                "content": [{"text": "Tool output withheld by Godel Gate"}],
            }


async def main():
    sid = session_id()
    sdk, transport = client("Order Operations Assistant", "aws")
    meta = {"session_id": sid, "framework": "strands", "provider": "aws-bedrock"}
    model = BedrockModel(
        model_id=os.environ.get(
            "BEDROCK_MODEL_ID", "au.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        region_name="ap-southeast-2",
        max_tokens=1200,
    )
    agent = Agent(
        model=model,
        tools=[tool(t) for t in TOOLS],
        hooks=[GateHooks(sdk, meta)],
        callback_handler=None,
        system_prompt="Read the order, check inventory, calculate shipping, check return policy, then prepare dispatch. Call each tool exactly once. Summarize in five lines.",
    )
    print(
        "AWS  |  Strands on Amazon Bedrock\nManual model and tool hooks | Buffered final response\n",
        flush=True,
    )
    result = await asyncio.to_thread(
        agent, "Review order SO-1042 and prepare a dispatch recommendation."
    )
    answer = str(result)
    require_scan(sdk.scan_response(answer, enforce=True, **meta))
    print(answer)
    await verify(
        sdk,
        transport,
        sid,
        "AWS Strands five-tool workflow",
        [t.__name__ for t in TOOLS],
    )


if __name__ == "__main__":
    asyncio.run(main())
