"""LlamaIndex callback adapter."""

from __future__ import annotations

from typing import Any

from ._common import extract_attachments_from_value, latest_user_message, scoped_metadata, textify
from ..core.client import get_client

try:
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
except Exception:
    class BaseCallbackHandler:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass


class GgateLlamaIndexCallbackHandler(BaseCallbackHandler):
    def __init__(self, *, sdk_client=None, framework: str = "llamaindex", **metadata):
        try:
            super().__init__(event_starts_to_ignore=[], event_ends_to_ignore=[])
        except TypeError:
            super().__init__()
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def on_event_start(self, event_type, payload=None, event_id="", parent_id="", **kwargs):
        # LlamaIndex also reports chunking, embedding, retrieval and synthesis events; each pair is
        # not a prompt and an answer. Only the model call is.
        if _event_name(event_type) != "llm":
            return
        data = _payload(payload)
        messages = data.get("messages")
        latest = latest_user_message(messages) if messages else data.get("formatted_prompt") or data.get("prompt") or data.get("template")
        attachments = extract_attachments_from_value(latest)
        text = textify(latest)
        if text or attachments:
            self.sdk.scan_prompt(
                text,
                attachments=attachments,
                enforce=self.sdk.config.mode == "sync",
                framework=self.framework,
                request_id=str(event_id or ""),
                **scoped_metadata(self.metadata),
            )

    def on_event_end(self, event_type, payload=None, event_id="", **kwargs):
        if _event_name(event_type) != "llm":
            return
        data = _payload(payload)
        answer = data.get("response") if data.get("response") is not None else data.get("completion")
        message = getattr(answer, "message", None)
        text = textify(message if message is not None else answer)
        if text:
            self.sdk.scan_response(
                text,
                enforce=self.sdk.config.mode == "sync",
                framework=self.framework,
                request_id=str(event_id or ""),
                **scoped_metadata(self.metadata),
            )

    def start_trace(self, trace_id=None):
        return None

    def end_trace(self, trace_id=None, trace_map=None):
        return None


def instrument(*, sdk_client=None, **metadata):
    return GgateLlamaIndexCallbackHandler(sdk_client=sdk_client or get_client(), **metadata)


def _event_name(event_type) -> str:
    return str(getattr(event_type, "value", event_type)).lower()


def _payload(payload) -> dict:
    """Payload keys are `EventPayload` members, which do not hash like their string values."""
    return {str(getattr(key, "value", key)): value for key, value in (payload or {}).items()}
