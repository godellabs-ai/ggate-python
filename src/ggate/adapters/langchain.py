"""LangChain and LangGraph callback adapter."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable, List

from ._common import (
    extract_attachments_from_value,
    latest_user_message,
    strip_prompt_echo,
    template_user_text,
    textify,
)
from ..core.client import get_client
from ..core.event import current_session_id

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:
    class BaseCallbackHandler:  # type: ignore
        pass

_MAX_TRACKED_RUNS = 10000


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
        # LangChain passes run metadata to start callbacks only, so what a run started with is kept
        # here for its end callback and for the runs nested inside it.
        self._runs: "OrderedDict[str, dict]" = OrderedDict()

    @property
    def _enforce(self) -> bool:
        return self.sdk.config.mode == "sync"

    def on_chain_start(self, serialized, inputs, **kwargs):
        self._track(kwargs)

    def on_chain_end(self, outputs, **kwargs):
        self._forget(kwargs)

    def on_chain_error(self, error, **kwargs):
        self._forget(kwargs)

    def on_llm_start(self, serialized, prompts, **kwargs):
        raw = "\n".join(str(p) for p in prompts or [])
        meta = self._meta(kwargs)
        self._runs[str(kwargs.get("run_id") or "")]["prompt"] = raw
        attachments = extract_attachments_from_value(kwargs.get("invocation_params"))
        self.sdk.scan_prompt(
            template_user_text(raw), attachments=attachments, enforce=self._enforce, framework=self.framework, **meta
        )

    def on_chat_model_start(self, serialized, messages, **kwargs):
        meta = self._meta(kwargs)
        for batch in messages or []:
            latest = latest_user_message(batch)
            if latest is None:
                continue  # a later step of this turn: tool results were scanned by on_tool_end
            self.sdk.scan_prompt(
                textify(latest),
                attachments=extract_attachments_from_value(latest),
                enforce=self._enforce,
                framework=self.framework,
                **meta,
            )

    def on_llm_end(self, response, **kwargs):
        meta = self._meta(kwargs)
        run = self._forget(kwargs)
        text = strip_prompt_echo(_llm_result_to_text(response), run.get("prompt"))
        if text:
            self.sdk.scan_response(text, enforce=self._enforce, framework=self.framework, **meta)

    def on_llm_error(self, error, **kwargs):
        self._forget(kwargs)

    def on_tool_start(self, serialized, input_str, **kwargs):
        tool = _name(serialized) or kwargs.get("name") or "tool"
        meta = self._meta(kwargs)
        self._runs[str(kwargs.get("run_id") or "")]["tool"] = tool
        self.sdk.scan_tool_call(tool, input_summary=input_str, enforce=self._enforce, framework=self.framework, **meta)

    def on_tool_end(self, output, **kwargs):
        meta = self._meta(kwargs)
        run = self._forget(kwargs)
        tool = kwargs.get("name") or run.get("tool") or "tool"
        self.sdk.scan_tool_result(tool, textify(output) or str(output), enforce=self._enforce, framework=self.framework, **meta)

    def on_tool_error(self, error, **kwargs):
        self._forget(kwargs)

    def _track(self, kwargs) -> str:
        """Record a run and return its session.

        The app's conversation id wins: LangChain run metadata (`session_id`, or LangGraph's
        `thread_id`), the session an enclosing run is in, ggate.session(), then the handler's own
        metadata. Only without any of those does the outermost run become the session, which keeps
        every event of one invocation together.
        """
        run_id, parent_id = str(kwargs.get("run_id") or ""), str(kwargs.get("parent_run_id") or "")
        run_metadata = kwargs.get("metadata") or {}
        known = self._runs.get(run_id) or self._runs.get(parent_id) or {}
        session_id = (
            run_metadata.get("session_id")
            or run_metadata.get("thread_id")
            or known.get("session_id")
            or current_session_id()
            or self.metadata.get("session_id")
            or parent_id
            or run_id
        )
        record = self._runs.setdefault(run_id, {})
        record["session_id"] = str(session_id) if session_id else None
        self._runs.move_to_end(run_id)
        while len(self._runs) > _MAX_TRACKED_RUNS:
            self._runs.popitem(last=False)
        return record["session_id"]

    def _forget(self, kwargs) -> dict:
        return self._runs.pop(str(kwargs.get("run_id") or ""), {})

    def _meta(self, kwargs):
        metadata = dict(self.metadata)
        metadata.update(
            {
                "session_id": self._track(kwargs),
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
        latest = latest_user_message(batch if isinstance(batch, (list, tuple)) else [batch])
        text = textify(latest)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _llm_result_to_text(response: Any) -> str:
    generations = getattr(response, "generations", None)
    if generations:
        parts = []
        for generation_list in generations:
            for generation in generation_list:
                text = getattr(generation, "text", None)
                if not text and getattr(generation, "message", None) is not None:
                    text = textify(getattr(generation.message, "content", None))
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
