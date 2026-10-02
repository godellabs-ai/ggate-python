"""CrewAI adapter.

CrewAI 1.x enforces through its execution hooks (`crewai.hooks`): a global `before_llm_call` hook
sees every model call's messages (plus the crew/task files from `kickoff(input_files=...)`) before it
runs, and raising `HookAborted` stops the call and reaches the caller. Tool calls get the same through
`before_tool_call`; answers and tool results are recorded from the `after_*` hooks.

Older CrewAI releases have no hooks; there the adapter falls back to event listeners, which observe
but cannot reliably stop a call (CrewAI dispatches sync handlers on a thread pool).
"""

from __future__ import annotations

from typing import Any

from ._common import extract_attachments_from_value, textify
from ..core.client import get_client
from ..exceptions import GgateBlockedError

try:  # CrewAI moved its event API from crewai.utilities.events to crewai.events in 1.x.
    from crewai.events import BaseEventListener
except Exception:
    try:
        from crewai.utilities.events.base_event_listener import BaseEventListener
    except Exception:
        class BaseEventListener:  # type: ignore
            def __init__(self, *args, **kwargs):
                pass

_REGISTERED: dict = {}


def _messages_text(messages: Any) -> str:
    parts = []
    for message in messages or []:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "system":
            continue
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        text = textify(content)
        if text:
            parts.append(f"{role or 'message'}: {text}")
    return "\n".join(parts)


def _task_files(context: Any) -> list:
    crew, task = getattr(context, "crew", None), getattr(context, "task", None)
    if crew is None or task is None:
        return []
    try:
        from crewai.utilities.file_store import get_all_files

        files = get_all_files(crew.id, task.id) or {}
    except Exception:
        return []
    return extract_attachments_from_value(list(files.values()))


class GgateCrewAIHooks:
    """Execution hooks that scan every model and tool call."""

    def __init__(self, *, sdk_client=None, framework: str = "crewai", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = {"framework": framework, **metadata}

    def _abort(self, error: GgateBlockedError):
        from crewai.hooks import HookAborted

        raise HookAborted(str(error), source="ggate") from error

    def before_llm_call(self, context: Any):
        messages = getattr(context, "messages", None)
        attachments = extract_attachments_from_value(messages) + _task_files(context)
        try:
            self.sdk.scan_prompt(_messages_text(messages), attachments=attachments, enforce=True, **self.metadata)
        except GgateBlockedError as error:
            self._abort(error)
        return None

    def after_llm_call(self, context: Any):
        response = getattr(context, "response", None)
        if response:
            self.sdk.scan_response(textify(response), **self.metadata)
        return None

    def before_tool_call(self, context: Any):
        try:
            self.sdk.scan_tool_call(getattr(context, "tool_name", "tool"), input_summary=textify(getattr(context, "tool_input", None)),
                                    enforce=True, **self.metadata)
        except GgateBlockedError as error:
            self._abort(error)
        return None

    def after_tool_call(self, context: Any):
        result = getattr(context, "tool_result", None)
        if result:
            self.sdk.scan_tool_result(getattr(context, "tool_name", "tool"), textify(result), **self.metadata)
        return None


def _register_hooks(hooks: GgateCrewAIHooks) -> bool:
    try:
        from crewai import hooks as crewai_hooks
    except Exception:
        return False
    # Register once per process; a second instrument() call replaces the client used.
    if _REGISTERED.get("hooks"):
        _REGISTERED["hooks"].sdk, _REGISTERED["hooks"].metadata = hooks.sdk, hooks.metadata
        return True
    crewai_hooks.register_before_llm_call_hook(hooks.before_llm_call)
    crewai_hooks.register_after_llm_call_hook(hooks.after_llm_call)
    crewai_hooks.register_before_tool_call_hook(hooks.before_tool_call)
    crewai_hooks.register_after_tool_call_hook(hooks.after_tool_call)
    _REGISTERED["hooks"] = hooks
    return True


class GgateCrewAIListener(BaseEventListener):
    """Fallback for CrewAI releases without execution hooks: scans task/agent/tool events."""

    def __init__(self, *, sdk_client=None, framework: str = "crewai", **metadata):
        super().__init__()
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def setup_listeners(self, crewai_event_bus):
        try:
            try:
                from crewai.events import (
                    AgentExecutionCompletedEvent,
                    AgentExecutionStartedEvent,
                    TaskCompletedEvent,
                    TaskStartedEvent,
                    ToolUsageFinishedEvent,
                    ToolUsageStartedEvent,
                )
            except Exception:
                from crewai.utilities.events import (  # type: ignore
                    AgentExecutionCompletedEvent,
                    AgentExecutionStartedEvent,
                    TaskCompletedEvent,
                    TaskStartedEvent,
                    ToolUsageFinishedEvent,
                    ToolUsageStartedEvent,
                )
        except Exception:
            return

        @crewai_event_bus.on(AgentExecutionStartedEvent)
        @crewai_event_bus.on(TaskStartedEvent)
        def on_start(source, event):
            self.handle_event(event, phase="start")

        @crewai_event_bus.on(AgentExecutionCompletedEvent)
        @crewai_event_bus.on(TaskCompletedEvent)
        def on_end(source, event):
            self.handle_event(event, phase="end")

        @crewai_event_bus.on(ToolUsageStartedEvent)
        def on_tool_start(source, event):
            self.handle_tool_event(event, started=True)

        @crewai_event_bus.on(ToolUsageFinishedEvent)
        def on_tool_end(source, event):
            self.handle_tool_event(event, started=False)

    def handle_event(self, event: Any, *, phase: str) -> None:
        text = textify(event)
        if not text:
            return
        metadata = {"framework": self.framework, **self.metadata}
        if phase == "start":
            attachments = extract_attachments_from_value(event)
            self.sdk.scan_prompt(text, attachments=attachments, enforce=True, **metadata)
        else:
            self.sdk.scan_response(text, **metadata)

    def handle_tool_event(self, event: Any, *, started: bool) -> None:
        tool = getattr(event, "tool_name", None) or getattr(event, "name", None) or "tool"
        metadata = {"framework": self.framework, **self.metadata}
        if started:
            self.sdk.scan_tool_call(tool, input_summary=textify(event), enforce=True, **metadata)
        else:
            self.sdk.scan_tool_result(tool, textify(event), enforce=True, **metadata)


def instrument(*, sdk_client=None, **metadata):
    hooks = GgateCrewAIHooks(sdk_client=sdk_client or get_client(), **metadata)
    if _register_hooks(hooks):
        return hooks
    return GgateCrewAIListener(sdk_client=sdk_client or get_client(), **metadata)
