"""SDK exceptions."""

from __future__ import annotations


class GgateError(Exception):
    """Base class for SDK errors."""


class GgateTransportError(GgateError):
    """Raised internally when the local agent cannot be reached."""


class GgateBlockedError(GgateError):
    """Raised by enforcing adapters when the agent returns a block or hard_block verdict."""

    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.message)
