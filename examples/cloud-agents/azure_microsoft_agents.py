"""Microsoft Agent Framework with explicit prompt, response and function middleware."""

import asyncio
import json
import os

from agent_framework import Agent, ChatMiddleware, FunctionMiddleware
from agent_framework_anthropic import AnthropicClient
from common import client, require_scan, session_id, verify
from order_tools import TOOLS


def text(value):
    if isinstance(value, list):
        return "\n".join(text(v) for v in value)
    if hasattr(value, "text"):
        return value.text or ""
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(), default=str)
    return json.dumps(value, default=str)


async def main():
    sid = session_id()
    sdk, transport = client("Order Operations Assistant", "azure")
    meta = {
        "session_id": sid,
        "framework": "microsoft-agent-framework",
        "provider": "azure-foundry",
    }

    class ScanChat(ChatMiddleware):
        async def process(self, context, call_next):
            if context.stream:
                raise RuntimeError("This application uses buffered responses")
            for message in context.messages:
                if message.role == "user" and message.text:
                    require_scan(
                        await sdk.scan_prompt_async(message.text, enforce=True, **meta)
                    )
            await call_next()
            output = text(context.result)
            if output:
                require_scan(
                    await sdk.scan_response_async(output, enforce=True, **meta)
                )

    class ScanFunctions(FunctionMiddleware):
        async def process(self, context, call_next):
            require_scan(
                await sdk.scan_tool_call_async(
                    context.function.name,
                    input_summary=text(context.arguments),
                    enforce=True,
                    **meta,
                )
            )
            await call_next()
            require_scan(
                await sdk.scan_tool_result_async(
                    context.function.name, text(context.result), enforce=True, **meta
                )
            )

    cloud = AnthropicClient(
        model=os.environ["AZURE_FOUNDRY_DEPLOYMENT"],
        base_url=os.environ["AZURE_FOUNDRY_ANTHROPIC_URL"],
        api_key=os.environ["AZURE_FOUNDRY_API_KEY"],
    )
    agent = Agent(
        client=cloud,
        name="Order Operations Assistant",
        tools=TOOLS,
        instructions="First call read_order by itself. Then use its returned product and destination to call check_inventory, calculate_shipping, and check_return_policy. Finally call prepare_dispatch. Call each tool exactly once. Summarize the five returned results in five short lines.",
        middleware=[ScanChat(), ScanFunctions()],
        default_options={"max_tokens": 1200},
    )
    print(
        "AZURE  |  Microsoft Agent Framework\nClaude on Microsoft Foundry | Prompt and function middleware\n",
        flush=True,
    )
    result = await agent.run(
        "Review order SO-1042 and prepare a dispatch recommendation."
    )
    print("\n" + result.text, flush=True)
    await verify(
        sdk,
        transport,
        sid,
        "Microsoft Agent Framework five-tool workflow",
        [t.__name__ for t in TOOLS],
    )
    print(
        "\nCoverage: two middleware classes scan prompts, function calls, function results and buffered responses."
    )


if __name__ == "__main__":
    asyncio.run(main())
