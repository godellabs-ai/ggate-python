"""OpenAI Python client adapter."""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Iterable, List

from ._common import _MEDIA_BLOCKS, extract_attachments_from_value, latest_assistant_text, latest_user_message, message_role
from ..core.client import get_client


def instrument(*, client=None, **kwargs):
    if client is None:
        raise ValueError("OpenAI instrumentation requires client=<OpenAI() or AsyncOpenAI()>")
    return wrap_client(client, **kwargs)


def wrap_client(client, *, framework: str = "openai", sdk_client=None):
    return OpenAIClientWrapper(client, framework=framework, sdk_client=sdk_client or get_client())


class OpenAIClientWrapper:
    def __init__(self, target, *, framework: str, sdk_client):
        self._target = target
        self._framework = framework
        self._sdk = sdk_client

    def __getattr__(self, name):
        if name == "chat" and hasattr(self._target, "chat"):
            return _ChatWrapper(getattr(self._target, "chat"), self._framework, self._sdk)
        if name == "responses" and hasattr(self._target, "responses"):
            return _ResponsesWrapper(getattr(self._target, "responses"), self._framework, self._sdk)
        if name == "files" and hasattr(self._target, "files"):
            return _FilesWrapper(getattr(self._target, "files"), self._framework, self._sdk)
        if name == "beta" and hasattr(self._target, "beta"):
            return _BetaWrapper(getattr(self._target, "beta"), self._framework, self._sdk)
        if name == "run" and hasattr(self._target, "run"):
            return _RunMethodWrapper(getattr(self._target, "run"), self._framework, self._sdk).run
        return getattr(self._target, name)


class _ChatWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "completions" and hasattr(self._target, "completions"):
            return _CompletionsWrapper(getattr(self._target, "completions"), self._framework, self._sdk)
        return getattr(self._target, name)


class _ModelCallWrapper:
    """`create` / `parse` / `stream` of one OpenAI model-call API, sync or async, streaming or not."""

    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        return getattr(self._target, name)

    def _request(self, args, kwargs):
        raise NotImplementedError

    def _call(self, method, args, kwargs):
        original = getattr(self._target, method)
        request, model = self._request(args, kwargs)
        meta = {"framework": self._framework, "provider": "openai", "model": model, **_call_meta(kwargs)}
        if inspect.iscoroutinefunction(original):
            return self._call_async(original, request, meta, args, kwargs)
        _scan_turn(self._sdk, request, meta)
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, meta)
        if _is_stream(result):
            return _Stream(result, lambda text: _scan_answer(self._sdk, text, {}, meta))
        _scan_answer(self._sdk, _response_to_text(result), _reasoning(result), meta)
        return result

    async def _call_async(self, original, request, meta, args, kwargs):
        await asyncio.to_thread(_scan_turn, self._sdk, request, meta)
        result = await original(*args, **kwargs)
        if _is_stream(result):
            return _Stream(result, lambda text: _scan_answer(self._sdk, text, {}, meta))
        await asyncio.to_thread(_scan_answer, self._sdk, _response_to_text(result), _reasoning(result), meta)
        return result

    def create(self, *args, **kwargs):
        return self._call("create", args, kwargs)

    def parse(self, *args, **kwargs):
        return self._call("parse", args, kwargs)

    def stream(self, *args, **kwargs):
        # A stream manager delivers its text directly to the caller: the prompt is gated, the answer
        # is not seen. Use create(stream=True) to record it.
        request, model = self._request(args, kwargs)
        _scan_turn(self._sdk, request, {"framework": self._framework, "provider": "openai", "model": model, **_call_meta(kwargs)})
        return self._target.stream(*args, **kwargs)


class _CompletionsWrapper(_ModelCallWrapper):
    def _request(self, args, kwargs):
        return kwargs.get("messages") or _arg_at(args, 1) or [], kwargs.get("model") or _arg_at(args, 0)


class _ResponsesWrapper(_ModelCallWrapper):
    def _request(self, args, kwargs):
        return kwargs.get("input") or _arg_at(args, 0), kwargs.get("model")


def _call_meta(kwargs) -> dict:
    """The conversation a request names: `metadata.session_id`, then a Responses `conversation`."""
    metadata = kwargs.get("metadata")
    session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
    conversation = kwargs.get("conversation")
    session_id = session_id or (_get(conversation, "id") if not isinstance(conversation, str) else conversation)
    return {"session_id": str(session_id)} if session_id else {}


def _scan_turn(sdk, request, meta) -> None:
    """Scan what this model call adds to the conversation: a new user turn or a tool result."""
    enforce = sdk.config.mode == "sync"
    if isinstance(request, (list, tuple)) and request and message_role(request[-1]) in {"tool", "function"}:
        last = request[-1]
        tool = str(_get(last, "name") or _get(last, "tool_call_id") or "tool")
        scan_meta = {k: v for k, v in meta.items() if k != "model"}
        sdk.scan_tool_result(tool, _input_to_text(_get(last, "content")), enforce=enforce, **scan_meta)
        return
    latest = latest_user_message(request)
    if latest is None:
        return
    text = _input_to_text(_get(latest, "content")) if isinstance(latest, dict) or hasattr(latest, "content") else _input_to_text(latest)
    attachments = extract_attachments_from_value(latest)
    if text or attachments:
        sdk.scan_prompt(text, attachments=attachments, enforce=enforce, **meta)


