"""Hardware-free tests for the retained-tick observer port (#954 slice 6, unit U2).

T-D5 runs first: schema-valid ticks at the evidence-schema limits must pass the
existing strict ``validate_record`` admission and then the existing carrier
snapshot with the exact planned tick carrier table.  All data is synthetic.
"""

# pyright: reportPrivateUsage=false

import asyncio
import enum
import gc
import inspect
import re
import typing
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from roastpilot_agent import cold_observation_stream as stream
from roastpilot_agent.cold_characterisation import advisory_conformance, engine, two_phase
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from tests.test_cold_characterisation_evidence_builders import (
    RUN_ID,
    device_state,
    observation,
    roast_fan_state,
    session_metadata,
)
from tests.test_cold_characterisation_two_phase import (
    ABORT_PAIRS,
    DRIVER,
    OFF,
    ON,
    SESSIONS,
    CapturingSink,
    Clock,
    Outcome,
    R,
    World,
    audio,
    conforming_tick,
    make_run,
    retained_abort,
    scripted_observer,
    utc_at,
)

RUN_ID_PROBE = "20260926T120000Z-probe-u2"
DIGEST_PROBE = "ab" * 32

#: The exact planned tick carrier table (contract §2.1(2)), built from trusted classes.
PLANNED_TICK = two_phase._carrier(
    (
        schema.ColdTickRecord,
        schema.ColdTickDeviceEvidence,
        schema.ColdTickRoastFanEvidence,
        schema.ColdTickSessionEvidence,
        schema.ColdTickAudioSample,
    ),
    (schema.ColdPhaseKind, schema.ColdTickRoastFanOutcome, schema.ColdTickSessionPhase),
)


def probe_audio(reason: str | None = None) -> schema.ColdTickAudioSample:
    """A synthetic strict audio sample."""
    return schema.ColdTickAudioSample(
        mode="audio",
        status="pending",
        detected_at_utc=None,
        detected_monotonic_seconds=None,
        allow_manual_override=False,
        reason=reason,
        audio_running=True,
        queued_window_count=0,
        emitted_window_count=1,
        dropped_window_count=0,
        processed_window_count=1,
        mic_peak_dbfs=-30.5,
        mic_rms_dbfs=-42.25,
        overflow_count_last_minute=0,
        estimated_lost_audio_ms_last_minute=0.0,
        total_overflow_count=0,
        max_consecutive_overflow_count=0,
        last_inference_duration_ms=12.5,
        max_inference_duration_ms=20.0,
        inference_overrun_count=0,
    )


def probe_tick(
    *,
    vendor: dict[str, typing.Any] | None = None,
    extra: dict[str, typing.Any] | None = None,
    reason: str | None = None,
    driver: str = "probe-driver",
    session_id: str = "probe-session",
    recorded_at_utc: str = "2026-09-26T12:00:02+00:00",
    bean: float | None = 21.5,
    heat: int = 0,
) -> schema.ColdTickRecord:
    """A synthetic tick whose bounded parts are supplied by the case."""
    return schema.ColdTickRecord(
        schema_version=1,
        stream="tick",
        run_id=RUN_ID_PROBE,
        phase=schema.ColdPhaseKind.RECORDING_OFF,
        recorded_at_utc=recorded_at_utc,
        monotonic_seconds=2.0,
        identity_sha256=DIGEST_PROBE,
        tick=0,
        device=schema.ColdTickDeviceEvidence(
            driver=driver,
            connected=True,
            bean_temp_c=bean,
            env_temp_c=22.0,
            heat_level_percent=heat,
            fan_level_percent=0,
            cooling_on=False,
            raw_vendor_data={} if vendor is None else vendor,
        ),
        roast_fan=schema.ColdTickRoastFanEvidence(
            outcome=schema.ColdTickRoastFanOutcome.OBSERVED, roast_fan_level_percent=0
        ),
        session=schema.ColdTickSessionEvidence(
            session_id=session_id,
            active=True,
            session_purpose="cold_characterisation",
            phase=schema.ColdTickSessionPhase.PRE_ROAST,
            elapsed_monotonic_seconds=1.0,
        ),
        audio=probe_audio(reason),
        raw_audio_extra={} if extra is None else extra,
    )


def _refusal(record: schema.ColdTickRecord) -> schema.ColdEvidenceFailure | None:
    """The closed schema refusal of one record, or ``None`` when admitted."""
    try:
        schema.validate_record(record)
    except schema.ColdEvidenceError as error:
        return error.failure
    return None


def _nested(depth: int) -> dict[str, typing.Any]:
    """A chain of ``depth`` nested dicts ending in one scalar."""
    value: typing.Any = 1
    for _ in range(depth):
        value = {"k": value}
    return typing.cast(dict[str, typing.Any], value)


def _padded(limit: int, key: str = "p") -> dict[str, str]:
    """A one-key mapping whose canonical JSON is exactly ``limit`` bytes."""
    overhead = len(schema._canonical_json({key: ""}).encode("utf-8"))
    return {key: "x" * (limit - overhead)}


def _vendor_nodes(count: int) -> dict[str, typing.Any]:
    """A vendor mapping carrying ``count`` integer leaves split into <=1024 lists."""
    lists: dict[str, typing.Any] = {}
    remaining = count
    index = 0
    while remaining > 0:
        size = min(remaining, schema.MAX_COLLECTION_LENGTH)
        lists[f"l{index}"] = [0] * size
        remaining -= size
        index += 1
    return lists


def _boundary(build: typing.Callable[[int], schema.ColdTickRecord], low: int, high: int) -> int:
    """The largest ``n`` in ``[low, high)`` the schema admits (monotone search)."""
    assert _refusal(build(low)) is None
    assert _refusal(build(high)) is not None
    while high - low > 1:
        middle = (low + high) // 2
        if _refusal(build(middle)) is None:
            low = middle
        else:
            high = middle
    return low


