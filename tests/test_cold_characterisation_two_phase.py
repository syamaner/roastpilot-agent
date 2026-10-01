"""Hardware-free tests for the two-phase cold-characterisation orchestrator (#954 4g-c).

Every run uses a scripted clock and fake MCP, child, identity and host ports over
the real evidence writer, seal, v2 reader and conformance checker under
``tmp_path``.  Nothing touches hardware, a serial port, a microphone, a provider
or a child process.  Expected facts are authored independently of the code under
test: instants follow from the fixed 1800 s window, the 450 s scripted reads and
the fixed 60 s budget, never from values the orchestrator computed.
"""

# pyright: reportPrivateUsage=false

import ast
import asyncio
import enum
import itertools
import json
import math
import types
import typing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import conformance, engine, two_phase
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.identity import ColdArtefactKind, ColdRunIdentity
from roastpilot_agent.cold_characterisation.mcp import (
    ColdFinalisationNotCleanError,
    ColdFinalisationSafetyError,
    ColdTickObservation,
    SessionFinalisationResult,
)
from roastpilot_agent.mcp_client import (
    EventCommandResult,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
)
from tests.test_cold_characterisation_acceptance import result_for
from tests.test_cold_characterisation_conformance import audio, safe_host_sample
from tests.test_cold_characterisation_engine import cold_identity, marked_document, start_result
from tests.test_cold_characterisation_evidence_builders import (
    RUN_ID,
    device_state,
    observation,
    roast_fan_state,
    session_metadata,
)
from tests.test_cold_characterisation_evidence_store import make_root

Phase = schema.ColdPhaseKind
OFF = Phase.RECORDING_OFF
ON = Phase.RECORDING_ON
Event = lifecycle.ColdLifecycleEvent
R = lifecycle.ColdRunTerminationReason
Outcome = two_phase.ColdTwoPhaseOutcome
Own = two_phase.ColdChildOwnership
Refusal = two_phase.ColdRunStartRefusal
Json = dict[str, typing.Any]
DRIVER = "hottop_kn8828b_2k_plus"
CANARY = "CANARY-4gc-91d2"
#: Session identities carry the canary so any leak into a result is caught.
SESSIONS: typing.Final = {OFF: f"session-{CANARY}-off", ON: f"session-{CANARY}-on"}
T0 = 100.0
BASE_UTC = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
READ_SECONDS = 450.0
#: Independently authored: OFF activates at T0, so its scheduled end is T0 + 1800.
OFF_END = 1900.0
#: The exact v1 success grammar, authored as text.
GRAMMAR: typing.Final = [
    ("recording_off", "phase_activated"),
    ("recording_off", "observation_window_elapsed"),
    ("recording_off", "finalisation_returned"),
    ("recording_off", "child_stopped"),
    ("recording_off", "child_started"),
    ("recording_on", "phase_activated"),
    ("recording_on", "transition_measured"),
    ("recording_on", "observation_window_elapsed"),
    ("recording_on", "finalisation_returned"),
    ("recording_on", "child_stopped"),
    ("recording_on", "run_terminated"),
]


# ------------------------------------------------------------------ harness


def utc_at(seconds: float) -> str:
    """Return the scripted clock's UTC text for one monotonic instant."""
    return (BASE_UTC + timedelta(seconds=seconds)).isoformat()


