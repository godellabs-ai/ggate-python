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

    # Semantic Kernel results must be its own FunctionResult objects, so a block is raised as
    # GgateBlockedError (the kernel wraps it in KernelInvokeException) instead of writing a string
    # into the context, which failed validation and broke every invocation.
    async def prompt_render_filter(self, context, next: Callable[[object], Awaitable[None]]) -> None:
        # The prompt exists only after rendering: render first, then scan what will be sent.
        await next(context)
        prompt = getattr(context, "rendered_prompt", None) or getattr(context, "prompt", None)
        # Images and files travel in the arguments (a ChatHistory with ImageContent/BinaryContent).
        from ._common import extract_attachments_from_value
        arguments = getattr(context, "arguments", None)
        attachments = extract_attachments_from_value(dict(arguments) if arguments is not None else None)
        if prompt or attachments:
            from ._common import strip_inline_media
            await self.sdk.scan_prompt_async(strip_inline_media(textify(prompt)), attachments=attachments, enforce=True,
                                             framework=self.framework, **self.metadata)

    async def function_invocation_filter(self, context, next: Callable[[object], Awaitable[None]]) -> None:
        function = getattr(context, "function", None)
        name = getattr(function, "name", None) or "function"
        # A prompt function is the model call itself (scanned by the render filter); only plugin
        # functions the model chose to call are tool calls.
        is_prompt = "Prompt" in type(function).__name__
        if not is_prompt:
            args = getattr(context, "arguments", None)
            await self.sdk.scan_tool_call_async(name, input_summary=textify(dict(args or {})), enforce=True,
                                                framework=self.framework, **self.metadata)
        await next(context)
        result = getattr(context, "result", None)
        value = getattr(result, "value", result)
        if value is None:
            return
        if is_prompt:
            await self.sdk.scan_response_async(textify(value), framework=self.framework, **self.metadata)
        else:
            await self.sdk.scan_tool_result_async(name, textify(value), enforce=True, framework=self.framework, **self.metadata)
