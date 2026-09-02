"""Public API for the Godel's Gate Python SDK."""

from __future__ import annotations

from .core.client import Client, get_client, init, monitor
from .core.decision import (
    Decision,
    DocumentIntelligence,
    DocumentTaxonomy,
    SensitivityClassification,
    TopicClassification,
)
from .core.event import Attachment
from .exceptions import GgateBlockedError, GgateError, GgateTransportError

__version__ = "0.3.0"


def instrument(framework: str, **kwargs):
    """Instrument a supported framework.

    OpenAI expects ``client=...`` and returns a wrapped client.
    LangChain/LangGraph returns a callback handler.
    """

    name = framework.lower().replace("_", "-")
    if name in {"openai", "openai-assistants", "openai-swarm", "swarm"}:
        from .adapters.openai import instrument as instrument_openai

        return instrument_openai(**kwargs)
    if name in {"langchain", "langgraph"}:
        from .adapters.langchain import langchain_callback

        return langchain_callback(framework=name, **kwargs)
    if name == "crewai":
        from .adapters.crewai import instrument as instrument_crewai

        return instrument_crewai(**kwargs)
    if name in {"autogen", "ag2"}:
        from .adapters.autogen import instrument as instrument_autogen

        return instrument_autogen(framework=name, **kwargs)
    if name in {"llamaindex", "llama-index"}:
        from .adapters.llamaindex import instrument as instrument_llamaindex

        return instrument_llamaindex(framework="llamaindex", **kwargs)
    if name == "haystack":
        from .adapters.haystack import instrument as instrument_haystack

        return instrument_haystack(**kwargs)
    if name in {"semantic-kernel", "semantic_kernel", "sk"}:
        from .adapters.semantic_kernel import instrument as instrument_semantic_kernel

        return instrument_semantic_kernel(framework="semantic-kernel", **kwargs)
    if name == "dspy":
        from .adapters.dspy import instrument as instrument_dspy

        return instrument_dspy(**kwargs)
    if name in {"phidata", "agno"}:
        from .adapters.phidata import instrument as instrument_phidata

        return instrument_phidata(framework=name, **kwargs)
    raise ValueError(
        f"unsupported framework adapter {framework!r}; use scan_prompt/scan_response for generic monitoring"
    )


def scan_prompt(text: str, **kwargs) -> Decision:
    return get_client().scan_prompt(text, **kwargs)


async def scan_prompt_async(text: str, **kwargs) -> Decision:
    return await get_client().scan_prompt_async(text, **kwargs)


def scan_response(text: str, **kwargs) -> Decision:
    return get_client().scan_response(text, **kwargs)


async def scan_response_async(text: str, **kwargs) -> Decision:
    return await get_client().scan_response_async(text, **kwargs)


def scan_file(path: str, **kwargs) -> Decision:
    return get_client().scan_file(path, **kwargs)


async def scan_file_async(path: str, **kwargs) -> Decision:
    return await get_client().scan_file_async(path, **kwargs)


def scan_shell(command: str, **kwargs) -> Decision:
    return get_client().scan_shell(command, **kwargs)


async def scan_shell_async(command: str, **kwargs) -> Decision:
    return await get_client().scan_shell_async(command, **kwargs)


def scan_web(url: str, **kwargs) -> Decision:
    return get_client().scan_web(url, **kwargs)


async def scan_web_async(url: str, **kwargs) -> Decision:
    return await get_client().scan_web_async(url, **kwargs)


def flush(timeout: float | None = None) -> bool:
    """Drain queued events. Returns False when the deadline expired with events left."""
    return get_client().flush(timeout)


def scan_tool_call(tool: str, input_summary=None, **kwargs) -> Decision:
    return get_client().scan_tool_call(tool, input_summary=input_summary, **kwargs)


def scan_tool_result(tool: str, output: str, **kwargs) -> Decision:
    return get_client().scan_tool_result(tool, output, **kwargs)


async def scan_tool_call_async(tool: str, input_summary=None, **kwargs) -> Decision:
    return await get_client().scan_tool_call_async(tool, input_summary=input_summary, **kwargs)


async def scan_tool_result_async(tool: str, output: str, **kwargs) -> Decision:
    return await get_client().scan_tool_result_async(tool, output, **kwargs)


__all__ = [
    "Attachment",
    "Client",
    "Decision",
    "DocumentIntelligence",
    "DocumentTaxonomy",
    "GgateBlockedError",
    "GgateError",
    "GgateTransportError",
    "SensitivityClassification",
    "TopicClassification",
    "flush",
    "get_client",
    "init",
    "instrument",
    "monitor",
    "scan_file",
    "scan_file_async",
    "scan_prompt",
    "scan_prompt_async",
    "scan_response",
    "scan_response_async",
    "scan_shell",
    "scan_shell_async",
    "scan_tool_call",
    "scan_tool_call_async",
    "scan_tool_result",
    "scan_tool_result_async",
    "scan_web",
    "scan_web_async",
]
