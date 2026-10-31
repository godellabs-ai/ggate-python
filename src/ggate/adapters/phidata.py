"""Phidata/Agno adapter that wraps Agent run methods."""

from __future__ import annotations

from ._common import extract_attachments_from_value, scoped_metadata, sync_around, textify, wrap_method
from ..core.client import get_client
from ..core.event import current_session_id


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
        attachments = extract_attachments_from_value(kwargs) + extract_attachments_from_value(args)
        sdk.scan_prompt(textify(prompt), attachments=attachments, enforce=sdk.config.mode == "sync", framework=fw,
                        **_session_meta(kwargs, metadata, agent))

    def after(result, fw):
        text = textify(getattr(result, "content", result))
        if text:
            sdk.scan_response(text, enforce=sdk.config.mode == "sync", framework=fw, **_session_meta({}, metadata, agent, result))

    for method in ("run", "arun", "print_response", "aprint_response"):
        wrap_method(agent, method, sync_around(before, after, framework))
    return agent


def _session_meta(kwargs, metadata, agent, result=None) -> dict:
    """A run's own `session_id`, then ggate.session(), then the one Agno/Phidata keeps for the run.

    The answer reads it from its RunResponse, because the arguments of the run are gone by then;
    without it a prompt and its response land in different sessions.
    """
    meta = scoped_metadata(metadata)
    session_id = kwargs.get("session_id")
    if not session_id and not current_session_id():
        session_id = getattr(result, "session_id", None) or getattr(agent, "session_id", None)
    return {**meta, "session_id": str(session_id)} if session_id else meta
