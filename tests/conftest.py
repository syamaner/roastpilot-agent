"""Shared test fixtures (component plan §8).

Established at E1 as placeholders; growing into the real test doubles with
their epics: protocol fakes for the controller tick pipeline (E4-S2), the
scripted MCP contract (E4/E5), advisor fixtures (E8), temp SQLite store
(E6). All M1 tests run hardware-free. Fakes accept an optional shared
``log`` list so tests can assert cross-collaborator call order.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    FakeAdvisor,
    RoastDecision,
)
from roastpilot_agent.mcp_client import (
    ExportRoastLogResult,
    reset_non_finite_telemetry_warnings,
)
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    AppliedRoasterState,
    RoastEventKind,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyEvaluation
from roastpilot_agent.store import RoastStore

#: The real drivers' (mock + Hottop) documented drop_beans() side effect:
#: heat 0 %, fan 100 %, cooling engaged (coffee_roaster_mcp.drivers). Used as
#: the fakes' default so a test that doesn't care about the exact applied
#: values still gets a realistic one — override per test to check adoption of
#: a DIFFERENT value (#507).
DEFAULT_DROP_APPLIED_STATE = AppliedRoasterState(
    heat_level_percent=0, fan_level_percent=100, cooling_on=True
)

#: The real driver's documented emergency_stop() side effect: heat 0 %, fan
#: 100 %, cooling engaged (coffee_roaster_mcp.drivers.EmergencyStopResult /
#: default_emergency_safety_payload). Same rationale as the drop default above.
DEFAULT_EMERGENCY_STOP_APPLIED_STATE = AppliedRoasterState(
    heat_level_percent=0, fan_level_percent=100, cooling_on=True
)


@pytest.fixture(autouse=True)
def reset_non_finite_warning_budget() -> None:
    """Give every test an independent per-run telemetry warning budget."""
    reset_non_finite_telemetry_warnings()


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
        export_result: ExportRoastLogResult | None = None,
        drop_applied_state: AppliedRoasterState | None = DEFAULT_DROP_APPLIED_STATE,
        emergency_stop_applied_state: AppliedRoasterState
        | None = DEFAULT_EMERGENCY_STOP_APPLIED_STATE,
    ) -> None:
        self.frames: list[RoastTelemetry | None | Exception] = list(frames or [])
        self._log = log if log is not None else []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.export_result = export_result
        #: Applied state returned by drop_beans()/emergency_stop() (#507).
        #: Override per-instance to assert adoption of a NON-default value, or
        #: set to None to simulate a malformed/out-of-contract MCP payload
        #: (the RoasterControlAdapter.None-on-malformed-payload contract).
        self.drop_applied_state = drop_applied_state
        self.emergency_stop_applied_state = emergency_stop_applied_state

    async def read_telemetry(self) -> RoastTelemetry | None:
        self._log.append("read")
        if not self.frames:
            return None
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        self._log.append("start_session")
        self.calls.append(
            (
                "start_session",
                {"recording_origin": recording_origin, "recording_roast_num": recording_roast_num},
            )
        )

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self._log.append("set_targets")
        self.calls.append(
            ("set_targets", {"heat_percent": heat_percent, "fan_percent": fan_percent})
        )

    async def mark_beans_added(self) -> None:
        self._log.append("mark_beans_added")
        self.calls.append(("mark_beans_added", {}))

    async def mark_first_crack(self) -> None:
        self._log.append("mark_first_crack")
        self.calls.append(("mark_first_crack", {}))

    async def drop_beans(self) -> AppliedRoasterState | None:
        self._log.append("drop_beans")
        self.calls.append(("drop_beans", {}))
        return self.drop_applied_state

    async def start_cooling(self) -> None:
        self._log.append("start_cooling")
        self.calls.append(("start_cooling", {}))

    async def stop_cooling(self) -> None:
        self._log.append("stop_cooling")
        self.calls.append(("stop_cooling", {}))

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
        self._log.append("emergency_stop")
        self.calls.append(("emergency_stop", {"reason": reason}))
        return self.emergency_stop_applied_state

    async def export_roast_log(self) -> ExportRoastLogResult:
        self._log.append("export_roast_log")
        self.calls.append(("export_roast_log", {}))
        if self.export_result is None:
            raise RuntimeError("FakeMCPClient has no export_result configured")
        return self.export_result

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

    def __init__(
        self,
        log: list[str] | None = None,
        drop_applied_state: AppliedRoasterState | None = DEFAULT_DROP_APPLIED_STATE,
        emergency_stop_applied_state: AppliedRoasterState
        | None = DEFAULT_EMERGENCY_STOP_APPLIED_STATE,
    ) -> None:
        self._log = log if log is not None else []
        self.targets: list[tuple[int, int]] = []
        self.estop_reasons: list[str] = []
        self.commands: list[str] = []
        #: (recording_origin, recording_roast_num) captured per start_session call.
        self.start_session_metadata: list[tuple[str | None, int | None]] = []
        #: Applied state returned by drop_beans()/emergency_stop() (#507).
        #: Override per-instance to assert adoption of a NON-default value, or
        #: set to None to simulate a malformed/out-of-contract MCP payload
        #: (the RoasterControlAdapter.None-on-malformed-payload contract).
        self.drop_applied_state = drop_applied_state
        self.emergency_stop_applied_state = emergency_stop_applied_state

    async def start_session(
        self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
    ) -> None:
        self._log.append("start_session")
        self.commands.append("start_session")
        self.start_session_metadata.append((recording_origin, recording_roast_num))

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        self._log.append("set_targets")
        self.targets.append((heat_percent, fan_percent))

    async def mark_beans_added(self) -> None:
        self._log.append("mark_beans_added")
        self.commands.append("mark_beans_added")

    async def mark_first_crack(self) -> None:
        self._log.append("mark_first_crack")
        self.commands.append("mark_first_crack")

    async def drop_beans(self) -> AppliedRoasterState | None:
        self._log.append("drop_beans")
        self.commands.append("drop_beans")
        return self.drop_applied_state

    async def start_cooling(self) -> None:
        self._log.append("start_cooling")
        self.commands.append("start_cooling")

    async def stop_cooling(self) -> None:
        self._log.append("stop_cooling")
        self.commands.append("stop_cooling")

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
        self._log.append("emergency_stop")
        self.estop_reasons.append(reason)
        return self.emergency_stop_applied_state


@dataclass
class RecordedAdvisorDecision:
    """One advisor decision recorded by :class:`RecordingSnapshotSink` (#167)."""

    descriptor: AdvisorDescriptor
    context: AdvisorContext
    latency_ms: int | None
    decision: RoastDecision | None
    status: AdvisorTraceStatus
    safety_evaluation_id: int | None


class RecordingSnapshotSink:
    """SnapshotSink protocol fake recording persisted ticks.

    ``persist_evaluation`` hands back a synthetic, monotonically increasing
    row id (as the SQLite sink does) so advisor decisions can record the
    safety_evaluation_id they linked to — the #167 trace join.
    """

    def __init__(self, log: list[str] | None = None) -> None:
        self._log = log if log is not None else []
        self.snapshots: list[RoastTelemetry | None] = []
        self.evaluations: list[SafetyEvaluation] = []
        self.advisor_decisions: list[RecordedAdvisorDecision] = []
        self._next_evaluation_id = 0

    async def persist_snapshot(self, telemetry: RoastTelemetry | None) -> None:
        self._log.append("persist_snapshot")
        self.snapshots.append(telemetry)

    async def persist_evaluation(self, evaluation: SafetyEvaluation) -> int | None:
        self._log.append(f"persist_evaluation:{evaluation.rule}")
        self.evaluations.append(evaluation)
        self._next_evaluation_id += 1
        return self._next_evaluation_id

    async def persist_advisor_decision(
        self,
        *,
        descriptor: AdvisorDescriptor,
        context: AdvisorContext,
        latency_ms: int | None,
        decision: RoastDecision | None,
        status: AdvisorTraceStatus,
        safety_evaluation_id: int | None,
    ) -> None:
        self._log.append(f"persist_advisor_decision:{status}")
        self.advisor_decisions.append(
            RecordedAdvisorDecision(
                descriptor=descriptor,
                context=context,
                latency_ms=latency_ms,
                decision=decision,
                status=status,
                safety_evaluation_id=safety_evaluation_id,
            )
        )


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


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Shut down bounded parse workers before pytest enters interpreter exit."""
    del session, exitstatus
    from roastpilot_agent import bean_sourcing

    with bean_sourcing._parse_executor_lock:  # pyright: ignore[reportPrivateUsage]
        executor = bean_sourcing._parse_executor  # pyright: ignore[reportPrivateUsage]
        bean_sourcing._parse_executor = None  # pyright: ignore[reportPrivateUsage]
    if executor is not None:
        # Test parse fakes cap their own waits at five seconds. This explicit
        # join is test cleanup, not a production claim that Python can kill a
        # genuinely stuck parser thread.
        executor.shutdown(wait=True, cancel_futures=True)
