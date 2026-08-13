"""CrewAI adapter based on CrewAI event listeners."""

from __future__ import annotations

from typing import Any

from ._common import textify
from ..core.client import get_client

try:
    from crewai.utilities.events.base_event_listener import BaseEventListener
except Exception:
    class BaseEventListener:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass


class GgateCrewAIListener(BaseEventListener):
    """CrewAI event listener that scans task/agent/tool event payloads."""

    def __init__(self, *, sdk_client=None, framework: str = "crewai", **metadata):
        super().__init__()
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def setup_listeners(self, crewai_event_bus):
        # CrewAI's event bus passes event objects to handlers registered with @event_bus.on(...).
        # Register broadly when the concrete event classes are available; otherwise users can call
        # handle_event directly from their own listener.
        try:
            from crewai.utilities.events import (
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
            from ._common import extract_attachments_from_value
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
            self.sdk.scan_tool_result(tool, textify(event), **metadata)


def instrument(*, sdk_client=None, **metadata):
    return GgateCrewAIListener(sdk_client=sdk_client or get_client(), **metadata)
