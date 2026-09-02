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