def _assert_carrier_admits(record: schema.ColdTickRecord) -> None:
    """Schema admission first, then the exact planned carrier must admit a fresh copy."""
    stored = schema.validate_record(record)
    assert type(stored) is schema.ColdTickRecord
    copy = two_phase._admit_carrier(stored, schema.ColdTickRecord, PLANNED_TICK)
    assert copy is not None
    assert copy is not stored
    assert copy == stored


# ------------------------------------------------------------------ T-D5


def test_t_d5_planned_carrier_matches_the_annotation_reachable_closure() -> None:
    """The planned table names exactly the classes reachable from ``ColdTickRecord``."""
    from tests.test_cold_characterisation_two_phase import _reachable

    models, enums = _reachable(schema.ColdTickRecord)
    assert {model for model, _ in PLANNED_TICK.models} == models
    assert {kind for kind, _ in PLANNED_TICK.enums} == enums


def test_t_d5_depth_limit_vendor_and_audio_extra() -> None:
    """The deepest schema-admitted vendor and audio-extra graphs are carrier-admitted."""
    # raw_vendor_data sits at depth 2, raw_audio_extra at depth 1 (root model is 0).
    vendor_ok = _nested(schema.MAX_JSON_DEPTH - 2)
    extra_ok = _nested(schema.MAX_JSON_DEPTH - 1)
    assert _refusal(probe_tick(vendor=vendor_ok)) is None
    assert _refusal(probe_tick(extra=extra_ok)) is None
    assert (
        _refusal(probe_tick(vendor=_nested(schema.MAX_JSON_DEPTH - 1)))
        is schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED
    )
    assert (
        _refusal(probe_tick(extra=_nested(schema.MAX_JSON_DEPTH)))
        is schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED
    )
    _assert_carrier_admits(probe_tick(vendor=vendor_ok))
    _assert_carrier_admits(probe_tick(extra=extra_ok))
    _assert_carrier_admits(probe_tick(vendor=vendor_ok, extra=extra_ok))


def test_t_d5_node_limit() -> None:
    """The largest schema-admitted node count is carrier-admitted."""
    limit = _boundary(lambda n: probe_tick(vendor=_vendor_nodes(n)), 1, schema.MAX_JSON_NODES)
    assert _refusal(probe_tick(vendor=_vendor_nodes(limit + 1))) is (
        schema.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED
    )
    _assert_carrier_admits(probe_tick(vendor=_vendor_nodes(limit)))


def test_t_d5_vendor_blob_limit() -> None:
    """A vendor blob of exactly the byte limit is carrier-admitted."""
    vendor = _padded(schema.MAX_VENDOR_BLOB_BYTES)
    assert _refusal(probe_tick(vendor=vendor)) is None
    over = _padded(schema.MAX_VENDOR_BLOB_BYTES + 1)
    assert _refusal(probe_tick(vendor=over)) is (
        schema.ColdEvidenceFailure.RECORD_VENDOR_BLOB_TOO_LARGE
    )
    _assert_carrier_admits(probe_tick(vendor=vendor))


def test_t_d5_raw_audio_extra_limit() -> None:
    """Raw audio extra of exactly the byte limit is carrier-admitted."""
    extra = _padded(schema.MAX_RAW_AUDIO_EXTRA_BYTES)
    assert _refusal(probe_tick(extra=extra)) is None
    over = _padded(schema.MAX_RAW_AUDIO_EXTRA_BYTES + 1)
    assert _refusal(probe_tick(extra=over)) is (
        schema.ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE
    )
    _assert_carrier_admits(probe_tick(extra=extra))


@pytest.mark.parametrize("char", ["x", "é", "\U0001f525", "\u0001"])
def test_t_d5_total_record_byte_limit(char: str) -> None:
    """A record of the largest admitted canonical size is carrier-admitted."""
    vendor = _padded(schema.MAX_VENDOR_BLOB_BYTES)
    extra = _padded(schema.MAX_RAW_AUDIO_EXTRA_BYTES)
    limit = _boundary(
        lambda n: probe_tick(vendor=vendor, extra=extra, reason=char * n),
        0,
        schema.MAX_RECORD_BYTES,
    )
    record = probe_tick(vendor=vendor, extra=extra, reason=char * limit)
    size = len(schema._canonical_json(record.model_dump(mode="json")).encode("utf-8"))
    assert schema.MAX_RECORD_BYTES - 12 <= size <= schema.MAX_RECORD_BYTES
    assert _refusal(probe_tick(vendor=vendor, extra=extra, reason=char * (limit + 1))) is (
        schema.ColdEvidenceFailure.RECORD_TOO_LARGE
    )
    _assert_carrier_admits(record)


def test_t_d5_text_key_and_scalar_extremes() -> None:
    """Maximal text fields and keys and extreme exact scalars are carrier-admitted."""
    text = "t" * schema.MAX_TEXT_FIELD_BYTES
    key = "k" * schema.MAX_JSON_KEY_BYTES
    bound = 10**schema.MAX_INT_DIGITS - 1
    vendor: dict[str, typing.Any] = {
        key: bound,
        "neg": -bound,
        "max": 1.7976931348623157e308,
        "tiny": 5e-324,
        "negzero": -0.0,
        "none": None,
        "flag": True,
        "empty_list": [],
        "empty_map": {},
        "unicode": "é\U0001f525\u0000",
    }
    record = probe_tick(
        vendor=vendor,
        extra={key: [bound, -bound, 0.1]},
        driver=text,
        session_id=text,
        recorded_at_utc=text,
        bean=-273.15,
        heat=bound,
    )
    assert _refusal(record) is None
    _assert_carrier_admits(record)


# ------------------------------------------------------- observer harness

Phase = schema.ColdPhaseKind
STREAMS: typing.Final = ("header", "tick", "host", "advisory", "finalisation", "abort", "lifecycle")
TICKS_PER_PHASE: typing.Final = 4


class PlainWorld(World):
    """A world whose runs pass no ``tick_observer`` keyword at all (the baseline)."""


class ObservedWorld(World):
    """A world whose runs pass one ``tick_observer`` through the U1 keyword."""

    def __init__(self, tmp_path: Path, observer: object) -> None:
        super().__init__(tmp_path)
        self.observer = observer

    def advisory_arguments(self) -> dict[str, typing.Any]:
        return {**super().advisory_arguments(), "tick_observer": self.observer}


