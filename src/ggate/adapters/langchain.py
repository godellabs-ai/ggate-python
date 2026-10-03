"""LangChain and LangGraph callback adapter."""

from __future__ import annotations

from typing import Any, Iterable, List

from ..core.client import get_client
from ..core.event import current_session_id as event_context_session

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:
    class BaseCallbackHandler:  # type: ignore
        pass


def langchain_callback(*, framework: str = "langchain", sdk_client=None, **metadata):
    return GgateCallbackHandler(framework=framework, sdk_client=sdk_client or get_client(), metadata=metadata)


class GgateCallbackHandler(BaseCallbackHandler):
    # LangChain logs and ignores exceptions from callback handlers unless `raise_error` is set, which
    # turned every block into a logged warning while the model call went ahead.
    raise_error = True

    def __init__(self, *, framework: str = "langchain", sdk_client=None, metadata=None):
        super().__init__()
        self.framework = framework
        self.sdk = sdk_client or get_client()
        self.metadata = metadata or {}
        # LangChain passes run metadata to start callbacks only, so the session a run starts in is
        # remembered here for its end and tool callbacks (and for runs nested inside it).
        self._run_sessions: dict = {}

    def on_llm_start(self, serialized, prompts, **kwargs):
        text = "\n".join(str(p) for p in prompts or [])
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(kwargs)
        self.sdk.scan_prompt(text, attachments=attachments, enforce=True, framework=self.framework, **self._meta(kwargs))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        text = _messages_to_text(messages)
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(messages)
        attachments.extend(extract_attachments_from_value(kwargs))
        self.sdk.scan_prompt(text, attachments=attachments, enforce=True, framework=self.framework, **self._meta(kwargs))

    def on_llm_end(self, response, **kwargs):
        text = _llm_result_to_text(response)
        meta = self._meta(kwargs)
        self._run_sessions.pop(str(kwargs.get("run_id") or ""), None)
        if text:
            self.sdk.scan_response(text, framework=self.framework, **meta)

    def on_llm_error(self, error, **kwargs):
        self._run_sessions.pop(str(kwargs.get("run_id") or ""), None)

    def on_tool_start(self, serialized, input_str, **kwargs):
        tool = _name(serialized) or kwargs.get("name") or "tool"
        self.sdk.scan_tool_call(tool, input_summary=input_str, enforce=True, framework=self.framework, **self._meta(kwargs))

    def on_tool_end(self, output, **kwargs):
        tool = kwargs.get("name") or "tool"
        meta = self._meta(kwargs)
        self._run_sessions.pop(str(kwargs.get("run_id") or ""), None)
        self.sdk.scan_tool_result(tool, str(output), enforce=True, framework=self.framework, **meta)

    def on_tool_error(self, error, **kwargs):
        self._run_sessions.pop(str(kwargs.get("run_id") or ""), None)

    def _meta(self, kwargs):
        metadata = dict(self.metadata)
        run_metadata = kwargs.get("metadata") or {}
        # The app's conversation id wins: LangChain run metadata (`session_id`, or LangGraph's
        # `thread_id`), then a session bound with ggate.monitor(session_id=...), then the handler's
        # own metadata. Only without one does each run become its own session.
        run_id, parent_id = str(kwargs.get("run_id") or ""), str(kwargs.get("parent_run_id") or "")
        session_id = (
            run_metadata.get("session_id")
            or run_metadata.get("thread_id")
            or self._run_sessions.get(run_id)
            or self._run_sessions.get(parent_id)
            or event_context_session()
            or metadata.get("session_id")
            or parent_id
            or run_id
        )
        if run_id and session_id and len(self._run_sessions) < 10000:
            self._run_sessions[run_id] = str(session_id)
        metadata.update(
            {
                "session_id": str(session_id) if session_id else None,
                "request_id": str(kwargs.get("run_id") or "") or metadata.get("request_id"),
                "source_detail": {"langchain": _json_safe(kwargs.get("metadata"))}
                if kwargs.get("metadata")
                else metadata.get("source_detail"),
            }
        )
        return {k: v for k, v in metadata.items() if v}


def _messages_to_text(messages: Iterable[Any]) -> str:
    parts: List[str] = []
    for batch in messages or []:
        if isinstance(batch, (list, tuple)):
            for message in batch:
                txt = _message_to_text(message)
                if txt:
                    parts.append(txt)
        else:
            txt = _message_to_text(batch)
            if txt:
                parts.append(txt)
    return "\n".join(parts)


def _message_to_text(message: Any) -> str:
    from ._common import textify
    return textify(message)


def _llm_result_to_text(response: Any) -> str:
    generations = getattr(response, "generations", None)
    if generations:
        parts = []
        for generation_list in generations:
            for generation in generation_list:
                text = getattr(generation, "text", None)
                if text is None and getattr(generation, "message", None) is not None:
                    text = getattr(generation.message, "content", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(response) if response is not None else ""


def _name(serialized):
    if isinstance(serialized, dict):
        return serialized.get("name") or serialized.get("id")
    return getattr(serialized, "name", None)


def _json_safe(value):
    try:
        import json

        json.dumps(value)
        return value
    except Exception:
        return str(value)