class Clock:
    """Scripted clock; ``pending`` faults apply to the next ``monotonic`` calls in order."""

    def __init__(self) -> None:
        self.t = T0
        self.pending: list[str] = []
        self.samples = 0

    def monotonic(self) -> float:
        self.samples += 1
        fault = self.pending.pop(0) if self.pending else ""
        if fault.startswith("regress"):
            return self.t - float(fault.partition(":")[2] or 50.0)
        if fault.startswith("jump"):
            self.t += float(fault.partition(":")[2])
        if fault == "nan":
            return math.nan
        if fault == "raise":
            raise RuntimeError(CANARY)
        return self.t

    def utc_now_iso(self) -> str:
        return utc_at(self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def conforming_tick(phase: Phase, index: int) -> ColdTickObservation:
    """One replay-clean tick whose heartbeat and audio counters rise with ``index``."""
    return observation(
        device_state(driver=DRIVER),
        roast_fan=roast_fan_state(level=0),
        audio=audio(index),
        session=session_metadata(
            session_id=SESSIONS[phase], elapsed_monotonic_seconds=float(index + 1)
        ),
    )


def clean_result(phase: Phase, session_id: str, **changes: object) -> SessionFinalisationResult:
    """One strict D195 result for ``session_id``; clean unless ``changes`` say otherwise."""
    return SessionFinalisationResult.model_validate_json(
        json.dumps(result_for(phase, session_id=session_id, **changes))
    )


def not_clean_result(phase: Phase, session_id: str) -> SessionFinalisationResult:
    """One strict, admitted D195 result that is not clean."""
    return clean_result(phase, session_id, status="completed_not_clean", clean=False)


def produce(item: object) -> typing.Any:
    """Raise a scripted exception, or return the scripted value."""
    if isinstance(item, BaseException):
        raise item
    return item


class Gates:
    """Named asyncio gates: an awaiting fake signals ``entered`` then waits."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}

    def arm(self, name: str) -> asyncio.Event:
        self.gates[name] = asyncio.Event()
        self.entered[name] = asyncio.Event()
        return self.entered[name]

    async def wait(self, name: str) -> None:
        gate = self.gates.get(name)
        if gate is not None:
            self.entered[name].set()
            await gate.wait()


class Mcp:
    """Cold MCP fake over the same scripted clock; phase follows the child's spawn."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.phase: Phase | None = None
        self.reads = 0
        self.calls: list[tuple[str, Phase | None]] = []
        self.finalised: list[str] = []
        self.before: dict[str, Callable[[], None]] = {}
        self.read_seconds: Callable[[Phase, int], float] = lambda _phase, _index: READ_SECONDS
        self.tick: Callable[[Phase, int], object] = conforming_tick
        self.finalise_advance = 1.0
        self.finalise: Callable[[Phase, str], object] = clean_result
        self.start: Callable[[Phase], object] = lambda phase: start_result(SESSIONS[phase])

    def begin(self, phase: Phase) -> None:
        self.phase = phase
        self.reads = 0

    @property
    def current(self) -> Phase:
        assert self.phase is not None
        return self.phase

    async def _enter(self, name: str) -> None:
        self.calls.append((name, self.phase))
        key = f"{name}:{self.current.value}"
        hook = self.before.get(key)
        if hook is not None:
            hook()
        await self.world.gates.wait(key)

    async def get_server_info(self) -> ServerInfo:
        await self._enter("get_server_info")
        return self.world.ids[self.current].server_info

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        await self._enter("get_runtime_config")
        return self.world.ids[self.current].runtime_config

    async def start_cold_session(self) -> StartRoastSessionResult:
        await self._enter("start_cold_session")
        return produce(self.start(self.current))

    async def mark_beans_added(self) -> EventCommandResult:
        await self._enter("mark_beans_added")
        return EventCommandResult.model_validate(marked_document())

    async def get_roast_state(self, session_id: str | None = None) -> ColdTickObservation:
        await self._enter("get_roast_state")
        index = self.reads
        self.reads += 1
        self.world.clock.t += self.read_seconds(self.current, index)
        return produce(self.tick(self.current, index))

    async def finalise_session(self, session_id: str) -> SessionFinalisationResult:
        await self._enter("finalise_session")
        self.finalised.append(session_id)
        self.world.clock.t += self.finalise_advance
        return produce(self.finalise(self.current, session_id))


class Child:
    """Child-lifecycle fake with exact, scriptable booleans and a sticky uncertainty flag."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.running_value: object = False
        self.unconfirmed_value: object = False
        self.phase: Phase | None = None
        self.calls: list[tuple[str, Phase | None]] = []
        self.start_modes: list[str] = []
        self.stop_modes: list[str] = []
        self.configure_errors: dict[Phase, BaseException] = {}
        self.on_configure: dict[Phase, Callable[[], None]] = {}
        self.before_start: dict[Phase, Callable[[], None]] = {}
        self.before_stop: dict[int, Callable[[], None]] = {}
        self.stops = 0
        self.stops_completed = 0

    @property
    def running(self) -> bool:
        return typing.cast(bool, self.running_value)

    @property
    def stop_unconfirmed(self) -> bool:
        return typing.cast(bool, self.unconfirmed_value)

    def configure_phase(self, phase: Phase) -> None:
        self.calls.append(("configure", phase))
        error = self.configure_errors.get(phase)
        if error is not None:
            raise error
        self.phase = phase
        hook = self.on_configure.get(phase)
        if hook is not None:
            hook()

    async def start(self) -> None:
        self.calls.append(("start", self.phase))
        mode = self.start_modes.pop(0) if self.start_modes else "ok"
        hook = self.before_start.get(typing.cast(Phase, self.phase))
        if hook is not None:
            hook()
        await self.world.gates.wait(f"start:{typing.cast(Phase, self.phase).value}")
        if mode == "raise":
            raise RuntimeError(CANARY)
        if mode == "silent":
            return
        self.running_value = True
        self.world.mcp.begin(typing.cast(Phase, self.phase))
        if mode == "unconfirmed":
            self.unconfirmed_value = True

    async def stop(self) -> None:
        index = self.stops
        self.stops += 1
        self.calls.append(("stop", self.phase))
        mode = self.stop_modes.pop(0) if self.stop_modes else "ok"
        hook = self.before_stop.get(index)
        if hook is not None:
            hook()
        try:
            await self.world.gates.wait(f"stop:{index}")
        except asyncio.CancelledError:
            # Models the real process: an interrupted stop leaves a sticky flag.
            self.unconfirmed_value = True
            raise
        self.stops_completed += 1
        if mode == "raise":
            raise RuntimeError(CANARY)
        if mode == "unconfirmed":
            self.running_value = False
            self.unconfirmed_value = True
        elif mode != "still_running":
            self.running_value = False


class Identities:
    """Phase identity source fake; ``values`` may hold forged carriers or errors."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.values: dict[Phase, object] = dict(world.ids)
        self.calls: list[Phase] = []
        self.before: dict[Phase, Callable[[], None]] = {}

    async def freeze(self, phase: Phase) -> ColdRunIdentity:
        self.calls.append(phase)
        hook = self.before.get(phase)
        if hook is not None:
            hook()
        await self.world.gates.wait(f"freeze:{phase.value}")
        return produce(self.values[phase])


class Host:
    """Host fake returning the safe AC15 baseline; start checks may run a hook."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.samples = 0
        self.before_start: dict[int, Callable[[], None]] = {}

    def check_start_bounds(self, evidence_root: Path) -> None:
        del evidence_root
        hook = self.before_start.get(self.start_calls)
        self.start_calls += 1
        if hook is not None:
            hook()

    def sample(self, evidence_root: Path) -> typing.Any:
        del evidence_root
        self.samples += 1
        return safe_host_sample()


def on_identity(tmp_path: Path, root: str, **document_changes: object) -> ColdRunIdentity:
    """The recording-on identity: differs from recording-off only in the five masked leaves."""
    document = cold_identity(tmp_path, root, phase=ON).model_dump(mode="json")
    document["server_info"]["started_at_utc"] = "2026-09-26T12:31:00Z"
    document["effective_mcp_profile"]["source_sha256"] = "d" * 64
    document["effective_mcp_profile"]["source_byte_length"] = 200
    document.update(document_changes)
    return ColdRunIdentity.model_validate_json(json.dumps(document))


class World:
    """One complete fake environment over one admitted root."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.root = make_root(tmp_path)
        self.admitted = store.admit_evidence_root(self.root)
        self.ids: dict[Phase, ColdRunIdentity] = {
            OFF: cold_identity(tmp_path, self.root, phase=OFF),
            ON: on_identity(tmp_path, self.root),
        }
        self.gates = Gates()
        self.clock = Clock()
        self.mcp = Mcp(self)
        self.child = Child(self)
        self.identities = Identities(self)
        self.host = Host()

    async def run(self) -> two_phase.ColdTwoPhaseResult:
        return await two_phase.run_two_phase_characterisation(
            root=self.admitted,
            mcp=self.mcp,
            child=self.child,
            identities=self.identities,
            host=self.host,
            clock=self.clock,
        )

    def records(self, stream: str) -> list[Json]:
        """Every retained line of one stream in both phases, read raw from disk."""
        found: list[Json] = []
        for phase in (OFF, ON):
            path = Path(self.root) / RUN_ID / "records" / phase.value / f"{stream}.jsonl"
            if path.exists():
                found.extend(json.loads(line) for line in path.read_text().splitlines())
        return found

    def lifecycle(self) -> list[Json]:
        """Retained lifecycle records in append order."""
        return sorted(self.records("lifecycle"), key=lambda record: record["sequence"])

    def events(self) -> list[tuple[str, str]]:
        return [(record["phase"], record["event"]) for record in self.lifecycle()]

    def event(self, phase: Phase, name: str) -> Json:
        matches = [r for r in self.lifecycle() if r["phase"] == phase.value and r["event"] == name]
        assert len(matches) == 1, (phase, name)
        return matches[0]

    def manifest_exists(self) -> bool:
        return (Path(self.root) / RUN_ID / "manifest.json").exists()

    def retained(self, digest: str | None) -> reader.ColdRetainedRunV2:
        assert digest is not None
        return reader.read_retained_run_v2(
            self.root, run_id=RUN_ID, expected_manifest_sha256=digest
        )

    def child_ops(self) -> list[tuple[str, Phase | None]]:
        return list(self.child.calls)


NORMAL_CHILD_OPS: typing.Final = [
    ("configure", OFF),
    ("start", OFF),
    ("stop", OFF),
    ("configure", ON),
    ("start", ON),
    ("stop", ON),
]


def assert_no_canary(result: two_phase.ColdTwoPhaseResult) -> None:
    """No session identity, path or error text reaches the public result."""
    for text in (repr(result), str(result), result.model_dump_json()):
        assert CANARY not in text
        assert "/" not in text.replace("://", "")


def findings_of(result: two_phase.ColdTwoPhaseResult) -> set[conformance.ColdConformanceFinding]:
    assert result.conformance is not None
    return set(result.conformance.findings)


def assert_failed(
    world: World, result: two_phase.ColdTwoPhaseResult, reason: lifecycle.ColdRunTerminationReason
) -> None:
    """A sealed run whose terminal is FAILED with ``reason`` and is never conformant."""
    assert result.outcome is Outcome.NOT_CONFORMANT
    assert result.termination_reason is reason
    terminal = world.lifecycle()[-1]
    assert terminal["event"] == "run_terminated"
    assert terminal["termination"] == "failed"
    assert terminal["termination_reason"] == reason.value
    retained = world.retained(result.manifest_sha256)
    checked = conformance.check_pre_advisory_conformance(retained)
    assert checked.outcome is conformance.ColdConformanceOutcome.NOT_CONFORMANT
    assert_no_canary(result)


# ------------------------------------------------- T16: carrier admission (unit)


class HostileStr(str):
    """A ``str`` subclass recording every comparison, hash or encode reached."""

    calls: typing.ClassVar[list[str]] = []

    def __eq__(self, other: object) -> bool:
        HostileStr.calls.append("eq")
        return True

    def __ne__(self, other: object) -> bool:
        HostileStr.calls.append("ne")
        return False

    def __hash__(self) -> int:
        HostileStr.calls.append("hash")
        return 0

    def encode(self, *args: typing.Any, **kwargs: typing.Any) -> bytes:  # type: ignore[override]
        HostileStr.calls.append("encode")
        return b""

    def strip(self, *args: typing.Any) -> str:  # type: ignore[override]
        HostileStr.calls.append("strip")
        return "x"


class HostileDict(dict[str, object]):
    """A ``dict`` subclass recording every access reached."""

    calls: typing.ClassVar[list[str]] = []

    def __getitem__(self, key: str) -> object:
        HostileDict.calls.append("getitem")
        return super().__getitem__(key)

    def __len__(self) -> int:
        HostileDict.calls.append("len")
        return super().__len__()

    def __iter__(self) -> typing.Iterator[str]:
        HostileDict.calls.append("iter")
        return super().__iter__()

    def __contains__(self, key: object) -> bool:
        HostileDict.calls.append("contains")
        return super().__contains__(key)


class HostileFieldsSet(set[str]):
    """A fields-set replacement that records any use."""

    calls: typing.ClassVar[list[str]] = []

    def __iter__(self) -> typing.Iterator[str]:
        HostileFieldsSet.calls.append("iter")
        return super().__iter__()

    def __contains__(self, key: object) -> bool:
        HostileFieldsSet.calls.append("contains")
        return super().__contains__(key)

    def __len__(self) -> int:
        HostileFieldsSet.calls.append("len")
        return super().__len__()


class ForeignArtefactKind(enum.Enum):
    """A look-alike enum with the same value as a real member."""

    WHEEL = "wheel"
    EDITABLE_SOURCE = "editable_source"


class ForeignOutcome(enum.Enum):
    """A look-alike conformance outcome."""

    PRE_ADVISORY_CONFORMANT = "pre_advisory_conformant"


def reset_spies() -> None:
    HostileStr.calls.clear()
    HostileDict.calls.clear()
    HostileFieldsSet.calls.clear()


def spy_calls() -> list[str]:
    return [*HostileStr.calls, *HostileDict.calls, *HostileFieldsSet.calls]


_ModelT = typing.TypeVar("_ModelT", bound=pydantic.BaseModel)


def raw_fields(model: pydantic.BaseModel) -> dict[str, typing.Any]:
    """The caller model's declared raw values, read without any model method."""
    return dict(object.__getattribute__(model, "__dict__"))


def forged(model: _ModelT, **changes: object) -> _ModelT:
    """Forge a copy through ``model_construct`` with selected raw values replaced."""
    values: dict[str, typing.Any] = {**raw_fields(model), **changes}
    return type(model).model_construct(**values)


def with_dict(model: _ModelT, data: dict[typing.Any, object]) -> _ModelT:
    """A forged copy whose ``__dict__`` is replaced by ``data``."""
    copy = forged(model)
    object.__setattr__(copy, "__dict__", data)
    return copy


def with_slot(model: _ModelT, slot: str, value: object) -> _ModelT:
    """A forged copy with one pydantic metadata slot replaced."""
    copy = forged(model)
    object.__setattr__(copy, slot, value)
    return copy


def nested_lists(depth: int) -> list[object]:
    """A list nested ``depth`` levels below its holder."""
    value: list[object] = ["leaf"]
    for _ in range(depth):
        value = [value]
    return value


def wide_lists(count: int, width: int) -> list[object]:
    """``count`` distinct lists of ``width`` strings (no shared container)."""
    return [[f"t{index}" for index in range(width)] for _ in range(count)]


def identity_forgeries(genuine: ColdRunIdentity) -> dict[str, object]:
    """Every forged identity shape the admission must refuse."""
    server = typing.cast(ServerInfo, raw_fields(genuine)["server_info"])
    runtime = typing.cast(RuntimeConfigSnapshot, raw_fields(genuine)["runtime_config"])
    provenance = typing.cast(pydantic.BaseModel, raw_fields(genuine)["build_provenance"])
    hostile_key = dict(raw_fields(genuine))
    hostile_key[HostileStr("run_id")] = hostile_key.pop("run_id")
    # A genuine non-empty tuple reused in a second compatible field: schema-valid,
    # so only the repeated-node guard refuses it.
    device = typing.cast(pydantic.BaseModel, raw_fields(genuine)["device_config"])
    shared = raw_fields(device)["recording_devices"]
    cyclic: list[object] = []
    cyclic.append(cyclic)

    class SubIdentity(ColdRunIdentity):
        pass

    return {
        "subclass": SubIdentity.model_construct(**raw_fields(genuine)),
        "str_subclass": forged(genuine, run_id=HostileStr(genuine.run_id)),
        "dict_subclass": with_dict(genuine, HostileDict(raw_fields(genuine))),
        "extra_dict_key": with_dict(genuine, {**raw_fields(genuine), "undeclared": 1}),
        "missing_dict_key": with_dict(
            genuine, {k: v for k, v in raw_fields(genuine).items() if k != "kernel"}
        ),
        "str_subclass_key": with_dict(genuine, hostile_key),
        "pydantic_extra": with_slot(genuine, "__pydantic_extra__", {"x": 1}),
        "pydantic_extra_empty": with_slot(genuine, "__pydantic_extra__", {}),
        "pydantic_private": with_slot(genuine, "__pydantic_private__", {"x": 1}),
        "missing_slots": ColdRunIdentity.__new__(ColdRunIdentity),
        "bool_for_float": forged(genuine, controller_tick_seconds=True),
        "int_for_float": forged(genuine, controller_tick_seconds=1),
        "bool_for_int_nested": forged(
            genuine, runtime_config=forged(runtime, roaster_baudrate=True)
        ),
        "nested_hostile": forged(
            genuine, runtime_config=forged(runtime, roaster_driver=HostileStr(DRIVER))
        ),
        "foreign_enum": forged(
            genuine,
            build_provenance=forged(provenance, artefact_kind=ForeignArtefactKind.EDITABLE_SOURCE),
        ),
        "fabricated_member": forged(
            genuine,
            build_provenance=forged(provenance, artefact_kind=object.__new__(ColdArtefactKind)),
        ),
        "nan": forged(genuine, controller_tick_seconds=math.nan),
        "big_int": forged(genuine, runtime_config=forged(runtime, roaster_baudrate=10**40)),
        "oversize_text": forged(genuine, operator_host_notes="x" * (schema.MAX_ENVELOPE_BYTES + 1)),
        "over_depth": forged(
            genuine,
            server_info=forged(server, available_bootstrap_tools=nested_lists(10)),
        ),
        "over_collection": forged(
            genuine,
            server_info=forged(
                server, available_bootstrap_tools=["t"] * (schema.MAX_COLLECTION_LENGTH + 1)
            ),
        ),
        "over_nodes": forged(
            genuine, server_info=forged(server, available_bootstrap_tools=wide_lists(5, 1000))
        ),
        "cycle": forged(genuine, server_info=forged(server, available_bootstrap_tools=cyclic)),
        "repeated_node": forged(
            genuine,
            server_info=forged(server, available_bootstrap_tools=shared),
        ),
        "unknown_object": forged(genuine, operator_host_notes=object()),
        "set_container": forged(
            genuine, server_info=forged(server, available_bootstrap_tools={"a"})
        ),
        "wrong_root": raw_fields(genuine),
    }


def test_4gc_t16_genuine_carriers_are_admitted_fresh(tmp_path: Path) -> None:
    """T16: each genuine carrier is admitted as a new, equal instance."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    fresh = two_phase._admit_carrier(genuine, ColdRunIdentity, two_phase._IDENTITY)
    assert fresh == genuine and fresh is not genuine
    result = clean_result(ON, SESSIONS[ON])
    admitted = two_phase._admit_carrier(result, SessionFinalisationResult, two_phase._FINALISATION)
    assert admitted == result and admitted is not result
    off_result = clean_result(OFF, SESSIONS[OFF])
    assert (
        two_phase._admit_carrier(off_result, SessionFinalisationResult, two_phase._FINALISATION)
        == off_result
    )
    for carrier in engine_carriers():
        again = two_phase._admit_carrier(carrier, type(carrier), two_phase._ENGINE)
        assert again == carrier and again is not carrier
    checked = conformance.ColdConformanceResult(
        policy_version=1,
        outcome=conformance.ColdConformanceOutcome.NOT_CONFORMANT,
        findings=(conformance.ColdConformanceFinding.TICKS_ABSENT,),
    )
    assert (
        two_phase._admit_carrier(checked, conformance.ColdConformanceResult, two_phase._CHECKER)
        == checked
    )


def engine_carriers() -> list[pydantic.BaseModel]:
    """Genuine engine results of all three roots, including every abort pair class."""
    Domain = schema.ColdAbortDomain
    return [
        engine.ColdPhaseCompleted(
            session_id=SESSIONS[OFF],
            observation_end_monotonic=OFF_END,
            observation_end_utc=utc_at(OFF_END),
            tick_count=4,
        ),
        engine.ColdPhaseAborted(
            aborts=(
                engine.ColdAbortClassification(
                    domain=Domain.HOST, reason=schema.ColdHostAbortReason.THERMAL_EXCEEDED
                ),
                engine.ColdAbortClassification(
                    domain=Domain.EVIDENCE, reason=schema.ColdEvidenceFailure.RECORD_TOO_LARGE
                ),
                engine.ColdAbortClassification(
                    domain=Domain.ENGINE, reason=schema.ColdEngineAbortReason.CANCELLED
                ),
            ),
            session_id=None,
            abort_recorded=True,
        ),
        engine.ColdPhaseActivationRefused(session_id=SESSIONS[ON]),
    ]


def test_4gc_t16_every_forged_identity_is_refused_with_zero_spy_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16: every forged identity shape returns ``None`` and no hostile method runs."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    dumped: list[int] = []
    original = ColdRunIdentity.model_dump

    def spy_dump(self: ColdRunIdentity, **kwargs: typing.Any) -> dict[str, typing.Any]:
        dumped.append(id(self))
        return original(self, **kwargs)

    monkeypatch.setattr(ColdRunIdentity, "model_dump", spy_dump)
    forgeries = identity_forgeries(genuine)
    assert len(forgeries) == 27
    for name, value in forgeries.items():
        reset_spies()
        dumped.clear()
        admitted = two_phase._admit_carrier(value, ColdRunIdentity, two_phase._IDENTITY)
        assert admitted is None, name
        assert spy_calls() == [], name
        assert id(value) not in dumped, name


def test_4gc_t16_hostile_fields_set_is_ignored_and_never_called(tmp_path: Path) -> None:
    """T16: caller ``__pydantic_fields_set__`` is never read; a genuine carrier still admits."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    value = with_slot(genuine, "__pydantic_fields_set__", HostileFieldsSet({"run_id"}))
    reset_spies()
    admitted = two_phase._admit_carrier(value, ColdRunIdentity, two_phase._IDENTITY)
    assert admitted == genuine
    assert spy_calls() == []


def test_4gc_t16_the_caller_model_is_never_dumped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16: only fresh snapshots are dumped; the caller instance is identified and absent."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    dumped: list[int] = []
    copied: list[int] = []
    original_dump = ColdRunIdentity.model_dump
    original_copy = ColdRunIdentity.model_copy

    def spy_dump(self: ColdRunIdentity, **kwargs: typing.Any) -> dict[str, typing.Any]:
        dumped.append(id(self))
        return original_dump(self, **kwargs)

    def spy_copy(self: ColdRunIdentity, **kwargs: typing.Any) -> ColdRunIdentity:
        copied.append(id(self))
        return original_copy(self, **kwargs)

    monkeypatch.setattr(ColdRunIdentity, "model_dump", spy_dump)
    monkeypatch.setattr(ColdRunIdentity, "model_copy", spy_copy)
    admitted = two_phase._admit_carrier(genuine, ColdRunIdentity, two_phase._IDENTITY)
    assert admitted is not None
    assert dumped == [id(admitted)]
    assert id(genuine) not in dumped
    assert copied == []


def test_4gc_t16_a_repeated_node_is_refused_immediately() -> None:
    """T16: a model or non-empty container seen before is refused on its second visit."""
    table = two_phase._IDENTITY
    shared: list[object] = ["a"]
    assert two_phase._copy_node(shared, table, set()) is not None
    assert two_phase._copy_node(shared, table, {id(shared)}) is None
    assert two_phase._copy_node((), table, {id(())}) == ([], [], 0)
    assert two_phase._copy_node({}, table, {id({})}) is not None


def test_4gc_t16_a_cycle_is_refused_after_one_revisit(monkeypatch: pytest.MonkeyPatch) -> None:
    """T16: the cycle guard refuses at the first revisit, not at the depth or node bound."""
    cyclic: list[object] = []
    cyclic.append(cyclic)
    copies: list[object] = []
    original = two_phase._copy_node

    def counting(current: object, table: two_phase._Carrier, visited: set[int]) -> typing.Any:
        copies.append(current)
        return original(current, table, visited)

    monkeypatch.setattr(two_phase, "_copy_node", counting)

    class Holder(pydantic.BaseModel):
        items: list[object]

    holder = Holder.model_construct(items=cyclic)
    table = two_phase._carrier((Holder,), ())
    assert two_phase._snapshot(holder, Holder, table) is None
    assert len(copies) == 3


def test_4gc_t16_bounds_admit_their_limits_and_refuse_one_beyond(tmp_path: Path) -> None:
    """T16: depth, collection and envelope bounds are inclusive at the settled limits."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    server = typing.cast(ServerInfo, raw_fields(genuine)["server_info"])
    table = two_phase._IDENTITY

    def snap(tools: object) -> object | None:
        value = forged(genuine, server_info=forged(server, available_bootstrap_tools=tools))
        return two_phase._snapshot(value, ColdRunIdentity, table)

    # The tools list sits at depth 2; a leaf at depth 8 is admitted, at 9 refused.
    assert snap(nested_lists(5)) is not None
    assert snap(nested_lists(6)) is None
    assert snap(["t"] * schema.MAX_COLLECTION_LENGTH) is not None
    assert snap(["t"] * (schema.MAX_COLLECTION_LENGTH + 1)) is None
    assert snap({"k": "v"}) is not None
    assert snap({1: "v"}) is None
    assert snap({HostileStr("k"): "v"}) is None
    assert snap({f"k{index}": "v" for index in range(schema.MAX_COLLECTION_LENGTH + 1)}) is None
    big = "é" * (schema.MAX_ENVELOPE_BYTES // 2)
    assert two_phase._scalar(big) == schema.MAX_ENVELOPE_BYTES
    assert snap([big]) is None
    with pytest.raises(UnicodeEncodeError):
        snap(["\ud800"])
    assert (
        two_phase._admit_carrier(
            forged(genuine, server_info=forged(server, available_bootstrap_tools=["\ud800"])),
            ColdRunIdentity,
            table,
        )
        is None
    )
    assert two_phase._scalar(10**32) is None
    assert two_phase._scalar(10**32 - 1) == schema.MAX_INT_DIGITS + 1
    assert two_phase._scalar(-(10**32) + 1) == schema.MAX_INT_DIGITS + 1
    assert two_phase._scalar(math.inf) is None
    assert two_phase._scalar(True) == 5
    assert two_phase._scalar(HostileStr("x")) is None


def test_4gc_t16_a_coercing_snapshot_is_not_silently_accepted(tmp_path: Path) -> None:
    """T16: a value that strict JSON would coerce fails the lossless round trip."""
    genuine = cold_identity(tmp_path, make_root(tmp_path))
    coerced = forged(genuine, controller_tick_seconds=1)
    assert two_phase._snapshot(coerced, ColdRunIdentity, two_phase._IDENTITY) is not None
    assert two_phase._admit_carrier(coerced, ColdRunIdentity, two_phase._IDENTITY) is None


def finalisation_forgeries(genuine: SessionFinalisationResult) -> dict[str, object]:
    """Forged finalisation results, nested and top-level."""
    disconnect = typing.cast(pydantic.BaseModel, raw_fields(genuine)["disconnect"])
    return {
        "str_subclass_session": forged(genuine, session_id=HostileStr(genuine.session_id)),
        "foreign_enum": forged(genuine, rejection_reason=ForeignArtefactKind.WHEEL),
        "bool_for_int": forged(genuine, attempt_number=True),
        "nested_hostile": forged(
            genuine, disconnect=forged(disconnect, last_error=HostileStr("e"))
        ),
        "extra_key": with_dict(genuine, {**raw_fields(genuine), "extra": 1}),
        "dict_subclass": with_dict(genuine, HostileDict(raw_fields(genuine))),
        "pydantic_extra": with_slot(genuine, "__pydantic_extra__", {"x": 1}),
        "subclass": type("Sub", (SessionFinalisationResult,), {}).model_construct(
            **raw_fields(genuine)
        ),
    }


def test_4gc_t16_forged_finalisation_results_are_refused(tmp_path: Path) -> None:
    """T16: forged D195 results are refused with zero spy calls."""
    del tmp_path
    genuine = clean_result(OFF, SESSIONS[OFF])
    for name, value in finalisation_forgeries(genuine).items():
        reset_spies()
        assert (
            two_phase._admit_carrier(value, SessionFinalisationResult, two_phase._FINALISATION)
            is None
        ), name
        assert spy_calls() == [], name


def test_4gc_t16_forged_engine_and_checker_carriers_are_refused() -> None:
    """T16: forged engine results and checker results are refused with zero spy calls."""
    completed, aborted, refused = engine_carriers()
    classification = typing.cast(tuple[pydantic.BaseModel, ...], raw_fields(aborted)["aborts"])[0]
    engine_forgeries: dict[str, object] = {
        "hostile_session": forged(completed, session_id=HostileStr(SESSIONS[OFF])),
        "bool_tick_count": forged(completed, tick_count=True),
        "fabricated_domain": forged(
            aborted,
            aborts=(forged(classification, domain=object.__new__(schema.ColdAbortDomain)),),
        ),
        "foreign_reason": forged(
            aborted, aborts=(forged(classification, reason=ForeignArtefactKind.WHEEL),)
        ),
        "mismatched_pair": forged(
            aborted,
            aborts=(forged(classification, domain=schema.ColdAbortDomain.ENGINE),),
        ),
        "refused_extra": with_dict(refused, {**raw_fields(refused), "x": 1}),
        "unknown_root": object(),
    }
    for name, value in engine_forgeries.items():
        reset_spies()
        for root in two_phase._ENGINE_ROOTS:
            assert two_phase._admit_carrier(value, root, two_phase._ENGINE) is None, name
        assert spy_calls() == [], name
    genuine = conformance.ColdConformanceResult(
        policy_version=1,
        outcome=conformance.ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT,
        findings=(),
    )
    checker_forgeries: dict[str, object] = {
        "bool_version": forged(genuine, policy_version=True),
        "foreign_outcome": forged(genuine, outcome=ForeignOutcome.PRE_ADVISORY_CONFORMANT),
        "fabricated_outcome": forged(
            genuine, outcome=object.__new__(conformance.ColdConformanceOutcome)
        ),
        "contradictory": forged(
            genuine, findings=(conformance.ColdConformanceFinding.TICKS_ABSENT,)
        ),
    }
    for name, value in checker_forgeries.items():
        reset_spies()
        assert (
            two_phase._admit_carrier(value, conformance.ColdConformanceResult, two_phase._CHECKER)
            is None
        ), name
        assert spy_calls() == [], name
    # A list where a tuple is declared is the same JSON array: the fresh copy is a tuple.
    listed = two_phase._admit_carrier(
        forged(genuine, findings=[]), conformance.ColdConformanceResult, two_phase._CHECKER
    )
    assert (
        listed == genuine
        and type(raw_fields(typing.cast(pydantic.BaseModel, listed))["findings"]) is tuple
    )


def test_4gc_t16_engine_and_finalisation_errors_are_admitted_from_exact_dicts() -> None:
    """T16: errors are read from an exact ``__dict__`` with closed, admitted fields only."""
    refused = engine.ColdAdmissionRefusedError(engine.ColdAdmissionFailure.MCP_READ_FAILED)
    assert two_phase._admission_failure(refused) is engine.ColdAdmissionFailure.MCP_READ_FAILED
    incomplete = engine.ColdEvidenceIncompleteError(
        engine.ColdAdmissionFailure.HEADER_APPEND_FAILED
    )
    assert (
        two_phase._admission_failure(incomplete) is engine.ColdAdmissionFailure.HEADER_APPEND_FAILED
    )
    forged_failure = engine.ColdAdmissionRefusedError(engine.ColdAdmissionFailure.CLOCK_INVALID)
    forged_failure.__dict__["failure"] = ForeignArtefactKind.WHEEL
    extra = engine.ColdAdmissionRefusedError(engine.ColdAdmissionFailure.CLOCK_INVALID)
    extra.__dict__["note"] = CANARY
    sub = type("Sub", (engine.ColdAdmissionRefusedError,), {})(
        engine.ColdAdmissionFailure.CLOCK_INVALID
    )
    for value in (forged_failure, extra, sub, RuntimeError(CANARY), object()):
        assert two_phase._admission_failure(value) is None
    unexpected = engine.ColdEngineUnexpectedError(abort_recorded=True, session_id=SESSIONS[ON])
    assert two_phase._unexpected_session(unexpected) == SESSIONS[ON]
    reset_spies()
    for session in (HostileStr(SESSIONS[ON]), None, "", " ", "x" * 2049):
        bad = engine.ColdEngineUnexpectedError(abort_recorded=True, session_id=session)  # type: ignore[arg-type]
        assert two_phase._unexpected_session(bad) is None
    not_bool = engine.ColdEngineUnexpectedError(abort_recorded=True, session_id=SESSIONS[ON])
    not_bool.__dict__["abort_recorded"] = 1
    assert two_phase._unexpected_session(not_bool) is None
    assert two_phase._unexpected_session(RuntimeError()) is None
    assert spy_calls() == []
    result = not_clean_result(OFF, SESSIONS[OFF])
    for kind in (ColdFinalisationNotCleanError, ColdFinalisationSafetyError):
        error = kind("m", result)
        assert two_phase._finalisation_error_result(error) == result
        forged_error = kind("m", forged(result, session_id=HostileStr(SESSIONS[OFF])))
        assert two_phase._finalisation_error_result(forged_error) is None
        extra_error = kind("m", result)
        extra_error.__dict__["extra"] = 1
        assert two_phase._finalisation_error_result(extra_error) is None
    sub_error = type("Sub", (ColdFinalisationNotCleanError,), {})("m", result)
    assert two_phase._finalisation_error_result(sub_error) is None
    assert two_phase._finalisation_error_result(RuntimeError()) is None
    slotless = ColdFinalisationNotCleanError.__new__(ColdFinalisationNotCleanError)
    object.__setattr__(slotless, "__dict__", HostileDict({"result": result}))
    reset_spies()
    assert two_phase._finalisation_error_result(slotless) is None
    assert spy_calls() == []
    assert two_phase._error_values(object(), ("x",)) is None


# -------------------------------------------------------- T18: result model


def _independent_row(
    outcome: Outcome,
    refusal: Refusal | None,
    reason: R | None,
    owner: Own,
    digest: bool,
    checked: str | None,
) -> bool:
    """The contract's §2.7 outcome table, authored independently of ``_row_admits``."""
    owned = owner is not Own.NOT_OWNED
    if outcome is Outcome.PRE_ADVISORY_CONFORMANT:
        return (
            refusal is None
            and reason is None
            and owner is Own.OWNED_STOP_CONFIRMED
            and digest
            and checked == "conformant"
        )
    if outcome is Outcome.NOT_CONFORMANT:
        return refusal is None and owned and digest and checked in (None, "not_conformant")
    if outcome is Outcome.EVIDENCE_NOT_SEALED:
        return refusal is None and owned and not digest and checked is None
    if refusal is None or reason is not None or digest or checked is not None:
        return False
    if refusal in (Refusal.CHILD_ALREADY_RUNNING, Refusal.CHILD_STOP_UNCONFIRMED_AT_ENTRY):
        return owner is Own.NOT_OWNED
    if refusal in (
        Refusal.IDENTITY_NOT_FROZEN,
        Refusal.ADMISSION_REFUSED,
        Refusal.EVIDENCE_OPEN_FAILED,
    ):
        return owned
    return True


CHECKED: typing.Final[dict[str | None, conformance.ColdConformanceResult | None]] = {
    None: None,
    "conformant": conformance.ColdConformanceResult(
        policy_version=1,
        outcome=conformance.ColdConformanceOutcome.PRE_ADVISORY_CONFORMANT,
        findings=(),
    ),
    "not_conformant": conformance.ColdConformanceResult(
        policy_version=1,
        outcome=conformance.ColdConformanceOutcome.NOT_CONFORMANT,
        findings=(conformance.ColdConformanceFinding.TICKS_ABSENT,),
    ),
}


def test_4gc_t18_every_field_combination_matches_the_table() -> None:
    """T18: exactly the §2.7 rows construct; every other combination is refused."""
    admitted = refused = 0
    for outcome, refusal, reason, owner, digest, checked in itertools.product(
        Outcome, [None, *Refusal], [None, *R], Own, (False, True), CHECKED
    ):
        expected = _independent_row(outcome, refusal, reason, owner, digest, checked)
        try:
            two_phase.ColdTwoPhaseResult(
                outcome=outcome,
                start_refusal=refusal,
                termination_reason=reason,
                child_ownership=owner,
                manifest_sha256="a" * 64 if digest else None,
                conformance=CHECKED[checked],
            )
        except pydantic.ValidationError:
            assert not expected, (outcome, refusal, reason, owner, digest, checked)
            refused += 1
        else:
            assert expected, (outcome, refusal, reason, owner, digest, checked)
            admitted += 1
    assert admitted > 0 and refused > 0


def test_4gc_t18_forged_fields_are_refused_and_results_hold_no_text() -> None:
    """T18: non-exact digests, fabricated members and forged checker carriers refuse."""
    base: dict[str, object] = {
        "outcome": Outcome.NOT_CONFORMANT,
        "start_refusal": None,
        "termination_reason": R.PHASE_ABORTED,
        "child_ownership": Own.OWNED_STOP_CONFIRMED,
        "manifest_sha256": "a" * 64,
        "conformance": None,
    }
    assert two_phase.ColdTwoPhaseResult.model_validate(base).termination_reason is R.PHASE_ABORTED
    contradicted = forged(
        typing.cast(conformance.ColdConformanceResult, CHECKED["conformant"]),
        findings=(conformance.ColdConformanceFinding.TICKS_ABSENT,),
    )
    bad: list[dict[str, object]] = [
        {"manifest_sha256": "A" * 64},
        {"manifest_sha256": "a" * 63},
        {"manifest_sha256": HostileStr("a" * 64)},
        {"manifest_sha256": "g" * 64},
        {"outcome": "not_conformant"},
        {"outcome": object.__new__(Outcome)},
        {"child_ownership": object.__new__(Own)},
        {"termination_reason": object.__new__(R)},
        {"conformance": contradicted},
        {
            "conformance": forged(
                typing.cast(pydantic.BaseModel, CHECKED["not_conformant"]), policy_version=True
            )
        },
        {"extra": 1},
    ]
    for change in bad:
        with pytest.raises(pydantic.ValidationError):
            two_phase.ColdTwoPhaseResult.model_validate({**base, **change})
    refusal = object.__new__(Refusal)
    with pytest.raises(pydantic.ValidationError):
        two_phase.ColdTwoPhaseResult.model_validate(
            {
                **base,
                "outcome": Outcome.REFUSED_BEFORE_EVIDENCE,
                "start_refusal": refusal,
                "termination_reason": None,
                "manifest_sha256": None,
            }
        )
    valid = two_phase.ColdTwoPhaseResult.model_validate(base)
    with pytest.raises(pydantic.ValidationError):
        valid.outcome = Outcome.PRE_ADVISORY_CONFORMANT  # type: ignore[misc]
    assert set(two_phase.ColdTwoPhaseResult.model_fields) == {
        "outcome",
        "start_refusal",
        "termination_reason",
        "child_ownership",
        "manifest_sha256",
        "conformance",
    }
    for kind in (Outcome, Refusal, Own):
        assert issubclass(kind, enum.Enum) and not issubclass(kind, str)


def test_4gc_t18_a_forged_conformance_carrier_is_replaced_by_a_fresh_copy() -> None:
    """T18: an admitted checker result is stored as a fresh snapshot, never the caller's."""
    caller = typing.cast(conformance.ColdConformanceResult, CHECKED["not_conformant"])
    result = two_phase.ColdTwoPhaseResult(
        outcome=Outcome.NOT_CONFORMANT,
        start_refusal=None,
        termination_reason=None,
        child_ownership=Own.OWNED_STOP_CONFIRMED,
        manifest_sha256="b" * 64,
        conformance=caller,
    )
    assert result.conformance == caller and result.conformance is not caller


# ------------------------------------------------------------ T19: fences


SOURCE = Path(two_phase.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
_COLD = "roastpilot_agent.cold_characterisation."


def test_4gc_t19_imports_stay_inside_the_contract_allowlist() -> None:
    """T19: stdlib asyncio/enum/math/typing, pydantic and the allowed cold modules only."""
    plain: set[str] = set()
    modules: dict[str, set[str]] = {}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            modules.setdefault(node.module, set()).update(a.name for a in node.names)
    assert plain == {"asyncio", "enum", "math", "typing", "pydantic"}
    assert set(modules) == {
        _COLD + name
        for name in (
            "conformance",
            "engine",
            "evidence_builders",
            "evidence_lifecycle",
            "evidence_reader",
            "evidence_schema",
            "evidence_store",
            "identity",
            "mcp",
        )
    } | {"roastpilot_agent.mcp_client"}
    assert modules["roastpilot_agent.mcp_client"] <= {
        "EventCommandResult",
        "RuntimeConfigSnapshot",
        "ServerInfo",
        "StartRoastSessionResult",
    }
    mcp_names = modules[_COLD + "mcp"]
    assert {"finalisation_is_clean", "finalisation_has_required_safety_evidence"} <= mcp_names
    assert not {name for name in mcp_names if name.startswith("_")}
    assert "MCPDeviceConfig" not in str(modules) and "PosixPath" not in str(modules)


def test_4gc_t19_no_actuator_transport_logging_or_output_reach() -> None:
    """T19: no actuator names, ``call_tool``, logging, ``print``, os or process surface."""
    source = SOURCE.read_text(encoding="utf-8")
    for name in (
        "set_heat",
        "set_fan",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "mark_first_crack",
        "set_targets",
        "call_tool",
        "_MASKED_LEAVES",
        "recording_autocapture",
        "MCPDeviceConfig",
        "PosixPath",
        "subprocess",
        "socket",
        "logging",
    ):
        assert name not in source, name
    names = {node.id for node in ast.walk(TREE) if isinstance(node, ast.Name)}
    assert not names & {"print", "open", "os", "Path", "logging", "eval", "exec"}
    calls = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open_phase_evidence"
    ]
    assert len(calls) == 1
    dumps = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Attribute) and node.attr in {"model_dump", "model_copy"}
    ]
    function = next(
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "_admit_carrier"
    )
    inside = {id(node) for node in ast.walk(function)}
    assert dumps and all(id(node) in inside for node in dumps)