class Recorder:
    """A display-only observer recording each copy; ``behaviour`` may raise or return."""

    def __init__(
        self,
        behaviour: Callable[[schema.ColdTickRecord, int], object] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.ticks: list[schema.ColdTickRecord] = []
        self.behaviour = behaviour
        self.log = log

    @property
    def calls(self) -> int:
        """The number of calls (a bound scalar, so no tick is ever formatted)."""
        return len(self.ticks)

    @property
    def phases(self) -> list[tuple[Phase, int]]:
        """The ``(phase, tick)`` of each call."""
        return [(tick.phase, tick.tick) for tick in self.ticks]

    def __call__(self, tick: schema.ColdTickRecord, /) -> typing.Any:
        self.ticks.append(tick)
        if self.log is not None:
            self.log.append("observer")
        if self.behaviour is not None:
            return self.behaviour(tick, len(self.ticks))
        return None


def snapshot(world: World, result: object) -> dict[str, object]:
    """Every observable fact of one finished run (records, calls, result)."""
    manifest = Path(world.root) / RUN_ID / "manifest.json"
    return {
        "records": {stream: world.records(stream) for stream in STREAMS},
        "manifest": manifest.read_bytes() if manifest.exists() else None,
        # Compared only as a bool by ``differing``; never dumped, rendered or formatted.
        "result": result,
        "mcp": list(world.mcp.calls),
        "finalised": list(world.mcp.finalised),
        "child": list(world.child.calls),
        "clock_samples": world.clock.samples,
        "clock_t": world.clock.t,
        "host": (world.host.samples, world.host.start_calls),
        "advisor": list(world.advisor.calls),
    }


def finalised_phases(world: World) -> list[Phase | None]:
    """The phase of each finalised session (``None`` if foreign); no ID is formatted."""
    return [
        next((phase for phase in (OFF, ON) if session == SESSIONS[phase]), None)
        for session in world.mcp.finalised
    ]


_RESULT_FIELDS: typing.Final = frozenset(
    {
        "outcome",
        "start_refusal",
        "termination_reason",
        "child_ownership",
        "manifest_sha256",
        "conformance",
        "advisory_path",
        "provider_check",
    }
)


def _closed_member(value: object) -> bool:
    """Whether one runtime value is an enum member (checked at runtime, not by type)."""
    return isinstance(value, enum.Enum)


def public_result_is_closed(result: two_phase.ColdTwoPhaseResult) -> bool:
    """Whether the public result holds only closed members, a digest and closed findings.

    Structural replacement for the frozen canary formatter: proves no session, path or
    error text can be present without rendering the result.
    """
    if frozenset(type(result).model_fields) != _RESULT_FIELDS:
        return False
    for name in _RESULT_FIELDS - {"manifest_sha256", "conformance"}:
        value: object = getattr(result, name)
        if not (value is None or isinstance(value, enum.Enum)):
            return False
    digest: object = result.manifest_sha256
    if digest is not None and not (
        type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    ):
        return False
    checked = result.conformance
    if checked is None:
        return True
    findings = (*checked.findings, *checked.pre_advisory_findings)
    return (
        type(checked) is advisory_conformance.ColdAdvisoryConformanceResult
        and type(checked.policy_version) is int
        and _closed_member(checked.outcome)
        and all(_closed_member(finding) for finding in findings)
    )


def assert_failed_safely(
    world: World, result: two_phase.ColdTwoPhaseResult, reason: lifecycle.ColdRunTerminationReason
) -> None:
    """A sealed FAILED run with ``reason``, never conformant; asserts closed locals only."""
    outcome = result.outcome
    assert outcome is Outcome.NOT_CONFORMANT
    termination_reason = result.termination_reason
    assert termination_reason is reason
    terminal = world.lifecycle()[-1]
    event, termination, terminal_reason = (
        terminal["event"],
        terminal["termination"],
        terminal["termination_reason"],
    )
    assert (event, termination, terminal_reason) == ("run_terminated", "failed", reason.value)
    digest = result.manifest_sha256
    sealed = digest is not None and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    assert sealed
    retained = world.retained(digest)
    checked = advisory_conformance.check_advisory_conformance(retained).outcome
    assert checked is advisory_conformance.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
    closed = public_result_is_closed(result)
    assert closed


def differing(left: dict[str, object], right: dict[str, object]) -> list[str]:
    """The fact names that differ (names only, so no record is ever formatted)."""
    assert sorted(left) == sorted(right)
    return [name for name in left if left[name] != right[name]]


def park(world: World, name: str) -> None:
    """Move one finished world's root aside so the next world reuses the same path."""
    Path(world.root).rename(Path(world.tmp_path) / name)


async def baseline(
    tmp_path: Path, configure: Callable[[World], None] = lambda _world: None
) -> dict[str, object]:
    """Run the no-observer world, snapshot it, then park its root."""
    world = PlainWorld(tmp_path)
    configure(world)
    result = await world.run()
    facts = snapshot(world, result)
    park(world, "baseline")
    return facts


# ------------------------------------------------------------------ T-D1


@pytest.mark.asyncio
async def test_t_d1_called_once_per_retained_tick_in_phase_order(tmp_path: Path) -> None:
    """T-D1: one call per retained tick, exact ``ColdTickRecord`` copies, phase order."""
    recorder = Recorder()
    world = ObservedWorld(tmp_path, recorder)
    result = await world.run()
    outcome = result.outcome
    assert outcome is Outcome.ADVISORY_CONFORMANT
    expected = [(OFF, index) for index in range(TICKS_PER_PHASE)] + [
        (ON, index) for index in range(TICKS_PER_PHASE)
    ]
    assert recorder.phases == expected
    assert all(type(tick) is schema.ColdTickRecord for tick in recorder.ticks)
    on_disk = world.records("tick")
    assert len(on_disk) == recorder.calls == 2 * TICKS_PER_PHASE
    same = [tick.model_dump(mode="json") for tick in recorder.ticks] == on_disk
    assert same


# ------------------------------------------------------------------ T-D2


@pytest.mark.asyncio
async def test_t_d2_a_no_op_observer_changes_nothing_and_absence_admits_no_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-D2: no-observer and no-op-observer runs are identical; no tick carrier without one."""
    roots: list[type] = []
    real_admit = two_phase._admit_carrier

    def spy_admit(
        value: object, root: type[typing.Any], *args: typing.Any, **kw: typing.Any
    ) -> object:
        roots.append(root)
        return real_admit(value, root, *args, **kw)

    CapturingSink.created.clear()
    monkeypatch.setattr(two_phase, "_admit_carrier", spy_admit)
    monkeypatch.setattr(two_phase, "_RunSink", CapturingSink)
    plain = await baseline(tmp_path)
    assert schema.ColdTickRecord not in roots
    (plain_sink,) = CapturingSink.created
    assert plain_sink.after_tick is None
    assert len(typing.cast(dict[str, list[object]], plain["records"])["tick"]) == 8

    roots.clear()
    CapturingSink.created.clear()
    recorder = Recorder()
    world = ObservedWorld(tmp_path, recorder)
    observed = snapshot(world, await world.run())
    assert differing(observed, plain) == []
    assert roots.count(schema.ColdTickRecord) == 2 * TICKS_PER_PHASE
    (observed_sink,) = CapturingSink.created
    assert observed_sink.after_tick is not None
    assert recorder.calls == 2 * TICKS_PER_PHASE


def test_t_d2b_frozen_construction_and_sink_substitution_stay_compatible(
    tmp_path: Path,
) -> None:
    """T-D2b: ``make_run`` needs no keyword; the sink keeps its one-argument signature."""
    run = make_run(World(tmp_path))
    assert run._observer is None
    parameters = list(inspect.signature(two_phase._RunSink.__init__).parameters)
    assert parameters == ["self", "writer"]
    entry = list(inspect.signature(two_phase.run_two_phase_characterisation).parameters.values())
    assert entry[-1].name == "tick_observer" and entry[-1].default is None
    assert entry[-1].kind is inspect.Parameter.KEYWORD_ONLY
    private = list(inspect.signature(two_phase._TwoPhaseRun.__init__).parameters.values())
    assert private[-1].name == "tick_observer" and private[-1].default is None


@pytest.mark.asyncio
async def test_t_d2b_capturing_sink_substitution_works_with_an_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-D2b: a frozen-style one-argument sink subclass still receives the hook."""
    CapturingSink.created.clear()
    monkeypatch.setattr(two_phase, "_RunSink", CapturingSink)
    recorder = Recorder()
    world = ObservedWorld(tmp_path, recorder)
    result = await world.run()
    outcome = result.outcome
    assert outcome is Outcome.ADVISORY_CONFORMANT
    (sink,) = CapturingSink.created
    assert sink.after_tick is not None
    assert recorder.calls == 2 * TICKS_PER_PHASE


# ------------------------------------------------------------------ T-D3


class FailingWriter:
    """Delegates to the real writer; the ``fail_at``-th tick append raises."""

    def __init__(self, writer: store.ColdEvidenceWriter, fail_at: int, log: list[str]) -> None:
        self.writer = writer
        self.fail_at = fail_at
        self.ticks = 0
        self.log = log

    def append(self, record: schema.ColdEvidenceRecord) -> None:
        if type(record) is schema.ColdTickRecord:
            self.ticks += 1
            if self.ticks == self.fail_at:
                raise RuntimeError("synthetic writer fault")
            self.writer.append(record)
            self.log.append("write:tick")
            return
        self.writer.append(record)
        self.log.append(f"write:{type(record).__name__}")

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self.writer, name)


