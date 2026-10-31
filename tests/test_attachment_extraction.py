"""Files and images in each framework's input shape reach the scan as attachments."""
import base64
import os

import pytest

import ggate
from ggate.adapters._common import extract_attachments_from_value

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
PDF = b"%PDF-1.4 secret document" * 5


@pytest.fixture(autouse=True)
def capture_on(monkeypatch):
    monkeypatch.setenv("GGATE_CAPTURE_FILE_TEXT", "1")
    ggate.init(agent_name="Extraction Test", team="QA", mode="async", console_url="", api_key="")


def data_url(mime, raw):
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def only(found):
    assert len(found) == 1, found
    return found[0]


def test_openai_chat_parts():
    image = only(extract_attachments_from_value({"role": "user", "content": [
        {"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": data_url("image/png", PNG)}}]}))
    assert image["mime_type"] == "image/png" and base64.b64decode(image["content_base64"]) == PNG
    document = only(extract_attachments_from_value([{"type": "file", "file": {"filename": "a.pdf", "file_data": data_url("application/pdf", PDF)}}]))
    assert document["filename"] == "a.pdf" and base64.b64decode(document["content_base64"]) == PDF


def test_openai_responses_parts():
    found = extract_attachments_from_value([{"role": "user", "content": [
        {"type": "input_image", "image_url": data_url("image/png", PNG)},
        {"type": "input_file", "filename": "a.pdf", "file_data": data_url("application/pdf", PDF)}]}])
    assert sorted(a["mime_type"] for a in found) == ["application/pdf", "image/png"]


def test_langchain_standard_blocks():
    found = extract_attachments_from_value([{"type": "image", "source_type": "base64", "data": base64.b64encode(PNG).decode(), "mime_type": "image/png"},
                                            {"type": "file", "source_type": "base64", "data": base64.b64encode(PDF).decode(), "mime_type": "application/pdf", "filename": "a.pdf"}])
    assert sorted(a["mime_type"] for a in found) == ["application/pdf", "image/png"]


def test_media_objects_by_path_bytes_and_base64(tmp_path):
    path = tmp_path / "secrets.pdf"
    path.write_bytes(PDF)

    class File:  # Agno / CrewAI / LlamaIndex DocumentBlock style
        def __init__(self, filepath):
            self.filepath = filepath

    class ImageContent:  # Haystack style
        base64_image = base64.b64encode(PNG).decode()
        mime_type = "image/png"

    class Image:  # AutoGen style
        def to_base64(self):
            return base64.b64encode(PNG).decode()

    class ChatMessage:
        def __init__(self, parts):
            self._content = parts

    found = extract_attachments_from_value({"files": [File(path)], "message": ChatMessage(["text", ImageContent()]), "task": [Image()]})
    assert {a.get("filename") for a in found} >= {"secrets.pdf"}
    assert sum(1 for a in found if a.get("mime_type") == "image/png") == 1  # the same image is reported once


def test_metadata_only_without_capture(monkeypatch):
    monkeypatch.delenv("GGATE_CAPTURE_FILE_TEXT")
    ggate.init(agent_name="Extraction Test", team="QA", mode="async", console_url="", api_key="")
    image = only(extract_attachments_from_value({"type": "image_url", "image_url": {"url": data_url("image/png", PNG)}}))
    assert image["capture"] == "metadata_only" and "content_base64" not in image and image["sha256"]


def test_inline_base64_is_kept_out_of_scanned_text():
    from ggate.adapters._common import strip_inline_media
    rendered = '<message role="user"><text>Describe this.</text><image>' + data_url("image/png", PNG * 40) + "</image></message>"
    cleaned = strip_inline_media(rendered)
    assert "Describe this." in cleaned and "[inline media]" in cleaned and "base64" not in cleaned
    assert len(cleaned) < 200
    assert strip_inline_media("short text") == "short text"