def test_4gc_t19_nothing_imports_the_orchestrator() -> None:
    """T19 reverse fence: no production module imports ``two_phase``."""
    package = SOURCE.parents[1]
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "two_phase" not in text or path == SOURCE, path


def _reachable(*roots: type[pydantic.BaseModel]) -> tuple[set[type], set[type]]:
    """Annotation-reachable model and enum classes from ``roots`` (independent walk)."""
    models: set[type] = set()
    enums: set[type] = set()
    stack: list[type[pydantic.BaseModel]] = list(roots)
    while stack:
        model = stack.pop()
        if model in models:
            continue
        models.add(model)
        for field in model.model_fields.values():
            pending: list[object] = [field.annotation]
            while pending:
                annotation = pending.pop()
                if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
                    stack.append(annotation)
                elif isinstance(annotation, type) and issubclass(annotation, enum.Enum):
                    enums.add(annotation)
                pending.extend(typing.get_args(annotation))
    return models, enums


@pytest.mark.parametrize(
    ("table", "roots"),
    [
        ("_IDENTITY", (ColdRunIdentity,)),
        ("_FINALISATION", (SessionFinalisationResult,)),
        (
            "_ENGINE",
            (engine.ColdPhaseCompleted, engine.ColdPhaseAborted, engine.ColdPhaseActivationRefused),
        ),
        ("_CHECKER", (conformance.ColdConformanceResult,)),
    ],
)
def test_4gc_t19_admission_tables_equal_the_reachable_classes(
    table: str, roots: tuple[type[pydantic.BaseModel], ...]
) -> None:
    """T19: each closed table is exactly its roots' annotation-reachable classes."""
    carrier = typing.cast(two_phase._Carrier, getattr(two_phase, table))
    models, enums = _reachable(*roots)
    assert {model for model, _names in carrier.models} == models
    assert len(carrier.models) == len(models)
    assert {kind for kind, _members in carrier.enums} == enums
    for model, names in carrier.models:
        assert names == tuple(model.model_fields)
    for kind, members in carrier.enums:
        assert members == tuple(kind)


