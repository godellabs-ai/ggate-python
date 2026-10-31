"""Haystack adapter that wraps Pipeline/Agent run boundaries."""

from __future__ import annotations

from ._common import extract_attachments_from_value, latest_assistant_text, latest_user_message, scoped_metadata, sync_around, textify, wrap_method
from ..core.client import get_client


def instrument(*, pipeline=None, agent=None, sdk_client=None, framework: str = "haystack", **metadata):
    target = pipeline or agent
    if target is None:
        return HaystackMonitor(sdk_client=sdk_client, framework=framework, **metadata)
    return wrap_target(target, sdk_client=sdk_client, framework=framework, **metadata)


class HaystackMonitor:
    def __init__(self, *, sdk_client=None, framework: str = "haystack", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def wrap(self, target):
        return wrap_target(target, sdk_client=self.sdk, framework=self.framework, **self.metadata)


def wrap_target(target, *, sdk_client=None, framework: str = "haystack", **metadata):
    sdk = sdk_client or get_client()

    def before(args, kwargs, fw):
        payload = kwargs.get("data") or kwargs.get("messages") or kwargs.get("user_prompt") or (args[0] if args else None)
        attachments = extract_attachments_from_value(kwargs) + extract_attachments_from_value(args)
        sdk.scan_prompt(_request_text(payload), attachments=attachments, enforce=sdk.config.mode == "sync", framework=fw, **scoped_metadata(metadata))

    def after(result, fw):
        text = _answer_text(result)
        if text:
            sdk.scan_response(text, enforce=sdk.config.mode == "sync", framework=fw, **scoped_metadata(metadata))

    for method in ("run", "run_async", "run_async_generator"):
        wrap_method(target, method, sync_around(before, after, framework))
    return target


def _request_text(payload) -> str:
    if isinstance(payload, (list, tuple)):
        return textify(latest_user_message(payload))
    if isinstance(payload, dict):  # pipeline data: {component: {input: value}}
        parts = []
        for value in payload.values():
            found = value.get("messages") if isinstance(value, dict) else None
            parts.append(_request_text(found) if found else textify(value))
        return "\n".join(part for part in parts if part)
    return textify(payload)


def _answer_text(result) -> str:
    """The answer of an Agent ({"messages": [...], "last_message": ...}) or pipeline ({"llm": {"replies": [...]}})."""
    if isinstance(result, (list, tuple)):
        return "".join(textify(getattr(chunk, "content", chunk)) for chunk in result)
    if isinstance(result, dict):
        last = result.get("last_message")
        if last is not None:
            return textify(last)
        if isinstance(result.get("messages"), (list, tuple)):
            return latest_assistant_text(result["messages"])
        if "replies" in result:
            return textify(result["replies"])
        return "\n".join(part for part in (_answer_text(value) for value in result.values() if isinstance(value, dict)) if part)
    return textify(result)
