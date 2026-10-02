"""AutoGen/AG2 adapter that wraps public agent run methods."""

from __future__ import annotations

from typing import Any

from ._common import sync_around, textify, wrap_method
from ..core.client import get_client


def instrument(*, agent=None, team=None, sdk_client=None, framework: str = "autogen", **metadata):
    target = agent or team
    if target is None:
        return AutoGenMonitor(sdk_client=sdk_client, framework=framework, **metadata)
    return wrap_agent(target, sdk_client=sdk_client, framework=framework, **metadata)


class AutoGenMonitor:
    def __init__(self, *, sdk_client=None, framework: str = "autogen", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def wrap_agent(self, agent):
        return wrap_agent(agent, sdk_client=self.sdk, framework=self.framework, **self.metadata)


def wrap_agent(agent, *, sdk_client=None, framework: str = "autogen", **metadata):
    sdk = sdk_client or get_client()

    def before(args, kwargs, fw):
        prompt = kwargs.get("task") or kwargs.get("message") or (args[0] if args else None)
        if isinstance(prompt, (list, tuple)):
            prompt = [part for part in prompt if isinstance(part, str)] or prompt
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(kwargs)
        attachments.extend(extract_attachments_from_value(args))
        sdk.scan_prompt(textify(prompt), attachments=attachments, enforce=True, framework=fw, **metadata)

    def after(result, fw):
        text = _task_result_to_text(result)
        if text:
            sdk.scan_response(text, framework=fw, **metadata)

    # AutoGen AgentChat: run/run_stream. AG2 0.x: initiate_chat. AG2 1.x: ask/run.
    for method in ("run", "run_stream", "a_run", "a_run_stream", "initiate_chat", "ask", "a_ask"):
        wrap_method(agent, method, sync_around(before, after, framework))
    return agent


def _task_result_to_text(result: Any) -> str:
    messages = getattr(result, "messages", None)
    if messages:
        return textify(messages)
    return textify(result)