# --------------------------------------------- T16: carrier admission (boundaries)

Behaviour = Callable[[typing.Any, typing.Any, Clock], typing.Awaitable[object]]


def scripted_observer(target: Phase, behaviour: Behaviour) -> Callable[..., typing.Any]:
    """An ``observe_cold_phase`` stand-in for ``target``: real header, session and hook.

    The other phase is observed by the real engine.  ``behaviour`` decides what the
    scripted phase retains and returns after a genuine activation hook call.
    """
    real = engine.observe_cold_phase

    async def observe(
        *,
        admission: engine.ColdPhaseAdmission,
        sink: typing.Any,
        mcp: typing.Any,
        host: typing.Any,
        clock: Clock,
        activation_hook: typing.Any,
    ) -> object:
        if admission.phase is not target:
            return await real(
                admission=admission,
                sink=sink,
                mcp=mcp,
                host=host,
                clock=clock,
                activation_hook=activation_hook,
            )
        sink.append(admission.header)
        await mcp.start_cold_session()
        verdict = activation_hook(
            session_id=SESSIONS[target],
            activated_monotonic=clock.monotonic(),
            activated_utc=clock.utc_now_iso(),
        )
        assert verdict is True
        return await behaviour(admission, sink, clock)

    return observe


def engine_forgeries() -> dict[str, object]:
    """Forged engine results for every admitted root."""
    completed, aborted, refused = engine_carriers()
    classification = typing.cast(tuple[pydantic.BaseModel, ...], raw_fields(aborted)["aborts"])[0]
    return {
        "hostile_session": forged(completed, session_id=HostileStr(SESSIONS[OFF])),
        "bool_tick_count": forged(completed, tick_count=True),
        "fabricated_domain": forged(
            aborted,
            aborts=(forged(classification, domain=object.__new__(schema.ColdAbortDomain)),),
        ),
        "refused_extra": with_dict(refused, {**raw_fields(refused), "x": 1}),
        "unknown_root": object(),
    }


