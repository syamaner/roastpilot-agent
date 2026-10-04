"""Hardware-free behavioural tests for the single-phase cold observation engine (#954 4f-c).

Every test uses a scripted clock, fake or real-client MCP over local callers,
fake hosts, and the real evidence writer under ``tmp_path``.  Nothing touches
hardware, a serial port, a microphone, a provider, or a child process.
"""

import ast
import asyncio
import enum
import gc
import json
import math
import threading
import types
import typing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import engine
from roastpilot_agent.cold_characterisation import engine_policy as policy
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.evidence_reader import (
    ABORT_REASON_BY_DOMAIN,
    read_retained_run_v6,
)
from roastpilot_agent.cold_characterisation.evidence_temperature import (
    ColdTickTemperatureRecord,
    pairs_with,
)
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    ColdTemperatureAbortRecord,
    ColdTemperatureScreenReason,
)
from roastpilot_agent.cold_characterisation.host import (
    ColdHostBoundError,
    ColdHostBoundFailure,
    HostBoundSample,
    LinuxHostBoundsReader,
)
from roastpilot_agent.cold_characterisation.identity import ColdRunIdentity
from roastpilot_agent.cold_characterisation.mcp import (
    ColdCharacterisationMCPClient,
    ColdMcpValidationError,
    ColdModeForbiddenToolError,
    ColdRoastFanOutcome,
    ColdTickAudioProjectionError,
    ColdTickDeviceProjectionError,
    ColdTickObservation,
    ColdTickRoastFanProjectionError,
    ColdTickSessionProjectionError,
    ColdTickTemperatureProjectionError,
)
from roastpilot_agent.cold_characterisation.temperature_screen import evaluate_temperature
from roastpilot_agent.config import MCPConfig
from roastpilot_agent.mcp_client import (
    EventCommandResult,
    MCPConnectionError,
    MCPServerProcess,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
)
from tests.test_cold_characterisation_evidence_builders import (
    FIXTURE_SESSION_ID,
    RUN_ID,
    STATE_FIXTURE_PATH,
    audio_payload,
    device_state,
    host_sample,
    make_identity,
    observation,
    roast_fan_state,
    screened_temperature,
    session_metadata,
)
from tests.test_cold_characterisation_evidence_store import make_root
from tests.test_cold_characterisation_temperature_projection import celsius_agree

OFF = schema.ColdPhaseKind.RECORDING_OFF
ON = schema.ColdPhaseKind.RECORDING_ON
DRIVER = "hottop_kn8828b_2k_plus"
SID = FIXTURE_SESSION_ID
T0 = 100.0
BASE_UTC = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
CANARY = "CANARY-4fc-7f3e"
Reason = schema.ColdEngineAbortReason
Domain = schema.ColdAbortDomain
_MCP_FIXTURES = Path(__file__).parent / "fixtures" / "mcp-tool-results"
_POLARITY: typing.Final = object()
#: Syntactically valid ISO 8601 UTC text over the record bound; only length refuses it.
OVERSIZED_UTC = "2026-09-26T12:00:00." + "0" * 2100 + "+00:00"


# ------------------------------------------------------------------ harness


def cold_identity(
    tmp_path: Path,
    root: str,
    *,
    phase: schema.ColdPhaseKind = OFF,
    recording: object = _POLARITY,
    dirty: bool = False,
    tick: float = 1.0,
    run_id: str = RUN_ID,
    host_notes: str = "host",
) -> ColdRunIdentity:
    """Return one genuine validated cold identity with explicit recording flags."""
    document = make_identity(tmp_path, pi_root=root, run_id=run_id).model_dump(mode="json")
    flag = (phase is ON) if recording is _POLARITY else recording
    document["device_config"]["recording_enabled"] = flag
    document["device_config"]["recording_autocapture"] = flag
    document["build_provenance"]["source_tree_dirty"] = dirty
    document["controller_tick_seconds"] = tick
    document["operator_host_notes"] = host_notes
    return ColdRunIdentity.model_validate_json(json.dumps(document))


def cold_state_document(session_id: str = SID) -> dict[str, typing.Any]:
    """Return the committed state fixture as a raw cold tick response."""
    document = typing.cast(dict[str, typing.Any], json.loads(STATE_FIXTURE_PATH.read_text()))
    document["session_id"] = session_id
    document["session_purpose"] = "cold_characterisation"
    document["cold_characterisation_observation"] = {
        "outcome": "observed",
        "roast_fan_level_percent": 0,
    }
    document["cold_temperature_projection"] = celsius_agree()
    return document


def start_result(session_id: str = SID) -> StartRoastSessionResult:
    """Return one strict cold start result naming ``session_id``."""
    return StartRoastSessionResult.model_validate({"session": cold_state_document(session_id)})


def marked_document() -> dict[str, typing.Any]:
    """Return the committed beans-added response for the cold session."""
    document = typing.cast(
        dict[str, typing.Any], json.loads((_MCP_FIXTURES / "mark_beans_added.json").read_text())
    )
    document["session_id"] = SID
    return document


def clean(index: int = 0, **overrides: typing.Any) -> ColdTickObservation:
    """Return one clean strict tick whose heartbeat and packet count advance with ``index``."""
    session = overrides.pop("session", None) or session_metadata(
        elapsed_monotonic_seconds=1.0 + index
    )
    audio = overrides.pop("audio", None)
    roast_fan = overrides.pop("roast_fan", None)
    temperature = overrides.pop("temperature", None) or screened_temperature(index)
    device = overrides.pop("device", "default")
    if device == "default":
        overrides.setdefault("driver", DRIVER)
        device = device_state(**overrides)
    return observation(
        device, roast_fan=roast_fan, audio=audio, session=session, temperature=temperature
    )


class FakeClock:
    """Scripted virtual clock; a fault is keyed by the zero-based sample index."""

    def __init__(
        self,
        *,
        faults: dict[int, str] | None = None,
        oversleep: dict[int, float] | None = None,
        sleep_error: bool = False,
        sleep_gate: asyncio.Event | None = None,
    ) -> None:
        self.t = T0
        self.samples = 0
        self.faults = faults or {}
        self.oversleep = oversleep or {}
        self.sleep_error = sleep_error
        self.sleep_gate = sleep_gate
        self.sleep_entered = asyncio.Event()
        self.sleeps: list[float] = []
        self.good: list[tuple[float, str]] = []
        self._index = -1

    def _utc(self) -> str:
        return (BASE_UTC + timedelta(seconds=self.t)).isoformat()

    def monotonic(self) -> float:
        self._index = self.samples
        self.samples += 1
        fault = self.faults.get(self._index)
        values: dict[str, float] = {"nan": math.nan, "inf": math.inf, "negative": -1.0}
        if fault in values:
            return values[fault]
        if fault == "decrease":
            return self.t - 50.0
        if fault == "int":
            return typing.cast(float, int(self.t))
        if fault == "mono_raise":
            raise RuntimeError(CANARY)
        return self.t

    def utc_now_iso(self) -> str:
        fault = self.faults.get(self._index)
        if fault == "utc_raise":
            raise RuntimeError(CANARY)
        if fault == "bad_utc":
            return "not-a-time"
        if fault == "offset":
            return (
                (BASE_UTC + timedelta(seconds=self.t))
                .astimezone(timezone(timedelta(hours=1)))
                .isoformat()
            )
        if fault == "oversize":
            return OVERSIZED_UTC
        if fault is None:
            self.good.append((self.t, self._utc()))
        return self._utc()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.sleep_gate is not None:
            self.sleep_entered.set()
            await self.sleep_gate.wait()
        if self.sleep_error:
            raise RuntimeError(CANARY)
        self.t += seconds + self.oversleep.get(len(self.sleeps) - 1, 0.0)


class FakeMcp:
    """Five-method fake cold MCP; reads advance the scripted clock."""

    def __init__(
        self,
        identity: ColdRunIdentity,
        clock: FakeClock,
        *,
        ticks: Callable[[int], object] = clean,
        durations: Callable[[int], float] = lambda _index: 0.05,
        start: object = None,
        mark_error: BaseException | None = None,
        info_error: BaseException | None = None,
        server_info: ServerInfo | None = None,
        runtime_config: RuntimeConfigSnapshot | None = None,
        read_gate: asyncio.Event | None = None,
    ) -> None:
        self.identity = identity
        self.clock = clock
        self.ticks = ticks
        self.durations = durations
        self.start = start_result() if start is None else start
        self.mark_error = mark_error
        self.info_error = info_error
        self.server_info = server_info or identity.server_info
        self.runtime_config = runtime_config or identity.runtime_config
        self.read_gate = read_gate
        self.read_entered = asyncio.Event()
        self.calls: list[str] = []
        self.read_starts: list[float] = []

    async def get_server_info(self) -> ServerInfo:
        self.calls.append("get_server_info")
        if self.info_error is not None:
            raise self.info_error
        return self.server_info

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        self.calls.append("get_runtime_config")
        return self.runtime_config

    async def start_cold_session(self) -> StartRoastSessionResult:
        self.calls.append("start_roast_session")
        if isinstance(self.start, BaseException):
            raise self.start
        return typing.cast(StartRoastSessionResult, self.start)

    async def mark_beans_added(self) -> EventCommandResult:
        self.calls.append("mark_beans_added")
        if self.mark_error is not None:
            raise self.mark_error
        return EventCommandResult.model_validate(marked_document())

    async def get_roast_state(self, session_id: str | None = None) -> ColdTickObservation:
        index = len(self.read_starts)
        self.calls.append("get_roast_state")
        self.read_starts.append(self.clock.t)
        assert session_id is not None
        if self.read_gate is not None:
            self.read_entered.set()
            await self.read_gate.wait()
        self.clock.t += self.durations(index)
        item = self.ticks(index)
        if isinstance(item, BaseException):
            raise item
        return typing.cast(ColdTickObservation, item)


class FakeHost:
    """Host fake whose calls can be gated on a thread event."""

    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        sample_error: BaseException | None = None,
        gate: threading.Event | None = None,
        gate_on: str = "sample",
    ) -> None:
        self.start_error = start_error
        self.sample_error = sample_error
        self.gate = gate
        self.gate_on = gate_on
        self.start_calls = 0
        self.sample_calls = 0
        self.entered = threading.Event()
        self.completed = threading.Event()

    def _run(self, method: str, error: BaseException | None) -> None:
        if method != self.gate_on:
            if error is not None:
                raise error
            return
        self.entered.set()
        try:
            if self.gate is not None:
                self.gate.wait(5.0)
            if error is not None:
                raise error
        finally:
            self.completed.set()

    def check_start_bounds(self, evidence_root: Path) -> None:
        del evidence_root
        self.start_calls += 1
        self._run("check_start_bounds", self.start_error)

    def sample(self, evidence_root: Path) -> HostBoundSample:
        del evidence_root
        self.sample_calls += 1
        self._run("sample", self.sample_error)
        return host_sample()


class SpySink:
    """Delegates to the real writer; an injected failure replaces one attempt.

    Paired tick-temperature and temperature-abort records are kept in their own
    lists, so ``records`` keeps listing v1 records only.
    """

    def __init__(
        self,
        writer: engine.ColdEngineSink,
        *,
        fail_at: int | None = None,
        error: BaseException | None = None,
        on_append: Callable[[schema.ColdEvidenceRecord], None] | None = None,
        temperature_error: BaseException | None = None,
    ) -> None:
        self.writer = writer
        self.fail_at = fail_at
        self.error = error
        self.on_append = on_append
        self.temperature_error = temperature_error
        self.attempts = 0
        self.records: list[schema.ColdEvidenceRecord] = []
        self.temperatures: list[ColdTickTemperatureRecord] = []
        self.temperature_aborts: list[ColdTemperatureAbortRecord] = []
        #: Every durable append's stream, across all three methods, in order.
        self.order: list[str] = []

    def append_tick_temperature(self, record: ColdTickTemperatureRecord) -> None:
        if self.temperature_error is not None:
            raise self.temperature_error
        self.writer.append_tick_temperature(record)
        self.temperatures.append(record)
        self.order.append(record.stream)

    def append_temperature_abort(self, record: ColdTemperatureAbortRecord) -> None:
        self.writer.append_temperature_abort(record)
        self.temperature_aborts.append(record)
        self.order.append(record.stream)

    def append(self, record: schema.ColdEvidenceRecord) -> None:
        index = self.attempts
        self.attempts += 1
        if self.on_append is not None:
            self.on_append(record)
        if index == self.fail_at and self.error is not None:
            raise self.error
        self.writer.append(record)
        self.records.append(record)
        self.order.append(record.stream)


class Rig(typing.NamedTuple):
    identity: ColdRunIdentity
    root: str
    admitted: store.ColdAdmittedRoot
    mcp: FakeMcp
    host: FakeHost
    clock: FakeClock
    phase: schema.ColdPhaseKind


