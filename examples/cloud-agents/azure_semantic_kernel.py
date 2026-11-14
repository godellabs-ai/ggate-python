"""Five-tool Semantic Kernel workflow using Claude on Microsoft Foundry."""

import asyncio
import os

from anthropic import AsyncAnthropic
from common import client, session_id, verify
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.anthropic import (
    AnthropicChatCompletion,
    AnthropicChatPromptExecutionSettings,
)
from semantic_kernel.connectors.ai.function_choice_behavior import (
    FunctionChoiceBehavior,
)
from semantic_kernel.functions import KernelArguments, kernel_function

from ggate.adapters.semantic_kernel import instrument


class OrderOperations:
    @kernel_function(description="Read the product and quantity for an order")
    def read_order(self, order_id: str) -> str:
        return f"Order {order_id}: two standing desks, destination Sydney."

    @kernel_function(description="Check stock for the product")
    def check_inventory(self, product: str) -> str:
        return f"{product}: 18 available in Sydney."

    @kernel_function(description="Calculate shipping to the destination")
    def calculate_shipping(self, destination: str) -> str:
        return f"Shipping to {destination}: AUD 35, two business days."

    @kernel_function(description="Read the product return policy")
    def check_return_policy(self, product: str) -> str:
        return f"{product}: 30-day returns in original packaging."

    @kernel_function(
        description="Prepare a dispatch recommendation; does not modify the order"
    )
    def prepare_dispatch(self, order_id: str) -> str:
        return f"Dispatch recommendation for {order_id}: ready for human review."


async def main():
    sid = session_id()
    sdk, transport = client("Order Operations Assistant", "azure")
    cloud = AsyncAnthropic(
        base_url=os.environ["AZURE_FOUNDRY_ANTHROPIC_URL"],
        api_key=os.environ["AZURE_FOUNDRY_API_KEY"],
    )
    kernel = Kernel()
    kernel.add_service(
        AnthropicChatCompletion(
            service_id="claude",
            ai_model_id=os.environ["AZURE_FOUNDRY_DEPLOYMENT"],
            api_key=os.environ["AZURE_FOUNDRY_API_KEY"],
            async_client=cloud,
        )
    )
    kernel.add_plugin(OrderOperations(), plugin_name="orders")
    instrument(kernel=kernel, sdk_client=sdk, session_id=sid, provider="azure-foundry")
    settings = AnthropicChatPromptExecutionSettings(
        service_id="claude",
        max_tokens=1200,
        function_choice_behavior=FunctionChoiceBehavior.Auto(
            maximum_auto_invoke_attempts=8, filters={"included_plugins": ["orders"]}
        ),
    )
    task = kernel.add_function(
        plugin_name="workflow",
        function_name="review",
        prompt="Review order SO-1042. First call orders.read_order by itself. Then use its product and destination to call orders.check_inventory, orders.calculate_shipping, and orders.check_return_policy exactly once each. Finally call orders.prepare_dispatch for SO-1042 exactly once. Never repeat a tool call. Summarize the five returned results in five short lines. Do not invent results.",
        prompt_execution_settings=settings,
    )
    print(
        "AZURE  |  Order Operations Assistant\nSemantic Kernel + Claude on Microsoft Foundry\n",
        flush=True,
    )
    try:
        result = await kernel.invoke(task, arguments=KernelArguments(settings=settings))
        print(str(result), flush=True)
        await verify(
            sdk,
            transport,
            sid,
            "Semantic Kernel five-tool workflow",
            [
                "read_order",
                "check_inventory",
                "calculate_shipping",
                "check_return_policy",
                "prepare_dispatch",
            ],
        )
    finally:
        await asyncio.to_thread(sdk.flush, 30)
        await cloud.close()


if __name__ == "__main__":
    asyncio.run(main())
