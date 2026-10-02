"""Haystack adapter that wraps Pipeline/Agent run boundaries."""

from __future__ import annotations

from ._common import sync_around, textify, wrap_method
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
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(kwargs) + extract_attachments_from_value(args)
        sdk.scan_prompt(textify(payload), attachments=attachments, enforce=True, framework=fw, **metadata)

    def after(result, fw):
        text = textify(result)
        if text:
            sdk.scan_response(text, framework=fw, **metadata)

    for method in ("run", "run_async", "run_async_generator"):
        wrap_method(target, method, sync_around(before, after, framework))
    return target