def make_rig(
    tmp_path: Path,
    *,
    phase: schema.ColdPhaseKind = OFF,
    clock: FakeClock | None = None,
    host: FakeHost | None = None,
    ticks: Callable[[int], object] = clean,
    durations: Callable[[int], float] = lambda _index: 0.05,
    start: object = None,
    mark_error: BaseException | None = None,
    read_gate: asyncio.Event | None = None,
) -> Rig:
    """Return one admitted root, genuine identity and fresh fakes."""
    root = make_root(tmp_path)
    identity = cold_identity(tmp_path, root, phase=phase)
    clock = clock or FakeClock()
    mcp = FakeMcp(
        identity,
        clock,
        ticks=ticks,
        durations=durations,
        start=start,
        mark_error=mark_error,
        read_gate=read_gate,
    )
    return Rig(
        identity, root, store.admit_evidence_root(root), mcp, host or FakeHost(), clock, phase
    )


async def admit(rig: Rig) -> engine.ColdPhaseAdmission:
    """Admit the rig's phase."""
    return await engine.admit_cold_phase(
        identity=rig.identity,
        phase=rig.phase,
        root=rig.admitted,
        mcp=rig.mcp,
        host=rig.host,
        clock=rig.clock,
    )


async def run_phase(
    rig: Rig, **sink_options: typing.Any
) -> tuple[engine.ColdPhaseCompleted | engine.ColdPhaseAborted, SpySink]:
    """Admit, open, and observe one phase through a spy over the real writer."""
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission), **sink_options)
    result = await engine.observe_cold_phase(
        admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
    )
    return result, sink


def streams(sink: SpySink) -> list[str]:
    """Return the retained stream sequence."""
    return [record.stream for record in sink.records]


def aborts_of(sink: SpySink) -> list[schema.ColdAbortRecord]:
    """Return the retained abort records."""
    return [record for record in sink.records if type(record) is schema.ColdAbortRecord]


def engine_aborts(
    *reasons: schema.ColdEngineAbortReason,
) -> tuple[engine.ColdAbortClassification, ...]:
    """Return ENGINE classifications in order."""
    return tuple(engine.ColdAbortClassification(domain=Domain.ENGINE, reason=r) for r in reasons)


def aborted(result: object) -> engine.ColdPhaseAborted:
    """Narrow a result to aborted."""
    assert type(result) is engine.ColdPhaseAborted
    return result


def completed(result: object) -> engine.ColdPhaseCompleted:
    """Narrow a result to completed."""
    assert type(result) is engine.ColdPhaseCompleted
    return result


def assert_contained(error: BaseException, message: str) -> None:
    """Assert a fixed closed error: no canary, no chains."""
    assert error.args == (message,)
    assert CANARY not in str(error) and CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def root_entries(root: str) -> list[str]:
    """Return the evidence root's entries."""
    return sorted(path.name for path in Path(root).iterdir())


def tick_record(obs: ColdTickObservation, tmp_path: Path) -> schema.ColdTickRecord:
    """Build one validated retained record for pure-policy tests."""
    root = str(tmp_path.resolve())
    header = builders.build_run_header(
        identity=cold_identity(tmp_path, root),
        phase=OFF,
        recorded_at_utc="2026-09-26T12:00:00+00:00",
        monotonic_seconds=1.0,
    )
    return builders.build_tick_record(
        header=header,
        tick=0,
        recorded_at_utc="2026-09-26T12:00:01+00:00",
        monotonic_seconds=2.0,
        observation=obs,
    )


def decide(
    record: schema.ColdTickRecord,
    *,
    since: float = 0.0,
    previous: float | None = None,
) -> tuple[schema.ColdEngineAbortReason, ...]:
    """Evaluate one record with the fixed test bindings."""
    return policy.evaluate_tick(
        record,
        established_session_id=SID,
        frozen_driver=DRIVER,
        since_activation_seconds=since,
        previous_elapsed=previous,
    ).reasons


# ---------------------------------------------------------------- T1 / T2


@pytest.mark.asyncio
async def test_t1_happy_path_observes_the_full_window_and_round_trips(tmp_path: Path) -> None:
    """T1: five operations only, one tick per read, retained run reads back."""
    rig = make_rig(tmp_path)
    admission = await admit(rig)
    writer = engine.open_phase_evidence(admission)
    sink = SpySink(writer)
    result = completed(
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    )
    reads = rig.mcp.calls.count("get_roast_state")
    assert rig.mcp.calls == [
        "get_server_info",
        "get_runtime_config",
        "start_roast_session",
        "mark_beans_added",
        *["get_roast_state"] * reads,
    ]
    assert reads == 600
    assert result == engine.ColdPhaseCompleted(
        session_id=SID,
        observation_end_monotonic=T0 + 600.0,
        observation_end_utc=(BASE_UTC + timedelta(seconds=T0 + 600.0)).isoformat(),
        tick_count=600,
    )
    assert streams(sink) == ["header", *["tick", "host"] * reads]
    ticks = [r for r in sink.records if type(r) is schema.ColdTickRecord]
    assert [r.tick for r in ticks] == list(range(reads))
    assert all(start < T0 + policy.COLD_PHASE_OBSERVATION_SECONDS for start in rig.mcp.read_starts)
    assert rig.host.start_calls == 1 and rig.host.sample_calls == reads
    assert len(sink.temperatures) == reads and sink.temperature_aborts == []
    sealed = writer.seal()
    # The tree now carries the paired tick-temperature stream, so it reads through V6.
    v6 = read_retained_run_v6(
        rig.root, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    retained = v6.run
    assert [h.header for h in retained.headers] == [admission.header]
    by_stream = {stream.stream.value: stream.records for stream in retained.streams}
    assert list(by_stream["tick"]) == ticks
    assert len(by_stream["host"]) == reads
    assert list(v6.tick_temperatures) == sink.temperatures
    assert v6.temperature_aborts == () and v6.mcp_candidates == ()


@pytest.mark.asyncio
async def test_t2a_late_wake_spaces_from_the_actual_read_start(tmp_path: Path) -> None:
    """T2a: a wake at 1.9 for a planned 1.0 schedules the next start at 2.9."""
    clock = FakeClock(oversleep={0: 0.9})
    rig = make_rig(
        tmp_path,
        clock=clock,
        durations=lambda index: 0.05,
        ticks=lambda index: clean(index) if index < 3 else clean(index, heat_level_percent=1),
    )
    result = aborted((await run_phase(rig))[0])
    starts = [start - T0 for start in rig.mcp.read_starts]
    assert starts[1] == pytest.approx(1.9)
    assert starts[2] == pytest.approx(2.9)
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(starts, starts[1:], strict=False))
    assert result.aborts == engine_aborts(Reason.COMMAND_STATE_NON_ZERO)


@pytest.mark.asyncio
async def test_t2b_long_read_starts_the_next_read_at_completion(tmp_path: Path) -> None:
    """T2b: a 3.5 s read makes the next start equal its completion, with no catch-up."""
    rig = make_rig(
        tmp_path,
        durations=lambda index: 3.5 if index == 0 else 0.05,
        ticks=lambda index: clean(index) if index < 2 else clean(index, heat_level_percent=1),
    )
    aborted((await run_phase(rig))[0])
    starts = [start - T0 for start in rig.mcp.read_starts]
    assert starts[:3] == pytest.approx([0.0, 3.5, 4.5])
    assert rig.clock.sleeps[0] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_t2c_oversleep_after_the_first_read_completes_without_a_further_read(
    tmp_path: Path,
) -> None:
    """T2c: an oversleep after the first read is re-checked; no read starts at or after it.

    An oversleep before the first read is impossible: the initial next start is
    the activation instant and the first valid P2 is not earlier, so no sleep
    precedes the first read.  The zero-tick clock-jump case is separate.
    """
    clock = FakeClock(oversleep={0: 1900.0})
    rig = make_rig(tmp_path, clock=clock)
    result = completed((await run_phase(rig))[0])
    assert result.tick_count == 1
    assert rig.mcp.read_starts == [T0]
    assert "not qualification" in (engine.ColdPhaseCompleted.__doc__ or "")
    assert "tick_count may be 0" in (engine.ColdPhaseCompleted.__doc__ or "")


@pytest.mark.asyncio
async def test_t2c_a_window_already_elapsed_completes_with_zero_ticks(tmp_path: Path) -> None:
    """T2c: a clock past the deadline before any read gives tick_count 0, not qualification."""
    clock = FakeClock()
    rig = make_rig(tmp_path, clock=clock)

    async def jump() -> EventCommandResult:
        rig.mcp.calls.append("mark_beans_added")
        return EventCommandResult.model_validate(marked_document())

    original = clock.monotonic

    def late() -> float:
        value = original()
        if clock.samples == 3:
            clock.t += 600.0
            return clock.t
        return value

    clock.monotonic = late  # type: ignore[method-assign]
    rig.mcp.mark_beans_added = jump  # type: ignore[method-assign]
    result = completed((await run_phase(rig))[0])
    assert result.tick_count == 0
    assert rig.mcp.read_starts == []


@pytest.mark.parametrize("unsafe", [False, True])
@pytest.mark.asyncio
async def test_t2d_an_in_flight_read_crossing_the_deadline_is_fully_processed(
    tmp_path: Path, unsafe: bool
) -> None:
    """T2d: a read started before the deadline is retained and evaluated, never early-clean.

    EN0 adaptation: a short pre-boundary first read supplies the D209 screen's
    previous snapshot, so the crossing second read is judged only on its own
    command state (a single crossing read would otherwise be ``PRIOR_MISSING``).
    """
    rig = make_rig(
        tmp_path,
        durations=lambda index: 0.05 if index == 0 else 600.5,
        ticks=lambda index: clean(index, heat_level_percent=1 if unsafe and index == 1 else 0),
    )
    result, sink = await run_phase(rig)
    assert rig.mcp.read_starts == [T0, T0 + 1.0]
    assert [record.tick for record in sink.temperatures] == [0, 1]
    if unsafe:
        assert aborted(result).aborts == engine_aborts(Reason.COMMAND_STATE_NON_ZERO)
        assert streams(sink) == ["header", "tick", "host", "tick", "abort"]
    else:
        assert completed(result).tick_count == 2
        assert streams(sink) == ["header", "tick", "host", "tick", "host"]


