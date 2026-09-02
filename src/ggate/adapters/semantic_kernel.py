"""Semantic Kernel filter adapter."""

from __future__ import annotations

from typing import Awaitable, Callable

from ._common import textify
from ..core.client import get_client


def instrument(*, kernel=None, sdk_client=None, framework: str = "semantic-kernel", **metadata):
    monitor = SemanticKernelMonitor(sdk_client=sdk_client, framework=framework, **metadata)
    if kernel is not None:
        monitor.register(kernel)
    return monitor


class SemanticKernelMonitor:
    def __init__(self, *, sdk_client=None, framework: str = "semantic-kernel", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def register(self, kernel):
        try:
            from semantic_kernel.filters import FilterTypes

            kernel.add_filter(FilterTypes.PROMPT_RENDERING, self.prompt_render_filter)
            kernel.add_filter(FilterTypes.FUNCTION_INVOCATION, self.function_invocation_filter)
        except Exception:
            # Older or JS-style objects can still attach these callables manually.
            pass
        return kernel

    async def prompt_render_filter(self, context, next: Callable[[object], Awaitable[None]]) -> None:
        prompt = getattr(context, "rendered_prompt", None) or getattr(context, "prompt", None) or textify(context)
        decision = await self.sdk.scan_prompt_async(
            textify(prompt),
            enforce=False,
            framework=self.framework,
            **self.metadata,
        )
        if decision.blocked:
            setattr(context, "result", decision.message)
            return
        await next(context)
        result = getattr(context, "result", None)
        if result is not None:
            await self.sdk.scan_response_async(textify(result), framework=self.framework, **self.metadata)

    async def function_invocation_filter(self, context, next: Callable[[object], Awaitable[None]]) -> None:
        function = getattr(context, "function", None)
        name = getattr(function, "name", None) or "function"
        args = getattr(context, "arguments", None)
        decision = await self.sdk.scan_tool_call_async(name, input_summary=textify(args), enforce=False, framework=self.framework, **self.metadata)
        if decision.blocked:
            setattr(context, "result", decision.message)
            return
        await next(context)
        result = getattr(context, "result", None)
        if result is not None:
            await self.sdk.scan_tool_result_async(
                name, textify(result), enforce=True, framework=self.framework, **self.metadata
            )