def install_writer(
    monkeypatch: pytest.MonkeyPatch, fail_at: int = 0, log: list[str] | None = None
) -> list[str]:
    """Wrap the run's real writer; returns the shared write log."""
    shared: list[str] = [] if log is None else log
    real_open = engine.open_phase_evidence

    def wrapped(admission: typing.Any) -> typing.Any:
        return FailingWriter(real_open(admission), fail_at, shared)

    monkeypatch.setattr(two_phase, "open_phase_evidence", wrapped)
    return shared


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 3, 6])
async def test_t_d3_no_call_for_a_failed_tick_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    """T-D3: a writer fault at tick N gives N-1 calls and the no-observer classification."""
    install_writer(monkeypatch, fail_at)
    plain = await baseline(tmp_path)
    recorder = Recorder()
    world = ObservedWorld(tmp_path, recorder)
    observed = snapshot(world, await world.run())
    assert recorder.calls == fail_at - 1
    assert differing(observed, plain) == []


# ------------------------------------------------------------------ T-D4


@pytest.mark.asyncio
async def test_t_d4_the_copy_is_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-D4: equal to the retained tick, never it; mutation reaches neither it nor disk."""
    CapturingSink.created.clear()
    monkeypatch.setattr(two_phase, "_RunSink", CapturingSink)
    flags: list[tuple[bool, ...]] = []

    def behaviour(tick: schema.ColdTickRecord, _count: int) -> None:
        (sink,) = CapturingSink.created
        stored = sink.latest_retained_tick()
        assert stored is not None and tick.device is not None and stored.device is not None
        flags.append(
            (
                tick == stored,
                tick is not stored,
                tick.device is not stored.device,
                tick.device.raw_vendor_data is not stored.device.raw_vendor_data,
                tick.raw_audio_extra is not stored.raw_audio_extra,
                tick.session is not stored.session,
                tick.audio is not stored.audio,
                tick.roast_fan is not stored.roast_fan,
            )
        )
        tick.device.raw_vendor_data["mutated"] = "by-observer"
        tick.raw_audio_extra["mutated"] = "by-observer"
        flags.append(
            (
                "mutated" not in stored.device.raw_vendor_data,
                "mutated" not in stored.raw_audio_extra,
            )
        )

    world = ObservedWorld(tmp_path, Recorder(behaviour))
    result = await world.run()
    assert flags == [(True,) * 8, (True, True)] * (2 * TICKS_PER_PHASE)
    outcome = result.outcome
    assert outcome is Outcome.ADVISORY_CONFORMANT
    for record in world.records("tick"):
        assert "mutated" not in record["device"]["raw_vendor_data"]
        assert "mutated" not in record["raw_audio_extra"]
        assert record["device"]["raw_vendor_data"] == {"packet": "abc", "count": 3}


# ------------------------------------------------------------------ T-D6


def foreign_session_tick(phase: Phase, at: int) -> Callable[[Phase, int], object]:
    """Ticks whose ``at``-th read in ``phase`` reports a foreign session."""

    def tick(current: Phase, index: int) -> object:
        if current is phase and index == at:
            return observation(
                device_state(driver=DRIVER),
                roast_fan=roast_fan_state(level=0),
                audio=audio(index),
                session=session_metadata(
                    session_id="session-foreign", elapsed_monotonic_seconds=float(index + 1)
                ),
            )
        return conforming_tick(current, index)

    return tick


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "at"), [(OFF, 0), (OFF, 2), (ON, 1)])
async def test_t_d6_a_session_mismatch_suppresses_publication_without_failing(
    tmp_path: Path, phase: Phase, at: int
) -> None:
    """T-D6: no call for the foreign-session tick or later; the engine's primary stands."""

    def configure(world: World) -> None:
        world.mcp.tick = foreign_session_tick(phase, at)

    plain = await baseline(tmp_path, configure)
    recorder = Recorder()
    world = ObservedWorld(tmp_path, recorder)
    configure(world)
    observed = snapshot(world, await world.run())
    before = at if phase is OFF else TICKS_PER_PHASE + at
    assert recorder.calls == before
    assert differing(observed, plain) == []
    reason = typing.cast(two_phase.ColdTwoPhaseResult, observed["result"]).termination_reason
    assert reason is R.PHASE_ABORTED


