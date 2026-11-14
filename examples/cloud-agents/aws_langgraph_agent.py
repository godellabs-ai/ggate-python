"""LangGraph with Claude on Amazon Bedrock and the Godel Gate callback."""

import asyncio
import os

from common import client, session_id, verify
from langchain_aws import ChatBedrockConverse
from langgraph.prebuilt import create_react_agent
from order_tools import TOOLS

from ggate.adapters.langchain import langchain_callback


async def main():
    sid = session_id()
    sdk, transport = client("Order Operations Assistant", "aws")
    model = ChatBedrockConverse(
        model=os.environ.get(
            "BEDROCK_MODEL_ID", "au.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        region_name="ap-southeast-2",
        max_tokens=1200,
    )
    callback = langchain_callback(
        framework="langgraph", sdk_client=sdk, session_id=sid, provider="aws-bedrock"
    )
    agent = create_react_agent(
        model,
        TOOLS,
        prompt="Read the order, check inventory, calculate shipping, check return policy, and prepare dispatch. Call each tool once. Summarize the results in five short lines.",
    )
    print(
        "AWS  |  LangGraph on Amazon Bedrock\nClaude in Australia | LangChain callback\n",
        flush=True,
    )
    result = await agent.ainvoke(
        {
            "messages": [
                ("user", "Review order SO-1042 and prepare a dispatch recommendation.")
            ]
        },
        config={"callbacks": [callback], "metadata": {"session_id": sid}},
    )
    print(result["messages"][-1].content)
    await verify(
        sdk,
        transport,
        sid,
        "AWS LangGraph five-tool workflow",
        [t.__name__ for t in TOOLS],
    )


if __name__ == "__main__":
    asyncio.run(main())
