"""Readiness gate a broker runtime waits on before accepting connections."""

from typing import Protocol


class ReadinessGate(Protocol):
    def register(self, peer: int) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def is_ready(self) -> bool: ...


class AlwaysReady:
    def register(self, peer: int) -> None:
        return None

    def wait(self, timeout: float | None = None) -> bool:
        return True

    def is_ready(self) -> bool:
        return True