# ------------------------------------------------------------ T-D7 (unit)


class UnitRig:
    """A private run with a bound sink, header and session for direct hook calls."""

    def __init__(self, tmp_path: Path, observer: object) -> None:
        self.world = ObservedWorld(tmp_path, observer)
        self.run = make_run(self.world)
        self.header = builders.build_run_header(
            identity=self.world.ids[OFF],
            phase=OFF,
            recorded_at_utc=utc_at(1.0),
            monotonic_seconds=1.0,
        )
        self.sink = two_phase._RunSink(typing.cast(store.ColdEvidenceWriter, None))
        self.sink.phase = OFF
        self.run._sink = self.sink
        self.run._headers[OFF] = typing.cast(
            schema.ColdRunHeader, schema.validate_record(self.header)
        )
        self.run._sessions[OFF] = SESSIONS[OFF]

    def stored(
        self, phase: Phase = OFF, update: dict[str, object] | None = None
    ) -> schema.ColdTickRecord:
        header = self.header
        if phase is ON:
            header = builders.build_run_header(
                identity=self.world.ids[ON],
                phase=ON,
                recorded_at_utc=utc_at(1.0),
                monotonic_seconds=1.0,
            )
        tick = builders.build_tick_record(
            header=header,
            tick=0,
            recorded_at_utc=utc_at(2.0),
            monotonic_seconds=2.0,
            observation=conforming_tick(OFF, 0),
        )
        validated = typing.cast(schema.ColdTickRecord, schema.validate_record(tick))
        return validated.model_copy(update=update) if update else validated


def assert_refused(rig: UnitRig, recorder: Recorder) -> None:
    assert recorder.calls == 0
    assert rig.run._primary is R.UNEXPECTED_FAILURE
    assert rig.run._observer is None


def test_t_d7_control_a_bound_tick_is_published_once(tmp_path: Path) -> None:
    """T-D7 control: a fully bound stored tick reaches the observer as a fresh copy."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    stored = rig.stored()
    rig.run._after_tick(stored)
    assert recorder.calls == 1
    (copy,) = recorder.ticks
    equal, distinct = copy == stored, copy is not stored
    assert equal and distinct
    assert rig.run._primary is None and rig.run._observer is recorder


@pytest.mark.parametrize(
    "update",
    [{"run_id": "20260926T120000Z-other-run"}, {"identity_sha256": "c" * 64}],
    ids=["t_d7a_run_id", "t_d7b_identity_digest"],
)
def test_t_d7ab_a_header_mismatch_fails_without_a_call(
    tmp_path: Path, update: dict[str, object]
) -> None:
    """T-D7a/b: a stored tick not bound to the admitted header is refused and fails."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    rig.run._after_tick(rig.stored(update=update))
    assert_refused(rig, recorder)
    rig.run._after_tick(rig.stored())
    assert recorder.calls == 0


