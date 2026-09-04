"""Readiness gate a broker runtime polls before accepting connections."""

from typing import Protocol


class ReadinessGate(Protocol):
    """Poll contract: True means ready, False means not yet; raise to fail."""

    def wait(self, timeout: float | None = None) -> bool: ...


class AlwaysReady:
    def wait(self, timeout: float | None = None) -> bool:
        return True
