"""Shared helpers for optional framework adapters."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Iterable


def _is_system_message(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        role = value.get("role")
        if role and "system" in str(role).lower():
            return True
    else:
        role = getattr(value, "role", None) or getattr(value, "type", None)
        if role and "system" in str(role).lower():
            return True
        class_name = value.__class__.__name__
        if "system" in class_name.lower():
            return True
    return False


def textify(value: Any) -> str:
    """Best-effort conversion of framework-specific payloads into scan text."""

    if value is None:
        return ""
    if _is_system_message(value):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type == "text":
            return textify(value.get("text"))
        elif block_type in ("image_url", "image", "file", "document", "attachment"):
            return ""
        if any(k in value for k in ("image_url", "file_path", "path", "url")):
            return ""
        for key in (
            "content",
            "text",
            "input",
            "prompt",
            "query",
            "message",
            "messages",
            "replies",
            "response",
            "answer",
            "completion",
            "generations",
            "output",
            "result",
            "llm",
        ):
            if key in value:
                return textify(value[key])
        return "\n".join(
            part
            for key, item in value.items()
            if key not in {"meta", "metadata", "usage", "token_usage", "finish_reason"}
            for part in [textify(item)]
            if part
        )
    if isinstance(value, (list, tuple, set)):
        return "\n".join(part for part in (textify(item) for item in value) if part)

    content = getattr(value, "content", None)
    if content is not None:
        role = getattr(value, "role", None) or getattr(value, "type", None)
        t = textify(content)
        if not t:
            return ""
        prefix = f"{role}: " if role else ""
        return f"{prefix}{t}"

    for attr in ("text", "response", "output", "message", "result"):
        item = getattr(value, attr, None)
        if item is not None:
            return textify(item)
    return str(value)


def first_text(values: Iterable[Any]) -> str:
    return "\n".join(part for part in (textify(value) for value in values) if part)


def maybe_awaitable_after(result: Any, callback):
    if inspect.isawaitable(result):
        return _await_and_callback(result, callback)
    if inspect.isasyncgen(result):
        return _asyncgen_and_callback(result, callback)
    if inspect.isgenerator(result):
        return _gen_and_callback(result, callback)
    callback(result)
    return result


async def _await_and_callback(awaitable, callback):
    result = await awaitable
    callback(result)
    return result


async def _asyncgen_and_callback(generator, callback):
    seen = []
    async for item in generator:
        seen.append(item)
        yield item
    callback(seen)


def _gen_and_callback(generator, callback):
    seen = []
    for item in generator:
        seen.append(item)
        yield item
    callback(seen)


def wrap_method(target: Any, method_name: str, wrapper_factory) -> bool:
    original = getattr(target, method_name, None)
    if original is None or getattr(original, "_ggate_wrapped", False):
        return False
    wrapped = wrapper_factory(original)
    wrapped._ggate_wrapped = True
    setattr(target, method_name, wrapped)
    return True


def sync_around(before, after, framework: str):
    def factory(original):
        @wraps(original)
        def wrapped(*args, **kwargs):
            before(args, kwargs, framework)
            result = original(*args, **kwargs)
            return maybe_awaitable_after(result, lambda value: after(value, framework))

        return wrapped

    return factory


def extract_attachments_from_value(value: Any) -> list:
    """Best-effort extraction of attachments, files, images or uploads arrays from framework inputs."""
    attachments = []
    if not value:
        return []

    if isinstance(value, dict):
        for key in ("attachments", "files", "images", "uploads"):
            arr = value.get(key)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        attachments.append(item)
                    elif isinstance(item, str):
                        attachments.append({
                            "filename": item.split("/")[-1],
                            "source": "file_ref",
                            "capture": "metadata_only"
                        })
            elif isinstance(arr, dict):
                attachments.append(arr)
            elif isinstance(arr, str):
                attachments.append({
                    "filename": arr.split("/")[-1],
                    "source": "file_ref",
                    "capture": "metadata_only"
                })

        block_type = value.get("type")
        if block_type == "image_url":
            img_url = value.get("image_url")
            url = img_url.get("url") if isinstance(img_url, dict) else img_url
            if url:
                attachments.append(_url_to_attachment(url))
        elif block_type in ("file", "document", "attachment"):
            path = value.get("path") or value.get("file_path") or value.get("filename")
            if path:
                attachments.append({
                    "filename": str(path).split("/")[-1],
                    "source": "file_ref",
                    "capture": "metadata_only"
                })

    elif isinstance(value, (list, tuple)):
        for item in value:
            attachments.extend(extract_attachments_from_value(item))

    elif hasattr(value, "content"):
        content = getattr(value, "content")
        attachments.extend(extract_attachments_from_value(content))

    return attachments


def _url_to_attachment(url: str) -> dict:
    if url.startswith("data:"):
        try:
            header, base64_data = url.split(",", 1)
            mime_part = header.split(";")[0]
            mime_type = mime_part.split(":", 1)[1]
            return {
                "source": "paste",
                "capture": "content",
                "mime_type": mime_type,
                "content_base64": base64_data,
                "filename": f"inline_image.{mime_type.split('/')[-1]}"
            }
        except Exception:
            return {
                "source": "paste",
                "capture": "metadata_only",
                "filename": "inline_image"
            }
    return {
        "source": "file_ref",
        "capture": "metadata_only",
        "filename": url.split("/")[-1] or "image",
        "id": url
    }