@pytest.mark.asyncio
async def test_4gc_t16_forged_off_identities_never_reach_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16 boundary: a forged OFF identity is IDENTITY_NOT_FROZEN; admission never runs."""
    admissions: list[object] = []
    real_admit = engine.admit_cold_phase

    async def spy_admit(**kwargs: typing.Any) -> typing.Any:
        admissions.append(kwargs)
        return await real_admit(**kwargs)

    monkeypatch.setattr(two_phase, "admit_cold_phase", spy_admit)
    genuine = cold_identity(tmp_path, make_root(tmp_path, "forgery-source"))
    for index, (name, value) in enumerate(identity_forgeries(genuine).items()):
        world = World(tmp_path / f"run{index}")
        world.identities.values[OFF] = value
        reset_spies()
        result = await world.run()
        assert result.outcome is Outcome.REFUSED_BEFORE_EVIDENCE, name
        assert result.start_refusal is Refusal.IDENTITY_NOT_FROZEN, name
        assert result.child_ownership is Own.OWNED_STOP_CONFIRMED, name
        assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)], name
        assert spy_calls() == [] and admissions == [], name
        assert not (Path(world.root) / RUN_ID).exists(), name
        assert_no_canary(result)


@pytest.mark.asyncio
async def test_4gc_t16_a_forged_on_identity_is_refused_before_on_admission(
    tmp_path: Path,
) -> None:
    """T16 boundary: a forged ON identity is RECORDING_ON_IDENTITY_REFUSED, before admission."""
    world = World(tmp_path)
    world.identities.values[ON] = forged(world.ids[ON], run_id=HostileStr(RUN_ID))
    reset_spies()
    result = await world.run()
    assert_failed(world, result, R.RECORDING_ON_IDENTITY_REFUSED)
    assert spy_calls() == []
    assert ("get_server_info", ON) not in world.mcp.calls
    assert world.host.start_calls == 1
    assert world.events() == [
        ("recording_off", "phase_activated"),
        ("recording_off", "observation_window_elapsed"),
        ("recording_off", "finalisation_returned"),
        ("recording_off", "child_stopped"),
        ("recording_off", "child_started"),
        ("recording_off", "child_stopped"),
        ("recording_off", "run_terminated"),
    ]
    assert result.child_ownership is Own.OWNED_STOP_CONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [False, True], ids=["returned", "exception_result"])
@pytest.mark.parametrize(
    "name",
    [
        "str_subclass_session",
        "foreign_enum",
        "bool_for_int",
        "nested_hostile",
        "extra_key",
        "dict_subclass",
        "pydantic_extra",
        "subclass",
    ],
)
async def test_4gc_t16_forged_finalisation_results_fail_without_result(
    tmp_path: Path, name: str, raised: bool
) -> None:
    """T16 boundary: a forged returned or carried result records FAILED_WITHOUT_RESULT."""
    world = World(tmp_path)

    def finalise(phase: Phase, session: str) -> object:
        value = finalisation_forgeries(clean_result(phase, session))[name]
        if raised:
            return ColdFinalisationNotCleanError("m", typing.cast(SessionFinalisationResult, value))
        return value

    world.mcp.finalise = finalise
    reset_spies()
    result = await world.run()
    assert_failed(world, result, R.FINALISATION_FAILED)
    assert spy_calls() == []
    returned = world.event(OFF, "finalisation_returned")
    assert returned["finalisation_result"] == "failed_without_result"
    assert world.records("finalisation") == []
    assert ("configure", ON) not in world.child_ops()
    assert world.mcp.finalised == [SESSIONS[OFF]]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(engine_forgeries()))
async def test_4gc_t16_forged_engine_results_are_never_finalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """T16 boundary: a forged engine result after a genuine activation is not finalised."""

    async def behaviour(admission: typing.Any, sink: typing.Any, clock: Clock) -> object:
        clock.t += 1800.0
        return engine_forgeries()[name]

    monkeypatch.setattr(two_phase, "observe_cold_phase", scripted_observer(OFF, behaviour))
    world = World(tmp_path)
    reset_spies()
    result = await world.run()
    assert_failed(world, result, R.PHASE_FAILED_UNEXPECTEDLY)
    assert spy_calls() == []
    assert world.mcp.finalised == []
    aborted = world.event(OFF, "phase_aborted_not_finalised")
    assert aborted["session_id"] == SESSIONS[OFF]
    assert aborted["activation_deadline_exceeded"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checker",
    ["forged_conformant", "foreign_outcome", "raises", "wrong_type"],
)
async def test_4gc_t16_a_forged_or_failing_checker_is_never_conformant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checker: str
) -> None:
    """T16 boundary: an inadmissible checker result is NOT_CONFORMANT with no result."""
    genuine = typing.cast(conformance.ColdConformanceResult, CHECKED["conformant"])
    returned: dict[str, object] = {
        "forged_conformant": forged(genuine, policy_version=True),
        "foreign_outcome": forged(genuine, outcome=ForeignOutcome.PRE_ADVISORY_CONFORMANT),
        "raises": RuntimeError(CANARY),
        "wrong_type": {"outcome": "pre_advisory_conformant"},
    }

    def check(run: object) -> object:
        del run
        return produce(returned[checker])

    monkeypatch.setattr(two_phase, "check_pre_advisory_conformance", check)
    world = World(tmp_path)
    result = await world.run()
    assert result.outcome is Outcome.NOT_CONFORMANT
    assert result.conformance is None and result.termination_reason is None
    assert result.child_ownership is Own.OWNED_STOP_CONFIRMED
    assert result.manifest_sha256 is not None and world.manifest_exists()
    assert world.events() == GRAMMAR
    assert_no_canary(result)


@pytest.mark.asyncio
async def test_4gc_t16_a_reload_failure_is_never_conformant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16 boundary: a reader refusal after a sealed clean run is NOT_CONFORMANT."""

    def refuse(*args: object, **kwargs: object) -> typing.NoReturn:
        raise store.ColdEvidenceStoreError(store.ColdEvidenceStoreFailure.HEADER_MISSING)

    monkeypatch.setattr(two_phase, "read_retained_run_v2", refuse)
    world = World(tmp_path)
    result = await world.run()
    assert result.outcome is Outcome.NOT_CONFORMANT and result.conformance is None
    assert result.manifest_sha256 is not None