def test_t_d7d_a_refused_carrier_fails_without_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-D7d: carrier refusal (``None``) records the failure, clears, never calls."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)

    def refuse(*_args: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(two_phase, "_admit_carrier", refuse)
    rig.run._after_tick(rig.stored())
    assert_refused(rig, recorder)


@pytest.mark.parametrize("case", ["same_object", "phase", "session"])
def test_t_d7_defensive_copy_bindings_fail_without_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Defensive: a carrier returning the original, another phase or session is refused."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    stored = rig.stored()
    forged = {
        "same_object": stored,
        "phase": stored.model_copy(update={"phase": ON}),
        "session": stored.model_copy(
            update={"session": stored.session.model_copy(update={"session_id": "session-x"})}
        ),
    }[case]

    def forge(*_args: object, **_kw: object) -> schema.ColdTickRecord:
        return forged

    monkeypatch.setattr(two_phase, "_admit_carrier", forge)
    rig.run._after_tick(stored)
    assert_refused(rig, recorder)


@pytest.mark.parametrize("case", ["no_sink", "unbound_sink", "other_phase", "no_header"])
def test_t_d7_defensive_sink_and_header_bindings_fail_without_a_call(
    tmp_path: Path, case: str
) -> None:
    """Defensive: no sink, an unbound sink, another phase or no header is refused."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    stored = rig.stored(ON) if case == "other_phase" else rig.stored()
    if case == "no_sink":
        rig.run._sink = None
    elif case == "unbound_sink":
        rig.sink.phase = None
    elif case == "no_header":
        del rig.run._headers[OFF]
    rig.run._after_tick(stored)
    assert_refused(rig, recorder)


def test_t_d7_no_activation_session_suppresses_without_failing(tmp_path: Path) -> None:
    """T-D6 unit: with no admitted activation session publication stops with no failure."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    del rig.run._sessions[OFF]
    rig.run._after_tick(rig.stored())
    assert recorder.calls == 0
    assert rig.run._primary is None and rig.run._observer is None


def test_t_d7_a_cleared_observer_is_never_called(tmp_path: Path) -> None:
    """After clearing, the hook returns at once with no failure."""
    recorder = Recorder()
    rig = UnitRig(tmp_path, recorder)
    rig.run._observer = None
    rig.run._after_tick(rig.stored())
    assert recorder.calls == 0 and rig.run._primary is None


# ------------------------------------------------------- T-D8 / T-D9


def assert_off_failure(world: World, result: two_phase.ColdTwoPhaseResult) -> None:
    """T-D8 oracle: OFF completes, is finalised once, and nothing of ON starts."""
    off_ticks = [r for r in world.records("tick") if r["phase"] == OFF.value]
    assert len(off_ticks) == TICKS_PER_PHASE
    assert (OFF.value, "observation_window_elapsed") in world.events()
    assert world.event(OFF, "observation_window_elapsed")["tick_count"] == TICKS_PER_PHASE
    assert len(world.records("abort")) == 0
    reason = result.termination_reason
    assert reason is R.UNEXPECTED_FAILURE
    assert_failed_safely(world, result, R.UNEXPECTED_FAILURE)
    outcome = result.outcome
    assert outcome is not Outcome.ADVISORY_CONFORMANT
    assert finalised_phases(world) == [OFF]
    assert [r["phase"] for r in world.records("header")] == [OFF.value]
    assert (OFF.value, "child_started") not in world.events()
    assert all(phase == OFF.value for phase, _event in world.events())


@pytest.mark.asyncio
async def test_t_d8_an_observer_exception_fails_the_run_without_an_abort(tmp_path: Path) -> None:
    """T-D8: a raise on OFF tick 1 is contained; OFF completes and finalises; no ON."""

    def boom(_tick: schema.ColdTickRecord, count: int) -> None:
        if count == 1:
            raise RuntimeError("synthetic observer fault")

    recorder = Recorder(boom)
    world = ObservedWorld(tmp_path, recorder)
    result = await world.run()
    assert recorder.calls == 1
    assert_off_failure(world, result)


@pytest.mark.asyncio
async def test_t_d8b_a_first_on_tick_failure_finalises_on_then_stops(tmp_path: Path) -> None:
    """T-D8b: a raise on the first ON tick: ON is finalised when eligible; the run fails."""

    def boom(tick: schema.ColdTickRecord, _count: int) -> None:
        if tick.phase is ON:
            raise RuntimeError("synthetic observer fault")

    recorder = Recorder(boom)
    world = ObservedWorld(tmp_path, recorder)
    result = await world.run()
    assert recorder.phases == [(OFF, i) for i in range(TICKS_PER_PHASE)] + [(ON, 0)]
    assert_failed_safely(world, result, R.UNEXPECTED_FAILURE)
    outcome = result.outcome
    assert outcome is not Outcome.ADVISORY_CONFORMANT
    assert finalised_phases(world) == [OFF, ON]
    assert world.event(ON, "observation_window_elapsed")["tick_count"] == TICKS_PER_PHASE
    assert len(world.records("abort")) == 0


async def _never_awaited() -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", ["one", "coroutine"])
async def test_t_d9_a_non_none_return_fails_the_run(tmp_path: Path, returned: str) -> None:
    """T-D9: a non-``None`` return fails like an exception; a coroutine is closed."""

    def give(_tick: schema.ColdTickRecord, _count: int) -> object:
        return 1 if returned == "one" else _never_awaited()

    recorder = Recorder(give)
    world = ObservedWorld(tmp_path, recorder)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await world.run()
        gc.collect()
    assert recorder.calls == 1
    assert_off_failure(world, result)
    never = [
        w
        for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert never == []


# ------------------------------------------------------------------ T-D10


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(("domain", "reason"), ABORT_PAIRS, ids=[p[0].value for p in ABORT_PAIRS])
async def test_t_d10_a_retained_abort_still_forbids_finalisation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    domain: schema.ColdAbortDomain,
    reason: typing.Any,
    phase: Phase,
) -> None:
    """T-D10: observer failure, then an abort in any domain: that phase is never finalised."""

    async def behaviour(admission: typing.Any, sink: typing.Any, clock: Clock) -> object:
        sink.append(
            builders.build_tick_record(
                header=admission.header,
                tick=0,
                recorded_at_utc=clock.utc_now_iso(),
                monotonic_seconds=clock.t,
                observation=conforming_tick(admission.phase, 0),
            )
        )
        return await retained_abort(domain, reason, True)(admission, sink, clock)

    monkeypatch.setattr(two_phase, "observe_cold_phase", scripted_observer(phase, behaviour))

    def boom(tick: schema.ColdTickRecord, _count: int) -> None:
        if tick.phase is phase:
            raise RuntimeError("synthetic observer fault")

    recorder = Recorder(boom)
    world = ObservedWorld(tmp_path, recorder)
    result = await world.run()
    assert [tick.phase for tick in recorder.ticks][-1] is phase
    assert_failed_safely(world, result, R.UNEXPECTED_FAILURE)
    assert phase not in finalised_phases(world)
    assert finalised_phases(world) == ([] if phase is OFF else [OFF])
    assert [(a["domain"], a["phase"]) for a in world.records("abort")] == [
        (domain.value, phase.value)
    ]
    assert (phase.value, "observation_window_elapsed") not in world.events()


# ------------------------------------------------------- T-D11 / T-D12


class ObserverInterrupt(BaseException):
    """A synthetic non-cancellation ``BaseException``."""


@pytest.mark.asyncio
async def test_t_d11_a_base_exception_propagates_identically_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-D11: the identical object propagates; cleanup stop; no terminal, seal or check."""
    checks: list[object] = []
    monkeypatch.setattr(two_phase, "check_advisory_conformance", checks.append)
    interrupt = ObserverInterrupt()

    def boom(_tick: schema.ColdTickRecord, _count: int) -> None:
        raise interrupt

    world = ObservedWorld(tmp_path, Recorder(boom))
    with pytest.raises(ObserverInterrupt) as raised:
        await world.run()
    assert raised.value is interrupt
    assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)]
    assert world.child.stops_completed == 1
    assert not any(event == "run_terminated" for _phase, event in world.events())
    assert not any(event == "finalisation_returned" for _phase, event in world.events())
    assert finalised_phases(world) == []
    assert not world.manifest_exists()
    assert checks == []
    assert len(world.records("abort")) == 0


@pytest.mark.asyncio
async def test_t_d12_cancellation_from_the_observer_records_cancelled_and_reaps(
    tmp_path: Path,
) -> None:
    """T-D12: a ``CancelledError`` propagates (by type); CANCELLED abort; no terminal or seal."""

    def cancel(_tick: schema.ColdTickRecord, _count: int) -> None:
        raise asyncio.CancelledError

    world = ObservedWorld(tmp_path, Recorder(cancel))
    with pytest.raises(asyncio.CancelledError):
        await world.run()
    assert [(a["domain"], a["reason"]) for a in world.records("abort")] == [
        (schema.ColdAbortDomain.ENGINE.value, schema.ColdEngineAbortReason.CANCELLED.value)
    ]
    assert world.child_ops() == [("configure", OFF), ("start", OFF), ("stop", OFF)]
    assert world.child.stops_completed == 1
    assert not any(event == "run_terminated" for _phase, event in world.events())
    assert finalised_phases(world) == []
    assert not world.manifest_exists()


# ------------------------------------------------------------------ T-D13


@pytest.mark.asyncio
async def test_t_d13_read_write_observer_host_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-D13: read, tick write, observer, host sample; no sleep between write and observer."""
    log: list[str] = []
    install_writer(monkeypatch, log=log)
    instants: list[tuple[float, float]] = []

    def at(tick: schema.ColdTickRecord, _count: int) -> None:
        instants.append((world.clock.t, tick.monotonic_seconds))

    world = ObservedWorld(tmp_path, Recorder(at, log=log))
    for phase in (OFF, ON):
        world.mcp.before[f"get_roast_state:{phase.value}"] = lambda: log.append("read")
    real_sample = world.host.sample

    def sample(root: Path) -> typing.Any:
        log.append("host")
        return real_sample(root)

    monkeypatch.setattr(world.host, "sample", sample)
    result = await world.run()
    outcome = result.outcome
    assert outcome is Outcome.ADVISORY_CONFORMANT
    writes = [index for index, entry in enumerate(log) if entry == "write:tick"]
    assert len(writes) == 2 * TICKS_PER_PHASE
    for index in writes:
        assert "read" in log[:index]
        assert log[index - 1] == "read"
        assert log[index : index + 3] == ["write:tick", "observer", "host"]
    # No clock movement (hence no engine sleep) between the read's completion and the call.
    assert len(instants) == 2 * TICKS_PER_PHASE
    assert all(now == recorded for now, recorded in instants)


# ------------------------------------------------------------------ T-D14


def test_t_d14_tick_carrier_is_the_reachable_closure() -> None:
    """T-D14: the production tick carrier names exactly the reachable classes."""
    from tests.test_cold_characterisation_two_phase import _reachable

    models, enums = _reachable(schema.ColdTickRecord)
    assert {model for model, _ in two_phase._TICK.models} == models
    assert {kind for kind, _ in two_phase._TICK.enums} == enums
    assert two_phase._TICK == PLANNED_TICK


# ------------------------------------------- T-D5 completion: actual _after_tick


class Publishing:
    """A display-only observer that keeps each copy and publishes it to the hub."""

    def __init__(self, hub: stream.ColdObservationHub) -> None:
        self.hub = hub
        self.copies: list[schema.ColdTickRecord] = []

    def __call__(self, tick: schema.ColdTickRecord, /) -> None:
        self.copies.append(tick)
        self.hub.publish(tick)


def bounded_tick(
    rig: UnitRig,
    *,
    vendor: dict[str, typing.Any] | None = None,
    extra: dict[str, typing.Any] | None = None,
    reason: str | None = None,
    driver: str | None = None,
    session: str | None = None,
    heat: int | None = None,
    bean: float | None = None,
    utc: str | None = None,
) -> schema.ColdTickRecord:
    """The rig's bound stored tick with boundary parts replaced (re-admitted by the caller)."""
    base = rig.stored()
    assert base.device is not None
    device: dict[str, object] = {}
    if vendor is not None:
        device["raw_vendor_data"] = vendor
    if driver is not None:
        device["driver"] = driver
    if heat is not None:
        device["heat_level_percent"] = heat
    if bean is not None:
        device["bean_temp_c"] = bean
    update: dict[str, object] = {"device": base.device.model_copy(update=device)}
    if extra is not None:
        update["raw_audio_extra"] = extra
    if reason is not None:
        update["audio"] = base.audio.model_copy(update={"reason": reason})
    if session is not None:
        update["session"] = base.session.model_copy(update={"session_id": session})
    if utc is not None:
        update["recorded_at_utc"] = utc
    return base.model_copy(update=update)


def _drain(subscription: stream.ColdSubscription) -> list[str | None]:
    items: list[str | None] = []
    while not subscription.queue.empty():
        items.append(subscription.queue.get_nowait())
    return items


BOUNDARY_CASES: typing.Final = (
    "depth_vendor",
    "depth_audio_extra",
    "nodes",
    "vendor_bytes",
    "audio_extra_bytes",
    "record_bytes_ascii",
    "record_bytes_multibyte",
    "text_key_scalar_extremes",
)


def boundary_record(rig: UnitRig, case: str) -> schema.ColdTickRecord:
    """One independently schema-admitted boundary tick bound to the rig's header and session."""
    vendor_max = _padded(schema.MAX_VENDOR_BLOB_BYTES)
    extra_max = _padded(schema.MAX_RAW_AUDIO_EXTRA_BYTES)
    if case == "depth_vendor":
        record = bounded_tick(rig, vendor=_nested(schema.MAX_JSON_DEPTH - 2))
        beyond = bounded_tick(rig, vendor=_nested(schema.MAX_JSON_DEPTH - 1))
    elif case == "depth_audio_extra":
        record = bounded_tick(rig, extra=_nested(schema.MAX_JSON_DEPTH - 1))
        beyond = bounded_tick(rig, extra=_nested(schema.MAX_JSON_DEPTH))
    elif case == "nodes":
        limit = _boundary(
            lambda n: bounded_tick(rig, vendor=_vendor_nodes(n)), 1, schema.MAX_JSON_NODES
        )
        record = bounded_tick(rig, vendor=_vendor_nodes(limit))
        beyond = bounded_tick(rig, vendor=_vendor_nodes(limit + 1))
    elif case == "vendor_bytes":
        record = bounded_tick(rig, vendor=vendor_max)
        beyond = bounded_tick(rig, vendor=_padded(schema.MAX_VENDOR_BLOB_BYTES + 1))
    elif case == "audio_extra_bytes":
        record = bounded_tick(rig, extra=extra_max)
        beyond = bounded_tick(rig, extra=_padded(schema.MAX_RAW_AUDIO_EXTRA_BYTES + 1))
    elif case in ("record_bytes_ascii", "record_bytes_multibyte"):
        char = "x" if case == "record_bytes_ascii" else "é"
        limit = _boundary(
            lambda n: bounded_tick(rig, vendor=vendor_max, extra=extra_max, reason=char * n),
            0,
            schema.MAX_RECORD_BYTES,
        )
        record = bounded_tick(rig, vendor=vendor_max, extra=extra_max, reason=char * limit)
        size = len(schema._canonical_json(record.model_dump(mode="json")).encode("utf-8"))
        near = schema.MAX_RECORD_BYTES - 2 <= size <= schema.MAX_RECORD_BYTES
        assert near
        beyond = bounded_tick(rig, vendor=vendor_max, extra=extra_max, reason=char * (limit + 1))
    else:
        text = "t" * schema.MAX_TEXT_FIELD_BYTES
        key = "k" * schema.MAX_JSON_KEY_BYTES
        bound = 10**schema.MAX_INT_DIGITS - 1
        rig.run._sessions[OFF] = text
        record = bounded_tick(
            rig,
            vendor={key: bound, "neg": -bound, "max": 1.7976931348623157e308, "tiny": 5e-324},
            extra={key: [bound, -bound, -0.0]},
            driver=text,
            session=text,
            heat=bound,
            bean=-273.15,
        )
        beyond = bounded_tick(rig, driver=text + "t")
    admitted = _refusal(record) is None
    refused = _refusal(beyond) is not None
    assert admitted and refused
    return typing.cast(schema.ColdTickRecord, schema.validate_record(record))


@pytest.mark.parametrize("case", BOUNDARY_CASES)
def test_t_d5_boundary_ticks_publish_one_frame_through_after_tick(
    tmp_path: Path, case: str
) -> None:
    """T-D5/AC3: each schema-admitted boundary tick reaches the hub once as an exclusive copy."""
    hub = stream.ColdObservationHub()
    subscription = hub.subscribe(None)
    assert subscription is not None
    publishing = Publishing(hub)
    rig = UnitRig(tmp_path, publishing)
    stored = boundary_record(rig, case)
    rig.run._after_tick(stored)
    frames = _drain(subscription)
    assert len(frames) == 1 and len(publishing.copies) == 1
    no_failure = rig.run._primary is None
    still_attached = rig.run._observer is publishing
    assert no_failure and still_attached
    (copy,) = publishing.copies
    assert copy.device is not None and stored.device is not None
    exclusive = (
        copy == stored,
        copy is not stored,
        copy.device is not stored.device,
        copy.device.raw_vendor_data is not stored.device.raw_vendor_data,
        copy.raw_audio_extra is not stored.raw_audio_extra,
        copy.session is not stored.session,
        copy.audio is not stored.audio,
    )
    assert exclusive == (True,) * 7
    frame = frames[0]
    assert frame is not None
    data_line = next(part for part in frame.split("\n") if part.startswith("data: "))
    data = stream.ColdObservationData.model_validate_json(data_line.removeprefix("data: "))
    same_utc = data.recorded_at_utc == stored.recorded_at_utc
    assert same_utc and data.fan_percent is None and data.device_reported is True
    if case == "text_key_scalar_extremes":
        # Beyond the JSON-exact bound the projection publishes null (existing U2 rule).
        assert data.heat_percent is None and data.bean_temp_c == -273.15


def test_t_d5_public_inadmissible_utc_is_a_projection_refusal_not_a_carrier_failure(
    tmp_path: Path,
) -> None:
    """A schema-valid 2048-char UTC passes the carrier but is refused by the projection."""
    hub = stream.ColdObservationHub()
    subscription = hub.subscribe(None)
    assert subscription is not None
    publishing = Publishing(hub)
    rig = UnitRig(tmp_path, publishing)
    record = bounded_tick(rig, utc="t" * schema.MAX_TEXT_FIELD_BYTES)
    admitted = _refusal(record) is None
    assert admitted
    stored = typing.cast(schema.ColdTickRecord, schema.validate_record(record))
    carried = two_phase._admit_carrier(stored, schema.ColdTickRecord, two_phase._TICK) is not None
    assert carried
    rig.run._after_tick(stored)
    assert len(publishing.copies) == 1
    assert len(_drain(subscription)) == 0
    assert rig.run._primary is R.UNEXPECTED_FAILURE
    detached = rig.run._observer is None
    assert detached
