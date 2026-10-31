"""SDK exceptions."""

from __future__ import annotations


class GgateError(Exception):
    """Base class for SDK errors."""


class GgateTransportError(GgateError):
    """Raised internally when the Console cannot be reached or rejects the request."""


class GgateBlockedError(GgateError):
    """Raised by enforcing adapters on a block verdict."""

    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.message)


class GgateAgentContainedError(GgateBlockedError):
    """The Console's Kill Switch has locked this agent.

    Subclasses :class:`GgateBlockedError` so existing block handling keeps working. Unlike an ordinary
    block it is raised even where the caller did not ask to enforce, including ``async`` mode: an agent
    under a lock must stop, not merely be observed.
    """