def make_run(world: World) -> two_phase._TwoPhaseRun:
    """One private run object over the world's ports (for direct hook and step calls)."""
    return two_phase._TwoPhaseRun(
        root=world.admitted,
        mcp=world.mcp,
        child=world.child,
        identities=world.identities,
        host=world.host,
        clock=world.clock,
    )


@pytest.mark.parametrize("hook", ["_off_hook", "_on_hook"])
def test_4gc_t16_forged_hook_arguments_are_refused_before_any_use(
    tmp_path: Path, hook: str
) -> None:
    """T16 boundary: inadmissible hook arguments refuse with no clock or sink call."""
    world = World(tmp_path)
    run = make_run(world)
    good: dict[str, object] = {
        "session_id": SESSIONS[OFF],
        "activated_monotonic": T0,
        "activated_utc": utc_at(T0),
    }
    bad: list[dict[str, object]] = [
        {"session_id": HostileStr(SESSIONS[OFF])},
        {"session_id": ""},
        {"session_id": "   "},
        {"session_id": "x" * 2049},
        {"session_id": None},
        {"activated_monotonic": True},
        {"activated_monotonic": 100},
        {"activated_monotonic": math.nan},
        {"activated_monotonic": -1.0},
        {"activated_utc": HostileStr(utc_at(T0))},
        {"activated_utc": "not-a-time"},
        {"activated_utc": "2026-09-26T12:00:00+01:00"},
    ]
    for change in bad:
        reset_spies()
        with pytest.raises(two_phase._HookFailed):
            getattr(run, hook)(**{**good, **change})
        assert world.clock.samples == 0, change
        assert spy_calls() == [], change
        assert run._sessions == {} and run._ledger.floor is None, change


# ----------------------------------------------------------- clock ledger


class CountingClock:
    """Returns scripted (monotonic, utc) pairs and counts each call."""

    def __init__(self, *pairs: tuple[object, object]) -> None:
        self.pairs = list(pairs)
        self.monotonic_calls = 0
        self.utc_calls = 0
        self.current: tuple[object, object] = (0.0, utc_at(0.0))

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        self.current = self.pairs.pop(0)
        if isinstance(self.current[0], BaseException):
            raise self.current[0]
        return typing.cast(float, self.current[0])

    def utc_now_iso(self) -> str:
        self.utc_calls += 1
        if isinstance(self.current[1], BaseException):
            raise self.current[1]
        return typing.cast(str, self.current[1])

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - never awaited.
        del seconds


def test_4gc_ledger_admits_ties_and_refuses_regressions_without_resampling() -> None:
    """Clock floor: ties admitted, a regression refused once with the floor unchanged."""
    ledger = two_phase._ClockFloor()
    clock = CountingClock(
        (10.0, utc_at(10.0)),
        (10.0, utc_at(10.0)),
        (9.5, utc_at(9.5)),
        (RuntimeError(CANARY), None),
        (11.0, RuntimeError(CANARY)),
        (math.nan, utc_at(1.0)),
        (12.0, "not-a-time"),
        (12.0, utc_at(12.0)),
    )
    assert ledger.sample(clock) == (10.0, utc_at(10.0))
    assert ledger.sample(clock) == (10.0, utc_at(10.0))
    for _ in range(5):
        assert ledger.sample(clock) is None
        assert ledger.floor == 10.0
    assert clock.monotonic_calls == 7
    assert ledger.sample(clock) == (12.0, utc_at(12.0))
    assert ledger.floor == 12.0


@pytest.mark.parametrize(
    ("monotonic", "utc", "admitted"),
    [
        (12.0, utc_at(12.0), True),
        (math.nextafter(12.0, -math.inf), utc_at(11.0), False),
        (13, utc_at(13.0), False),
        (True, utc_at(13.0), False),
        (-1.0, utc_at(13.0), False),
        (math.inf, utc_at(13.0), False),
        (13.0, "2026-09-26T12:00:00+01:00", False),
        (13.0, HostileStr(utc_at(13.0)), False),
    ],
    ids=repr,
)
def test_4gc_ledger_facts_are_admitted_exactly(
    monotonic: object, utc: object, admitted: bool
) -> None:
    """Clock floor: an engine fact must be an exact admitted pair at or after the floor."""
    ledger = two_phase._ClockFloor()
    assert ledger.observe_fact(12.0, utc_at(12.0)) is not None
    reset_spies()
    fact = ledger.observe_fact(monotonic, utc)
    assert (fact is not None) is admitted
    assert ledger.floor == (monotonic if admitted else 12.0)
    assert spy_calls() == []


# ------------------------------------------------- T15: guarded run sink


class WriterProxy:
    """Delegates to a real writer and records every call; ``fail`` injects a fault."""

    def __init__(self, writer: store.ColdEvidenceWriter) -> None:
        self.writer = writer
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.digest: object = None

    def _call(self, name: str, action: Callable[[], object]) -> typing.Any:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(CANARY)
        return action()

    def append(self, record: schema.ColdEvidenceRecord) -> None:
        self._call("append", lambda: self.writer.append(record))

    def append_lifecycle(self, record: lifecycle.ColdLifecycleRecord) -> None:
        self._call("append_lifecycle", lambda: self.writer.append_lifecycle(record))

    def seal(self) -> typing.Any:
        sealed = self._call("seal", self.writer.seal)
        if self.digest is not None:
            return types_namespace(manifest_sha256=self.digest)
        return sealed

    def close(self) -> None:
        self._call("close", self.writer.close)


def types_namespace(**values: object) -> typing.Any:
    """A plain attribute bag."""
    return types.SimpleNamespace(**values)


class SinkRig:
    """A guarded sink over a real writer with genuine headers for both phases."""

    def __init__(self, tmp_path: Path) -> None:
        self.world = World(tmp_path)
        self.proxy = WriterProxy(store.open_run(self.world.admitted, RUN_ID))
        self.sink = two_phase._RunSink(typing.cast(store.ColdEvidenceWriter, self.proxy))
        self.headers = {
            phase: builders.build_run_header(
                identity=self.world.ids[phase],
                phase=phase,
                recorded_at_utc=utc_at(1.0),
                monotonic_seconds=1.0,
            )
            for phase in (OFF, ON)
        }

    def lc(
        self, phase: Phase, event: lifecycle.ColdLifecycleEvent, **fields: typing.Any
    ) -> typing.Any:
        return builders.build_lifecycle_record(
            header=self.headers[phase],
            sequence=self.sink.next_sequence,
            event=event,
            event_utc=utc_at(2000.0),
            event_monotonic_seconds=2000.0,
            recorded_at_utc=utc_at(2000.0),
            monotonic_seconds=2000.0,
            **fields,
        )

    def tick(self, phase: Phase) -> schema.ColdTickRecord:
        return builders.build_tick_record(
            header=self.headers[phase],
            tick=0,
            recorded_at_utc=utc_at(2.0),
            monotonic_seconds=2.0,
            observation=conforming_tick(phase, 0),
        )

    def host(self, phase: Phase) -> schema.ColdHostRecord:
        return builders.build_host_record(
            header=self.headers[phase],
            sample=safe_host_sample(),
            recorded_at_utc=utc_at(2.0),
            monotonic_seconds=2.0,
        )

    def abort(self, phase: Phase) -> schema.ColdAbortRecord:
        return builders.build_abort_record(
            header=self.headers[phase],
            domain=schema.ColdAbortDomain.OPERATOR,
            reason=schema.ColdOperatorAbortReason.OPERATOR_STOP,
            recorded_at_utc=utc_at(2.0),
            monotonic_seconds=2.0,
        )

    def fin(self, phase: Phase) -> schema.ColdFinalisationRecord:
        return builders.build_finalisation_record(
            header=self.headers[phase],
            result=clean_result(phase, SESSIONS[phase]),
            recorded_at_utc=utc_at(2001.0),
            monotonic_seconds=2001.0,
        )

    def elapsed(self, phase: Phase) -> typing.Any:
        return self.lc(
            phase,
            Event.OBSERVATION_WINDOW_ELAPSED,
            session_id=SESSIONS[phase],
            scheduled_end_monotonic=1900.0,
            tick_count=1,
        )

    def returned(
        self, phase: Phase, result: lifecycle.ColdLifecycleFinalisationResult
    ) -> typing.Any:
        return self.lc(
            phase,
            Event.FINALISATION_RETURNED,
            session_id=SESSIONS[phase],
            finalisation_result=result,
        )

    def aborted(self, phase: Phase) -> typing.Any:
        return self.lc(phase, Event.PHASE_ABORTED_NOT_FINALISED, activation_deadline_exceeded=False)

    def started(self) -> typing.Any:
        return self.lc(
            OFF, Event.CHILD_STARTED, child_start=lifecycle.ColdLifecycleChildStart.STARTED
        )

    def terminal(self, phase: Phase) -> typing.Any:
        return self.lc(
            phase, Event.RUN_TERMINATED, termination=lifecycle.ColdRunTermination.COMPLETED
        )

    def bind_off(self) -> None:
        self.sink.append(self.headers[OFF])

    def bind_on(self) -> None:
        self.bind_off()
        self.sink.append_lifecycle(self.started())
        self.sink.append(self.headers[ON])

    def refused(self, action: Callable[[], object]) -> None:
        """Assert one fixed refusal that poisons and never reaches the writer."""
        before = list(self.proxy.calls)
        with pytest.raises(two_phase.ColdRunSinkRefusedError) as caught:
            action()
        assert caught.value.args == ("Cold run evidence refused.",)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert CANARY not in repr(caught.value)
        assert self.sink.poisoned and not self.sink.usable
        assert self.proxy.calls == before


