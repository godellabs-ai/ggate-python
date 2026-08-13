"""High-level SDK client.

Latency/failure contract: the SDK never breaks the host application. In ``sync``
mode a scan blocks only up to the configured budget for a verdict and fails open on
any transport problem; after a failure a cooldown breaker fails open instantly
instead of re-dialing a struggling agent on every call. In ``async`` mode (and for
post-hoc surfaces like responses and tool results) events are queued to a background
worker and the call returns immediately.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..exceptions import GgateBlockedError
from .config import Config
from .decision import Decision
from .event import SDK_VERSION, Attachment, RuntimeEventBuilder, attachment_from_path, event_context
from .queue import DeliveryQueue
from .transport import Transport

logger = logging.getLogger("ggate")

_client: Optional["Client"] = None


class _Breaker:
    """Fail open instantly for `cooldown` seconds after a sync-scan transport failure."""

    def __init__(self, cooldown: float):
        self._cooldown = cooldown
        self._until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self._until

    def trip(self) -> None:
        self._until = time.monotonic() + self._cooldown

    def reset(self) -> None:
        self._until = 0.0


class Client:
    def __init__(self, config: Optional[Config] = None, transport=None):
        self.config = config or Config.from_values()
        if transport is None:
            if self.config.console_url and self.config.api_key:
                from .console_transport import ConsoleTransport

                transport = ConsoleTransport(self.config)
            else:
                transport = Transport(self.config)
        self.transport = transport
        self.builder = RuntimeEventBuilder(self.config)
        self.queue = DeliveryQueue(self.transport, self.config.queue_max, self.config.flush_timeout)
        self._breaker = _Breaker(self.config.cooldown)
        if self.config.enabled:
            self._report_collector_ready()

    def _report_collector_ready(self) -> None:
        # Queued, not sent inline: registration must never delay application startup.
        self.queue.submit(
            {
                "op": "collector_ready",
                "collector_id": self.config.collector_id,
                "collector_type": "sdk",
                "name": "ggate-python-sdk",
                "agents": ["agent-framework"],
                "metadata": {"language": "python", "version": SDK_VERSION},
            }
        )

    def scan_prompt(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        event, redaction = self.builder.prompt(text, attachments=attachments, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync")
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    async def scan_prompt_async(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        event, redaction = self.builder.prompt(text, attachments=attachments, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=self.config.mode == "sync")
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    def scan_response(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        wait: bool = False,
        **metadata,
    ) -> Decision:
        event, redaction = self.builder.response(text, attachments=attachments, **metadata)
        return self._send_or_queue(event, redaction.to_json(), wait=wait)

    async def scan_response_async(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        wait: bool = False,
        **metadata,
    ) -> Decision:
        event, redaction = self.builder.response(text, attachments=attachments, **metadata)
        return await self._send_or_queue_async(event, redaction.to_json(), wait=wait)

    def scan_tool_call(self, tool: str, *, input_summary=None, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_call(tool, input_summary=input_summary, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync")
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    async def scan_tool_call_async(self, tool: str, *, input_summary=None, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_call(tool, input_summary=input_summary, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=self.config.mode == "sync")
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    def scan_tool_result(self, tool: str, output: str, *, wait: bool = False, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_result(tool, output, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=wait or enforce)
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    async def scan_tool_result_async(self, tool: str, output: str, *, wait: bool = False, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_result(tool, output, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=wait or enforce)
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    def scan_file(self, path: str, *, action: str = "read", **metadata) -> Decision:
        file_path = Path(path)
        content_len = file_path.stat().st_size if file_path.exists() else None
        event, redaction = self.builder.file_event(path, action=action, content_len=content_len, **metadata)
        return self._send_or_queue(event, redaction.to_json(), wait=False)

    def attachment_from_path(self, path: str, *, source: str = "upload") -> Attachment:
        return attachment_from_path(path, source=source, config=self.config)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Drain queued events. Returns False when the deadline expired with events left."""
        return self.queue.flush(timeout)

    @contextmanager
    def monitor(self, *, prompt: Optional[str] = None, enforce: bool = True, **metadata):
        with event_context(**metadata):
            if prompt is not None:
                self.scan_prompt(prompt, enforce=enforce, **metadata)
            yield self

    def _send_or_queue(self, event, redaction, *, wait: bool) -> Decision:
        if not self.config.enabled:
            return Decision.pass_()
        request = {"op": "scan_text", "event": event, "redaction": redaction}
        if not wait:
            self.queue.submit(request)
            return Decision.pass_()
        if self._breaker.is_open():
            return Decision.fail_open_decision("agent unavailable (cooling down after failure)")
        try:
            decision = Decision.from_agent_response(self.transport.request(request))
        except Exception as exc:  # noqa: BLE001 - any scan failure fails open
            self._breaker.trip()
            logger.warning("ggate agent scan failed (failing open for %.0fs): %s", self.config.cooldown, exc)
            return Decision.fail_open_decision(str(exc))
        self._breaker.reset()
        return decision

    async def _send_or_queue_async(self, event, redaction, *, wait: bool) -> Decision:
        if not self.config.enabled:
            return Decision.pass_()
        request = {"op": "scan_text", "event": event, "redaction": redaction}
        if not wait:
            self.queue.submit(request)
            return Decision.pass_()
        if self._breaker.is_open():
            return Decision.fail_open_decision("agent unavailable (cooling down after failure)")
        try:
            decision = Decision.from_agent_response(await self.transport.request_async(request))
        except Exception as exc:  # noqa: BLE001 - any scan failure fails open
            self._breaker.trip()
            logger.warning("ggate agent scan failed (failing open for %.0fs): %s", self.config.cooldown, exc)
            return Decision.fail_open_decision(str(exc))
        self._breaker.reset()
        return decision


def init(**kwargs) -> Client:
    global _client
    _client = Client(Config.from_values(**kwargs))
    return _client


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client()
    return _client


@contextmanager
def monitor(**kwargs):
    with get_client().monitor(**kwargs) as client:
        yield client
