"""LlamaIndex callback adapter."""

from __future__ import annotations

from typing import Any

from ._common import textify
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
        text = textify(payload)
        if text:
            from ._common import extract_attachments_from_value
            attachments = extract_attachments_from_value(payload)
            attachments.extend(extract_attachments_from_value(kwargs))
            self.sdk.scan_prompt(
                text,
                attachments=attachments,
                enforce=True,
                framework=self.framework,
                request_id=str(event_id or ""),
                **self.metadata,
            )

    def on_event_end(self, event_type, payload=None, event_id="", **kwargs):
        text = textify(payload)
        if text:
            self.sdk.scan_response(
                text,
                framework=self.framework,
                request_id=str(event_id or ""),
                **self.metadata,
            )

    def start_trace(self, trace_id=None):
        return None

    def end_trace(self, trace_id=None, trace_map=None):
        return None


def instrument(*, sdk_client=None, **metadata):
    return GgateLlamaIndexCallbackHandler(sdk_client=sdk_client or get_client(), **metadata)