Result = lifecycle.ColdLifecycleFinalisationResult


def test_4gc_t15_a_full_grammar_passes_the_guard_and_seals(tmp_path: Path) -> None:
    """T15 positive control: the success grammar with ticks, hosts and results seals."""
    rig = SinkRig(tmp_path)
    sink = rig.sink
    for phase in (OFF, ON):
        if phase is OFF:
            rig.bind_off()
        else:
            sink.append_lifecycle(rig.started())
            sink.append(rig.headers[ON])
        sink.append_lifecycle(rig.lc(phase, Event.PHASE_ACTIVATED, session_id=SESSIONS[phase]))
        sink.append(rig.tick(phase))
        sink.append(rig.host(phase))
        assert not sink.finalisation_eligible(phase)
        sink.append_lifecycle(rig.elapsed(phase))
        assert sink.finalisation_eligible(phase)
        sink.append(rig.fin(phase))
        sink.append_lifecycle(rig.returned(phase, Result.CLEAN_RECORDED))
        assert not sink.finalisation_eligible(phase)
    sink.append_lifecycle(rig.terminal(ON))
    digest = sink.seal()
    assert len(digest) == 64 and not sink.poisoned
    assert rig.proxy.calls.count("seal") == 1


def test_4gc_t15_r1_a_poisoned_sink_may_only_be_closed(tmp_path: Path) -> None:
    """R1: after poison every append and seal is refused without the writer; close works."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.refused(lambda: rig.sink.append(rig.headers[ON]))
    rig.refused(lambda: rig.sink.append(rig.tick(OFF)))
    rig.refused(
        lambda: rig.sink.append_lifecycle(
            rig.lc(OFF, Event.PHASE_ACTIVATED, session_id=SESSIONS[OFF])
        )
    )
    rig.refused(rig.sink.seal)
    rig.sink.close()
    assert rig.proxy.calls == ["append", "close"]


def test_4gc_t15_r2_nothing_appends_after_the_terminal(tmp_path: Path) -> None:
    """R2: after RUN_TERMINATED every append is refused."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append_lifecycle(rig.terminal(OFF))
    assert rig.sink.terminated and not rig.sink.usable
    rig.refused(lambda: rig.sink.append(rig.tick(OFF)))
    rig2 = SinkRig(tmp_path / "second")
    rig2.bind_off()
    rig2.sink.append_lifecycle(rig2.terminal(OFF))
    rig2.refused(lambda: rig2.sink.append_lifecycle(rig2.started()))


@pytest.mark.parametrize("case", ["no_header", "not_terminated", "terminal_without_header"])
def test_4gc_t15_r3_seal_and_terminal_require_a_bound_header(tmp_path: Path, case: str) -> None:
    """R3: seal needs a terminal and a header; a terminal needs a header."""
    rig = SinkRig(tmp_path)
    if case == "no_header":
        rig.refused(rig.sink.seal)
    elif case == "not_terminated":
        rig.bind_off()
        rig.refused(rig.sink.seal)
    else:
        rig.refused(lambda: rig.sink.append_lifecycle(rig.terminal(OFF)))


@pytest.mark.parametrize("case", ["on_first", "off_twice", "on_before_child_started", "on_twice"])
def test_4gc_t15_r4_headers_bind_only_in_order(tmp_path: Path, case: str) -> None:
    """R4: OFF first; ON only after an OFF CHILD_STARTED and only once."""
    rig = SinkRig(tmp_path)
    if case == "on_first":
        rig.refused(lambda: rig.sink.append(rig.headers[ON]))
    elif case == "off_twice":
        rig.bind_off()
        rig.refused(lambda: rig.sink.append(rig.headers[OFF]))
    elif case == "on_before_child_started":
        rig.bind_off()
        rig.refused(lambda: rig.sink.append(rig.headers[ON]))
    else:
        rig.bind_on()
        assert rig.sink.phase is ON
        rig.refused(lambda: rig.sink.append(rig.headers[ON]))


@pytest.mark.parametrize("case", ["v1_before_header", "v1_prior_phase", "lifecycle_prior_phase"])
def test_4gc_t15_r5_records_belong_to_the_current_phase(tmp_path: Path, case: str) -> None:
    """R5: no record before a header, and no prior-phase record after the switch."""
    rig = SinkRig(tmp_path)
    if case == "v1_before_header":
        rig.refused(lambda: rig.sink.append(rig.tick(OFF)))
        return
    rig.bind_on()
    if case == "v1_prior_phase":
        rig.refused(lambda: rig.sink.append(rig.tick(OFF)))
    else:
        rig.refused(
            lambda: rig.sink.append_lifecycle(
                rig.lc(
                    OFF, Event.CHILD_STOPPED, child_stop=lifecycle.ColdLifecycleChildStop.CONFIRMED
                )
            )
        )


def test_4gc_t15_r6_advisory_evidence_is_refused(tmp_path: Path) -> None:
    """R6: pre-advisory policy refuses any advisory record."""
    from tests.test_cold_characterisation_conformance import advisory_for_at

    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.refused(lambda: rig.sink.append(advisory_for_at(rig.headers[OFF], 2.0)))


@pytest.mark.parametrize("closer", ["elapsed", "aborted"])
@pytest.mark.parametrize("kind", ["tick", "host", "abort"])
def test_4gc_t15_r7_no_observation_after_the_window_closes(
    tmp_path: Path, closer: str, kind: str
) -> None:
    """R7: ticks, hosts and aborts are refused once the window is closed."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append_lifecycle(rig.elapsed(OFF) if closer == "elapsed" else rig.aborted(OFF))
    record = {"tick": rig.tick, "host": rig.host, "abort": rig.abort}[kind](OFF)
    rig.refused(lambda: rig.sink.append(record))


@pytest.mark.parametrize(
    "case", ["before_elapsed", "second", "after_failed_return", "after_aborted"]
)
def test_4gc_t15_r8_finalisation_records_need_an_open_elapsed_phase(
    tmp_path: Path, case: str
) -> None:
    """R8: one finalisation record, only after ELAPSED and before any return or abort fact."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    if case == "before_elapsed":
        rig.refused(lambda: rig.sink.append(rig.fin(OFF)))
        return
    if case == "after_aborted":
        rig.sink.append_lifecycle(rig.aborted(OFF))
        rig.refused(lambda: rig.sink.append(rig.fin(OFF)))
        return
    rig.sink.append_lifecycle(rig.elapsed(OFF))
    if case == "second":
        rig.sink.append(rig.fin(OFF))
    else:
        rig.sink.append_lifecycle(rig.returned(OFF, Result.FAILED_WITHOUT_RESULT))
    rig.refused(lambda: rig.sink.append(rig.fin(OFF)))


@pytest.mark.parametrize(
    ("with_record", "result"),
    [
        (False, Result.CLEAN_RECORDED),
        (False, Result.NOT_CLEAN_RECORDED),
        (True, Result.FAILED_WITHOUT_RESULT),
        (True, Result.RECORD_NOT_RETAINED),
    ],
)
def test_4gc_t15_r9_returned_results_match_the_retained_record(
    tmp_path: Path, with_record: bool, result: lifecycle.ColdLifecycleFinalisationResult
) -> None:
    """R9: recorded results need the v1 record; the other two need its absence."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append_lifecycle(rig.elapsed(OFF))
    if with_record:
        rig.sink.append(rig.fin(OFF))
    rig.refused(lambda: rig.sink.append_lifecycle(rig.returned(OFF, result)))


def test_4gc_t15_r10_no_abort_fact_after_a_finalisation_record(tmp_path: Path) -> None:
    """R10: PHASE_ABORTED_NOT_FINALISED is refused once a finalisation record exists."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append_lifecycle(rig.elapsed(OFF))
    rig.sink.append(rig.fin(OFF))
    rig.refused(lambda: rig.sink.append_lifecycle(rig.aborted(OFF)))


def test_4gc_t15_r11_no_elapsed_fact_after_a_retained_abort(tmp_path: Path) -> None:
    """R11: OBSERVATION_WINDOW_ELAPSED is refused after any retained v1 abort."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append(rig.abort(OFF))
    assert rig.sink.abort_retained(OFF) and not rig.sink.abort_retained(ON)
    rig.refused(lambda: rig.sink.append_lifecycle(rig.elapsed(OFF)))


def test_4gc_t15_forged_records_are_refused_before_the_writer(tmp_path: Path) -> None:
    """T15: a forged record fails the settled snapshot and never reaches the writer."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.refused(lambda: rig.sink.append(forged(rig.tick(OFF), tick=True)))
    rig2 = SinkRig(tmp_path / "second")
    rig2.bind_off()
    rig2.refused(
        lambda: rig2.sink.append_lifecycle(forged(rig2.terminal(OFF), sequence=HostileStr("0")))
    )


