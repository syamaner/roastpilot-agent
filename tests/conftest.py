"""Shared test fixtures (component plan §8).

Established at E1 as placeholders; growing into the real test doubles with
their epics: protocol fakes for the controller tick pipeline (E4-S2), the
scripted MCP contract (E4/E5), advisor fixtures (E8), temp SQLite store
(E6). All M1 tests run hardware-free. Fakes accept an optional shared
``log`` list so tests can assert cross-collaborator call order.
"""

from pathlib import Path

import pytest

from roastpilot_agent.advisor import FakeAdvisor
from roastpilot_agent.models import RoastEventKind, RoastTelemetry
from roastpilot_agent.safety import SafetyEvaluation
from roastpilot_agent.store import RoastStore


class FakeClock:
    """Deterministic monotonic clock for scheduler/controller tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeMCPClient:
    """Scripted fake roaster MCP: StateReader + CommandExecutor combined.

    Feed it a full-roast telemetry script (list of RoastTelemetry / None /
    Exception frames, last frame repeating); it records every write
    command. E5's real client replaces it behind the same protocols;
    E9's vertical slice reuses the scripts.
    """

    def __init__(
        self,
        frames: list[RoastTelemetry | None | Exception] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.frames: list[RoastTelemetry | None | Exception] = list(frames or [])
        self._log = log if log is not None else []
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def read_telemetry(self) -> RoastTelemetry | None:
        self._log.append("read")
        if not self.frames:
            return None
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def start_session(self) -> None:
        self._log.append("start_session")
        self.calls.append(("start_session", {}))

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self._log.append("set_targets")
        self.calls.append(
            ("set_targets", {"heat_percent": heat_percent, "fan_percent": fan_percent})
        )

    async def mark_first_crack(self) -> None:
        self._log.append("mark_first_crack")
        self.calls.append(("mark_first_crack", {}))

    async def drop_beans(self) -> None:
        self._log.append("drop_beans")
        self.calls.append(("drop_beans", {}))

    async def stop_cooling(self) -> None:
        self._log.append("stop_cooling")
        self.calls.append(("stop_cooling", {}))

    async def emergency_stop(self, *, reason: str) -> None:
        self._log.append("emergency_stop")
        self.calls.append(("emergency_stop", {"reason": reason}))

    def commands(self) -> list[str]:
        return [name for name, _ in self.calls]


class ScriptedStateReader:
    """StateReader protocol fake: yields scripted readings in order.

    Items may be RoastTelemetry, None (clean no-session read), or an
    Exception instance (raised, simulating an MCP read fault). The last
    item repeats once the script is exhausted.
    """

    def __init__(
        self,
        readings: list[RoastTelemetry | None | Exception] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.readings: list[RoastTelemetry | None | Exception] = list(readings or [])
        self._log = log if log is not None else []

    async def read_telemetry(self) -> RoastTelemetry | None:
        self._log.append("read")
        if not self.readings:
            return None
        item = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(item, Exception):
            raise item
        return item


class RecordingExecutor:
    """CommandExecutor protocol fake recording every write."""

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.targets: list[tuple[int, int]] = []
        self.estop_reasons: list[str] = []
        self.commands: list[str] = []

    async def start_session(self) -> None:
        self._log.append("start_session")
        self.commands.append("start_session")

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self._log.append("set_targets")
        self.targets.append((heat_percent, fan_percent))

    async def mark_first_crack(self) -> None:
        self._log.append("mark_first_crack")
        self.commands.append("mark_first_crack")

    async def drop_beans(self) -> None:
        self._log.append("drop_beans")
        self.commands.append("drop_beans")

    async def stop_cooling(self) -> None:
        self._log.append("stop_cooling")
        self.commands.append("stop_cooling")

    async def emergency_stop(self, *, reason: str) -> None:
        self._log.append("emergency_stop")
        self.estop_reasons.append(reason)


class RecordingSnapshotSink:
    """SnapshotSink protocol fake recording persisted ticks."""

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.snapshots: list[RoastTelemetry | None] = []
        self.evaluations: list[SafetyEvaluation] = []

    async def persist_snapshot(self, telemetry: RoastTelemetry | None) -> None:
        self._log.append("persist_snapshot")
        self.snapshots.append(telemetry)

    async def persist_evaluation(self, evaluation: SafetyEvaluation) -> None:
        self._log.append(f"persist_evaluation:{evaluation.rule}")
        self.evaluations.append(evaluation)


class EventSink:
    """EventEmitter protocol fake recording emitted events."""

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.events: list[tuple[RoastEventKind, object]] = []

    def emit(self, kind: RoastEventKind, payload: object) -> None:
        self._log.append(f"emit:{kind.value}")
        self.events.append((kind, payload))

    def kinds(self) -> list[RoastEventKind]:
        return [kind for kind, _ in self.events]


@pytest.fixture
def fake_mcp_client() -> FakeMCPClient:
    """A fake MCP client with no scripted behavior yet (E4/E5)."""
    return FakeMCPClient()


@pytest.fixture
def fake_advisor() -> FakeAdvisor:
    """The deterministic scriptable advisor with an empty script (E8-S1).

    The src ``FakeAdvisor`` absorbed the former conftest ScriptedAdvisor:
    one deterministic advisor double, scriptable with decisions and
    failure modes, shared by tests and demos.
    """
    return FakeAdvisor()


@pytest.fixture
def event_sink() -> EventSink:
    """An event-sink test double recording emitted events (E4/E7)."""
    return EventSink()


@pytest.fixture
def tmp_store(tmp_path: Path) -> RoastStore:
    """A RoastStore backed by a temporary SQLite path (initialization: E6)."""
    return RoastStore(db_path=tmp_path / "roastpilot-test.sqlite3")
