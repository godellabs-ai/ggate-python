"""A five-tool Google ADK workflow with explicit Gödel Gate scan boundaries."""

import asyncio
import json
import os

from common import client, require_scan, session_id, verify
from google import genai
from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.oauth2.credentials import Credentials


def read_order(order_id: str) -> dict:
    """Read the product and quantity for a customer order."""
    return {
        "order_id": order_id,
        "product": "Standing desk",
        "quantity": 2,
        "destination": "Sydney",
    }


def check_inventory(product: str) -> dict:
    """Check whether the requested product is available."""
    return {"product": product, "available": 18, "warehouse": "Sydney"}


def calculate_shipping(destination: str) -> dict:
    """Calculate shipping for the order's destination."""
    return {"destination": destination, "cost_aud": 35, "business_days": 2}


def check_return_policy(product: str) -> dict:
    """Read the return window for the product."""
    return {
        "product": product,
        "return_window_days": 30,
        "condition": "Original packaging",
    }


def prepare_dispatch(order_id: str) -> dict:
    """Prepare a dispatch recommendation for human review; no order is changed."""
    return {"order_id": order_id, "status": "Ready for review", "warehouse": "Sydney"}


TOOLS = [
    read_order,
    check_inventory,
    calculate_shipping,
    check_return_policy,
    prepare_dispatch,
]


async def main():
    sid = session_id()
    sdk, transport = client("Order Operations Assistant", "google-cloud")
    metadata = {"session_id": sid, "framework": "google-adk"}
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "australia-southeast1")
    kwargs = {"vertexai": True, "project": project, "location": region}
    if os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
        kwargs["credentials"] = Credentials(
            token=os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]
        )
    cloud = genai.Client(**kwargs)

    async def before_model(callback_context, llm_request):
        # User text is scanned once per model invocation; tool results have their own boundary.
        for content in llm_request.contents:
            text = "\n".join(p.text for p in content.parts or [] if p.text)
            if content.role == "user" and text:
                require_scan(
                    await sdk.scan_prompt_async(text, enforce=True, **metadata)
                )

    async def after_model(callback_context, llm_response):
        text = "\n".join(
            p.text
            for p in (llm_response.content.parts if llm_response.content else [])
            if p.text and not p.thought
        )
        if text:
            require_scan(await sdk.scan_response_async(text, enforce=True, **metadata))

    async def before_tool(tool, args, tool_context):
        require_scan(
            await sdk.scan_tool_call_async(
                tool.name, input_summary=json.dumps(args), enforce=True, **metadata
            )
        )

    async def after_tool(tool, args, tool_context, tool_response):
        require_scan(
            await sdk.scan_tool_result_async(
                tool.name, json.dumps(tool_response), enforce=True, **metadata
            )
        )

    agent = Agent(
        name="order_operations",
        model=Gemini(model="gemini-2.5-flash", client=cloud),
        instruction="Use each of the five available tools exactly once. Summarize the order, stock, shipping, returns, and dispatch recommendation in five short lines. Do not invent results.",
        tools=TOOLS,
        before_model_callback=before_model,
        after_model_callback=after_model,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        generate_content_config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=1200,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name="order_operations", user_id="operations", session_id=sid
    )
    runner = Runner(agent=agent, app_name="order_operations", session_service=sessions)
    print("GOOGLE CLOUD  |  Order Operations Assistant")
    print(f"Google ADK + Gemini 2.5 Flash  |  Sydney ({region})")
    print("\nTask: Review order SO-1042 using all five tools.\n", flush=True)
    async for event in runner.run_async(
        user_id="operations",
        session_id=sid,
        new_message=types.Content(
            role="user",
            parts=[
                types.Part(
                    text="Review order SO-1042 using all five tools and prepare a dispatch recommendation."
                )
            ],
        ),
    ):
        if event.is_final_response() and event.content:
            print(
                "\n" + "\n".join(p.text for p in event.content.parts if p.text),
                flush=True,
            )
    await verify(
        sdk,
        transport,
        sid,
        "Google ADK five-tool workflow",
        [t.__name__ for t in TOOLS],
    )
    await runner.close()
    cloud.close()


if __name__ == "__main__":
    asyncio.run(main())