@pytest.mark.parametrize("method", ["append", "append_lifecycle", "seal"])
def test_4gc_t15_writer_faults_poison_with_the_fixed_error(tmp_path: Path, method: str) -> None:
    """T15: any writer exception poisons the guard and is replaced by the fixed refusal."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    if method == "seal":
        rig.sink.append_lifecycle(rig.terminal(OFF))
    rig.proxy.fail.add(method)
    action: dict[str, Callable[[], object]] = {
        "append": lambda: rig.sink.append(rig.tick(OFF)),
        "append_lifecycle": lambda: rig.sink.append_lifecycle(rig.elapsed(OFF)),
        "seal": rig.sink.seal,
    }
    with pytest.raises(two_phase.ColdRunSinkRefusedError) as caught:
        action[method]()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert CANARY not in repr(caught.value)
    assert rig.sink.poisoned
    assert rig.proxy.calls[-1] == method


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, None, HostileStr("a" * 64)], ids=repr)
def test_4gc_t15_a_seal_without_an_admitted_digest_is_refused(
    tmp_path: Path, digest: object
) -> None:
    """T15: a seal returning anything but an exact lowercase digest is refused."""
    rig = SinkRig(tmp_path)
    rig.bind_off()
    rig.sink.append_lifecycle(rig.terminal(OFF))
    rig.proxy.digest = digest if digest is not None else 12345
    reset_spies()
    with pytest.raises(two_phase.ColdRunSinkRefusedError):
        rig.sink.seal()
    assert rig.sink.poisoned and spy_calls() == []


def test_4gc_t15_a_close_fault_is_absorbed(tmp_path: Path) -> None:
    """T15: closing never raises; the sink stays poisoned."""
    rig = SinkRig(tmp_path)
    rig.proxy.fail.add("close")
    rig.sink.close()
    assert rig.sink.poisoned and rig.proxy.calls == ["close"]


GUARD_FLAGS: typing.Final = (
    "abort_seen",
    "aborted_not_finalised",
    "finalisation_appended",
    "finalisation_returned",
)


def eligible_rig(tmp_path: Path) -> SinkRig:
    """A sink whose OFF guard is set directly to the eligible baseline."""
    rig = SinkRig(tmp_path)
    rig.sink.phase = OFF
    guard = rig.sink._guards[OFF]
    guard.header = True
    guard.elapsed = True
    guard.window_closed = True
    return rig


@pytest.mark.parametrize(
    "toggle",
    [*GUARD_FLAGS, "poisoned", "terminated", "no_header", "no_elapsed", "other_phase", None],
)
def test_4gc_t21_eligibility_truth_table(tmp_path: Path, toggle: str | None) -> None:
    """Isolated eligibility: each failing predicate alone refuses; the baseline admits."""
    rig = eligible_rig(tmp_path)
    guard = rig.sink._guards[OFF]
    if toggle in GUARD_FLAGS:
        setattr(guard, typing.cast(str, toggle), True)
    elif toggle == "poisoned":
        rig.sink.poisoned = True
    elif toggle == "terminated":
        rig.sink.terminated = True
    elif toggle == "no_header":
        guard.header = False
    elif toggle == "no_elapsed":
        guard.elapsed = False
    elif toggle == "other_phase":
        rig.sink.phase = ON
    assert rig.sink.finalisation_eligible(OFF) is (toggle is None)


@pytest.mark.parametrize("toggle", [*GUARD_FLAGS, "no_elapsed", None])
def test_4gc_t21_r8_truth_table(tmp_path: Path, toggle: str | None) -> None:
    """Isolated R8: a finalisation record is admitted only from the eligible baseline."""
    rig = eligible_rig(tmp_path)
    guard = rig.sink._guards[OFF]
    if toggle in GUARD_FLAGS:
        setattr(guard, typing.cast(str, toggle), True)
    elif toggle == "no_elapsed":
        guard.elapsed = False
    snapshot = schema.validate_record(rig.fin(OFF))
    assert rig.sink._admits(snapshot) is (toggle is None)


@pytest.mark.parametrize("abort_seen", [False, True])
def test_4gc_t21_r11_truth_table(tmp_path: Path, abort_seen: bool) -> None:
    """Isolated R11: ELAPSED is admitted exactly when no abort was retained."""
    rig = SinkRig(tmp_path)
    rig.sink.phase = OFF
    rig.sink._guards[OFF].abort_seen = abort_seen
    snapshot = lifecycle.validate_lifecycle_record(rig.elapsed(OFF))
    assert rig.sink._admits_lifecycle(snapshot) is (not abort_seen)


# --------------------------------------------- T12: writer faults in a run


class WriterSpy:
    """Class-level writer spy: one injected fault, then counts every later call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, trigger: Callable[[str, object], bool]):
        self.calls: list[tuple[str, str, bool]] = []
        self.armed = False
        self.trigger = trigger
        original_chunk = store._write_chunk

        def chunk(descriptor: int, data: memoryview) -> int:
            if self.armed:
                self.armed = False
                raise OSError(CANARY)
            return original_chunk(descriptor, data)

        monkeypatch.setattr(store, "_write_chunk", chunk)
        for name in ("append", "append_lifecycle", "seal"):
            monkeypatch.setattr(store.ColdEvidenceWriter, name, self._wrap(name))

    def _wrap(self, name: str) -> Callable[..., typing.Any]:
        original = getattr(store.ColdEvidenceWriter, name)
        spy = self

        def call(writer: store.ColdEvidenceWriter, *args: typing.Any) -> typing.Any:
            label = self.label(name, args)
            if spy.trigger(name, args[0] if args else None) and not any(
                failed is False for _n, _l, failed in spy.calls
            ):
                spy.armed = True
            try:
                value = original(writer, *args)
            except Exception:
                spy.calls.append((name, label, False))
                raise
            spy.calls.append((name, label, True))
            return value

        return call

    @staticmethod
    def label(name: str, args: tuple[typing.Any, ...]) -> str:
        if not args:
            return name
        record = args[0]
        if name == "append_lifecycle":
            return f"{record.phase.value}:{record.event.value}"
        return f"{record.phase.value}:{record.stream}"

    def after_failure(self) -> list[tuple[str, str, bool]]:
        index = next(i for i, call in enumerate(self.calls) if call[2] is False)
        return self.calls[index + 1 :]


def _is(
    name: str, stream: str | None = None, event: str | None = None
) -> Callable[[str, object], bool]:
    def trigger(method: str, record: object) -> bool:
        if method != name:
            return False
        if stream is not None:
            return getattr(record, "stream", None) == stream
        if event is not None:
            return getattr(getattr(record, "event", None), "value", None) == event
        return True

    return trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "trigger"),
    [
        ("v1_tick", _is("append", stream="tick")),
        ("hook_lifecycle", _is("append_lifecycle", event="phase_activated")),
        ("elapsed_lifecycle", _is("append_lifecycle", event="observation_window_elapsed")),
        ("seal", _is("seal")),
        ("off_header", _is("append", stream="header")),
    ],
)
async def test_4gc_t12_a_write_fault_leaves_unsealed_evidence_and_stops_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    trigger: Callable[[str, object], bool],
) -> None:
    """T12: one failed writer call, no later writer call, no digest, child stopped."""
    spy = WriterSpy(monkeypatch, trigger)
    world = World(tmp_path)
    result = await world.run()
    assert result.outcome is Outcome.EVIDENCE_NOT_SEALED
    assert result.manifest_sha256 is None and result.conformance is None
    assert result.child_ownership is Own.OWNED_STOP_CONFIRMED
    assert [call for call in spy.calls if call[2] is False] != []
    assert len([call for call in spy.calls if call[2] is False]) == 1
    assert spy.after_failure() == []
    # A seal failing mid-write may leave a partial manifest; it never yields a digest.
    assert world.manifest_exists() is (case == "seal")
    assert_no_canary(result)
    if case == "off_header":
        assert spy.calls == [("append", "recording_off:header", False)]
        assert world.records("lifecycle") == []
    if case == "seal":
        assert world.events() == GRAMMAR
        assert world.child_ops() == NORMAL_CHILD_OPS
        assert result.termination_reason is None
    else:
        assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)]
        assert world.mcp.finalised == []
        assert not any(event == "run_terminated" for _phase, event in world.events())


# ---------------------------------------------- T6: orchestrator clock floor


def assert_lifecycle_ordered(world: World) -> None:
    """Lifecycle recording instants never regress and no event follows its recording."""
    records = world.lifecycle()
    recorded = [record["monotonic_seconds"] for record in records]
    assert recorded == sorted(recorded)
    assert all(r["event_monotonic_seconds"] <= r["monotonic_seconds"] for r in records)


@pytest.mark.asyncio
async def test_4gc_t6_a_regressed_finalisation_return_is_clock_invalid(tmp_path: Path) -> None:
    """T6: ``fr`` below the floor records nothing from it and starts nothing new."""
    world = World(tmp_path)
    world.mcp.before["finalise_session:recording_off"] = lambda: world.clock.pending.append(
        "regress"
    )
    result = await world.run()
    assert_failed(world, result, R.CLOCK_INVALID)
    assert world.records("finalisation") == []
    assert ("recording_off", "finalisation_returned") not in world.events()
    assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)]
    assert result.child_ownership is Own.OWNED_STOP_CONFIRMED
    assert_lifecycle_ordered(world)


@pytest.mark.asyncio
async def test_4gc_t6_a_regressed_on_activation_is_clock_invalid(tmp_path: Path) -> None:
    """T6: an ON activation the engine admits but the run floor refuses reads nothing."""
    world = World(tmp_path)

    def advance() -> None:
        world.clock.t += 10.0

    world.host.before_start[1] = advance
    world.mcp.before["mark_beans_added:recording_on"] = lambda: world.clock.pending.append(
        "regress:5"
    )
    result = await world.run()
    assert_failed(world, result, R.CLOCK_INVALID)
    assert ("get_roast_state", ON) not in world.mcp.calls
    assert world.mcp.finalised == [SESSIONS[OFF]]
    assert ("recording_on", "phase_activated") not in world.events()
    aborted = world.event(ON, "phase_aborted_not_finalised")
    assert aborted["session_id"] == SESSIONS[ON]
    assert_lifecycle_ordered(world)


@pytest.mark.asyncio
async def test_4gc_t6_a_regressed_completion_is_clock_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: a completion below the floor is not recorded and the phase is not finalised."""

    async def behaviour(admission: typing.Any, sink: typing.Any, clock: Clock) -> object:
        clock.t += 1800.0
        return engine.ColdPhaseCompleted(
            session_id=SESSIONS[OFF],
            observation_end_monotonic=T0 - 10.0,
            observation_end_utc=utc_at(T0 - 10.0),
            tick_count=3,
        )

    monkeypatch.setattr(two_phase, "observe_cold_phase", scripted_observer(OFF, behaviour))
    world = World(tmp_path)
    result = await world.run()
    assert_failed(world, result, R.CLOCK_INVALID)
    assert world.mcp.finalised == []
    assert ("recording_off", "observation_window_elapsed") not in world.events()
    assert world.event(OFF, "phase_aborted_not_finalised")["session_id"] == SESSIONS[OFF]
    assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)]
    assert_lifecycle_ordered(world)


@pytest.mark.asyncio
async def test_4gc_t6_no_terminal_without_an_admitted_instant_means_no_seal(
    tmp_path: Path,
) -> None:
    """T6: an inadmissible terminal sample leaves no terminal, no seal and no digest."""
    world = World(tmp_path)
    world.child.before_stop[1] = lambda: world.clock.pending.extend(["", "", "raise"])
    result = await world.run()
    assert result.outcome is Outcome.EVIDENCE_NOT_SEALED
    assert result.termination_reason is R.CLOCK_INVALID
    assert result.child_ownership is Own.OWNED_STOP_CONFIRMED
    assert world.events() == GRAMMAR[:-1]
    assert not world.manifest_exists()