def _scan_answer(sdk, text, reasoning, meta) -> None:
    if text:
        sdk.scan_response(text, enforce=sdk.config.mode == "sync", **reasoning, **meta)


def _is_stream(result) -> bool:
    return hasattr(result, "__next__") or hasattr(result, "__anext__") or hasattr(result, "__iter__") and not isinstance(result, (str, bytes, dict, list, tuple)) and not hasattr(result, "choices") and not hasattr(result, "output_text")


def _chunk_text(chunk) -> str:
    if str(_get(chunk, "type") or "").endswith("output_text.delta"):
        return str(_get(chunk, "delta") or "")
    parts = []
    for choice in _get(chunk, "choices") or []:
        delta = _get(choice, "delta")
        content = _get(delta, "content") if delta is not None else None
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


class _Stream:
    """Passes a streamed answer through unchanged and scans the whole text when it ends.

    The chunks have already reached the caller by then, so a verdict here can record the answer and
    stop what follows, but cannot recall it.
    """

    def __init__(self, stream, on_done):
        self._stream = stream
        self._on_done = on_done
        self._parts: List[str] = []

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __iter__(self):
        for chunk in self._stream:
            self._parts.append(_chunk_text(chunk))
            yield chunk
        self._on_done("".join(self._parts))

    async def __aiter__(self):
        async for chunk in self._stream:
            self._parts.append(_chunk_text(chunk))
            yield chunk
        await asyncio.to_thread(self._on_done, "".join(self._parts))

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._stream.__aexit__(*exc)


class _FilesWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "create":
            return self.create
        if name in {"content", "retrieve_content"}:
            return _RunMethodWrapper(getattr(self._target, name), self._framework, self._sdk, response_only=True).run
        return getattr(self._target, name)

    def create(self, *args, **kwargs):
        file_obj = kwargs.get("file") or _arg_at(args, 0)
        attachment = _file_to_attachment(self._sdk, file_obj)
        if attachment:
            self._sdk.scan_prompt(
                f"OpenAI file upload: {attachment.filename or attachment.id or 'file'}",
                attachments=[attachment],
                enforce=self._sdk.config.mode == "sync",
                framework=self._framework,
                provider="openai",
            )
        result = self._target.create(*args, **kwargs)
        return result


class _BetaWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "threads" and hasattr(self._target, "threads"):
            return _ThreadsWrapper(getattr(self._target, "threads"), self._framework, self._sdk)
        return getattr(self._target, name)


class _ThreadsWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "messages" and hasattr(self._target, "messages"):
            return _ThreadMessagesWrapper(getattr(self._target, "messages"), self._framework, self._sdk)
        if name == "runs" and hasattr(self._target, "runs"):
            return _ThreadRunsWrapper(getattr(self._target, "runs"), self._framework, self._sdk)
        if name == "create":
            return _RunMethodWrapper(getattr(self._target, "create"), self._framework, self._sdk).run
        return getattr(self._target, name)


class _ThreadMessagesWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "create":
            return self.create
        if name in {"list", "retrieve"}:
            return _RunMethodWrapper(getattr(self._target, name), self._framework, self._sdk, response_only=True).run
        return getattr(self._target, name)

    def create(self, *args, **kwargs):
        content = kwargs.get("content") or _arg_at(args, 2)
        attachments = _attachments_from_openai_refs(kwargs.get("attachments") or [])
        self._sdk.scan_prompt(
            _input_to_text(content),
            attachments=attachments,
            enforce=self._sdk.config.mode == "sync",
            framework=self._framework,
            provider="openai",
            conversation_id=str(kwargs.get("thread_id") or _arg_at(args, 0) or ""),
        )
        return self._target.create(*args, **kwargs)


class _ThreadRunsWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "create":
            return self.create
        if name in {"create_and_poll", "stream"}:
            return _RunMethodWrapper(getattr(self._target, name), self._framework, self._sdk).run
        if name == "submit_tool_outputs":
            return self.submit_tool_outputs
        if name in {"retrieve", "list"}:
            return _RunMethodWrapper(getattr(self._target, name), self._framework, self._sdk, response_only=True).run
        return getattr(self._target, name)

    def create(self, *args, **kwargs):
        prompt = _input_to_text(kwargs.get("additional_messages")) or _input_to_text(kwargs.get("instructions"))
        if prompt:
            self._sdk.scan_prompt(
                prompt,
                enforce=self._sdk.config.mode == "sync",
                framework=self._framework,
                provider="openai",
                conversation_id=str(kwargs.get("thread_id") or _arg_at(args, 0) or ""),
            )
        result = self._target.create(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, self._framework, None)
        text = _response_to_text(result)
        if text:
            self._sdk.scan_response(text, framework=self._framework, provider="openai")
        return result

    def submit_tool_outputs(self, *args, **kwargs):
        outputs = kwargs.get("tool_outputs") or _arg_at(args, 2) or []
        for output in outputs:
            self._sdk.scan_tool_result(
                str(_get(output, "tool_call_id") or "tool"),
                _input_to_text(_get(output, "output")),
                enforce=self._sdk.config.mode == "sync",
                framework=self._framework,
                provider="openai",
            )
        return self._target.submit_tool_outputs(*args, **kwargs)


