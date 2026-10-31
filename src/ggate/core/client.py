"""High-level SDK client.

Every scan goes to the Console's ``POST /api/v1/scan``. That is the only destination the SDK
has: it holds the Console URL and an API key, and talks to nothing else.

Latency/failure contract: a transport problem never breaks the host application. In ``sync`` mode
a scan blocks only up to the configured budget for a verdict and fails open on any transport
problem; after a failure a cooldown breaker fails open instantly instead of re-calling a struggling
Console on every request. In ``async`` mode events are queued to a background worker and the call
returns immediately.

Kill Switch: a lock the Console reported is the one exception. It raises
``GgateAgentContainedError`` in both modes, turns ``async`` scanning synchronous so a release is seen
at once, and stays in force (fails closed) while the Console cannot confirm that release.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..exceptions import GgateAgentContainedError, GgateBlockedError, GgateTransportError
from .config import Config
from .console_transport import ConsoleTransport
from .decision import Decision
from .event import SDK_VERSION, Attachment, RuntimeEventBuilder, attachment_from_path, event_context
from .queue import DeliveryQueue

logger = logging.getLogger("ggate")

_client: Optional["Client"] = None
_latest_client: Optional["Client"] = None

_UNCONFIGURED = (
    "GGATE_CONSOLE_URL and GGATE_API_KEY are not set, so there is nowhere to scan: "
    "every scan will fail open. Set both (Console UI -> Admin -> API keys), or pass "
    "console_url=/api_key= to init()."
)


class _Breaker:
    """Fail open instantly for `cooldown` seconds after a sync-scan failure."""

    def __init__(self, cooldown: float):
        self._cooldown = cooldown
        self._until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self._until

    def trip(self) -> None:
        self._until = time.monotonic() + self._cooldown

    def reset(self) -> None:
        self._until = 0.0


class _UnconfiguredTransport:
    """Stands in when no Console was configured.

    Raising here rather than at construction keeps the promise that the SDK never breaks the
    host application: a misconfigured deployment degrades to fail-open allows with a message
    naming the missing setting, exactly as an unreachable Console would.
    """

    def request(self, request):
        raise GgateTransportError(_UNCONFIGURED)

    async def request_async(self, request):
        raise GgateTransportError(_UNCONFIGURED)


class Client:
    def __init__(self, config: Optional[Config] = None, transport=None):
        global _latest_client
        _latest_client = self
        self.config = config or Config.from_values()
        if transport is None:
            if self.config.configured:
                transport = ConsoleTransport(self.config)
            else:
                if self.config.enabled:
                    logger.warning("ggate: %s", _UNCONFIGURED)
                transport = _UnconfiguredTransport()
        self.transport = transport
        self.builder = RuntimeEventBuilder(self.config)
        self._contained: Optional[Decision] = None
        self.queue = DeliveryQueue(
            self.transport, self.config.queue_max, self.config.flush_timeout, on_result=self._on_queued_result
        )
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
                "metadata": {
                    "language": "python",
                    "version": SDK_VERSION,
                    "agent_name": self.config.agent_name,
                    "team": self.config.team,
                },
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
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    async def scan_prompt_async(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        event, redaction = self.builder.prompt(text, attachments=attachments, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    def scan_response(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        wait: bool = False,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        if enforce:
            metadata["before_delivery"] = True
        event, redaction = self.builder.response(text, attachments=attachments, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=wait or enforce or bool(metadata.get("before_delivery")))
        return self._finish(decision, enforce)

    async def scan_response_async(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        wait: bool = False,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        if enforce:
            metadata["before_delivery"] = True
        event, redaction = self.builder.response(text, attachments=attachments, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=wait or enforce or bool(metadata.get("before_delivery")))
        return self._finish(decision, enforce)

    def scan_tool_call(self, tool: str, *, input_summary=None, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_call(tool, input_summary=input_summary, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    async def scan_tool_call_async(self, tool: str, *, input_summary=None, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.tool_call(tool, input_summary=input_summary, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    def scan_tool_result(self, tool: str, output: str, *, wait: bool = False, enforce: bool = False, **metadata) -> Decision:
        if enforce:
            metadata["before_delivery"] = True
        event, redaction = self.builder.tool_result(tool, output, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=wait or enforce or bool(metadata.get("before_delivery")))
        return self._finish(decision, enforce)

    async def scan_tool_result_async(self, tool: str, output: str, *, wait: bool = False, enforce: bool = False, **metadata) -> Decision:
        if enforce:
            metadata["before_delivery"] = True
        event, redaction = self.builder.tool_result(tool, output, **metadata)
        decision = await self._send_or_queue_async(event, redaction.to_json(), wait=wait or enforce or bool(metadata.get("before_delivery")))
        return self._finish(decision, enforce)

    def scan_file(
        self,
        path: str,
        *,
        action: str = "read",
        content: Optional[str] = None,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        file_path = Path(path)
        content_len = file_path.stat().st_size if file_path.exists() else None
        if content is None and file_path.exists() and self.config.capture_file_text:
            try:
                content = attachment_from_path(path, source="file_ref", config=self.config).text
            except OSError:
                # A path-only policy scan is still useful when the process cannot read content.
                pass
        event, redaction = self.builder.file_event(
            path, action=action, content_len=content_len, content=content, **metadata
        )
        decision = self._send_or_queue(
            event, redaction.to_json(), wait=self.config.mode == "sync" or enforce
        )
        return self._finish(decision, enforce)

    async def scan_file_async(
        self,
        path: str,
        *,
        action: str = "read",
        content: Optional[str] = None,
        enforce: bool = False,
        **metadata,
    ) -> Decision:
        file_path = Path(path)
        content_len = file_path.stat().st_size if file_path.exists() else None
        if content is None and file_path.exists() and self.config.capture_file_text:
            try:
                content = attachment_from_path(path, source="file_ref", config=self.config).text
            except OSError:
                # A path-only policy scan is still useful when the process cannot read content.
                pass
        event, redaction = self.builder.file_event(
            path, action=action, content_len=content_len, content=content, **metadata
        )
        decision = await self._send_or_queue_async(
            event, redaction.to_json(), wait=self.config.mode == "sync" or enforce
        )
        return self._finish(decision, enforce)

    def scan_shell(self, command: str, *, argv=None, enforce: bool = False, **metadata) -> Decision:
        event, redaction = self.builder.shell(command, argv=argv, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    async def scan_shell_async(
        self, command: str, *, argv=None, enforce: bool = False, **metadata
    ) -> Decision:
        event, redaction = self.builder.shell(command, argv=argv, **metadata)
        decision = await self._send_or_queue_async(
            event, redaction.to_json(), wait=self.config.mode == "sync" or enforce
        )
        return self._finish(decision, enforce)

    def scan_web(
        self, url: str, *, method=None, domain=None, enforce: bool = False, **metadata
    ) -> Decision:
        event, redaction = self.builder.web(url, method=method, domain=domain, **metadata)
        decision = self._send_or_queue(event, redaction.to_json(), wait=self.config.mode == "sync" or enforce)
        return self._finish(decision, enforce)

    async def scan_web_async(
        self, url: str, *, method=None, domain=None, enforce: bool = False, **metadata
    ) -> Decision:
        event, redaction = self.builder.web(url, method=method, domain=domain, **metadata)
        decision = await self._send_or_queue_async(
            event, redaction.to_json(), wait=self.config.mode == "sync" or enforce
        )
        return self._finish(decision, enforce)

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

    @property
    def contained(self) -> bool:
        """Whether the Console last reported this agent as locked by the Kill Switch."""
        return self._contained is not None

    def _finish(self, decision: Decision, enforce: bool) -> Decision:
        if decision.contained and decision.blocked:
            raise GgateAgentContainedError(decision)
        if enforce and decision.blocked:
            raise GgateBlockedError(decision)
        return decision

    def _observe(self, decision: Decision) -> Decision:
        """Track the Kill Switch. Only a real Console answer can set or clear the lock."""
        if decision.contained:
            if self._contained is None:
                logger.warning("ggate: Kill Switch active for this agent: %s", decision.message)
            self._contained = decision
        elif not decision.fail_open:
            if self._contained is not None:
                logger.warning("ggate: Kill Switch released for this agent")
            self._contained = None
        return decision

    def _on_queued_result(self, request, response) -> None:
        # A queued event's verdict is how an `async` deployment first learns of a lock. From then on
        # every scan waits for the Console, so a release takes effect at the very next scan.
        if request.get("op") == "scan_text":
            self._observe(Decision.from_response(response))

    def _unavailable(self, message: str) -> Decision:
        """The Console gave no answer: fail open, unless a known lock has not been released."""
        if self._contained is not None:
            return Decision.kill_switch_held(self._contained, "the Console has not confirmed a release")
        return Decision.fail_open_decision(message)

    def _send_or_queue(self, event, redaction, *, wait: bool) -> Decision:
        if not self.config.enabled:
            return Decision.pass_()
        request = {"op": "scan_text", "event": event, "redaction": redaction}
        if not wait and self._contained is None:
            self.queue.submit(request)
            return Decision.pass_()
        if self._breaker.is_open():
            return self._unavailable("console unavailable (cooling down after failure)")
        try:
            decision = self._observe(Decision.from_response(self.transport.request(request)))
        except Exception as exc:  # noqa: BLE001 - any scan failure fails open
            self._breaker.trip()
            logger.warning("ggate console scan failed (failing open for %.0fs): %s", self.config.cooldown, exc)
            return self._unavailable(str(exc))
        self._breaker.reset()
        return decision

    async def _send_or_queue_async(self, event, redaction, *, wait: bool) -> Decision:
        if not self.config.enabled:
            return Decision.pass_()
        request = {"op": "scan_text", "event": event, "redaction": redaction}
        if not wait and self._contained is None:
            self.queue.submit(request)
            return Decision.pass_()
        if self._breaker.is_open():
            return self._unavailable("console unavailable (cooling down after failure)")
        try:
            decision = self._observe(Decision.from_response(await self.transport.request_async(request)))
        except Exception as exc:  # noqa: BLE001 - any scan failure fails open
            self._breaker.trip()
            logger.warning("ggate console scan failed (failing open for %.0fs): %s", self.config.cooldown, exc)
            return self._unavailable(str(exc))
        self._breaker.reset()
        return decision


def init(**kwargs) -> Client:
    """Configure the process-wide client.

    ``agent_name`` and ``team`` are required — the Console lists a session under the agent's name
    and holds its team accountable, and neither is something the SDK can infer from the process it
    happens to be running in. Both may equivalently come from ``GGATE_AGENT_NAME`` / ``GGATE_TEAM``
    for deployments that configure through the environment::

        ggate.init(agent_name="JIRA Project Assistant", team="Platform Engineering")
    """
    global _client
    _client = Client(Config.from_values(**kwargs))
    return _client


def get_client() -> Client:
    """The process-wide client, built from the environment when :func:`init` was never called.

    That fallback still needs ``GGATE_AGENT_NAME`` and ``GGATE_TEAM``, and raises naming whichever
    is absent.
    """
    global _client
    if _client is None:
        _client = Client()
    return _client


@contextmanager
def monitor(**kwargs):
    with get_client().monitor(**kwargs) as client:
        yield client
