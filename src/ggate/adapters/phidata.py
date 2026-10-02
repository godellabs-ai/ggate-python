"""Phidata/Agno adapter that wraps Agent run methods."""

from __future__ import annotations

from ._common import sync_around, textify, wrap_method
from ..core.client import get_client


def instrument(*, agent=None, sdk_client=None, framework: str = "phidata", **metadata):
    if agent is None:
        return PhidataMonitor(sdk_client=sdk_client, framework=framework, **metadata)
    return wrap_agent(agent, sdk_client=sdk_client, framework=framework, **metadata)


class PhidataMonitor:
    def __init__(self, *, sdk_client=None, framework: str = "phidata", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def wrap(self, agent):
        return wrap_agent(agent, sdk_client=self.sdk, framework=self.framework, **self.metadata)


def wrap_agent(agent, *, sdk_client=None, framework: str = "phidata", **metadata):
    sdk = sdk_client or get_client()

    def before(args, kwargs, fw):
        prompt = kwargs.get("input") or kwargs.get("message") or kwargs.get("prompt") or (args[0] if args else None)
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(kwargs) + extract_attachments_from_value(args)
        sdk.scan_prompt(textify(prompt), attachments=attachments, enforce=True, framework=fw, **metadata)

    def after(result, fw):
        text = textify(result)
        if text:
            sdk.scan_response(text, framework=fw, **metadata)

    for method in ("run", "arun", "print_response", "aprint_response"):
        wrap_method(agent, method, sync_around(before, after, framework))
    return agent
