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


# ---- inline media in text -------------------------------------------------------------------------

import re as _re

_DATA_URI = _re.compile(r"data:[\w.+/-]+(?:;[\w=.+-]+)*;base64,[A-Za-z0-9+/=\s]+")
_BASE64_RUN = _re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")


def strip_inline_media(text: str) -> str:
    """Replace embedded base64 media (data URIs, long base64 runs) with a short placeholder.

    Frameworks that render a chat history into one prompt (Semantic Kernel's XML, some templates)
    embed images and files as base64 text. Scanning that as prose made the Console's text models
    run past their deadline (HTTP 503) and the scan fail open; the media itself is scanned as an
    attachment instead.
    """
    if not text or len(text) < 400:
        return text
    text = _DATA_URI.sub("[inline media]", text)
    return _BASE64_RUN.sub("[inline media]", text)


# ---- attachments -------------------------------------------------------------------------------

_LIST_KEYS = ("attachments", "files", "images", "uploads", "input_files", "documents")
_MEDIA_TYPES = ("image_url", "input_image", "image", "file", "input_file", "document", "attachment", "audio", "input_audio")
_CONTAINER_ATTRS = ("content", "blocks", "content_parts", "_content", "items", "messages", "images", "files")
_PATH_ATTRS = ("path", "filepath", "file_path", "source")
_DATA_ATTRS = ("base64_image", "base64_data", "data", "content", "image", "file_data")
_MIME_ATTRS = ("mime_type", "mimetype", "mimeType", "image_mimetype", "document_mimetype", "media_type", "content_type")
# Media objects that load their own bytes (CrewAI `File.read()`, file-like wrappers).
_READ_METHODS = ("read_bytes", "read")
_NAME_ATTRS = ("filename", "file_name", "name", "title")


def _capture_limits():
    try:
        from ..core.client import get_client
        config = get_client().config
        return bool(config.capture_file_text), int(config.max_file_bytes)
    except Exception:
        return False, 65536


def _bytes_attachment(raw: bytes, *, mime_type=None, filename=None, source="upload") -> dict:
    """An attachment for in-memory content, honoring the content-capture settings."""
    import hashlib
    import mimetypes

    mime_type = mime_type or (mimetypes.guess_type(filename)[0] if filename else None)
    capture, max_bytes = _capture_limits()
    attachment = {"source": source, "capture": "metadata_only", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    # Always name the file with an extension: Console file-type policies match on the name's extension.
    if mime_type and (not filename or "." not in str(filename).split("/")[-1]):
        filename = (str(filename).split("/")[-1] if filename else "attachment") + (mimetypes.guess_extension(mime_type) or "")
    if filename:
        attachment["filename"] = str(filename).split("/")[-1]
    if mime_type:
        attachment["mime_type"] = mime_type
    if capture:
        bounded = raw[:max_bytes]
        if len(raw) > max_bytes:
            attachment["truncated"] = True
        if (mime_type or "").startswith("text/"):
            attachment.update(capture="text", text=bounded.decode("utf-8", errors="replace"))
        else:
            import base64

            attachment.update(capture="content", content_base64=base64.b64encode(bounded).decode("ascii"))
    return attachment


def _b64_attachment(data: str, **kwargs) -> dict | None:
    import base64
    import binascii

    try:
        return _bytes_attachment(base64.b64decode(data, validate=False), **kwargs)
    except (binascii.Error, ValueError):
        return None


def _path_attachment(path, *, mime_type=None, filename=None, source="upload") -> dict | None:
    from pathlib import Path

    try:
        file_path = Path(path)
        if not file_path.is_file():
            return None
        return _bytes_attachment(file_path.read_bytes(), mime_type=mime_type, filename=filename or file_path.name, source=source)
    except (OSError, TypeError, ValueError):
        return None


def _url_to_attachment(url: str, *, mime_type=None, filename=None) -> dict:
    if isinstance(url, str) and url.startswith("data:"):
        header, _, data = url.partition(",")
        mime = mime_type or header[5:].split(";")[0] or None
        name = filename or f"inline.{(mime or 'application/octet-stream').split('/')[-1]}"
        attachment = _b64_attachment(data, mime_type=mime, filename=name, source="paste") if ";base64" in header else \
            _bytes_attachment(data.encode(), mime_type=mime, filename=name, source="paste")
        if attachment:
            return attachment
    return {"source": "file_ref", "capture": "metadata_only", "id": str(url),
            "filename": filename or str(url).split("?")[0].split("/")[-1] or "file"}


def _first(value: Any, names) -> Any:
    for name in names:
        found = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if found not in (None, "", b""):
            return found
    return None


def _media_attachment(value: Any) -> dict | None:
    """A file or image given as a dict block or a framework media object, by data, data URL or path."""
    nested = value.get("file") if isinstance(value, dict) and isinstance(value.get("file"), dict) else None
    holder = nested or value
    mime = _first(holder, _MIME_ATTRS) or _first(value, _MIME_ATTRS)
    name = _first(holder, _NAME_ATTRS) or (_first(value.get("metadata"), _NAME_ATTRS) if isinstance(value, dict) and isinstance(value.get("metadata"), dict) else None)
    name = name if isinstance(name, str) else None
    mime = mime if isinstance(mime, str) else None
    for attr in ("data_uri", "url", "image_url"):
        url = _first(holder, (attr,))
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str) and url.startswith("data:"):
            return _url_to_attachment(url, mime_type=mime, filename=name)
    if not isinstance(value, dict):
        for method in _READ_METHODS:
            reader = getattr(value, method, None)
            if callable(reader):
                try:
                    data = reader()
                except Exception:
                    continue
                if isinstance(data, (bytes, bytearray)) and data:
                    return _bytes_attachment(bytes(data), mime_type=mime, filename=name)
    to_base64 = getattr(value, "to_base64", None)
    if callable(to_base64):  # AutoGen Image
        try:
            return _b64_attachment(to_base64(), mime_type=mime or "image/png", filename=name or "image.png")
        except Exception:
            pass
    for attr in _DATA_ATTRS:
        data = _first(holder, (attr,))
        if isinstance(data, (bytes, bytearray)):
            return _bytes_attachment(bytes(data), mime_type=mime, filename=name)
        if isinstance(data, str) and len(data) > 64 and not data.startswith(("http://", "https://", "/")):
            if data.startswith("data:"):
                return _url_to_attachment(data, mime_type=mime, filename=name)
            found = _b64_attachment(data, mime_type=mime, filename=name)
            if found:
                return found
    for attr in _PATH_ATTRS:
        path = _first(holder, (attr,))
        if path is not None and not isinstance(path, (dict, list)):
            found = _path_attachment(str(path), mime_type=mime, filename=name)
            if found:
                return found
    for attr in ("url", "image_url"):
        url = _first(holder, (attr,))
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str) and url:
            return _url_to_attachment(url, mime_type=mime, filename=name)
    if name:
        return {"source": "file_ref", "capture": "metadata_only", "filename": name, **({"mime_type": mime} if mime else {})}
    return None