# -------------------------------------------------------------------- T3


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
@pytest.mark.asyncio
async def test_t3_refusals_create_nothing_and_leak_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: each admission step refuses closed, before any creation or session start."""
    failure = engine.ColdAdmissionFailure
    cases: list[tuple[engine.ColdAdmissionFailure, Callable[[Rig], dict[str, typing.Any]]]] = []

    def expect(
        member: engine.ColdAdmissionFailure,
    ) -> Callable[[Callable[[Rig], dict[str, typing.Any]]], None]:
        def register(build: Callable[[Rig], dict[str, typing.Any]]) -> None:
            cases.append((member, build))

        return register

    expect(failure.PHASE_NOT_ADMITTED)(lambda rig: {"phase": "recording_off"})
    expect(failure.ROOT_NOT_ADMITTED)(lambda rig: {"root": object.__new__(store.ColdAdmittedRoot)})
    expect(failure.ROOT_NOT_ADMITTED)(lambda rig: {"root": rig.root})
    expect(failure.IDENTITY_NOT_ADMITTED)(
        lambda rig: {"identity": rig.identity.model_dump(mode="json")}
    )

    def fahrenheit(rig: Rig) -> dict[str, typing.Any]:
        config = rig.identity.runtime_config.model_copy(update={"temperature_unit": "fahrenheit"})
        return {"identity": rig.identity.model_copy(update={"runtime_config": config})}

    expect(failure.IDENTITY_NOT_ADMITTED)(fahrenheit)

    def coerced(rig: Rig) -> dict[str, typing.Any]:
        nested = rig.identity.runtime_config.model_dump(mode="json")
        return {"identity": rig.identity.model_copy(update={"runtime_config": nested})}

    expect(failure.IDENTITY_NOT_ADMITTED)(coerced)
    expect(failure.SOURCE_TREE_DIRTY)(
        lambda rig: {"identity": cold_identity(tmp_path, rig.root, dirty=True)}
    )
    expect(failure.EVIDENCE_ROOT_MISMATCH)(
        lambda rig: {"identity": cold_identity(tmp_path, rig.root + "-other")}
    )
    expect(failure.TICK_INTERVAL_MISMATCH)(
        lambda rig: {"identity": cold_identity(tmp_path, rig.root, tick=0.5)}
    )
    expect(failure.RECORDING_CONFIG_MISMATCH)(
        lambda rig: {"identity": cold_identity(tmp_path, rig.root, recording=None)}
    )
    expect(failure.RECORDING_CONFIG_MISMATCH)(
        lambda rig: {"identity": cold_identity(tmp_path, rig.root, recording=True)}
    )
    expect(failure.RECORDING_CONFIG_MISMATCH)(
        lambda rig: {
            "phase": ON,
            "identity": cold_identity(tmp_path, rig.root, phase=ON, recording=False),
        }
    )

    def drift(field: str) -> Callable[[Rig], dict[str, typing.Any]]:
        def build(rig: Rig) -> dict[str, typing.Any]:
            if field == "server":
                rig.mcp.server_info = rig.identity.server_info.model_copy(update={"version": "9"})
            else:
                rig.mcp.runtime_config = rig.identity.runtime_config.model_copy(
                    update={"roaster_port": "/dev/other"}
                )
            return {}

        return build

    expect(failure.MCP_IDENTITY_DRIFT)(drift("server"))
    expect(failure.MCP_IDENTITY_DRIFT)(drift("runtime"))

    def read_error(rig: Rig) -> dict[str, typing.Any]:
        rig.mcp.info_error = RuntimeError(CANARY)
        return {}

    expect(failure.MCP_READ_FAILED)(read_error)
    expect(failure.CLOCK_INVALID)(lambda rig: {"clock": FakeClock(faults={0: "nan"})})
    expect(failure.CLOCK_INVALID)(lambda rig: {"clock": FakeClock(faults={0: "mono_raise"})})

    def header_error(rig: Rig) -> dict[str, typing.Any]:
        def boom(**_kwargs: object) -> schema.ColdRunHeader:
            raise ValueError(CANARY)

        monkeypatch.setattr(engine, "build_run_header", boom)
        return {}

    expect(failure.HEADER_NOT_BOUND)(header_error)
    expect(failure.HOST_START_BOUND_FAILED)(
        lambda rig: {"host": FakeHost(start_error=RuntimeError(CANARY))}
    )
    expect(failure.HOST_START_BOUND_FAILED)(
        lambda rig: {
            "host": FakeHost(start_error=ColdHostBoundError(ColdHostBoundFailure.DISK_UNREADABLE))
        }
    )
    for index, (member, build) in enumerate(cases):
        rig = make_rig(tmp_path / f"case{index}")
        arguments: dict[str, typing.Any] = {
            "identity": rig.identity,
            "phase": rig.phase,
            "root": rig.admitted,
            "mcp": rig.mcp,
            "host": rig.host,
            "clock": rig.clock,
        }
        arguments.update(build(rig))
        with pytest.raises(engine.ColdAdmissionRefusedError) as raised:
            await engine.admit_cold_phase(**arguments)
        monkeypatch.undo()
        assert raised.value.failure is member, index
        assert_contained(raised.value, "Cold phase admission refused.")
        assert "start_roast_session" not in rig.mcp.calls
        assert root_entries(rig.root) == []


@pytest.mark.asyncio
async def test_t3_a_genuine_identity_is_admitted_for_both_phases(tmp_path: Path) -> None:
    """T3: genuine identities round-trip and admit with the frozen driver."""
    for phase in (OFF, ON):
        rig = make_rig(tmp_path / phase.value, phase=phase)
        admission = await admit(rig)
        assert admission.phase is phase
        assert admission.frozen_driver == DRIVER
        assert admission.identity == rig.identity
        assert root_entries(rig.root) == []


# -------------------------------------------------------------------- T4


def forge(base: engine.ColdPhaseAdmission, **slots: typing.Any) -> engine.ColdPhaseAdmission:
    """Mint an admission with replaced slots through the private token."""
    values: dict[str, typing.Any] = {
        "identity": base.identity,
        "phase": base.phase,
        "header": base.header,
        "root": base.root,
        "frozen_driver": base.frozen_driver,
    }
    values.update(slots)
    return engine.ColdPhaseAdmission(
        **values,
        token=engine._ADMISSION_TOKEN,  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
async def test_t4_forged_or_mismatched_admissions_are_refused_before_any_append(
    tmp_path: Path,
) -> None:
    """T4: missing slots or mismatched bindings give ADMISSION_NOT_VALID with no append."""
    rig = make_rig(tmp_path)
    base = await admit(rig)
    other_root = make_root(tmp_path, "other")
    other = cold_identity(tmp_path, rig.root, run_id="20260926T120000Z-cold-other")
    variant = cold_identity(tmp_path, rig.root, host_notes="changed")

    def header(identity: ColdRunIdentity, phase: schema.ColdPhaseKind, utc: str, mono: float):
        return builders.build_run_header(
            identity=identity, phase=phase, recorded_at_utc=utc, monotonic_seconds=mono
        )

    good_utc = base.header.recorded_at_utc
    forged = [
        object.__new__(engine.ColdPhaseAdmission),
        forge(base, header=header(rig.identity, ON, good_utc, T0)),
        forge(base, header=header(other, OFF, good_utc, T0)),
        forge(base, header=header(variant, OFF, good_utc, T0)),
        forge(base, root=store.admit_evidence_root(other_root)),
        forge(base, frozen_driver="other-driver"),
        forge(base, header=header(rig.identity, OFF, "2026-09-26T12:00:00+01:00", T0)),
        forge(base, header=header(rig.identity, OFF, good_utc, -1.0)),
        forge(base, identity="not-an-identity"),
        typing.cast(engine.ColdPhaseAdmission, "not-an-admission"),
    ]
    for candidate in forged:
        sink = SpySink(typing.cast(engine.ColdEngineSink, None))
        with pytest.raises(engine.ColdAdmissionRefusedError) as raised:
            await engine.observe_cold_phase(
                admission=candidate, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
            )
        assert raised.value.failure is engine.ColdAdmissionFailure.ADMISSION_NOT_VALID
        assert sink.attempts == 0
        with pytest.raises(engine.ColdAdmissionRefusedError):
            engine.open_phase_evidence(candidate)
    assert root_entries(rig.root) == []


@pytest.mark.asyncio
async def test_t4_capability_is_immutable_and_token_checked(tmp_path: Path) -> None:
    """T4: assignment, deletion, re-initialisation and token-less construction raise."""
    admission = await admit(make_rig(tmp_path))
    with pytest.raises(engine.ColdAdmissionRefusedError):
        admission.frozen_driver = "x"  # type: ignore[misc]
    with pytest.raises(engine.ColdAdmissionRefusedError):
        del admission.header
    with pytest.raises(engine.ColdAdmissionRefusedError):
        engine.ColdPhaseAdmission(
            identity=admission.identity,
            phase=admission.phase,
            header=admission.header,
            root=admission.root,
            frozen_driver=admission.frozen_driver,
            token=object(),
        )
    with pytest.raises(engine.ColdAdmissionRefusedError):
        admission.__init__(  # type: ignore[misc]
            identity=admission.identity,
            phase=admission.phase,
            header=admission.header,
            root=admission.root,
            frozen_driver=admission.frozen_driver,
            token=engine._ADMISSION_TOKEN,  # pyright: ignore[reportPrivateUsage]
        )
    assert admission.frozen_driver == DRIVER


@pytest.mark.asyncio
async def test_t4_evidence_open_and_header_append_failures_are_incomplete(tmp_path: Path) -> None:
    """T4: an existing run directory and a failed header append are closed incompletes."""
    rig = make_rig(tmp_path)
    admission = await admit(rig)
    engine.open_phase_evidence(admission)
    with pytest.raises(engine.ColdEvidenceIncompleteError) as opened:
        engine.open_phase_evidence(admission)
    assert opened.value.failure is engine.ColdAdmissionFailure.EVIDENCE_OPEN_FAILED
    assert_contained(opened.value, "Cold phase evidence is incomplete.")
    rig2 = make_rig(tmp_path / "second")
    admission2 = await admit(rig2)
    writer = engine.open_phase_evidence(admission2)
    sink = SpySink(writer, fail_at=0, error=RuntimeError(CANARY))
    with pytest.raises(engine.ColdEvidenceIncompleteError) as appended:
        await engine.observe_cold_phase(
            admission=admission2, sink=sink, mcp=rig2.mcp, host=rig2.host, clock=rig2.clock
        )
    assert appended.value.failure is engine.ColdAdmissionFailure.HEADER_APPEND_FAILED
    assert_contained(appended.value, "Cold phase evidence is incomplete.")
    assert root_entries(rig2.root) == [RUN_ID]
    assert "start_roast_session" not in rig2.mcp.calls


@pytest.mark.asyncio
async def test_t4_first_last_valid_instant_is_the_header_pair(tmp_path: Path) -> None:
    """T4/T13: an invalid activation instant records the header's own pair."""
    rig = make_rig(tmp_path, clock=FakeClock(faults={1: "nan"}))
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    result = aborted(
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    )
    assert result.aborts == engine_aborts(Reason.CLOCK_INVALID)
    (record,) = aborts_of(sink)
    assert (record.monotonic_seconds, record.recorded_at_utc) == (
        admission.header.monotonic_seconds,
        admission.header.recorded_at_utc,
    )


# ------------------------------------------------------------ T5 / T9 / T11


