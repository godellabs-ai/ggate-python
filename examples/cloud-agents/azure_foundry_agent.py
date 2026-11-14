"""Scan caller-controlled boundaries of a managed Foundry prompt agent."""

import asyncio
import inspect
import json
import os
from contextlib import ExitStack

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.identity import AzureCliCredential
from common import client, require_scan, session_id, verify
from order_tools import TOOLS


async def main():
    sid = session_id()
    sdk, transport = client("Managed Order Assistant", "azure")
    meta = {
        "session_id": sid,
        "framework": "foundry-agent-service",
        "provider": "azure-foundry",
    }
    endpoint = os.environ["AZURE_FOUNDRY_PROJECT_ENDPOINT"]
    tools = []
    for function in TOOLS:
        params = inspect.signature(function).parameters
        tools.append(
            FunctionTool(
                name=function.__name__,
                description=function.__doc__,
                strict=True,
                parameters={
                    "type": "object",
                    "properties": {p: {"type": "string"} for p in params},
                    "required": list(params),
                    "additionalProperties": False,
                },
            )
        )
    prompt = "Review order SO-1042 and prepare a dispatch recommendation."
    require_scan(sdk.scan_prompt(prompt, enforce=True, **meta))
    with ExitStack() as cleanup:
        credential = cleanup.enter_context(AzureCliCredential())
        project = cleanup.enter_context(
            AIProjectClient(
                endpoint=endpoint, credential=credential, allow_preview=True
            )
        )
        agent = project.agents.create_version(
            agent_name="ggate-orders-" + sid[:8],
            definition=PromptAgentDefinition(
                model=os.environ["AZURE_FOUNDRY_DEPLOYMENT"],
                tools=tools,
                instructions="First call read_order alone. Then call check_inventory, calculate_shipping and check_return_policy using the returned fields. Finally call prepare_dispatch. Call every tool once. Summarize in five short lines.",
            ),
        )
        cleanup.callback(project.agents.delete, agent_name=agent.name)
        cloud = cleanup.enter_context(project.get_openai_client(agent_name=agent.name))
        print(
            "AZURE  |  Foundry Agent Service\nManaged Claude agent | Manual scans at caller-controlled boundaries\n",
            flush=True,
        )
        conversation = cloud.conversations.create()
        cleanup.callback(cloud.conversations.delete, conversation_id=conversation.id)
        response = cloud.responses.create(input=prompt, conversation=conversation.id)
        for _ in range(8):
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                break
            outputs = []
            for item in calls:
                function = next(t for t in TOOLS if t.__name__ == item.name)
                require_scan(
                    sdk.scan_tool_call(
                        item.name, input_summary=item.arguments, enforce=True, **meta
                    )
                )
                result = json.dumps(function(**json.loads(item.arguments)))
                require_scan(
                    sdk.scan_tool_result(item.name, result, enforce=True, **meta)
                )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": result,
                    }
                )
            response = cloud.responses.create(
                input=outputs, conversation=conversation.id
            )
        else:
            raise RuntimeError("Managed agent exceeded the function round limit")
        if not response.output_text:
            raise RuntimeError("Managed agent did not return a final answer")
        require_scan(sdk.scan_response(response.output_text, enforce=True, **meta))
        print("\n" + response.output_text, flush=True)
        await verify(
            sdk,
            transport,
            sid,
            "Foundry Agent Service five-tool workflow",
            [t.__name__ for t in TOOLS],
        )
        print(
            "\nCoverage: four manual scan points around input, custom functions and final output."
        )
        print(
            "Boundary: service-internal model turns and hosted tools require server-side controls."
        )


if __name__ == "__main__":
    asyncio.run(main())