def _is_media_object(value: Any) -> bool:
    name = type(value).__name__.lower()
    return any(word in name for word in ("image", "file", "document", "audio", "video", "pdf", "binary")) and \
        "message" not in name and "agent" not in name and "tool" not in name


def extract_attachments_from_value(value: Any, _depth: int = 0, _seen: set | None = None) -> list:
    """Files and images in framework inputs, so their content is scanned with the prompt.

    Understands OpenAI chat/Responses parts (`image_url`, `input_image`, `file`/`input_file` with
    `file_data`), LangChain content blocks (`image`/`file` with base64 `data`, `url` or a path),
    `attachments`/`files`/`images` lists, and media objects such as AutoGen `Image`, LlamaIndex
    `ImageBlock`/`DocumentBlock`, Haystack `ImageContent`/`FileContent`, Agno `Image`/`File`, DSPy
    `Image`/`File`, CrewAI `File` and AG2 `ImageInput`/`DocumentInput`, read from their bytes, data URL
    or path. Content is included only when content capture is on (`GGATE_CAPTURE_FILE_TEXT`).
    """
    seen = _seen if _seen is not None else set()
    if value is None or _depth > 8 or id(value) in seen or isinstance(value, (str, bytes, int, float, bool)):
        return []
    seen.add(id(value))
    found: list = []
    if isinstance(value, dict):
        if value.get("capture") and (value.get("content_base64") or value.get("text") or value.get("sha256")):
            return [value]  # already an SDK attachment
        if value.get("type") in _MEDIA_TYPES or isinstance(value.get("file"), dict):
            attachment = _media_attachment(value)
            return [attachment] if attachment else []
        for key in _LIST_KEYS:
            items = value.get(key)
            items = items if isinstance(items, (list, tuple)) else ([items] if items is not None else [])
            for item in items:
                if isinstance(item, str):
                    found.append(_path_attachment(item, source="file_ref") or
                                 {"filename": item.split("/")[-1], "source": "file_ref", "capture": "metadata_only"})
                else:
                    found.extend(extract_attachments_from_value(item, _depth + 1, seen) or
                                 ([a] if (a := _media_attachment(item)) else []))
        for key, item in value.items():
            if key not in _LIST_KEYS and isinstance(item, (dict, list, tuple)) or (key not in _LIST_KEYS and not isinstance(item, (str, bytes, int, float, bool, type(None)))):
                found.extend(extract_attachments_from_value(item, _depth + 1, seen))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(extract_attachments_from_value(item, _depth + 1, seen))
    elif _is_media_object(value):
        attachment = _media_attachment(value)
        if attachment:
            found.append(attachment)
    else:
        for attr in _CONTAINER_ATTRS:
            item = getattr(value, attr, None)
            if item is not None and not callable(item) and not isinstance(item, (str, bytes)):
                found.extend(extract_attachments_from_value(item, _depth + 1, seen))
    unique, hashes = [], set()
    for attachment in found:
        key = attachment.get("sha256") or attachment.get("id") or attachment.get("filename")
        if key in hashes:
            continue
        hashes.add(key)
        unique.append(attachment)
    return unique

