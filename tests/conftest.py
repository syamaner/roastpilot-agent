"""Shared test fixtures (component plan §8).

Established at E1 as placeholders; growing into the real test doubles with
their epics: protocol fakes for the controller tick pipeline (E4-S2), the
scripted MCP contract (E4/E5), advisor fixtures (E8), temp SQLite store
(E6). All M1 tests run hardware-free. Fakes accept an optional shared
``log`` list so tests can assert cross-collaborator call order.
"""

from pathlib import Path

import pytest

from roastpilot_agent.advisor import AdvisorContext, RoastAdvisor, RoastDecision
from roastpilot_agent.models import RoastEventKind, RoastTelemetry
from roastpilot_agent.safety import SafetyEvaluation
from roastpilot_agent.store import RoastStore


class FakeMCPClient:
    """Test double for RoasterMCPClient.

    The scripted 13-tool contract (state sequences, latched T0/FC events,
    read/write fault injection) lands in E4/E5.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []


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

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self._log.append("set_targets")
        self.targets.append((heat_percent, fan_percent))

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


class ScriptedAdvisor(RoastAdvisor):
    """Deterministic advisor double returning pre-scripted decisions.

    The full fixture set (valid / malformed / unsafe / timeout / provider
    error) lands in E8.
    """

    def __init__(
        self,
        decisions: list[RoastDecision] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._decisions: list[RoastDecision] = list(decisions or [])
        self._log = log if log is not None else []
        self.contexts: list[AdvisorContext] = []

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Pop and return the next scripted decision."""
        self._log.append("advisor")
        self.contexts.append(context)
        if not self._decisions:
            raise AssertionError("ScriptedAdvisor has no scripted decisions left")
        return self._decisions.pop(0)


@pytest.fixture
def fake_mcp_client() -> FakeMCPClient:
    """A fake MCP client with no scripted behavior yet (E4/E5)."""
    return FakeMCPClient()


@pytest.fixture
def scripted_advisor() -> ScriptedAdvisor:
    """A deterministic advisor double with an empty script (E8)."""
    return ScriptedAdvisor()


@pytest.fixture
def event_sink() -> EventSink:
    """An event-sink test double recording emitted events (E4/E7)."""
    return EventSink()


@pytest.fixture
def tmp_store(tmp_path: Path) -> RoastStore:
    """A RoastStore backed by a temporary SQLite path (initialization: E6)."""
    return RoastStore(db_path=tmp_path / "roastpilot-test.sqlite3")