_IMMEDIATE: list[tuple[str, Callable[[], ColdTickObservation], tuple[Reason, ...]]] = [
    ("heat", lambda: clean(heat_level_percent=1), (Reason.COMMAND_STATE_NON_ZERO,)),
    ("cooling", lambda: clean(cooling_on=True), (Reason.COMMAND_STATE_NON_ZERO,)),
    ("main-fan", lambda: clean(fan_level_percent=30), (Reason.COMMAND_STATE_NON_ZERO,)),
    (
        "roast-fan",
        lambda: clean(roast_fan=roast_fan_state(level=20)),
        (Reason.COMMAND_STATE_NON_ZERO,),
    ),
    ("disconnected", lambda: clean(connected=False), (Reason.DEVICE_DISCONNECTED,)),
    ("driver", lambda: clean(driver="mock"), (Reason.DRIVER_IDENTITY_MISMATCH,)),
    *[
        (
            f"fan-{outcome.value}",
            (lambda o=outcome: clean(roast_fan=roast_fan_state(o, None))),
            (Reason.ROAST_FAN_NOT_OBSERVABLE,),
        )
        for outcome in ColdRoastFanOutcome
        if outcome is not ColdRoastFanOutcome.OBSERVED
    ],
    (
        "inactive",
        lambda: clean(session=session_metadata(active=False)),
        (Reason.SESSION_INACTIVE,),
    ),
    (
        "phase",
        lambda: clean(session=session_metadata(phase="development")),
        (Reason.SESSION_PHASE_CHANGED,),
    ),
    (
        "fc-status",
        lambda: clean(audio={**audio_payload(), "status": "detected"}),
        (Reason.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "fc-utc",
        lambda: clean(audio={**audio_payload(), "detected_at_utc": "2026-09-26T12:00:00Z"}),
        (Reason.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "fc-mono",
        lambda: clean(audio={**audio_payload(), "detected_monotonic_seconds": 3.0}),
        (Reason.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "mode",
        lambda: clean(audio={**audio_payload(), "mode": "manual"}),
        (Reason.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "faulted",
        lambda: clean(audio={**audio_payload(), "status": "faulted"}),
        (Reason.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "unavailable",
        lambda: clean(audio={**audio_payload(), "status": "unavailable"}),
        (Reason.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "foreign-session",
        lambda: clean(session=session_metadata(session_id="foreign")),
        (Reason.MCP_SESSION_IDENTITY_CHANGED,),
    ),
    (
        "device-null",
        lambda: clean(device=None),
        (Reason.MCP_RESPONSE_NOT_ADMITTED,),
    ),
]


@pytest.mark.parametrize(("name", "build", "expected"), _IMMEDIATE, ids=[c[0] for c in _IMMEDIATE])
def test_t5_each_immediate_rule_aborts_without_grace(
    tmp_path: Path,
    name: str,
    build: Callable[[], ColdTickObservation],
    expected: tuple[Reason, ...],
) -> None:
    """T5: each immediate row triggers exactly its reason at 0.0 s."""
    del name
    record = tick_record(build(), tmp_path)
    assert decide(record) == expected


def test_t5_a_clean_tick_decides_nothing_and_carries_the_heartbeat(tmp_path: Path) -> None:
    """T5: the clean baseline has no reason and returns the retained heartbeat."""
    record = tick_record(clean(), tmp_path)
    decision = policy.evaluate_tick(
        record,
        established_session_id=SID,
        frozen_driver=DRIVER,
        since_activation_seconds=1000.0,
        previous_elapsed=0.5,
    )
    assert decision == policy.ColdTickDecision(reasons=(), next_previous_elapsed_seconds=1.0)


_ABSENT: list[tuple[str, Callable[[], ColdTickObservation]]] = [
    ("bean-none", lambda: clean(bean_temp_c=None)),
    ("env-none", lambda: clean(env_temp_c=None)),
    ("audio-disabled", lambda: clean(audio={**audio_payload(), "status": "disabled"})),
    ("audio-manual", lambda: clean(audio={**audio_payload(), "status": "manual"})),
    ("audio-not-running", lambda: clean(audio={**audio_payload(), "audio_running": False})),
]


@pytest.mark.parametrize(("name", "build"), _ABSENT, ids=[c[0] for c in _ABSENT])
def test_t9_absent_telemetry_is_tolerated_strictly_before_sixty_seconds(
    tmp_path: Path, name: str, build: Callable[[], ColdTickObservation]
) -> None:
    """T9: each absent form is silent at 59.999 s and aborts at exactly 60.0 s."""
    del name
    record = tick_record(build(), tmp_path)
    assert decide(record, since=59.999) == ()
    assert decide(record, since=60.0) == (Reason.TELEMETRY_ABSENT_AFTER_STARTUP,)


def test_t9_unreadable_roast_fan_is_never_absent_telemetry(tmp_path: Path) -> None:
    """T9: an unreadable roast fan is unknown commanded state, not absence."""
    record = tick_record(
        clean(roast_fan=roast_fan_state(ColdRoastFanOutcome.UNREADABLE, None)), tmp_path
    )
    assert decide(record, since=0.0) == (Reason.ROAST_FAN_NOT_OBSERVABLE,)
    assert decide(record, since=60.0) == (Reason.ROAST_FAN_NOT_OBSERVABLE,)


def test_t9_a_null_device_is_unknown_state_never_graced(tmp_path: Path) -> None:
    """T9/A2: a null device aborts immediately, never as absent telemetry."""
    record = tick_record(clean(device=None), tmp_path)
    assert decide(record, since=0.0) == (Reason.MCP_RESPONSE_NOT_ADMITTED,)
    assert decide(record, since=59.999) == (Reason.MCP_RESPONSE_NOT_ADMITTED,)
    assert decide(record, since=60.0) == (Reason.MCP_RESPONSE_NOT_ADMITTED,)


def test_t11_reasons_follow_declaration_order(tmp_path: Path) -> None:
    """T11: reasons are filtered from the enum in declaration order."""
    first = clean(
        heat_level_percent=1,
        connected=False,
        session=session_metadata(active=False),
        audio={**audio_payload(), "status": "detected"},
    )
    assert decide(tick_record(first, tmp_path)) == (
        Reason.COMMAND_STATE_NON_ZERO,
        Reason.DEVICE_DISCONNECTED,
        Reason.SESSION_INACTIVE,
        Reason.FIRST_CRACK_CONFIRMED,
    )
    second = clean(
        roast_fan=roast_fan_state(ColdRoastFanOutcome.UNREADABLE, None),
        session=session_metadata(session_id="foreign"),
    )
    assert decide(tick_record(second, tmp_path)) == (
        Reason.ROAST_FAN_NOT_OBSERVABLE,
        Reason.MCP_SESSION_IDENTITY_CHANGED,
    )
    third = clean(bean_temp_c=None, session=session_metadata(session_id="foreign"))
    assert decide(tick_record(third, tmp_path), since=60.0) == (
        Reason.TELEMETRY_ABSENT_AFTER_STARTUP,
        Reason.MCP_SESSION_IDENTITY_CHANGED,
    )


# --------------------------------------------------------------- T6 / T10


@pytest.mark.parametrize(
    "build",
    [
        lambda: clean(roast_fan=roast_fan_state(ColdRoastFanOutcome.UNREADABLE, None)),
        lambda: clean(heat_level_percent=5),
        lambda: clean(device=None),
    ],
    ids=["unreadable", "heat", "null-device"],
)
@pytest.mark.asyncio
async def test_t6_immediate_abort_retains_the_tick_first_and_stops(
    tmp_path: Path, build: Callable[[], ColdTickObservation]
) -> None:
    """T6: at 0.0 s the tick precedes the abort; no host call, no further MCP call."""
    rig = make_rig(tmp_path, ticks=lambda index: build())
    result, sink = await run_phase(rig)
    assert streams(sink) == ["header", "tick", "abort"]
    assert rig.host.sample_calls == 0
    assert rig.mcp.calls[-1] == "get_roast_state"
    assert rig.mcp.calls.count("get_roast_state") == 1
    (record,) = aborts_of(sink)
    (first,) = aborted(result).aborts
    assert (record.domain, record.reason) == (first.domain, first.reason)
    assert aborted(result).abort_recorded is True
    assert aborted(result).session_id == SID


@pytest.mark.parametrize(
    ("second", "stalls"),
    [(1.0, True), (0.5, True), (1.5, False)],
    ids=["equal", "decrease", "increase"],
)
@pytest.mark.asyncio
async def test_t10_one_non_advancing_heartbeat_aborts(
    tmp_path: Path, second: float, stalls: bool
) -> None:
    """T10: an equal or decreasing second heartbeat aborts at once; an increase continues."""
    values = [1.0, second, second + 1.0]

    def ticks(index: int) -> ColdTickObservation:
        if index >= len(values):
            return clean(index, heat_level_percent=1)
        return clean(session=session_metadata(elapsed_monotonic_seconds=values[index]))

    rig = make_rig(tmp_path, ticks=ticks)
    result = aborted((await run_phase(rig))[0])
    if stalls:
        assert result.aborts == engine_aborts(Reason.SESSION_CLOCK_STALLED)
        assert rig.mcp.calls.count("get_roast_state") == 2
    else:
        assert result.aborts == engine_aborts(Reason.COMMAND_STATE_NON_ZERO)
        assert rig.mcp.calls.count("get_roast_state") == 4


def test_t10_the_first_read_never_stalls(tmp_path: Path) -> None:
    """T10: with no previous heartbeat, even a zero clock does not stall."""
    record = tick_record(clean(session=session_metadata(elapsed_monotonic_seconds=0.0)), tmp_path)
    assert decide(record, previous=None) == ()
    assert decide(record, previous=0.0) == (Reason.SESSION_CLOCK_STALLED,)


# ------------------------------------------------------------ T7 / T8


class _MappingCaller:
    """Local transport returning per-tool payloads, or raising a transport error."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, tool: str, args: dict[str, object]) -> object:
        del args
        self.calls.append(tool)
        response = self.responses[tool]
        if isinstance(response, BaseException):
            raise response
        return response


def real_responses(identity: ColdRunIdentity, state: object) -> dict[str, object]:
    """Return the five-tool local responses for the real cold client."""
    return {
        "get_server_info": identity.server_info.model_dump(mode="json"),
        "get_runtime_config": identity.runtime_config.model_dump(mode="json"),
        "start_roast_session": {"session": cold_state_document()},
        "mark_beans_added": marked_document(),
        "get_roast_state": state,
    }


def _state(**changes: object) -> dict[str, typing.Any]:
    document = cold_state_document()
    for key, value in changes.items():
        if key == "drop_audio_field":
            del document["first_crack_status"][typing.cast(str, value)]
        elif key == "device_extra":
            document["device_state"]["extra"] = value
        else:
            document[key] = value
    return document


_REAL_CASES: list[tuple[str, object, type[BaseException], Reason]] = [
    ("foreign", _state(session_id="foreign"), Exception, Reason.MCP_SESSION_IDENTITY_CHANGED),
    ("purpose", _state(session_purpose="roast"), Exception, Reason.MCP_SESSION_IDENTITY_CHANGED),
    (
        "audio",
        _state(drop_audio_field="inference_overrun_count"),
        ColdTickAudioProjectionError,
        Reason.MCP_RESPONSE_NOT_ADMITTED,
    ),
    (
        "device",
        _state(device_extra=1),
        ColdTickDeviceProjectionError,
        Reason.MCP_RESPONSE_NOT_ADMITTED,
    ),
    (
        "roast-fan",
        _state(cold_characterisation_observation=None),
        ColdTickRoastFanProjectionError,
        Reason.MCP_RESPONSE_NOT_ADMITTED,
    ),
    (
        "session",
        _state(elapsed_monotonic_seconds=1),
        ColdTickSessionProjectionError,
        Reason.MCP_RESPONSE_NOT_ADMITTED,
    ),
    (
        "temperature",
        _state(cold_temperature_projection=None),
        ColdTickTemperatureProjectionError,
        Reason.MCP_RESPONSE_NOT_ADMITTED,
    ),
    ("transport", MCPConnectionError(CANARY), Exception, Reason.MCP_TRANSPORT_FAILED),
]


@pytest.mark.parametrize(
    ("name", "state", "projection", "reason"), _REAL_CASES, ids=[c[0] for c in _REAL_CASES]
)
@pytest.mark.asyncio
async def test_t7_real_client_failures_map_by_type_with_no_tick_and_no_retry(
    tmp_path: Path, name: str, state: object, projection: type[BaseException], reason: Reason
) -> None:
    """T7: each real-client refusal maps to its closed reason after exactly one read."""
    del name
    rig = make_rig(tmp_path)
    if projection is not Exception:
        probe = ColdCharacterisationMCPClient(_MappingCaller(real_responses(rig.identity, state)))
        await probe.start_cold_session()
        with pytest.raises(projection):
            await probe.get_roast_state()
    caller = _MappingCaller(real_responses(rig.identity, state))
    client = ColdCharacterisationMCPClient(caller)
    rig = rig._replace(mcp=typing.cast(FakeMcp, client))
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == engine_aborts(reason)
    assert caller.calls.count("get_roast_state") == 1
    assert streams(sink) == ["header", "abort"]
    # A retained engine abort is never followed by finalisation (D199).
    assert "finalise_cold_characterisation_session" not in caller.calls


@pytest.mark.asyncio
async def test_t7_a_forbidden_tool_error_is_unexpected(tmp_path: Path) -> None:
    """T7: a cold MCP error outside the three mapped classes is unexpected."""
    rig = make_rig(tmp_path, ticks=lambda index: ColdModeForbiddenToolError(CANARY))
    with pytest.raises(engine.ColdEngineUnexpectedError) as raised:
        await run_phase(rig)
    assert raised.value.abort_recorded is True
    assert rig.mcp.calls.count("get_roast_state") == 1
    assert_contained(raised.value, "Cold engine failed unexpectedly.")


class _HangingColdSession:
    """Local tool session answering every tool except a read, which never returns."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, typing.Any] | None = None) -> object:
        del arguments
        self.calls.append(name)
        if name == "get_roast_state":
            await asyncio.Event().wait()
        return types.SimpleNamespace(
            isError=False, structuredContent=self.responses[name], content=[]
        )


@pytest.mark.asyncio
async def test_t8_a_hung_read_times_out_into_a_transport_abort(tmp_path: Path) -> None:
    """T8: the real timeout-bounded transport turns a hung read into MCP_TRANSPORT_FAILED."""
    rig = make_rig(tmp_path)
    session = _HangingColdSession(real_responses(rig.identity, None))
    process = MCPServerProcess(MCPConfig(call_timeout_seconds=0.05), session=session)
    client = ColdCharacterisationMCPClient(process.call_tool)
    rig = rig._replace(mcp=typing.cast(FakeMcp, client))
    started = asyncio.get_running_loop().time()
    result, sink = await asyncio.wait_for(run_phase(rig), timeout=1.0)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert aborted(result).aborts == engine_aborts(Reason.MCP_TRANSPORT_FAILED)
    assert session.calls.count("get_roast_state") == 1
    assert streams(sink) == ["header", "abort"]


# ---------------------------------------------------------------- T12


@pytest.mark.asyncio
async def test_t12_an_oversized_session_id_is_refused_before_activation(tmp_path: Path) -> None:
    """T12: a 2,049-byte id never activates, and the exact returned string is carried."""
    oversized = "s" * (schema.MAX_TEXT_FIELD_BYTES + 1)
    rig = make_rig(tmp_path, start=start_result(oversized))
    result = aborted((await run_phase(rig))[0])
    assert result.aborts == engine_aborts(Reason.SESSION_START_FAILED)
    assert result.session_id == oversized
    assert "mark_beans_added" not in rig.mcp.calls


@pytest.mark.parametrize(
    "start",
    [
        types.SimpleNamespace(session=types.SimpleNamespace(session_id=12345)),
        types.SimpleNamespace(session=types.SimpleNamespace(session_id="")),
        types.SimpleNamespace(session=types.SimpleNamespace(session_id="\ud800")),
        ColdMcpValidationError(CANARY),
    ],
    ids=["non-string", "empty", "unencodable", "mcp-error"],
)
@pytest.mark.asyncio
async def test_t12_a_malformed_start_is_closed_and_never_activates(
    tmp_path: Path, start: object
) -> None:
    """T12: a non-string, empty, unencodable or refused start fails closed."""
    rig = make_rig(tmp_path, start=start)
    result = aborted((await run_phase(rig))[0])
    assert result.aborts == engine_aborts(Reason.SESSION_START_FAILED)
    expected = getattr(getattr(start, "session", None), "session_id", None)
    assert result.session_id == (expected if type(expected) is str else None)
    assert "mark_beans_added" not in rig.mcp.calls


@pytest.mark.asyncio
async def test_t12_the_boundary_session_id_is_admitted(tmp_path: Path) -> None:
    """T12: a 2,048-byte id activates."""
    boundary = "s" * schema.MAX_TEXT_FIELD_BYTES
    rig = make_rig(tmp_path, start=start_result(boundary))
    result = aborted((await run_phase(rig))[0])
    assert "mark_beans_added" in rig.mcp.calls
    assert result.aborts == engine_aborts(Reason.MCP_SESSION_IDENTITY_CHANGED)


@pytest.mark.asyncio
async def test_t12_an_oversized_driver_is_an_evidence_abort_with_no_tick(tmp_path: Path) -> None:
    """T12: the tick builder's refusal member is recorded in EVIDENCE; no tick line."""
    driver = "d" * (schema.MAX_TEXT_FIELD_BYTES + 1)
    rig = make_rig(tmp_path, ticks=lambda index: clean(driver=driver))
    admission = await admit(rig)
    with pytest.raises(schema.ColdEvidenceError) as direct:
        builders.build_tick_record(
            header=admission.header,
            tick=0,
            recorded_at_utc="2026-09-26T12:00:01+00:00",
            monotonic_seconds=T0,
            observation=clean(driver=driver),
        )
    sink = SpySink(engine.open_phase_evidence(admission))
    result = aborted(
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    )
    assert result.aborts == (
        engine.ColdAbortClassification(domain=Domain.EVIDENCE, reason=direct.value.failure),
    )
    assert streams(sink) == ["header", "abort"]


# ---------------------------------------------------------------- T13


_FAULTS = [
    "nan",
    "inf",
    "negative",
    "decrease",
    "int",
    "mono_raise",
    "utc_raise",
    "bad_utc",
    "offset",
    "oversize",
]
#: Sample indices: A0=0, P1=1, P2=2, P4=3, P5=4, second P2=5, first P3=6.
_POSITIONS = {"P1": 1, "P2": 2, "P4": 3, "P5": 4, "P3": 6}


@pytest.mark.parametrize("fault", _FAULTS)
@pytest.mark.parametrize("position", list(_POSITIONS))
@pytest.mark.asyncio
async def test_t13_an_invalid_sample_at_each_position_is_clock_invalid(
    tmp_path: Path, position: str, fault: str
) -> None:
    """T13: every sampled position admits its instant; failures use the last valid pair."""
    clock = FakeClock(faults={_POSITIONS[position]: fault})
    rig = make_rig(tmp_path, clock=clock)
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == engine_aborts(Reason.CLOCK_INVALID)
    (record,) = aborts_of(sink)
    assert (record.monotonic_seconds, record.recorded_at_utc) == clock.good[-1]
    expected_streams = {
        "P1": ["header", "abort"],
        "P2": ["header", "abort"],
        "P4": ["header", "abort"],
        "P5": ["header", "tick", "abort"],
        "P3": ["header", "tick", "host", "abort"],
    }[position]
    assert streams(sink) == expected_streams


@pytest.mark.asyncio
async def test_t13_a_raising_sleep_is_clock_invalid(tmp_path: Path) -> None:
    """T13: a raising clock sleep is CLOCK_INVALID, never unexpected."""
    clock = FakeClock(sleep_error=True)
    rig = make_rig(tmp_path, clock=clock)
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == engine_aborts(Reason.CLOCK_INVALID)
    (record,) = aborts_of(sink)
    assert (record.monotonic_seconds, record.recorded_at_utc) == clock.good[-1]


def test_t13_instant_admission_rules() -> None:
    """T13: the pure instant check enforces type, finiteness, order, size and UTC."""
    admissible = engine._instant_is_admissible  # pyright: ignore[reportPrivateUsage]
    instant = engine._Instant  # pyright: ignore[reportPrivateUsage]
    utc = "2026-09-26T12:00:00+00:00"
    assert admissible(1.0, utc, None)
    assert admissible(1.0, "2026-09-26T12:00:00Z", instant(1.0, utc))
    assert not admissible(0.5, utc, instant(1.0, utc))
    assert not admissible(1.0, "2026-09-26T12:00:00", None)
    assert not admissible(1.0, "", None)
    assert not admissible(1.0, 5, None)
    assert not admissible(True, utc, None)
    assert datetime.fromisoformat(OVERSIZED_UTC).utcoffset() == timedelta(0)
    assert len(OVERSIZED_UTC.encode("utf-8")) > schema.MAX_TEXT_FIELD_BYTES
    assert not admissible(1.0, OVERSIZED_UTC, None)


@pytest.mark.asyncio
async def test_t13_an_oversized_valid_utc_at_admission_is_clock_invalid(tmp_path: Path) -> None:
    """T13/A0: a parseable but oversized UTC header instant refuses admission."""
    rig = make_rig(tmp_path, clock=FakeClock(faults={0: "oversize"}))
    with pytest.raises(engine.ColdAdmissionRefusedError) as raised:
        await admit(rig)
    assert raised.value.failure is engine.ColdAdmissionFailure.CLOCK_INVALID
    assert root_entries(rig.root) == []


# ------------------------------------------------------------- T14 / T15


def _raiser(*_args: object, **_kwargs: object) -> typing.NoReturn:
    raise ValueError(CANARY)


@pytest.mark.parametrize("source", ["mcp", "tick-builder", "policy", "host-thread", "host-builder"])
@pytest.mark.asyncio
async def test_t14_unexpected_failures_record_then_raise_a_closed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """T14: each unexpected source records UNEXPECTED_FAILURE, then raises closed."""
    ticks: Callable[[int], object] = clean
    host = FakeHost()
    if source == "mcp":
        ticks = lambda index: RuntimeError(CANARY)  # noqa: E731
    elif source == "tick-builder":
        monkeypatch.setattr(engine, "build_tick_record", _raiser)
    elif source == "policy":
        monkeypatch.setattr(engine, "evaluate_tick", _raiser)
    elif source == "host-thread":
        host = FakeHost(sample_error=RuntimeError(CANARY))
    else:
        monkeypatch.setattr(engine, "build_host_record", _raiser)
    rig = make_rig(tmp_path, host=host, ticks=ticks)
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    with pytest.raises(engine.ColdEngineUnexpectedError) as raised:
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    assert raised.value.abort_recorded is True
    assert raised.value.session_id == SID
    assert_contained(raised.value, "Cold engine failed unexpectedly.")
    (record,) = aborts_of(sink)
    assert (record.domain, record.reason) == (Domain.ENGINE, Reason.UNEXPECTED_FAILURE)
    expected_ticks = 0 if source in {"mcp", "tick-builder"} else 1
    assert streams(sink).count("tick") == expected_ticks
    assert streams(sink).count("host") == 0


@pytest.mark.parametrize(
    ("fail_at", "error", "expected"),
    [
        (
            1,
            store.ColdEvidenceStoreError(store.ColdEvidenceStoreFailure.WRITE_FAILED),
            engine_aborts(Reason.UNEXPECTED_FAILURE),
        ),
        (
            1,
            schema.ColdEvidenceError(schema.ColdEvidenceFailure.RECORD_TOO_LARGE),
            (
                engine.ColdAbortClassification(
                    domain=Domain.EVIDENCE, reason=schema.ColdEvidenceFailure.RECORD_TOO_LARGE
                ),
            ),
        ),
        (
            2,
            store.ColdEvidenceStoreError(store.ColdEvidenceStoreFailure.WRITE_FAILED),
            engine_aborts(Reason.UNEXPECTED_FAILURE),
        ),
    ],
    ids=["a-tick-store", "b-tick-evidence", "a-host-store"],
)
@pytest.mark.asyncio
async def test_t15_typed_sink_failures_return_unrecorded_aborts(
    tmp_path: Path,
    fail_at: int,
    error: BaseException,
    expected: tuple[engine.ColdAbortClassification, ...],
) -> None:
    """T15a/b: a typed append failure stops all further appends."""
    rig = make_rig(tmp_path)
    result, sink = await run_phase(rig, fail_at=fail_at, error=error)
    assert aborted(result).aborts == expected
    assert aborted(result).abort_recorded is False
    assert sink.attempts == fail_at + 1


@pytest.mark.asyncio
async def test_t15c_an_unknown_sink_failure_raises_with_no_further_append(tmp_path: Path) -> None:
    """T15c: an unknown append exception raises closed with zero further appends."""
    rig = make_rig(tmp_path)
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission), fail_at=1, error=RuntimeError(CANARY))
    with pytest.raises(engine.ColdEngineUnexpectedError) as raised:
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    assert raised.value.abort_recorded is False
    assert_contained(raised.value, "Cold engine failed unexpectedly.")
    assert sink.attempts == 2


@pytest.mark.asyncio
async def test_t15d_a_failed_second_abort_keeps_both_and_does_not_recurse(tmp_path: Path) -> None:
    """T15d: recording stops at the first failure; classifications are unchanged."""
    rig = make_rig(tmp_path, ticks=lambda index: clean(heat_level_percent=1, connected=False))
    result, sink = await run_phase(rig, fail_at=3, error=RuntimeError(CANARY))
    assert aborted(result).aborts == engine_aborts(
        Reason.COMMAND_STATE_NON_ZERO, Reason.DEVICE_DISCONNECTED
    )
    assert aborted(result).abort_recorded is False
    assert sink.attempts == 4
    assert streams(sink) == ["header", "tick", "abort"]


@pytest.mark.asyncio
async def test_t15d_a_failed_first_abort_stops_with_both_classifications(tmp_path: Path) -> None:
    """T15d/A2: the first of two abort appends failing stops recording at once."""
    rig = make_rig(tmp_path, ticks=lambda index: clean(heat_level_percent=1, connected=False))
    result, sink = await run_phase(rig, fail_at=2, error=RuntimeError(CANARY))
    assert aborted(result).aborts == engine_aborts(
        Reason.COMMAND_STATE_NON_ZERO, Reason.DEVICE_DISCONNECTED
    )
    assert aborted(result).abort_recorded is False
    assert sink.attempts == 3
    assert streams(sink) == ["header", "tick"]


@pytest.mark.asyncio
async def test_t15_successful_multi_abort_persists_in_declaration_order(tmp_path: Path) -> None:
    """T6/T15, A2: every abort record is retained, in reason declaration order."""
    rig = make_rig(tmp_path, ticks=lambda index: clean(heat_level_percent=1, connected=False))
    result, sink = await run_phase(rig)
    assert aborted(result).abort_recorded is True
    assert [(r.domain, r.reason) for r in aborts_of(sink)] == [
        (Domain.ENGINE, Reason.COMMAND_STATE_NON_ZERO),
        (Domain.ENGINE, Reason.DEVICE_DISCONNECTED),
    ]
    assert streams(sink) == ["header", "tick", "abort", "abort"]


@pytest.mark.asyncio
async def test_t15d_an_abort_builder_failure_stops_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T15d: a builder failure while recording returns False with no append attempt."""
    rig = make_rig(tmp_path, ticks=lambda index: clean(heat_level_percent=1))
    monkeypatch.setattr(engine, "build_abort_record", _raiser)
    result, sink = await run_phase(rig)
    assert aborted(result).abort_recorded is False
    assert sink.attempts == 2


@pytest.mark.asyncio
async def test_t15e_failure_recording_unexpected_still_raises_unrecorded(tmp_path: Path) -> None:
    """T15e: a failed UNEXPECTED_FAILURE record still raises the closed error."""
    rig = make_rig(tmp_path, ticks=lambda index: RuntimeError(CANARY))
    with pytest.raises(engine.ColdEngineUnexpectedError) as raised:
        await run_phase(rig, fail_at=1, error=RuntimeError(CANARY))
    assert raised.value.abort_recorded is False


# ---------------------------------------------------------------- T16


@pytest.mark.asyncio
async def test_t16_host_bound_failures_are_host_domain_aborts(tmp_path: Path) -> None:
    """T16: a host bound failure maps to its HOST reason by exact value."""
    host = FakeHost(sample_error=ColdHostBoundError(ColdHostBoundFailure.THERMAL_EXCEEDED))
    rig = make_rig(tmp_path, host=host)
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == (
        engine.ColdAbortClassification(
            domain=Domain.HOST, reason=schema.ColdHostAbortReason.THERMAL_EXCEEDED
        ),
    )
    assert streams(sink) == ["header", "tick", "abort"]
    assert {m.value for m in ColdHostBoundFailure} == {m.value for m in schema.ColdHostAbortReason}


@pytest.mark.asyncio
async def test_t16_a_host_record_builder_refusal_is_an_evidence_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16: a typed host-record builder refusal is recorded in EVIDENCE."""

    def refuse(**_kwargs: object) -> schema.ColdHostRecord:
        raise schema.ColdEvidenceError(schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED)

    monkeypatch.setattr(engine, "build_host_record", refuse)
    result, sink = await run_phase(make_rig(tmp_path))
    assert aborted(result).aborts == (
        engine.ColdAbortClassification(
            domain=Domain.EVIDENCE, reason=schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
        ),
    )
    assert streams(sink) == ["header", "tick", "abort"]


@pytest.mark.asyncio
async def test_activation_failure_is_a_closed_engine_abort(tmp_path: Path) -> None:
    """AC-C1: a refused activation aborts with ACTIVATION_FAILED and no read."""
    rig = make_rig(tmp_path, mark_error=ColdMcpValidationError(CANARY))
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == engine_aborts(Reason.ACTIVATION_FAILED)
    assert "get_roast_state" not in rig.mcp.calls
    assert streams(sink) == ["header", "abort"]


# ---------------------------------------------------------------- T17


async def _cancel_observer(task: asyncio.Task[typing.Any]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_t17a_cancellation_during_sleep_records_cancelled(tmp_path: Path) -> None:
    """T17a: cancelling during sleep records CANCELLED once and makes no further call."""
    clock = FakeClock(sleep_gate=asyncio.Event())
    rig = make_rig(tmp_path, clock=clock)
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    task = asyncio.ensure_future(
        engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=clock
        )
    )
    await clock.sleep_entered.wait()
    calls = list(rig.mcp.calls)
    await _cancel_observer(task)
    assert rig.mcp.calls == calls
    assert [(r.domain, r.reason) for r in aborts_of(sink)] == [(Domain.ENGINE, Reason.CANCELLED)]


@pytest.mark.asyncio
async def test_t17b_cancellation_during_a_read_records_cancelled(tmp_path: Path) -> None:
    """T17b: cancelling during a read records CANCELLED and writes no tick."""
    rig = make_rig(tmp_path, read_gate=asyncio.Event())
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    task = asyncio.ensure_future(
        engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    )
    await rig.mcp.read_entered.wait()
    await _cancel_observer(task)
    assert rig.mcp.calls.count("get_roast_state") == 1
    assert streams(sink) == ["header", "abort"]
    assert [(r.domain, r.reason) for r in aborts_of(sink)] == [(Domain.ENGINE, Reason.CANCELLED)]


@pytest.mark.asyncio
async def test_t17c_host_thread_is_reaped_once_before_cancelled_is_recorded(
    tmp_path: Path,
) -> None:
    """T17c: two cancellations wait for the host thread; CANCELLED follows its completion."""
    gate = threading.Event()
    host = FakeHost(gate=gate)
    rig = make_rig(tmp_path, host=host)
    admission = await admit(rig)
    completion_at_abort: list[bool] = []

    def watch(record: schema.ColdEvidenceRecord) -> None:
        if type(record) is schema.ColdAbortRecord:
            completion_at_abort.append(host.completed.is_set())

    sink = SpySink(engine.open_phase_evidence(admission), on_append=watch)
    task = asyncio.ensure_future(
        engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=host, clock=rig.clock
        )
    )
    await asyncio.to_thread(host.entered.wait, 5.0)
    calls = list(rig.mcp.calls)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.sample_calls == 1
    assert completion_at_abort == [True]
    assert rig.mcp.calls == calls
    assert streams(sink) == ["header", "tick", "abort"]


@pytest.mark.asyncio
async def test_t17d_cancellation_during_admission_host_check_creates_nothing(
    tmp_path: Path,
) -> None:
    """T17d: admission reaps its host check, creates nothing and re-raises."""
    gate = threading.Event()
    host = FakeHost(gate=gate, gate_on="check_start_bounds")
    rig = make_rig(tmp_path, host=host)
    task = asyncio.ensure_future(admit(rig))
    await asyncio.to_thread(host.entered.wait, 5.0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.completed.is_set() and host.start_calls == 1
    assert root_entries(rig.root) == []
    assert "start_roast_session" not in rig.mcp.calls


def _thread_task(outer: asyncio.Task[typing.Any]) -> asyncio.Task[typing.Any]:
    current = asyncio.current_task()
    for task in asyncio.all_tasks():
        name = getattr(task.get_coro(), "__qualname__", "")
        if task is not outer and task is not current and name == "to_thread":
            return task
    raise AssertionError("owned thread task not found")


@pytest.mark.parametrize("early_cancels", [0, 1, 2])
@pytest.mark.parametrize("fails", [False, True], ids=["success", "failing-host"])
@pytest.mark.asyncio
async def test_owned_thread_cancellation_winning_a_completion_race(
    fails: bool, early_cancels: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead finding 2: cancellation delivered after completion still wins, observed once.

    The ``_discard`` spy pins that the cancellation path itself observes a
    completed outcome.  CPython's ``shield`` callback also reads the inner
    outcome, so the diagnostic assertion alone would not detect its removal.
    """
    loop = asyncio.get_running_loop()
    diagnostics: list[dict[str, typing.Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: diagnostics.append(context))
    gate = threading.Event()
    calls: list[Path] = []
    observed: list[tuple[bool, bool]] = []
    real_discard = engine._discard  # pyright: ignore[reportPrivateUsage]

    def spy(future: "asyncio.Future[typing.Any]") -> None:
        observed.append((future.done(), future.cancelled()))
        real_discard(future)

    monkeypatch.setattr(engine, "_discard", spy)

    def work(path: Path) -> int:
        calls.append(path)
        gate.wait(5.0)
        if fails:
            raise RuntimeError(CANARY)
        return 7

    try:
        outer = asyncio.ensure_future(
            engine._owned_thread(work, Path("/evidence"))  # pyright: ignore[reportPrivateUsage]
        )
        await asyncio.sleep(0)
        inner = _thread_task(outer)
        for _ in range(early_cancels):
            outer.cancel()
            await asyncio.sleep(0)
        inner.add_done_callback(lambda _task: outer.cancel())
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert outer.cancelled()
        assert inner.done()
        assert calls == [Path("/evidence")]
        assert (True, False) in observed
        gc.collect()
        await asyncio.sleep(0)
        assert diagnostics == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.parametrize("fails", [False, True], ids=["success", "failing-host"])
@pytest.mark.asyncio
async def test_owned_thread_drain_observes_a_completion_after_cancellation(fails: bool) -> None:
    """Cancelled before completion, the drain awaits the thread once and still cancels."""
    gate = threading.Event()
    calls: list[Path] = []

    def work(path: Path) -> int:
        calls.append(path)
        gate.wait(5.0)
        if fails:
            raise RuntimeError(CANARY)
        return 7

    outer = asyncio.ensure_future(
        engine._owned_thread(work, Path("/evidence"))  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.sleep(0)
    outer.cancel()
    await asyncio.sleep(0)
    assert not outer.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert calls == [Path("/evidence")]


@pytest.mark.asyncio
async def test_record_aborts_never_appends_after_the_sink_is_unusable(tmp_path: Path) -> None:
    """Sink rule: once unusable, recording returns False with no append attempt."""
    rig = make_rig(tmp_path)
    admission = await admit(rig)
    admitted = engine._validated_admission(admission)  # pyright: ignore[reportPrivateUsage]
    assert admitted is not None
    sink = SpySink(engine.open_phase_evidence(admission))
    run = engine._PhaseRun(admitted, sink, rig.mcp, rig.host, rig.clock)  # pyright: ignore[reportPrivateUsage]
    run._sink_usable = False  # pyright: ignore[reportPrivateUsage]
    assert run._record_aborts(engine_aborts(Reason.CANCELLED)) is False  # pyright: ignore[reportPrivateUsage]
    assert sink.attempts == 0


@pytest.mark.asyncio
async def test_owned_thread_returns_the_result_and_propagates_errors() -> None:
    """The ordinary owned-thread path returns the host result or its exception."""
    owned = engine._owned_thread  # pyright: ignore[reportPrivateUsage]
    assert await owned(lambda path: path.name, Path("/evidence")) == "evidence"
    with pytest.raises(ValueError, match=CANARY):
        await owned(_raiser, Path("/evidence"))


# ---------------------------------------------------------------- T18


def test_t18_classification_pairs_are_closed() -> None:
    """T18: exactly HOST, EVIDENCE and ENGINE pairs, parity with the reader table."""
    admitted = {
        Domain.HOST: schema.ColdHostAbortReason.THERMAL_EXCEEDED,
        Domain.EVIDENCE: schema.ColdEvidenceFailure.RECORD_TOO_LARGE,
        Domain.ENGINE: Reason.CANCELLED,
    }
    for domain, reason in admitted.items():
        assert type(reason) is ABORT_REASON_BY_DOMAIN[domain]
        assert engine.ColdAbortClassification(domain=domain, reason=reason).reason is reason
    refused: list[tuple[object, object]] = [
        (Domain.ENGINE, schema.ColdHostAbortReason.THERMAL_EXCEEDED),
        (Domain.HOST, Reason.CANCELLED),
        (Domain.MCP, Reason.CANCELLED),
        (Domain.MCP, schema.ColdMcpAbortReason.EMERGENCY_STOP),
        (Domain.ENGINE, "cancelled"),
        ("engine", Reason.CANCELLED),
    ]
    for domain, reason in refused:
        with pytest.raises(pydantic.ValidationError):
            engine.ColdAbortClassification.model_validate({"domain": domain, "reason": reason})


def test_t18_results_are_strict_frozen_and_bounded() -> None:
    """T18: completed and aborted refuse invalid values and extra keys, and are frozen."""
    ok = {
        "session_id": SID,
        "observation_end_monotonic": 1.0,
        "observation_end_utc": "2026-09-26T12:00:00+00:00",
        "tick_count": 0,
    }
    engine.ColdPhaseCompleted.model_validate(ok)
    for bad in (
        {"observation_end_monotonic": -1.0},
        {"observation_end_monotonic": math.nan},
        {"tick_count": -1},
        {"session_id": ""},
        {"extra": 1},
    ):
        with pytest.raises(pydantic.ValidationError):
            engine.ColdPhaseCompleted.model_validate({**ok, **bad})
    with pytest.raises(pydantic.ValidationError):
        engine.ColdPhaseAborted(aborts=(), session_id=None, abort_recorded=False)
    result = engine.ColdPhaseAborted(
        aborts=engine_aborts(Reason.CANCELLED), session_id=None, abort_recorded=True
    )
    with pytest.raises(pydantic.ValidationError):
        result.abort_recorded = False  # type: ignore[misc]
    decision = policy.ColdTickDecision(reasons=(), next_previous_elapsed_seconds=1.0)
    with pytest.raises(pydantic.ValidationError):
        policy.ColdTickDecision.model_validate(
            {"reasons": ("cancelled",), "next_previous_elapsed_seconds": 1.0}
        )
    with pytest.raises(pydantic.ValidationError):
        policy.ColdTickDecision.model_validate(
            {"reasons": (), "next_previous_elapsed_seconds": math.inf}
        )
    with pytest.raises(pydantic.ValidationError):
        decision.reasons = (Reason.CANCELLED,)  # type: ignore[misc]
    base = set(dir(pydantic.BaseModel))
    for model in (
        engine.ColdPhaseCompleted,
        engine.ColdPhaseAborted,
        engine.ColdAbortClassification,
        policy.ColdTickDecision,
    ):
        names = (set(dir(model)) - base) | set(model.model_fields)
        assert not [n for n in names if any(w in n.lower() for w in ("verdict", "qualif", "pass"))]


def test_t18_closed_errors_hide_their_attributes() -> None:
    """T18: the unexpected error carries no attribute in args, str or repr."""
    error = engine.ColdEngineUnexpectedError(abort_recorded=True, session_id=CANARY)
    assert error.args == ("Cold engine failed unexpectedly.",)
    assert CANARY not in str(error) and CANARY not in repr(error)
    assert error.session_id == CANARY


# ---------------------------------------------------------------- T19


def _ports(
    client: ColdCharacterisationMCPClient,
    writer: store.ColdEvidenceWriter,
    reader: LinuxHostBoundsReader,
    clock: engine.MonotonicEngineClock,
) -> tuple[
    engine.ColdEngineMcp, engine.ColdEngineSink, engine.ColdEngineHost, engine.ColdEngineClock
]:
    """Pyright-checked structural conformance of the production adapters."""
    return client, writer, reader, clock


def _fake_ports(
    mcp: FakeMcp, sink: SpySink, host: FakeHost, clock: FakeClock
) -> tuple[
    engine.ColdEngineMcp, engine.ColdEngineSink, engine.ColdEngineHost, engine.ColdEngineClock
]:
    """Pyright-checked structural conformance of the fakes."""
    return mcp, sink, host, clock


@pytest.mark.asyncio
async def test_t19_ports_are_exact_and_the_production_clock_is_admissible() -> None:
    """T19: the MCP port declares exactly five operations; the real clock admits."""
    assert _ports is not None and _fake_ports is not None
    declared = {
        name
        for name, value in vars(engine.ColdEngineMcp).items()
        if not name.startswith("_") and callable(value)
    }
    assert declared == {
        "get_server_info",
        "get_runtime_config",
        "start_cold_session",
        "mark_beans_added",
        "get_roast_state",
    }
    assert "finalise_session" not in dir(engine.ColdEngineMcp)
    clock = engine.MonotonicEngineClock()
    first = (clock.monotonic(), clock.utc_now_iso())
    await clock.sleep(0.0)
    admissible = engine._instant_is_admissible  # pyright: ignore[reportPrivateUsage]
    assert admissible(first[0], first[1], None)
    assert admissible(
        clock.monotonic(),
        clock.utc_now_iso(),
        engine._Instant(*first),  # pyright: ignore[reportPrivateUsage]
    )


# ---------------------------------------------------------------- T20


_PACKAGE = Path(engine.__file__).parent
_BANNED_ATTRIBUTES = frozenset(
    {
        "state",
        "device_state",
        "first_crack_status",
        "finalise_session",
        "seal",
        "close",
        "set_heat",
        "set_fan",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "mark_first_crack",
        "set_targets",
        "call_tool",
    }
)


def test_t20_new_modules_are_fenced() -> None:
    """T20: no banned attribute, one ``open_run`` call, and a closed policy import set."""
    trees = {
        name: ast.parse((_PACKAGE / name).read_text(encoding="utf-8"))
        for name in ("engine.py", "engine_policy.py")
    }
    for name, tree in trees.items():
        banned = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTRIBUTES
        ]
        assert banned == [], name
    open_calls = [
        (function.name, node)
        for function in ast.walk(trees["engine.py"])
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open_run"
    ]
    assert [name for name, _node in open_calls] == ["open_phase_evidence"]
    all_calls = [
        node
        for node in ast.walk(trees["engine.py"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open_run"
    ]
    assert len(all_calls) == 1
    imported: set[str] = set()
    for node in ast.walk(trees["engine_policy.py"]):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {
        "enum",
        "typing",
        "pydantic",
        "roastpilot_agent.cold_characterisation.evidence_schema",
        "roastpilot_agent.cold_characterisation.duration_policy",
    }
    assert imported <= allowed
    engine_mcp_client_imports = [
        sorted(alias.name for alias in node.names)
        for node in ast.walk(trees["engine.py"])
        if isinstance(node, ast.ImportFrom) and node.module == "roastpilot_agent.mcp_client"
    ]
    assert engine_mcp_client_imports == [
        ["EventCommandResult", "RuntimeConfigSnapshot", "ServerInfo", "StartRoastSessionResult"]
    ]
    policy_names = [
        node.attr for node in ast.walk(trees["engine_policy.py"]) if isinstance(node, ast.Attribute)
    ]
    engine_names = [
        node.attr for node in ast.walk(trees["engine.py"]) if isinstance(node, ast.Attribute)
    ]
    assert "elapsed_monotonic_seconds" not in engine_names
    assert "next_previous_elapsed_seconds" in engine_names
    assert policy_names.count("elapsed_monotonic_seconds") == 1


def test_fixed_constants_are_never_shortened() -> None:
    """AC-C1/C3: the interval, startup deadline and window are fixed."""
    assert policy.COLD_OBSERVATION_INTERVAL_SECONDS == 1.0
    assert policy.COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS == 60.0
    assert policy.COLD_PHASE_OBSERVATION_SECONDS == 600.0
    assert issubclass(engine.ColdAdmissionFailure, enum.Enum)
    assert not issubclass(engine.ColdAdmissionFailure, str)


# ------------------------------------------------- 4g-c: hook, floor, completion UTC


class RecordingHook:
    """Activation hook spy recording its arguments and the rig state when called."""

    def __init__(self, rig: Rig, verdict: object = True, error: BaseException | None = None):
        self.rig = rig
        self.verdict = verdict
        self.error = error
        self.calls: list[tuple[str, float, str]] = []
        self.samples_at_call: list[int] = []
        self.mcp_calls_at_call: list[list[str]] = []

    def __call__(self, *, session_id: str, activated_monotonic: float, activated_utc: str) -> bool:
        self.calls.append((session_id, activated_monotonic, activated_utc))
        self.samples_at_call.append(self.rig.clock.samples)
        self.mcp_calls_at_call.append(list(self.rig.mcp.calls))
        if self.error is not None:
            raise self.error
        return typing.cast(bool, self.verdict)


async def run_hooked(
    rig: Rig, hook: engine.ColdActivationHook
) -> tuple[
    engine.ColdPhaseCompleted | engine.ColdPhaseAborted | engine.ColdPhaseActivationRefused, SpySink
]:
    """Admit, open, and observe one phase with an activation hook."""
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    result = await engine.observe_cold_phase(
        admission=admission,
        sink=sink,
        mcp=rig.mcp,
        host=rig.host,
        clock=rig.clock,
        activation_hook=hook,
    )
    return result, sink


@pytest.mark.asyncio
async def test_4gc_t5_hook_runs_once_after_activation_and_before_any_read(
    tmp_path: Path,
) -> None:
    """T5: one call with the admitted P1 pair, after mark_beans_added, before any read."""
    rig = make_rig(tmp_path)
    hook = RecordingHook(rig)
    result, sink = await run_hooked(rig, hook)
    done = completed(result)
    activation_utc = (BASE_UTC + timedelta(seconds=T0)).isoformat()
    assert hook.calls == [(SID, T0, activation_utc)]
    # Sample 0 is the admission header, sample 1 is P1; no loop sample yet.
    assert hook.samples_at_call == [2]
    assert hook.mcp_calls_at_call == [
        ["get_server_info", "get_runtime_config", "start_roast_session", "mark_beans_added"]
    ]
    assert done.tick_count == 600
    assert done.observation_end_monotonic == T0 + 600.0
    assert done.observation_end_utc == (BASE_UTC + timedelta(seconds=T0 + 600.0)).isoformat()
    assert done.observation_end_utc == rig.clock.good[-1][1]
    assert streams(sink) == ["header", *["tick", "host"] * 600]


@pytest.mark.asyncio
async def test_4gc_t5_false_refuses_with_no_read_and_no_abort_record(tmp_path: Path) -> None:
    """T5: exactly ``False`` returns the refusal, retains only the header, reads nothing."""
    rig = make_rig(tmp_path)
    hook = RecordingHook(rig, verdict=False)
    result, sink = await run_hooked(rig, hook)
    assert result == engine.ColdPhaseActivationRefused(session_id=SID)
    assert type(result) is engine.ColdPhaseActivationRefused
    assert len(hook.calls) == 1
    assert "get_roast_state" not in rig.mcp.calls
    assert streams(sink) == ["header"]
    assert aborts_of(sink) == []
    assert rig.host.sample_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [1, 0, None, "True", 1.0], ids=repr)
async def test_4gc_t5_a_non_bool_verdict_is_unexpected(tmp_path: Path, verdict: object) -> None:
    """T5: a non-``bool`` verdict records UNEXPECTED_FAILURE and raises the closed error."""
    rig = make_rig(tmp_path)
    hook = RecordingHook(rig, verdict=verdict)
    with pytest.raises(engine.ColdEngineUnexpectedError) as caught:
        await run_hooked(rig, hook)
    assert caught.value.abort_recorded is True
    assert caught.value.session_id == SID
    assert "get_roast_state" not in rig.mcp.calls
    assert len(hook.calls) == 1


@pytest.mark.asyncio
async def test_4gc_t5_a_raising_hook_is_unexpected_and_contained(tmp_path: Path) -> None:
    """T5: a raising hook records UNEXPECTED_FAILURE; nothing of its text escapes."""
    rig = make_rig(tmp_path)
    hook = RecordingHook(rig, error=RuntimeError(CANARY))
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission))
    with pytest.raises(engine.ColdEngineUnexpectedError) as caught:
        await engine.observe_cold_phase(
            admission=admission,
            sink=sink,
            mcp=rig.mcp,
            host=rig.host,
            clock=rig.clock,
            activation_hook=hook,
        )
    assert caught.value.abort_recorded is True
    assert [record.reason for record in aborts_of(sink)] == [Reason.UNEXPECTED_FAILURE]
    assert "get_roast_state" not in rig.mcp.calls
    assert CANARY not in str(caught.value) and CANARY not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["start", "activation"])
async def test_4gc_t5_hook_is_never_called_after_a_start_or_activation_failure(
    tmp_path: Path, failure: str
) -> None:
    """T5: a failed start or activation never reaches the hook."""
    rig = (
        make_rig(tmp_path, start=ColdMcpValidationError(CANARY))
        if failure == "start"
        else make_rig(tmp_path, mark_error=ColdMcpValidationError(CANARY))
    )
    hook = RecordingHook(rig)
    result, _sink = await run_hooked(rig, hook)
    expected = Reason.SESSION_START_FAILED if failure == "start" else Reason.ACTIVATION_FAILED
    assert aborted(result).aborts == engine_aborts(expected)
    assert hook.calls == []


@pytest.mark.asyncio
async def test_4gc_t5_a_hook_activation_clock_failure_never_reaches_the_hook(
    tmp_path: Path,
) -> None:
    """T5: an inadmissible P1 sample is CLOCK_INVALID before the hook."""
    rig = make_rig(tmp_path, clock=FakeClock(faults={1: "nan"}))
    hook = RecordingHook(rig)
    result, _sink = await run_hooked(rig, hook)
    assert aborted(result).aborts == engine_aborts(Reason.CLOCK_INVALID)
    assert hook.calls == []


async def admit_with_floor(rig: Rig, floor: object) -> engine.ColdPhaseAdmission:
    """Admit the rig's phase with an explicit admission floor."""
    return await engine.admit_cold_phase(
        identity=rig.identity,
        phase=rig.phase,
        root=rig.admitted,
        mcp=rig.mcp,
        host=rig.host,
        clock=rig.clock,
        not_before_monotonic=typing.cast(float, floor),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("floor", [T0, 0.0, 50.0], ids=repr)
async def test_4gc_t6_a_floor_at_or_below_the_admission_instant_is_admitted(
    tmp_path: Path, floor: float
) -> None:
    """T6: equality is admitted; the header instant is the sampled A0."""
    rig = make_rig(tmp_path)
    admission = await admit_with_floor(rig, floor)
    assert admission.header.monotonic_seconds == T0
    assert rig.host.start_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "floor",
    [math.nextafter(T0, math.inf), T0 + 60.0, math.nan, math.inf, -1.0, 100, True, "100.0"],
    ids=repr,
)
async def test_4gc_t6_a_floor_above_a0_or_inadmissible_is_clock_invalid(
    tmp_path: Path, floor: object
) -> None:
    """T6: A0 below the floor, or a non-exact or non-finite floor, refuses creating nothing."""
    rig = make_rig(tmp_path)
    with pytest.raises(engine.ColdAdmissionRefusedError) as caught:
        await admit_with_floor(rig, floor)
    assert caught.value.failure is engine.ColdAdmissionFailure.CLOCK_INVALID
    assert rig.host.start_calls == 0
    assert root_entries(rig.root) == []


@pytest.mark.asyncio
async def test_4gc_t6_floor_none_is_unchanged(tmp_path: Path) -> None:
    """T6: an explicit ``None`` floor admits exactly as the default does."""
    rig = make_rig(tmp_path)
    admission = await admit_with_floor(rig, None)
    assert admission.header.monotonic_seconds == T0


def test_4gc_activation_refused_result_is_strict_and_bounded() -> None:
    """The refusal result holds only a bounded session and is frozen."""
    assert set(engine.ColdPhaseActivationRefused.model_fields) == {"session_id"}
    for bad in ("", "x" * 2049, 1, None):
        with pytest.raises(pydantic.ValidationError):
            engine.ColdPhaseActivationRefused.model_validate({"session_id": bad})
    with pytest.raises(pydantic.ValidationError):
        engine.ColdPhaseActivationRefused.model_validate({"session_id": SID, "extra": 1})
    for bad_utc in ("", "x" * 2049):
        with pytest.raises(pydantic.ValidationError):
            engine.ColdPhaseCompleted.model_validate(
                {
                    "session_id": SID,
                    "observation_end_monotonic": 1.0,
                    "observation_end_utc": bad_utc,
                    "tick_count": 0,
                }
            )


# ------------------------------------------- #997 T2: D209 runtime activation (EN1-EN10)

Screen = ColdTemperatureScreenReason


def screened_rig(
    tmp_path: Path,
    temperatures: dict[int, dict[str, object]] | None = None,
    *,
    durations: dict[int, float] | None = None,
    late: float = 600.0,
    heat: dict[int, int] | None = None,
    clock: FakeClock | None = None,
) -> Rig:
    """A rig whose tick ``i`` carries ``screened_temperature(i, **temperatures[i])``.

    ``durations`` maps read indices to read durations (default 0.05); any later read
    lasts ``late`` seconds, so a phase that survives its scripted ticks ends at once.
    """
    changes = temperatures or {}
    lengths = durations or {}
    heats = heat or {}
    last = max(lengths, default=-1)
    return make_rig(
        tmp_path,
        clock=clock,
        durations=lambda index: lengths.get(index, 0.05 if index <= last else late),
        ticks=lambda index: clean(
            index,
            heat_level_percent=heats.get(index, 0),
            temperature=screened_temperature(index, **changes.get(index, {})),
        ),
    )


#: Read durations that end tick 0 before and tick 1 after the 60-second boundary
#: (activation 100.0; tick 0 ends at 100.05, tick 1 starts at 101.0, ends at 161.0).
ELIGIBLE_SECOND = {0: 0.05, 1: 60.0}


def screen_aborts(*reasons: Screen) -> tuple[engine.ColdAbortClassification, ...]:
    """Return ENGINE classifications carrying temperature screen reasons, in order."""
    return tuple(engine.ColdAbortClassification(domain=Domain.ENGINE, reason=r) for r in reasons)


@pytest.mark.asyncio
async def test_en1_every_tick_retains_one_paired_temperature_from_its_own_read(
    tmp_path: Path,
) -> None:
    """EN1 (AC1-AC5, O1): one read per tick; both records come from that read and instant."""
    returned: list[ColdTickObservation] = []

    def ticks(index: int) -> ColdTickObservation:
        item = clean(index)
        returned.append(item)
        return item

    rig = make_rig(tmp_path, ticks=ticks)
    result, sink = await run_phase(rig)
    count = completed(result).tick_count
    assert count == 600 == rig.mcp.calls.count("get_roast_state") == len(returned)
    ticks_retained = [r for r in sink.records if type(r) is schema.ColdTickRecord]
    assert len(ticks_retained) == len(sink.temperatures) == count
    assert sink.temperature_aborts == []
    for tick, temperature, read in zip(ticks_retained, sink.temperatures, returned, strict=True):
        assert pairs_with(tick, temperature)
        assert (
            temperature.run_id,
            temperature.phase,
            temperature.identity_sha256,
            temperature.tick,
            temperature.recorded_at_utc,
            temperature.monotonic_seconds,
        ) == (
            tick.run_id,
            tick.phase,
            tick.identity_sha256,
            tick.tick,
            tick.recorded_at_utc,
            tick.monotonic_seconds,
        )
        assert temperature.temperature == read.temperature
        assert temperature.temperature is not read.temperature
    # Each tick's temperature line directly follows its tick line.
    assert sink.order[:5] == ["header", "tick", "tick_temperature", "host", "tick"]


@pytest.mark.asyncio
async def test_en2_a_newly_counted_read_error_at_the_first_eligible_tick_aborts(
    tmp_path: Path,
) -> None:
    """EN2: pre-boundary tick 0, then a read error +1 at tick 1 is one schema-5 abort."""
    rig = screened_rig(tmp_path, {1: {"status_read_error_count": 1}}, durations=ELIGIBLE_SECOND)
    result, sink = await run_phase(rig)
    aborted_result = aborted(result)
    assert aborted_result.aborts == screen_aborts(Screen.FAULT_COUNTED)
    assert aborted_result.abort_recorded is True
    assert [(r.tick, r.reason, r.domain) for r in sink.temperature_aborts] == [
        (1, Screen.FAULT_COUNTED, Domain.ENGINE)
    ]
    assert aborts_of(sink) == []
    assert sink.order[-3:] == ["tick", "tick_temperature", "temperature_abort"]


@pytest.mark.asyncio
async def test_en3_a_first_read_ending_at_the_boundary_has_no_prior(tmp_path: Path) -> None:
    """EN3: a first observation at or after 60 s has no previous snapshot."""
    rig = screened_rig(tmp_path, durations={0: 60.0})
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == screen_aborts(Screen.PRIOR_MISSING)
    assert [(r.tick, r.reason) for r in sink.temperature_aborts] == [(0, Screen.PRIOR_MISSING)]


_MALFORMED = dict.fromkeys(
    (
        "configured_temperature_unit",
        "reported_temperature_unit",
        "last_packet_valid",
        "last_packet_bean_temp_c",
        "last_packet_env_temp_c",
        "retained_bean_temp_c",
        "retained_env_temp_c",
        "value_agreement",
        "status_packet_count",
        "ignored_temperature_packet_count",
        "status_read_error_count",
        "command_loop_error_count",
    )
)

#: (id, per-tick raw overrides, durations, expected reasons, abort tick).
EN4_CASES: list[
    tuple[str, dict[int, dict[str, object]], dict[int, float], tuple[Screen, ...], int]
] = [
    (
        "bean_below_screen",
        {1: {"last_packet_bean_temp_c": 4.0, "retained_bean_temp_c": 4.0}},
        ELIGIBLE_SECOND,
        (Screen.OUTSIDE_SCREEN,),
        1,
    ),
    (
        "env_above_screen",
        {1: {"last_packet_env_temp_c": 41.0, "retained_env_temp_c": 41.0}},
        ELIGIBLE_SECOND,
        (Screen.OUTSIDE_SCREEN,),
        1,
    ),
    (
        "values_disagree",
        {1: {"retained_bean_temp_c": 21.0, "value_agreement": "disagree"}},
        ELIGIBLE_SECOND,
        (Screen.VALUES_DISAGREE,),
        1,
    ),
    (
        "non_celsius_reported_unit",
        {
            1: {
                "configured_temperature_unit": "auto",
                "reported_temperature_unit": "fahrenheit",
                "last_packet_bean_temp_c": None,
                "last_packet_env_temp_c": None,
                "value_agreement": "indeterminate",
            }
        },
        ELIGIBLE_SECOND,
        (Screen.LAST_PACKET_NOT_VALID_CELSIUS,),
        1,
    ),
    (
        "packet_not_progressed",
        {1: {"status_packet_count": 1}},
        ELIGIBLE_SECOND,
        (Screen.PACKET_NOT_PROGRESSED,),
        1,
    ),
    (
        "loop_error_counted",
        {1: {"command_loop_error_count": 1}},
        ELIGIBLE_SECOND,
        (Screen.FAULT_COUNTED,),
        1,
    ),
    (
        "ignored_packet_counted",
        {1: {"status_packet_count": 3, "ignored_temperature_packet_count": 1}},
        ELIGIBLE_SECOND,
        (Screen.FAULT_COUNTED,),
        1,
    ),
    (
        "malformed_at_tick_zero",
        {0: {"outcome": "malformed", **_MALFORMED}},
        {0: 0.05},
        (Screen.PROJECTION_MALFORMED,),
        0,
    ),
    (
        "unsupported_at_tick_zero",
        {0: {"outcome": "unsupported", **_MALFORMED}},
        {0: 0.05},
        (Screen.NOT_OBSERVABLE,),
        0,
    ),
    (
        "pre_boundary_ignored_count_regressed",
        {
            0: {"status_packet_count": 2, "ignored_temperature_packet_count": 1},
            1: {"status_packet_count": 3, "ignored_temperature_packet_count": 0},
        },
        {0: 0.05, 1: 0.05},
        (Screen.COUNTER_REGRESSED,),
        1,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "durations", "expected", "abort_tick"),
    [case[1:] for case in EN4_CASES],
    ids=[case[0] for case in EN4_CASES],
)
async def test_en4_each_screen_reason_aborts_alone_at_its_tick(
    tmp_path: Path,
    changes: dict[int, dict[str, object]],
    durations: dict[int, float],
    expected: tuple[Screen, ...],
    abort_tick: int,
) -> None:
    """EN4: each isolated screen fact gives exactly its own reason at its own tick."""
    rig = screened_rig(tmp_path, changes, durations=durations)
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == screen_aborts(*expected)
    assert [(r.tick, r.reason) for r in sink.temperature_aborts] == [
        (abort_tick, reason) for reason in expected
    ]
    assert [r.tick for r in sink.temperatures] == list(range(abort_tick + 1))


@pytest.mark.asyncio
async def test_en4_the_inclusive_screen_bounds_do_not_abort(tmp_path: Path) -> None:
    """EN4: bean 5.0 °C and environment 40.0 °C at an eligible tick are inside the screen."""
    bounds: dict[str, object] = {
        "last_packet_bean_temp_c": 5.0,
        "retained_bean_temp_c": 5.0,
        "last_packet_env_temp_c": 40.0,
        "retained_env_temp_c": 40.0,
    }
    rig = screened_rig(tmp_path, {1: bounds}, durations=ELIGIBLE_SECOND)
    result, sink = await run_phase(rig)
    assert completed(result).tick_count == 3
    assert sink.temperature_aborts == []


# Anchor 0.0 keeps the instants representable: 1.0 + (t - 1.0) == t for both cases.
_BELOW = math.nextafter(60.0, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_end", [_BELOW, 60.0], ids=["below", "at"])
async def test_en5_engine_and_replay_share_one_activation_anchor(
    tmp_path: Path, second_end: float
) -> None:
    """EN5: the engine's screen equals a replay from the stored tick and activation instants."""
    clock = FakeClock()
    clock.t = 0.0
    hot: dict[str, object] = {"last_packet_bean_temp_c": 41.0, "retained_bean_temp_c": 41.0}
    rig = screened_rig(tmp_path, {1: hot}, durations={0: 0.05, 1: second_end - 1.0}, clock=clock)
    hook = RecordingHook(rig)
    admission = await admit(rig)
    writer = engine.open_phase_evidence(admission)
    sink = SpySink(writer)
    result = await engine.observe_cold_phase(
        admission=admission,
        sink=sink,
        mcp=rig.mcp,
        host=rig.host,
        clock=rig.clock,
        activation_hook=hook,
    )
    activated = hook.calls[0][1]
    writer.seal()
    stored = [r for r in sink.records if type(r) is schema.ColdTickRecord]
    since = stored[1].monotonic_seconds - activated
    if second_end == 60.0:
        assert since == 60.0
    else:
        assert since < 60.0
    replay = evaluate_temperature(
        sink.temperatures[1].temperature,
        previous=sink.temperatures[0].temperature,
        since_activation_seconds=since,
    )
    engine_reasons = (
        tuple(typing.cast(Screen, abort.reason) for abort in aborted(result).aborts)
        if type(result) is engine.ColdPhaseAborted
        else ()
    )
    assert engine_reasons == replay
    assert replay == ((Screen.OUTSIDE_SCREEN,) if second_end == 60.0 else ())


@pytest.mark.asyncio
async def test_en6_v1_reasons_precede_temperature_reasons_on_one_tick(tmp_path: Path) -> None:
    """EN6 (precedence check): the v1 abort record is retained before the schema-5 one."""
    hot: dict[str, object] = {"last_packet_bean_temp_c": 41.0, "retained_bean_temp_c": 41.0}
    rig = screened_rig(tmp_path, {1: hot}, durations=ELIGIBLE_SECOND, heat={1: 1})
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == (
        *engine_aborts(Reason.COMMAND_STATE_NON_ZERO),
        *screen_aborts(Screen.OUTSIDE_SCREEN),
    )
    assert sink.order[-4:] == ["tick", "tick_temperature", "abort", "temperature_abort"]


@pytest.mark.asyncio
async def test_en7_a_temperature_builder_refusal_writes_no_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EN7: the temperature record is built before any write; its refusal is EVIDENCE."""

    def refuse(**_kwargs: object) -> typing.NoReturn:
        raise schema.ColdEvidenceError(schema.ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE)

    monkeypatch.setattr(engine, "build_tick_temperature_record", refuse)
    rig = make_rig(tmp_path)
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == (
        engine.ColdAbortClassification(
            domain=Domain.EVIDENCE, reason=schema.ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE
        ),
    )
    assert [r for r in sink.records if type(r) is schema.ColdTickRecord] == []
    assert sink.temperatures == []
    assert streams(sink) == ["header", "abort"]


@pytest.mark.asyncio
async def test_en8_a_failed_temperature_append_is_unexpected_and_leaves_a_raw_tail(
    tmp_path: Path,
) -> None:
    """EN8: the raw writer keeps one unpaired tick (the documented raw-writer residual)."""
    rig = make_rig(tmp_path)
    with pytest.raises(engine.ColdEngineUnexpectedError) as caught:
        await run_phase(rig, temperature_error=RuntimeError(CANARY))
    assert caught.value.abort_recorded is False
    assert caught.value.session_id == SID
    assert_contained(caught.value, "Cold engine failed unexpectedly.")
    assert rig.mcp.calls.count("get_roast_state") == 1
    assert rig.host.sample_calls == 0


@pytest.mark.asyncio
async def test_en8_the_raw_writer_retains_the_unpaired_tick(tmp_path: Path) -> None:
    """EN8: the tick line is on disk with no temperature line and no abort."""
    rig = make_rig(tmp_path)
    admission = await admit(rig)
    sink = SpySink(engine.open_phase_evidence(admission), temperature_error=RuntimeError(CANARY))
    with pytest.raises(engine.ColdEngineUnexpectedError):
        await engine.observe_cold_phase(
            admission=admission, sink=sink, mcp=rig.mcp, host=rig.host, clock=rig.clock
        )
    assert streams(sink) == ["header", "tick"]
    assert sink.temperatures == [] and sink.temperature_aborts == []


_PAIR_SAMPLES: tuple[enum.Enum, ...] = (
    next(iter(schema.ColdHostAbortReason)),
    schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED,
    Reason.CANCELLED,
    Screen.OUTSIDE_SCREEN,
)
_ADMITTED_PAIRS = {
    (Domain.HOST, schema.ColdHostAbortReason),
    (Domain.EVIDENCE, schema.ColdEvidenceFailure),
    (Domain.ENGINE, schema.ColdEngineAbortReason),
    (Domain.ENGINE, ColdTemperatureScreenReason),
}


def test_en9_abort_classification_admits_exactly_the_engine_pairs() -> None:
    """EN9: every domain/reason-class pairing is admitted or refused exactly."""
    for domain in Domain:
        for reason in _PAIR_SAMPLES:
            payload = {"domain": domain, "reason": reason}
            if (domain, type(reason)) in _ADMITTED_PAIRS:
                admitted = engine.ColdAbortClassification.model_validate(payload)
                assert admitted.domain is domain and admitted.reason is reason
            else:
                with pytest.raises(pydantic.ValidationError):
                    engine.ColdAbortClassification.model_validate(payload)


@pytest.mark.asyncio
async def test_en10_the_previous_snapshot_advances_on_every_screened_tick(
    tmp_path: Path,
) -> None:
    """EN10: t1 (eligible) compares with pre-boundary t0; t2 with t1, and stalls only there."""
    rig = screened_rig(
        tmp_path,
        {2: {"status_packet_count": 2}},
        durations={0: 0.05, 1: 60.0, 2: 0.05},
    )
    result, sink = await run_phase(rig)
    assert aborted(result).aborts == screen_aborts(Screen.PACKET_NOT_PROGRESSED)
    assert [(r.tick, r.reason) for r in sink.temperature_aborts] == [
        (2, Screen.PACKET_NOT_PROGRESSED)
    ]
    assert [r.temperature.status_packet_count for r in sink.temperatures] == [1, 2, 2]