class _RunMethodWrapper:
    def __init__(self, target, framework, sdk, *, response_only: bool = False):
        self._target = target
        self._framework = framework
        self._sdk = sdk
        self._response_only = response_only

    def run(self, *args, **kwargs):
        meta = {"framework": self._framework, "provider": "openai", "model": kwargs.get("model")}
        if not self._response_only:
            request = kwargs.get("messages") or kwargs.get("input") or kwargs.get("task") or args
            if isinstance(request, tuple):  # Swarm: run(agent, messages)
                request = next((item for item in request if isinstance(item, (list, str))), None)
            _scan_turn(self._sdk, request, {k: v for k, v in meta.items() if v})
        result = self._target(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, meta)
        _scan_answer(self._sdk, _response_to_text(result), {}, meta)
        return result


async def _await_and_scan(awaitable, sdk, meta_or_framework, model=None):
    meta = meta_or_framework if isinstance(meta_or_framework, dict) else {"framework": meta_or_framework, "provider": "openai", "model": model}
    result = await awaitable
    await asyncio.to_thread(_scan_answer, sdk, _response_to_text(result), _reasoning(result), meta)
    return result


def _reasoning(result) -> dict:
    """The model's reasoning, when the provider returns it (`reasoning_content` / `reasoning`)."""
    parts = []
    for choice in _get(result, "choices") or []:
        message = _get(choice, "message")
        extra = getattr(message, "model_extra", None) or {}
        for key in ("reasoning_content", "reasoning"):
            value = _get(message, key) if not isinstance(message, dict) else message.get(key)
            value = value or extra.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
                break
    if not parts:
        return {}
    thinking = "\n".join(parts)
    return {"thinking": thinking, "thinking_len": len(thinking)}


def _arg_at(args, index):
    return args[index] if len(args) > index else None


def _messages_to_text(messages: Iterable[Any]) -> str:
    latest = latest_user_message(list(messages or []))
    return _input_to_text(_get(latest, "content")) if latest is not None else ""


def _input_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_input_to_text(item) for item in value) if part)
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type == "text":
            return _input_to_text(value.get("text"))
        elif block_type in _MEDIA_BLOCKS:
            return ""
        if any(k in value for k in ("image_url", "file_path", "path", "url", "input_audio")):
            return ""
        for key in (
            "text",
            "content",
            "input_text",
            "input",
            "prompt",
            "message",
            "messages",
            "replies",
            "response",
            "answer",
            "completion",
            "output",
            "result",
            "llm",
        ):
            if key in value:
                return _input_to_text(value[key])
        return "\n".join(
            part
            for key, item in value.items()
            if key not in {"meta", "metadata", "usage", "token_usage", "finish_reason"}
            for part in [_input_to_text(item)]
            if part
        )
    return str(value)


def _response_to_text(result: Any) -> str:
    if hasattr(result, "output_text"):
        return str(result.output_text)
    if isinstance(result, dict) and "output_text" in result:
        return str(result["output_text"])

    choices = _get(result, "choices") or []
    texts: List[str] = []
    for choice in choices:
        message = _get(choice, "message")
        content = _get(message, "content") if message is not None else _get(choice, "text")
        if content:
            texts.append(_input_to_text(content))
    if texts:
        return "\n".join(texts)
    data = _get(result, "data")
    if data:
        # A thread listing is newest-first and holds the user's messages too.
        answers = [item for item in data if str(_get(item, "role") or "") == "assistant"]
        if answers:
            return _input_to_text(_get(answers[0], "content"))
        return "" if any(_get(item, "role") for item in data) else _input_to_text(data)
    messages = _get(result, "messages")
    if messages:
        return latest_assistant_text(list(messages)) or (_input_to_text(messages) if not _has_roles(messages) else "")
    return ""


def _has_roles(messages) -> bool:
    return any(message_role(message) for message in messages)


def _get(value: Any, name: str):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _file_to_attachment(sdk, file_obj):
    name = getattr(file_obj, "name", None)
    if isinstance(file_obj, (str, os.PathLike)):
        name = os.fspath(file_obj)
    if not name:
        return None
    try:
        return sdk.attachment_from_path(name, source="upload")
    except Exception:
        return None


def _attachments_from_openai_refs(attachments):
    output = []
    for item in attachments or []:
        file_id = _get(item, "file_id")
        if file_id:
            output.append({"id": file_id, "source": "file_ref", "capture": "metadata_only"})
    return output
