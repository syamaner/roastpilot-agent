"""HUMAN persistence deadlines and immutable operation evidence."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

_T = TypeVar("_T")


class HistoryUnavailable(RuntimeError):
    """A storage operation cannot supply trusted results within its admission."""


class Settlement(Enum):
    """Durable disposition of one admitted or refused operation."""

    UNRESOLVED = "unresolved"
    VERIFIED_NO_WRITE = "verified_no_write"
    OBSERVED_COMMITTED = "observed_committed"


@dataclass
class StorageDeadline:
    """Hold one absolute deadline and fail closed if its clock goes backwards."""

    expires_at: float
    _clock: Callable[[], float]
    _last: float
    _real_expiry: float
    _invalid: bool = False

    @classmethod
    def start(cls, clock: Callable[[], float], interval: float) -> StorageDeadline:
        """Capture the one deadline shared by the caller's storage operations."""
        now = clock()
        expires = now + interval
        return cls(
            expires,
            clock,
            now,
            time.monotonic() + interval,
            not math.isfinite(expires) or interval <= 0,
        )

    def remaining(self) -> float:
        """Return the remaining budget, or zero for an expired or invalid clock."""
        now = self._clock()
        if self._invalid or not math.isfinite(now) or now < self._last:
            self._invalid = True
            return 0.0
        self._last = now
        return max(0.0, min(self.expires_at - now, self._real_expiry - time.monotonic()))

    async def wait(self, start: Callable[[], Awaitable[_T]]) -> _T:
        """Bound a cancellable foreground wait without starting it at equality."""
        remaining = self.remaining()
        if remaining <= 0:
            raise HistoryUnavailable("HUMAN history deadline elapsed")
        try:
            async with asyncio.timeout(remaining):
                return await start()
        except TimeoutError as exc:
            raise HistoryUnavailable("HUMAN history deadline elapsed") from exc


@dataclass(frozen=True)
class CommandReference:
    """Exact MCP command-attempt reference when the caller has positive evidence."""

    session_id: str
    child_generation: int
    command_generation: int
    attempt: int


@dataclass(frozen=True)
class StorageReceipt:
    """Causal evidence for a single history operation."""

    token: int
    kind: str
    run_id: str | None
    turn: int | None
    deadline: float
    writer_entered: bool | None
    timely: bool | None
    settlement: Settlement
    writer_connection: int | None = None
    command: CommandReference | None = None
    detail: str | None = None


@dataclass(frozen=True)
class StorageCaller:
    """Task-owned attribution for a runner turn or a direct operator call."""

    deadline: StorageDeadline
    run_id: str
    turn: int | None = None
    command: CommandReference | None = None


_caller: ContextVar[tuple[asyncio.Task[object] | None, StorageCaller] | None] = ContextVar(
    "human_storage_caller", default=None
)


def current_storage_caller() -> StorageCaller | None:
    """Read the caller only in its original task, including after an await."""
    bound = _caller.get()
    if bound is None or bound[0] is not asyncio.current_task():
        return None
    return bound[1]


@contextmanager
def storage_caller(caller: StorageCaller) -> Generator[None]:
    """Bind storage attribution locally without granting it to background tasks."""
    token = _caller.set((asyncio.current_task(), caller))
    try:
        yield
    finally:
        _caller.reset(token)


_command_event: ContextVar[CommandReference | None] = ContextVar(
    "human_audit_command", default=None
)


def current_audit_command() -> CommandReference | None:
    """Read the command reference bound to a synchronous event emission."""
    return _command_event.get()


@contextmanager
def audit_command(reference: CommandReference | None) -> Generator[None]:
    """Bind one synchronous producer emission to its own command attempt."""
    token = _command_event.set(reference)
    try:
        yield
    finally:
        _command_event.reset(token)
