"""OpenAI Python client adapter."""

from __future__ import annotations

import inspect
import os
from typing import Any, Iterable, List

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


class _CompletionsWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "create":
            return self.create
        return getattr(self._target, name)

    def create(self, *args, **kwargs):
        prompt = _messages_to_text(kwargs.get("messages") or _arg_at(args, 1) or [])
        model = kwargs.get("model") or _arg_at(args, 0)
        self._sdk.scan_prompt(prompt, enforce=True, framework=self._framework, provider="openai", model=model)
        result = self._target.create(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, self._framework, model)
        self._scan_result(result, model)
        return result

    def _scan_result(self, result, model):
        text = _response_to_text(result)
        if text:
            self._sdk.scan_response(text, framework=self._framework, provider="openai", model=model)


class _ResponsesWrapper:
    def __init__(self, target, framework, sdk):
        self._target = target
        self._framework = framework
        self._sdk = sdk

    def __getattr__(self, name):
        if name == "create":
            return self.create
        return getattr(self._target, name)

    def create(self, *args, **kwargs):
        prompt = _input_to_text(kwargs.get("input") or _arg_at(args, 0))
        model = kwargs.get("model")
        self._sdk.scan_prompt(prompt, enforce=True, framework=self._framework, provider="openai", model=model)
        result = self._target.create(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, self._framework, model)
        text = _response_to_text(result)
        if text:
            self._sdk.scan_response(text, framework=self._framework, provider="openai", model=model)
        return result


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
                enforce=True,
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
            enforce=True,
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
                enforce=True,
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
                enforce=True,
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
        if not self._response_only:
            prompt = _input_to_text(kwargs.get("messages") or kwargs.get("input") or kwargs.get("task") or args)
            if prompt:
                self._sdk.scan_prompt(prompt, enforce=True, framework=self._framework, provider="openai")
        result = self._target(*args, **kwargs)
        if inspect.isawaitable(result):
            return _await_and_scan(result, self._sdk, self._framework, kwargs.get("model"))
        text = _response_to_text(result)
        if text:
            self._sdk.scan_response(text, framework=self._framework, provider="openai", model=kwargs.get("model"))
        return result


async def _await_and_scan(awaitable, sdk, framework, model):
    result = await awaitable
    text = _response_to_text(result)
    if text:
        await sdk.scan_response_async(text, framework=framework, provider="openai", model=model)
    return result


def _arg_at(args, index):
    return args[index] if len(args) > index else None


def _messages_to_text(messages: Iterable[Any]) -> str:
    user_msgs = []
    all_msgs = list(messages or [])
    for msg in all_msgs:
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            if role and "user" in str(role).lower():
                user_msgs.append(msg)
        else:
            role = getattr(msg, "role", None) or getattr(msg, "type", None) or msg.__class__.__name__
            if role and ("user" in str(role).lower() or "human" in str(role).lower()):
                user_msgs.append(msg)

    if user_msgs:
        target = user_msgs[-1]
    elif all_msgs:
        target = all_msgs[-1]
    else:
        return ""

    if isinstance(target, dict):
        return _input_to_text(target.get("content"))
    else:
        content = getattr(target, "content", None)
        return str(content) if content is not None else str(target)


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
        elif block_type in ("image_url", "image", "file", "document", "attachment"):
            return ""
        if any(k in value for k in ("image_url", "file_path", "path", "url")):
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
        return _input_to_text(data)
    messages = _get(result, "messages")
    if messages:
        return _input_to_text(messages)
    return ""


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
