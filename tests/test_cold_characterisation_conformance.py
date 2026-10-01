"""Behavioural, fail-closed and hostile-carrier tests for the 4g-b conformance checker (#954).

Runs are written through the real writer, sealed, and read back with the strict v2
reader under ``tmp_path``; in-process carriers are derived from such a run only for
cases the writer refuses or for hostile-carrier admission.  Every negative case pins
one independently reasoned, exact full finding tuple.  Hardware-free throughout.
"""

import ast
import dataclasses
import json
import math
import typing
import unicodedata
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import acceptance, conformance, engine_policy, host
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.mcp import SessionFinalisationResult
from tests.test_cold_characterisation_acceptance import (
    final_status,
    result_for,
    runtime,
    status,
    tick_audio,
)
from tests.test_cold_characterisation_evidence_builders import (
    RUN_ID,
    device_state,
    observation,
    roast_fan_state,
    session_metadata,
)
from tests.test_cold_characterisation_evidence_reader import envelope_of, identity_document
from tests.test_cold_characterisation_evidence_store import OFF, ON, open_writer

F = conformance.ColdConformanceFinding
Outcome = conformance.ColdConformanceOutcome
Event = lifecycle.ColdLifecycleEvent
Phase = schema.ColdPhaseKind
Json = dict[str, typing.Any]
Findings = tuple[conformance.ColdConformanceFinding, ...]
T0 = "2026-09-26T12:00:00Z"
S_OFF = "session-recording-off"
S_ON = "session-recording-on"
DRIVER = "hottop_kn8828b_2k_plus"
PHASE_SECONDS = engine_policy.COLD_PHASE_OBSERVATION_SECONDS
BUDGET = lifecycle.COLD_TRANSITION_BUDGET_SECONDS
CANARY = "sk-live-Canary0123456789AbCdEfGh"


# ------------------------------------------------------------------- run builder


@dataclasses.dataclass
class Tick:
    """One tick to write; ``device`` holds overrides, or ``None`` for a null device."""

    mono: float
    index: int
    session: str
    elapsed: float
    audio: Json
    device: dict[str, object] | None = dataclasses.field(default_factory=dict[str, object])
    roast_fan: int = 0
    record: schema.ColdTickRecord | None = None


@dataclasses.dataclass
class Entry:
    """One lifecycle record to write: event instant ``ev`` and append instant ``rec``."""

    phase: Phase
    event: lifecycle.ColdLifecycleEvent
    ev: float
    rec: float
    fields: dict[str, typing.Any]


Maker = typing.Callable[[schema.ColdRunHeader], schema.ColdEvidenceRecord]


@dataclasses.dataclass
class Plan:
    """A complete run plan; tests edit it before ``write`` materialises it."""

    documents: dict[Phase, Json]
    headers: dict[Phase, float]
    ticks: dict[Phase, list[Tick]]
    hosts: dict[Phase, list[float]]
    results: dict[Phase, list[tuple[Json, float]]]
    lifecycle: list[Entry]
    extras: list[tuple[Phase, Maker]] = dataclasses.field(default_factory=list[tuple[Phase, Maker]])
    late: list[tuple[Phase, Maker]] = dataclasses.field(default_factory=list[tuple[Phase, Maker]])
    phases: tuple[Phase, ...] = (OFF, ON)
    host_overrides: dict[tuple[Phase, int], dict[str, object]] = dataclasses.field(
        default_factory=dict[tuple[Phase, int], dict[str, object]]
    )


def document(tmp_path: Path, root: str, recording: bool, driver: str = DRIVER) -> Json:
    """One v1 identity; the recording-on copy differs at exactly the five allowlisted leaves."""
    doc = identity_document(tmp_path, root)
    doc["runtime_config"]["roaster_driver"] = driver
    doc["device_config"]["recording_enabled"] = recording
    doc["device_config"]["recording_autocapture"] = recording
    if recording:
        doc["server_info"]["started_at_utc"] = "2026-09-26T12:31:00Z"
        doc["effective_mcp_profile"]["source_sha256"] = "d" * 64
        doc["effective_mcp_profile"]["source_byte_length"] = 200
    return doc


def audio(index: int, **overrides: object) -> Json:
    """One active pending tick audio payload whose window counters rise with ``index``."""
    return tick_audio(emitted_window_count=index + 1, processed_window_count=index + 1, **overrides)


def ticks_from(activation: float, session: str, driver: str = DRIVER) -> list[Tick]:
    """Three replay-clean ticks one, two and three seconds after activation."""
    return [
        Tick(
            activation + 1.0 + index,
            index,
            session,
            float(index + 1),
            audio(index),
            {"driver": driver},
        )
        for index in range(3)
    ]


def plan(
    tmp_path: Path,
    *,
    e_off: float = 1810.0,
    a_on: float | None = None,
    t_start: float | None = None,
    s_on: str = S_ON,
    s_off: str = S_OFF,
    driver: str = DRIVER,
) -> Plan:
    """The conforming base run; dependent instants follow ``e_off`` and ``a_on``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = str(tmp_path.resolve() / "pi")
    a_off = 10.0
    scheduled_off = a_off + PHASE_SECONDS
    a_on = e_off + 20.0 if a_on is None else a_on
    e_on = a_on + PHASE_SECONDS
    ticks = {OFF: ticks_from(a_off, s_off, driver), ON: ticks_from(a_on, s_on, driver)}
    return Plan(
        documents={
            OFF: document(tmp_path, root, False, driver),
            ON: document(tmp_path, root, True, driver),
        },
        headers={OFF: 1.0, ON: e_off + 4.0},
        ticks=ticks,
        hosts={phase: [tick.mono for tick in items] for phase, items in ticks.items()},
        results={
            OFF: [(result_for(OFF, session_id=s_off), e_off + 1.0)],
            ON: [(result_for(ON, session_id=s_on), e_on + 1.0)],
        },
        lifecycle=[
            Entry(OFF, Event.PHASE_ACTIVATED, a_off, a_off, {"session_id": s_off}),
            Entry(
                OFF,
                Event.OBSERVATION_WINDOW_ELAPSED,
                e_off,
                e_off,
                {"session_id": s_off, "scheduled_end_monotonic": scheduled_off},
            ),
            Entry(
                OFF,
                Event.FINALISATION_RETURNED,
                e_off + 1.0,
                e_off + 1.5,
                {"session_id": s_off, "finalisation_result": CLEAN},
            ),
            Entry(OFF, Event.CHILD_STOPPED, e_off + 2.0, e_off + 2.0, {"child_stop": CONFIRMED}),
            Entry(OFF, Event.CHILD_STARTED, e_off + 3.0, e_off + 3.0, {"child_start": STARTED}),
            Entry(ON, Event.PHASE_ACTIVATED, a_on, a_on, {"session_id": s_on}),
            Entry(
                ON,
                Event.TRANSITION_MEASURED,
                a_on,
                a_on,
                {
                    "session_id": s_on,
                    "previous_phase_session_id": s_off,
                    "transition_start_monotonic": scheduled_off if t_start is None else t_start,
                },
            ),
            Entry(
                ON,
                Event.OBSERVATION_WINDOW_ELAPSED,
                e_on,
                e_on,
                {"session_id": s_on, "scheduled_end_monotonic": e_on},
            ),
            Entry(
                ON,
                Event.FINALISATION_RETURNED,
                e_on + 1.0,
                e_on + 1.5,
                {"session_id": s_on, "finalisation_result": CLEAN},
            ),
            Entry(ON, Event.CHILD_STOPPED, e_on + 2.0, e_on + 2.0, {"child_stop": CONFIRMED}),
            Entry(ON, Event.RUN_TERMINATED, e_on + 3.0, e_on + 3.0, {"termination": COMPLETED}),
        ],
    )


CLEAN = lifecycle.ColdLifecycleFinalisationResult.CLEAN_RECORDED
CONFIRMED = lifecycle.ColdLifecycleChildStop.CONFIRMED
STARTED = lifecycle.ColdLifecycleChildStart.STARTED
COMPLETED = lifecycle.ColdRunTermination.COMPLETED


def header_of(doc: Json, phase: Phase, mono: float) -> schema.ColdRunHeader:
    """A phase header around one identity document at a chosen admission instant."""
    envelope = envelope_of(doc)
    return schema.ColdRunHeader(
        schema_version=1,
        stream="header",
        run_id=RUN_ID,
        phase=phase,
        recorded_at_utc=T0,
        monotonic_seconds=mono,
        identity_sha256=envelope.sha256,
        identity=envelope,
    )


def tick_record(header: schema.ColdRunHeader, tick: Tick) -> schema.ColdTickRecord:
    """Build one tick through the real builder."""
    if tick.record is not None:
        return tick.record
    device = None if tick.device is None else device_state(**{"driver": DRIVER, **tick.device})
    return builders.build_tick_record(
        header=header,
        tick=tick.index,
        recorded_at_utc=T0,
        monotonic_seconds=tick.mono,
        observation=observation(
            device,
            roast_fan=roast_fan_state(level=tick.roast_fan),
            audio=tick.audio,
            session=session_metadata(
                session_id=tick.session, elapsed_monotonic_seconds=tick.elapsed
            ),
        ),
    )


#: A retained host sample strictly inside every AC15 during-run and start bound.
SAFE_HOST_VALUES: typing.Final[dict[str, object]] = {
    "captured_at_utc": "2026-09-26T12:00:01Z",
    "monotonic_seconds": 2.0,
    "soc_temp_c": 45.5,
    "throttled_word_hex": "0x0",
    "mem_available_bytes": 1024 * 2**20,
    "free_bytes": 4 * 2**30,
}


def safe_host_sample(**overrides: object) -> host.HostBoundSample:
    """The safe local host baseline, with optional single-dimension overrides."""
    return host.HostBoundSample.model_validate({**SAFE_HOST_VALUES, **overrides})


def host_record(
    header: schema.ColdRunHeader, mono: float, **overrides: object
) -> schema.ColdHostRecord:
    """Build one host record from the safe baseline through the real builder."""
    return builders.build_host_record(
        header=header,
        sample=safe_host_sample(**overrides),
        recorded_at_utc=T0,
        monotonic_seconds=mono,
    )


def finalisation(
    header: schema.ColdRunHeader, payload: Json, mono: float
) -> schema.ColdFinalisationRecord:
    """Build one finalisation record from an in-memory result through the real builder."""
    return builders.build_finalisation_record(
        header=header,
        result=SessionFinalisationResult.model_validate_json(json.dumps(payload)),
        recorded_at_utc=T0,
        monotonic_seconds=mono,
    )


def write(tmp_path: Path, run: Plan) -> reader.ColdRetainedRunV2:
    """Write the plan through the real writer, seal it, and read it back strictly (v2)."""
    writer, root = open_writer(tmp_path)
    headers: dict[Phase, schema.ColdRunHeader] = {}
    sequence = 0
    for phase in run.phases:
        header = header_of(run.documents[phase], phase, run.headers[phase])
        headers[phase] = header
        writer.append(header)
        for tick in run.ticks[phase]:
            writer.append(tick_record(header, tick))
        for position, mono in enumerate(run.hosts[phase]):
            overrides = run.host_overrides.get((phase, position), {})
            writer.append(host_record(header, mono, **overrides))
        for payload, mono in run.results[phase]:
            writer.append(finalisation(header, payload, mono))
        for owner, make in run.extras:
            if owner is phase:
                writer.append(make(header))
        for entry in run.lifecycle:
            if entry.phase is not phase:
                continue
            fields = dict(entry.fields)
            if entry.event is Event.OBSERVATION_WINDOW_ELAPSED:
                fields.setdefault("tick_count", len(run.ticks[phase]))
            writer.append_lifecycle(
                builders.build_lifecycle_record(
                    header=header,
                    sequence=sequence,
                    event=entry.event,
                    event_utc=T0,
                    event_monotonic_seconds=entry.ev,
                    recorded_at_utc=T0,
                    monotonic_seconds=entry.rec,
                    **fields,
                )
            )
            sequence += 1
    for owner, make in run.late:
        writer.append(make(headers[owner]))
    digest = writer.seal().manifest_sha256
    return reader.read_retained_run_v2(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def check(run: object) -> conformance.ColdConformanceResult:
    """Run the checker under test."""
    return conformance.check_pre_advisory_conformance(run)


def findings(tmp_path: Path, run: Plan) -> Findings:
    """Write one plan and return its exact finding tuple."""
    result = check(write(tmp_path, run))
    conformant = result.outcome is Outcome.PRE_ADVISORY_CONFORMANT
    assert conformant == (result.findings == ())
    return result.findings


def entry(run: Plan, phase: Phase, event: lifecycle.ColdLifecycleEvent) -> Entry:
    """Return the first lifecycle entry of one phase and event."""
    return next(item for item in run.lifecycle if item.phase is phase and item.event is event)


def interpreted(run: reader.ColdRetainedRunV2) -> acceptance.ColdInterpretation:
    """Interpret the retained run with the delivered acceptance module (independent oracle)."""
    return acceptance.interpret_retained_run(run.run)


def late_pair(phase: Phase, tick: Tick) -> list[tuple[Phase, Maker]]:
    """A tick and its matching host sample, appended after every other record."""

    def make_tick(header: schema.ColdRunHeader) -> schema.ColdEvidenceRecord:
        return tick_record(header, tick)

    def make_host(header: schema.ColdRunHeader) -> schema.ColdEvidenceRecord:
        return host_record(header, tick.mono)

    return [(phase, make_tick), (phase, make_host)]


# ------------------------------------------------------------ positive controls


def test_p1_conforming_run_is_pre_advisory_conformant(tmp_path: Path) -> None:
    """P1: the base run conforms; the result holds exactly the three closed fields."""
    retained = write(tmp_path, plan(tmp_path))
    assert retained.lifecycle_state is lifecycle.ColdLifecycleEvidenceState.PRESENT
    for item in interpreted(retained).phases:
        assert all(r.outcome is acceptance.ColdCheckOutcome.PASS for r in item.results)
    result = check(retained)
    assert result.outcome is Outcome.PRE_ADVISORY_CONFORMANT
    assert result.findings == ()
    assert result.policy_version == 1 == conformance.CONFORMANCE_POLICY_VERSION
    assert set(conformance.ColdConformanceResult.model_fields) == {
        "policy_version",
        "outcome",
        "findings",
    }


Edit = typing.Callable[[Plan], None]


def _tick_at(phase: Phase, index: int, mono: float) -> Edit:
    def edit(run: Plan) -> None:
        run.ticks[phase][index].mono = mono

    return edit


def _off_result_at(mono: float) -> Edit:
    def edit(run: Plan) -> None:
        run.results[OFF][0] = (run.results[OFF][0][0], mono)

    return edit


def _on_header_at(mono: float) -> Edit:
    def edit(run: Plan) -> None:
        run.headers[ON] = mono

    return edit


def _stop_and_start_tied(run: Plan) -> None:
    started = entry(run, OFF, Event.CHILD_STARTED)
    started.ev = started.rec = entry(run, OFF, Event.CHILD_STOPPED).ev


def _on_tail_tied(run: Plan) -> None:
    end = entry(run, ON, Event.OBSERVATION_WINDOW_ELAPSED).ev
    for event in (Event.FINALISATION_RETURNED, Event.CHILD_STOPPED, Event.RUN_TERMINATED):
        tail = entry(run, ON, event)
        tail.ev = tail.rec = end
    run.results[ON][0] = (run.results[ON][0][0], end)


def _on_header_at_phase(phase: Phase, mono: float) -> Edit:
    """Move one phase header's admission instant."""

    def edit(run: Plan) -> None:
        run.headers[phase] = mono

    return edit


def _off_return_event_at(instant: float) -> Edit:
    """Move only the OFF finalisation-return event instant; its recording stays 1811.5."""

    def edit(run: Plan) -> None:
        entry(run, OFF, Event.FINALISATION_RETURNED).ev = instant

    return edit


def _off_child_stop_event_at(instant: float) -> Edit:
    """Move only the OFF child-stop event instant; its recording stays 1812.0."""

    def edit(run: Plan) -> None:
        entry(run, OFF, Event.CHILD_STOPPED).ev = instant

    return edit


INCLUSIVE_POSITIVES: list[tuple[str, Edit]] = [
    ("tick_at_activation", _tick_at(OFF, 0, 10.0)),
    ("tick_at_elapsed", _tick_at(OFF, 2, 1810.0)),
    ("finalisation_at_elapsed", _off_result_at(1810.0)),
    ("finalisation_at_return_recording", _off_result_at(1811.5)),
    ("child_start_equals_on_header", _on_header_at(1813.0)),
    ("on_header_equals_activation", _on_header_at(1830.0)),
    ("child_stop_equals_child_start", _stop_and_start_tied),
    ("on_finalisation_at_terminal_with_tied_chain", _on_tail_tied),
    ("off_header_equals_activation", _on_header_at_phase(OFF, 10.0)),
    ("off_elapsed_equals_return_event", _off_return_event_at(1810.0)),
    ("off_return_event_equals_child_stop", _off_child_stop_event_at(1811.0)),
]


@pytest.mark.parametrize(
    ("name", "edit"), INCLUSIVE_POSITIVES, ids=[c[0] for c in INCLUSIVE_POSITIVES]
)
def test_p2_inclusive_boundaries_conform(tmp_path: Path, name: str, edit: Edit) -> None:
    """P2: every inclusive window and causal tie conforms."""
    del name
    run = plan(tmp_path)
    edit(run)
    assert findings(tmp_path, run) == ()


def test_p3_finalisation_is_bracketed_by_the_return_recording_instant(tmp_path: Path) -> None:
    """P3: a v1 finalisation between the return's event and recording instants conforms."""
    run = plan(tmp_path)
    returned = entry(run, OFF, Event.FINALISATION_RETURNED)
    assert (returned.ev, returned.rec) == (1811.0, 1811.5)
    _off_result_at(1811.3)(run)
    assert findings(tmp_path, run) == ()


def test_p4_overrun_transition_is_measured_from_the_scheduled_end(tmp_path: Path) -> None:
    """P4: a late OFF completion still conforms when activation is 60 s after the scheduled end."""
    run = plan(tmp_path, e_off=1850.0, a_on=1870.0)
    activated = entry(run, ON, Event.PHASE_ACTIVATED).ev
    scheduled_end = entry(run, OFF, Event.OBSERVATION_WINDOW_ELAPSED).fields[
        "scheduled_end_monotonic"
    ]
    assert activated - scheduled_end == 60.0 == BUDGET
    assert findings(tmp_path, run) == ()


def _reset_on(*leaves: tuple[str, str]) -> Edit:
    def edit(run: Plan) -> None:
        for section, leaf in leaves:
            run.documents[ON][section][leaf] = run.documents[OFF][section][leaf]

    return edit


_STARTED = ("server_info", "started_at_utc")
_SHA = ("effective_mcp_profile", "source_sha256")
_LENGTH = ("effective_mcp_profile", "source_byte_length")
OPTIONAL_LEAF_CASES: list[tuple[str, Edit]] = [
    ("started_at_only", _reset_on(_SHA, _LENGTH)),
    ("source_sha256_only", _reset_on(_STARTED, _LENGTH)),
    ("source_byte_length_only", _reset_on(_STARTED, _SHA)),
    ("all_three", _reset_on()),
]


@pytest.mark.parametrize(
    ("name", "edit"), OPTIONAL_LEAF_CASES, ids=[c[0] for c in OPTIONAL_LEAF_CASES]
)
def test_p5_optional_identity_leaves_may_differ(tmp_path: Path, name: str, edit: Edit) -> None:
    """P5: each optional allowlisted leaf may differ alone or together; flags always differ."""
    run = plan(tmp_path)
    edit(run)
    off, on = run.documents[OFF], run.documents[ON]
    differing = {
        leaf for leaf in (_STARTED, _SHA, _LENGTH) if off[leaf[0]][leaf[1]] != on[leaf[0]][leaf[1]]
    }
    expected = {"started_at_only": {_STARTED}, "source_sha256_only": {_SHA}}
    expected |= {"source_byte_length_only": {_LENGTH}, "all_three": {_STARTED, _SHA, _LENGTH}}
    assert differing == expected[name]
    assert off["device_config"]["recording_enabled"] is False
    assert on["device_config"]["recording_enabled"] is True
    assert findings(tmp_path, run) == ()


def _d191_at_limits(run: Plan) -> None:
    run.ticks[ON][1].audio = audio(1, estimated_lost_audio_ms_last_minute=200.0)
    final = final_status(max_consecutive_overflow_count=1, total_overflow_count=1)
    run.results[ON][0] = (
        result_for(ON, session_id=S_ON, first_crack_runtime=runtime(final)),
        run.results[ON][0][1],
    )


def test_p6_d191_limits_pass_inclusively(tmp_path: Path) -> None:
    """P6: N equal to its limit and X equal to its limit conform."""
    run = plan(tmp_path)
    _d191_at_limits(run)
    retained = write(tmp_path, run)
    metrics = interpreted(retained).phases[1].d191
    assert metrics is not None
    assert (metrics.max_consecutive_overflow_count, metrics.peak_trailing_lost_audio_ms) == (
        acceptance.D191_N_LIMIT,
        acceptance.D191_X_LIMIT_MS,
    )
    assert check(retained).findings == ()


def _nest(levels: int) -> object:
    value: object = 0
    for _ in range(levels):
        value = {"n": value}
    return value


def _with_raw(
    record: schema.ColdTickRecord, vendor: Json, extra: Json | None = None
) -> schema.ColdTickRecord:
    assert record.device is not None
    device = record.device.model_copy(update={"raw_vendor_data": vendor})
    update: dict[str, object] = {"device": device}
    if extra is not None:
        update["raw_audio_extra"] = extra
    return record.model_copy(update=update)


def _admits(record: schema.ColdTickRecord) -> schema.ColdEvidenceFailure | None:
    """``validate_record`` as the independent admission oracle: ``None`` when admitted."""
    try:
        schema.validate_record(record)
    except schema.ColdEvidenceError as error:
        return error.failure
    return None


def _base_on_tick(tmp_path: Path, run: Plan) -> schema.ColdTickRecord:
    header = header_of(run.documents[ON], ON, run.headers[ON])
    return tick_record(header, run.ticks[ON][0])


def _replace_on_tick(run: Plan, record: schema.ColdTickRecord) -> None:
    run.ticks[ON][0].record = record


def _deepest(build: typing.Callable[[int], schema.ColdTickRecord]) -> int:
    levels = 0
    while _admits(build(levels + 1)) is None:
        levels += 1
    assert _admits(build(levels + 1)) is schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED
    return levels


def test_p7_record_at_validate_record_depth_key_int_and_collection_limits(tmp_path: Path) -> None:
    """P7: a tick at the validator's own depth, key, int and collection limits is admitted."""
    run = plan(tmp_path)
    base = _base_on_tick(tmp_path, run)
    bound = 10**schema.MAX_INT_DIGITS - 1
    wide: Json = {"k" * schema.MAX_JSON_KEY_BYTES: 1, "big": bound, "neg": -bound}
    wide["items"] = [0] * schema.MAX_COLLECTION_LENGTH
    vendor_depth = _deepest(lambda levels: _with_raw(base, {**wide, "deep": _nest(levels)}))
    extra_depth = _deepest(lambda levels: _with_raw(base, {}, {"deep": _nest(levels)}))
    assert (vendor_depth, extra_depth) == (5, 6)
    record = _with_raw(base, {**wide, "deep": _nest(vendor_depth)}, {"deep": _nest(extra_depth)})
    assert _admits(record) is None
    assert _admits(
        _with_raw(base, {**wide, "items": [0] * (schema.MAX_COLLECTION_LENGTH + 1)})
    ) is (schema.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
    assert _admits(_with_raw(base, {"k" * (schema.MAX_JSON_KEY_BYTES + 1): 1})) is (
        schema.ColdEvidenceFailure.JSON_KEY_INVALID
    )
    assert _admits(_with_raw(base, {"big": bound + 1})) is (
        schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    )
    _replace_on_tick(run, record)
    assert findings(tmp_path, run) == ()


def _node_vendor(tail: int) -> Json:
    full = [0] * schema.MAX_COLLECTION_LENGTH
    return {"a": full, "b": list(full), "c": list(full), "d": [0] * tail}


def test_p7_record_at_validate_record_node_limit(tmp_path: Path) -> None:
    """P7: a tick holding exactly the validator's maximum node count is admitted."""
    run = plan(tmp_path)
    base = _base_on_tick(tmp_path, run)
    low, high = 0, schema.MAX_COLLECTION_LENGTH
    assert _admits(_with_raw(base, _node_vendor(low))) is None
    assert _admits(_with_raw(base, _node_vendor(high))) is not None
    while high - low > 1:
        middle = (low + high) // 2
        if _admits(_with_raw(base, _node_vendor(middle))) is None:
            low = middle
        else:
            high = middle
    assert _admits(_with_raw(base, _node_vendor(low + 1))) is (
        schema.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED
    )
    _replace_on_tick(run, _with_raw(base, _node_vendor(low)))
    assert findings(tmp_path, run) == ()


def test_p7_large_identity_at_the_admitted_header_boundary(tmp_path: Path) -> None:
    """A1-2: the largest identity the frozen header validator admits is admitted here.

    ``MAX_RECORD_BYTES`` on the header record dominates the identity size, so the
    nominal identity text budget is not reachable; the actual boundary is tested.
    """
    run = plan(tmp_path)

    def header(length: int) -> schema.ColdRunHeader:
        doc = json.loads(json.dumps(run.documents[OFF]))
        doc["server_info"]["bulk"] = "x" * length
        return header_of(doc, OFF, run.headers[OFF])

    def admitted(length: int) -> bool:
        try:
            schema.validate_record(header(length))
        except schema.ColdEvidenceError as error:
            assert error.failure is schema.ColdEvidenceFailure.RECORD_TOO_LARGE
            return False
        return True

    low, high = 0, schema.MAX_RECORD_BYTES
    assert admitted(low) and not admitted(high)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if admitted(middle) else (low, middle)
    assert low > 200_000
    for phase in (OFF, ON):
        run.documents[phase]["server_info"]["bulk"] = "x" * low
    retained = write(tmp_path, run)
    assert len(retained.run.headers[0].header.identity.canonical_json) > 200_000
    assert check(retained).findings == ()


def test_p8_backdated_off_tick_after_on_header_documents_the_limitation(tmp_path: Path) -> None:
    """P8 (limitation pin): append provenance is not proven; a backdated tick conforms."""
    run = plan(tmp_path)
    late = Tick(13.5, 3, S_OFF, 4.0, audio(3))
    run.late += late_pair(OFF, late)
    entry(run, OFF, Event.OBSERVATION_WINDOW_ELAPSED).fields["tick_count"] = 4
    retained = write(tmp_path, run)
    assert [tick.tick for tick in interpreted(retained).rebound.phases[0].ticks] == [0, 1, 2, 3]
    assert check(retained).findings == ()


# ------------------------------------------------------------ negative controls


def _abort(domain: schema.ColdAbortDomain, mono: float) -> Maker:
    reason: typing.Any = next(iter(reader.ABORT_REASON_BY_DOMAIN[domain]))
    return lambda header: schema.ColdAbortRecord(
        schema_version=1,
        stream="abort",
        run_id=header.run_id,
        phase=header.phase,
        recorded_at_utc=T0,
        monotonic_seconds=mono,
        identity_sha256=header.identity_sha256,
        domain=domain,
        reason=reason,
    )


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize("domain", list(schema.ColdAbortDomain))
def test_n1_any_abort_record_refuses(
    tmp_path: Path, phase: Phase, domain: schema.ColdAbortDomain
) -> None:
    """N1: one in-window abort in any of the seven domains, in either phase."""
    run = plan(tmp_path)
    run.extras.append((phase, _abort(domain, run.ticks[phase][0].mono + 0.5)))
    assert findings(tmp_path, run) == (F.ABORT_RECORDED,)


@pytest.mark.parametrize("phase", [OFF, ON])
def test_n3_empty_tick_stream_refuses(tmp_path: Path, phase: Phase) -> None:
    """N3: a phase with no ticks and no hosts (tick_count 0) never conforms."""
    run = plan(tmp_path)
    run.ticks[phase], run.hosts[phase] = [], []
    assert findings(tmp_path, run) == (
        F.TICKS_ABSENT,
        F.ACCEPTANCE_CHECK_FAILED,
        F.D191_METRICS_UNAVAILABLE,
    )


def _only(phase: Phase) -> Edit:
    def edit(run: Plan) -> None:
        run.phases = (phase,)
        run.lifecycle = [item for item in run.lifecycle if item.phase is phase]

    return edit


def _set(phase: Phase, event: lifecycle.ColdLifecycleEvent, **fields: object) -> Edit:
    def edit(run: Plan) -> None:
        entry(run, phase, event).fields.update(fields)

    return edit


def _drop_terminal(run: Plan) -> None:
    run.lifecycle.remove(entry(run, ON, Event.RUN_TERMINATED))


def _insert_aborted(run: Plan) -> None:
    fields = {"session_id": S_OFF, "activation_deadline_exceeded": False}
    aborted = Entry(OFF, Event.PHASE_ABORTED_NOT_FINALISED, 1810.5, 1810.5, fields)
    run.lifecycle.insert(2, aborted)


def _on_result(**changes: object) -> Edit:
    def edit(run: Plan) -> None:
        payload = result_for(ON, session_id=S_ON)
        payload.update(changes)
        run.results[ON][0] = (payload, run.results[ON][0][1])

    return edit


def _driver_evidence(**changes: object) -> Edit:
    def edit(run: Plan) -> None:
        payload = result_for(ON, session_id=S_ON)
        payload["final_driver_evidence"]["evidence"].update(changes)
        run.results[ON][0] = (payload, run.results[ON][0][1])

    return edit


def _driver_unreadable(run: Plan) -> None:
    payload = result_for(ON, session_id=S_ON)
    final = payload["final_driver_evidence"]
    final.update(outcome="unreadable", error="driver_state_unreadable", evidence=None)
    run.results[ON][0] = (payload, run.results[ON][0][1])


def _two_off_results(run: Plan) -> None:
    payload, _ = run.results[OFF][0]
    run.results[OFF] = [(payload, 1811.0), (payload, 1811.2)]


def _tick(phase: Phase, position: int, **changes: typing.Any) -> Edit:
    def edit(run: Plan) -> None:
        for name, value in changes.items():
            setattr(run.ticks[phase][position], name, value)

    return edit


def _drop_host(run: Plan) -> None:
    run.hosts[OFF].pop()


def _on_ticks_from_1832(run: Plan) -> None:
    run.headers[ON] = 1831.0
    for tick in run.ticks[ON]:
        tick.mono += 1.0
    run.hosts[ON] = [tick.mono for tick in run.ticks[ON]]


def _on_tail_recorded_late(run: Plan) -> None:
    for event in (Event.FINALISATION_RETURNED, Event.CHILD_STOPPED, Event.RUN_TERMINATED):
        entry(run, ON, event).rec = 3640.0
    run.results[ON][0] = (run.results[ON][0][0], 3635.0)


def _late_on_tick(run: Plan) -> None:
    late = Tick(3634.0, 3, S_ON, 4.0, audio(3))
    run.late += late_pair(ON, late)
    entry(run, ON, Event.OBSERVATION_WINDOW_ELAPSED).fields["tick_count"] = 4


def _doc(phase: Phase, path: str, value: object) -> Edit:
    *parents, leaf = path.split(".")

    def edit(run: Plan) -> None:
        target = run.documents[phase]
        for key in parents:
            target = target[key]
        target[leaf] = value

    return edit


def _both(*edits: Edit) -> Edit:
    def edit(run: Plan) -> None:
        for item in edits:
            item(run)

    return edit


def _on_audio(**columns: tuple[object, object, object]) -> Edit:
    def edit(run: Plan) -> None:
        for index, tick in enumerate(run.ticks[ON]):
            tick.audio = audio(index, **{name: values[index] for name, values in columns.items()})

    return edit


def _on_counters(pre: Json, final: Json) -> Edit:
    def edit(run: Plan) -> None:
        payload = result_for(
            ON,
            session_id=S_ON,
            pre_finalisation_first_crack_status=status(**pre),
            first_crack_runtime=runtime(final_status(**final)),
        )
        run.results[ON][0] = (payload, run.results[ON][0][1])

    return edit


_NONE: dict[str, typing.Any] = {}
_TERMINATION_FAILED = {
    "termination": lifecycle.ColdRunTermination.FAILED,
    "termination_reason": lifecycle.ColdRunTerminationReason.UNEXPECTED_FAILURE,
}
_RESULT = lifecycle.ColdLifecycleFinalisationResult
_NFC = unicodedata.normalize("NFC", "sesión-recording-on")
_N34_OVERFLOW = _both(
    _on_audio(max_consecutive_overflow_count=(0, 1, 2), total_overflow_count=(0, 1, 2)),
    _on_counters(
        {"max_consecutive_overflow_count": 2, "total_overflow_count": 2},
        {"max_consecutive_overflow_count": 2, "total_overflow_count": 2},
    ),
)

#: ``(id, plan keyword arguments, edit, exact expected findings)``.
WRITER_CASES: list[tuple[str, dict[str, typing.Any], Edit, Findings]] = [
    (
        "n2_advisory",
        _NONE,
        lambda run: run.extras.append(
            (OFF, lambda h: advisory_for_at(h, 12.5)),
        ),
        (F.ADVISORY_EVIDENCE_PRESENT,),
    ),
    (
        "n4_off_only",
        _NONE,
        _only(OFF),
        (F.PHASE_MISSING, F.LIFECYCLE_GRAMMAR_MISMATCH, F.TERMINAL_ABSENT),
    ),
    ("n4_on_only", _NONE, _only(ON), (F.PHASE_MISSING, F.LIFECYCLE_GRAMMAR_MISMATCH)),
    ("n5_v1_only", _NONE, lambda run: setattr(run, "lifecycle", []), (F.LIFECYCLE_ABSENT,)),
    (
        "n7_failed_terminal",
        _NONE,
        _set(ON, Event.RUN_TERMINATED, **_TERMINATION_FAILED),
        (F.TERMINATION_NOT_COMPLETED,),
    ),
    (
        "n8_terminal_missing",
        _NONE,
        _drop_terminal,
        (F.LIFECYCLE_GRAMMAR_MISMATCH, F.TERMINAL_ABSENT),
    ),
    (
        "n9_stop_unconfirmed",
        _NONE,
        _set(OFF, Event.CHILD_STOPPED, child_stop=lifecycle.ColdLifecycleChildStop.UNCONFIRMED),
        (F.CHILD_OPERATION_NOT_CLEAN,),
    ),
    (
        "n9_start_failed",
        _NONE,
        _set(OFF, Event.CHILD_STARTED, child_start=lifecycle.ColdLifecycleChildStart.FAILED),
        (F.CHILD_OPERATION_NOT_CLEAN,),
    ),
    *[
        (
            f"n10_{member.value}",
            _NONE,
            _set(ON, Event.FINALISATION_RETURNED, finalisation_result=member),
            (F.FINALISATION_RESULT_NOT_RECORDED_CLEAN,),
        )
        for member in (
            _RESULT.NOT_CLEAN_RECORDED,
            _RESULT.FAILED_WITHOUT_RESULT,
            _RESULT.RECORD_NOT_RETAINED,
        )
    ],
    (
        "n11_phase_aborted",
        _NONE,
        _insert_aborted,
        (F.LIFECYCLE_GRAMMAR_MISMATCH, F.PHASE_ABORTED_NOT_FINALISED),
    ),
    (
        "n12_completed_but_not_clean",
        _NONE,
        _on_result(session_active_after=True),
        (F.FINALISATION_NOT_CLEAN,),
    ),
    (
        "n13_roast_purpose",
        _NONE,
        _on_result(session_purpose="roast"),
        (F.FINALISATION_NOT_COLD_PURPOSE,),
    ),
    *[
        (
            f"n14_{name}",
            _NONE,
            _driver_evidence(**{name: value}),
            (F.FINALISATION_SAFETY_EVIDENCE_MISSING,),
        )
        for name, value in (
            ("heat_level_percent", 1),
            ("roast_fan_level_percent", 1),
            ("main_fan_level_percent", 1),
            ("drum_motor_on", True),
            ("cooling_motor_on", True),
            ("solenoid_open", True),
        )
    ],
    ("n14_driver_unreadable", _NONE, _driver_unreadable, (F.FINALISATION_SAFETY_EVIDENCE_MISSING,)),
    ("n15_two_finalisations", _NONE, _two_off_results, (F.FINALISATION_NOT_UNIQUE,)),
    (
        "n16_finalisation_session",
        _NONE,
        _on_result(session_id="other"),
        (F.FINALISATION_SESSION_MISMATCH,),
    ),
    (
        "n17_elapsed_session",
        _NONE,
        _set(OFF, Event.OBSERVATION_WINDOW_ELAPSED, session_id="session-other"),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    (
        "n17_previous_session",
        _NONE,
        _set(ON, Event.TRANSITION_MEASURED, previous_phase_session_id="session-other"),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    (
        "n18_foreign_tick_session",
        _NONE,
        _tick(OFF, 1, session="session-other"),
        (F.SESSION_BINDING_MISMATCH, F.RETAINED_TICK_POLICY_VIOLATED),
    ),
    (
        "n19_trailing_space",
        _NONE,
        _set(ON, Event.OBSERVATION_WINDOW_ELAPSED, session_id=S_ON + " "),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    (
        "n19_nfd_of_nfc",
        {"s_on": _NFC},
        _set(ON, Event.OBSERVATION_WINDOW_ELAPSED, session_id=unicodedata.normalize("NFD", _NFC)),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    ("n20_same_sessions", {"s_on": S_OFF}, lambda run: None, (F.PHASE_SESSIONS_NOT_DISTINCT,)),
    (
        "n21_scheduled_end",
        _NONE,
        _set(
            OFF,
            Event.OBSERVATION_WINDOW_ELAPSED,
            scheduled_end_monotonic=math.nextafter(1810.0, -math.inf),
        ),
        (F.SCHEDULED_END_MISMATCH,),
    ),
    (
        "n22_tick_count",
        _NONE,
        _set(OFF, Event.OBSERVATION_WINDOW_ELAPSED, tick_count=2),
        (F.TICK_COUNT_MISMATCH,),
    ),
    (
        "n22_tick_indices",
        _NONE,
        _both(_tick(OFF, 1, index=2), _tick(OFF, 2, index=3)),
        (F.TICK_COUNT_MISMATCH,),
    ),
    (
        "n23_before_activation",
        _NONE,
        _tick(OFF, 0, mono=math.nextafter(10.0, -math.inf)),
        (F.TICK_OUTSIDE_WINDOW,),
    ),
    (
        "n23_after_elapsed",
        _NONE,
        _tick(OFF, 2, mono=math.nextafter(1810.0, math.inf)),
        (F.TICK_OUTSIDE_WINDOW,),
    ),
    ("n24_host_missing", _NONE, _drop_host, (F.HOST_EVIDENCE_MISMATCH,)),
    (
        "n25_heat",
        _NONE,
        _tick(ON, 1, device={"heat_level_percent": 1}),
        (F.RETAINED_TICK_POLICY_VIOLATED,),
    ),
    ("n25_roast_fan", _NONE, _tick(ON, 1, roast_fan=1), (F.RETAINED_TICK_POLICY_VIOLATED,)),
    ("n25_stalled_clock", _NONE, _tick(ON, 1, elapsed=1.0), (F.RETAINED_TICK_POLICY_VIOLATED,)),
    (
        "n25_driver",
        _NONE,
        _tick(ON, 1, device={"driver": "mock"}),
        (F.RETAINED_TICK_POLICY_VIOLATED,),
    ),
    ("n25_null_device", _NONE, _tick(ON, 1, device=None), (F.RETAINED_TICK_POLICY_VIOLATED,)),
    (
        "n26_budget",
        {"a_on": math.nextafter(1810.0 + 60.0, math.inf)},
        lambda run: None,
        (F.TRANSITION_BUDGET_EXCEEDED,),
    ),
    (
        "n27_misbound",
        {"e_off": 1850.0, "t_start": 1850.0, "a_on": 1870.0},
        lambda run: None,
        (F.TRANSITION_NOT_BOUND,),
    ),
    (
        "n28_misbound_and_over",
        {"e_off": 1850.0, "t_start": 1850.0, "a_on": 1871.0},
        lambda run: None,
        (F.TRANSITION_NOT_BOUND, F.TRANSITION_BUDGET_EXCEEDED),
    ),
    (
        "n29_after_return_recording",
        _NONE,
        _off_result_at(math.nextafter(1811.5, math.inf)),
        (F.CAUSAL_ORDER_VIOLATED,),
    ),
    ("n30_on_header_after_activation", _NONE, _on_ticks_from_1832, (F.CAUSAL_ORDER_VIOLATED,)),
    ("n31_after_terminal_event", _NONE, _on_tail_recorded_late, (F.RECORD_AFTER_TERMINATION,)),
    ("n32_late_tick", _NONE, _late_on_tick, (F.TICK_OUTSIDE_WINDOW, F.RECORD_AFTER_TERMINATION)),
    (
        "n33_dirty_source",
        _NONE,
        _both(
            _doc(OFF, "build_provenance.source_tree_dirty", True),
            _doc(ON, "build_provenance.source_tree_dirty", True),
        ),
        (F.ACCEPTANCE_CHECK_FAILED,),
    ),
    ("n34_n_exceeded", _NONE, _N34_OVERFLOW, (F.D191_N_EXCEEDED,)),
    (
        "n36_decreasing_total",
        _NONE,
        _on_audio(total_overflow_count=(1, 0, 0)),
        (F.ACCEPTANCE_CHECK_FAILED, F.D191_METRICS_UNAVAILABLE),
    ),
    *[
        (f"n37_{name}", _NONE, edit, (F.IDENTITY_DELTA_NOT_ADMITTED,))
        for name, edit in (
            (
                "off_flags_true",
                _both(
                    _doc(OFF, "device_config.recording_enabled", True),
                    _doc(OFF, "device_config.recording_autocapture", True),
                ),
            ),
            ("flag_none", _doc(OFF, "device_config.recording_enabled", None)),
            (
                "equal_flags",
                _both(
                    _doc(ON, "device_config.recording_enabled", False),
                    _doc(ON, "device_config.recording_autocapture", False),
                ),
            ),
            ("run_started_at", _doc(ON, "started_at_utc", "2026-09-26T12:31:00Z")),
            ("advisor_model", _doc(ON, "advisor_model", "test/other-model")),
            ("credential_present", _doc(ON, "credential_present", False)),
            ("runtime_extra", _doc(ON, "runtime_config.future_key", "x")),
            (
                "server_extra_int_float",
                _both(_doc(OFF, "server_info.future", 1), _doc(ON, "server_info.future", 1.0)),
            ),
            (
                "server_extra_bool_int",
                _both(_doc(OFF, "server_info.future", True), _doc(ON, "server_info.future", 1)),
            ),
        )
    ],
]


def advisory_for_at(header: schema.ColdRunHeader, mono: float) -> schema.ColdAdvisoryRecord:
    """One observation-only advisory record at a chosen in-window instant."""
    return schema.ColdAdvisoryRecord(
        schema_version=1,
        stream="advisory",
        run_id=header.run_id,
        phase=header.phase,
        recorded_at_utc=T0,
        monotonic_seconds=mono,
        identity_sha256=header.identity_sha256,
        requested_heat=0,
        requested_fan=0,
        should_drop=False,
        confidence=0.5,
        latency_seconds=0.25,
        evaluation=schema.ColdSafetyEvaluation(
            rule="observation_only",
            verdict=schema.ColdSafetyVerdict.REJECT,
            input_heat=0,
            input_fan=0,
            adjusted_heat=None,
            adjusted_fan=None,
            reason="observation only",
        ),
        failure=None,
    )


@pytest.mark.parametrize(
    ("name", "kwargs", "edit", "expected"), WRITER_CASES, ids=[c[0] for c in WRITER_CASES]
)
def test_writer_built_negative_cases_pin_exact_findings(
    tmp_path: Path, name: str, kwargs: dict[str, typing.Any], edit: Edit, expected: Findings
) -> None:
    """N2-N37 (writer-built): each targeted inconsistency yields its exact full tuple."""
    del name
    run = plan(tmp_path, **kwargs)
    edit(run)
    assert findings(tmp_path, run) == expected


def test_n26_to_n28_recompute_the_transition_from_facts() -> None:
    """N26-N28 oracle: the recomputed durations the exact tuples above rely on."""
    assert math.nextafter(1810.0 + 60.0, math.inf) - 1810.0 > BUDGET
    durations = {"n27_scheduled": 1870.0 - 1810.0, "n27_completed": 1870.0 - 1850.0}
    durations |= {"n28_scheduled": 1871.0 - 1810.0, "n28_completed": 1871.0 - 1850.0}
    assert durations == {
        "n27_scheduled": BUDGET,
        "n27_completed": 20.0,
        "n28_scheduled": 61.0,
        "n28_completed": 21.0,
    }


def test_n35_x_just_above_the_limit_refuses(tmp_path: Path) -> None:
    """N35: one tick's lost audio just above the limit makes X exceed it."""
    run = plan(tmp_path)
    above = math.nextafter(acceptance.D191_X_LIMIT_MS, math.inf)
    run.ticks[ON][0].audio = audio(0, estimated_lost_audio_ms_last_minute=above)
    retained = write(tmp_path, run)
    metrics = interpreted(retained).phases[1].d191
    assert metrics is not None and metrics.peak_trailing_lost_audio_ms == above
    assert check(retained).findings == (F.D191_X_EXCEEDED,)


# ------------------------------------------------ in-process carriers (N6, N38-N43)


def _replace_record(
    run: reader.ColdRetainedRunV2,
    phase: Phase,
    stream: schema.ColdEvidenceStream,
    record: object,
) -> reader.ColdRetainedRunV2:
    streams: list[reader.ColdRetainedStream] = []
    for item in run.run.streams:
        if item.phase is phase and item.stream is stream:
            item = item.model_copy(update={"records": (record, *item.records[1:])})
        streams.append(item)
    return run.model_copy(update={"run": run.run.model_copy(update={"streams": tuple(streams)})})


def _replace_stream(
    run: reader.ColdRetainedRunV2, position: int, item: object
) -> reader.ColdRetainedRunV2:
    streams: list[object] = list(run.run.streams)
    streams[position] = item
    return run.model_copy(update={"run": run.run.model_copy(update={"streams": tuple(streams)})})


def _replace_identity(run: reader.ColdRetainedRunV2, identity: object) -> reader.ColdRetainedRunV2:
    first = run.run.headers[0].model_copy(update={"identity": identity})
    headers = (first, *run.run.headers[1:])
    return run.model_copy(update={"run": run.run.model_copy(update={"headers": headers})})


def _first_tick(run: reader.ColdRetainedRunV2) -> schema.ColdTickRecord:
    return interpreted(run).rebound.phases[0].ticks[0]


def _with_extra_key(model: pydantic.BaseModel) -> pydantic.BaseModel:
    copy = model.model_copy()
    object.__getattribute__(copy, "__dict__")["undeclared"] = 1
    return copy


def test_n6_lifecycle_out_of_phase_order_is_not_admitted(tmp_path: Path) -> None:
    """N6: a validly constructed carrier whose lifecycle puts an OFF record after an ON one."""
    retained = write(tmp_path, plan(tmp_path))
    items = list(retained.lifecycle)
    items[4], items[5] = items[5], items[4]
    assert (items[4].phase, items[5].phase) == (ON, OFF)
    swapped = reader.ColdRetainedRunV2(
        run=retained.run,
        lifecycle_state=lifecycle.ColdLifecycleEvidenceState.PRESENT,
        lifecycle=tuple(items),
    )
    assert check(swapped).findings == (F.LIFECYCLE_NOT_ADMITTED,)


class SubRunV2(reader.ColdRetainedRunV2):
    """A carrier subclass (never admitted)."""


class SubLifecycle(lifecycle.ColdLifecycleRecord):
    """A lifecycle record subclass (never admitted)."""


Forge = typing.Callable[[reader.ColdRetainedRunV2], object]


def _construct(genuine: reader.ColdRetainedRunV2, /, **changes: typing.Any) -> object:
    values: dict[str, typing.Any] = {
        "run": genuine.run,
        "lifecycle_state": genuine.lifecycle_state,
        "lifecycle": genuine.lifecycle,
    }
    return reader.ColdRetainedRunV2.model_construct(**(values | changes))


def _pydantic_extra(run: reader.ColdRetainedRunV2) -> object:
    copy = run.model_copy()
    object.__setattr__(copy, "__pydantic_extra__", {"undeclared": 1})
    return copy


def _subclass_item(run: reader.ColdRetainedRunV2) -> object:
    values: dict[str, typing.Any] = dict(run.lifecycle[0])
    first = SubLifecycle.model_construct(**values)
    return _construct(run, lifecycle=(first, *run.lifecycle[1:]))


def _deep_record(run: reader.ColdRetainedRunV2) -> object:
    tick = _first_tick(run)
    assert tick.device is not None
    device = tick.device.model_copy(update={"raw_vendor_data": _nest(12)})
    values: dict[str, typing.Any] = dict(tick) | {"device": device}
    forged = schema.ColdTickRecord.model_construct(**values)
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, forged)


def _forged_enum(run: reader.ColdRetainedRunV2) -> object:
    forged = object.__new__(schema.ColdPhaseKind)
    return _replace_stream(run, 0, run.run.streams[0].model_copy(update={"phase": forged}))


def _forged_event() -> object:
    """An instance of the real lifecycle event enum class that is no member of it."""
    forged = object.__new__(lifecycle.ColdLifecycleEvent)
    assert type(forged) is lifecycle.ColdLifecycleEvent
    assert not any(forged is member for member in lifecycle.ColdLifecycleEvent)
    return forged


def _lifecycle_field(run: reader.ColdRetainedRunV2, **update: object) -> object:
    """A copy of an actual lifecycle record with raw field values replaced, put back."""
    first = run.lifecycle[0].model_copy(update=update)
    return _construct(run, lifecycle=(first, *run.lifecycle[1:]))


CARRIER_FORGERIES: list[tuple[str, Forge]] = [
    ("lifecycle_list", lambda run: _construct(run, lifecycle=list(run.lifecycle))),
    (
        "state_mismatch",
        lambda run: _construct(run, lifecycle_state=lifecycle.ColdLifecycleEvidenceState.ABSENT),
    ),
    ("pydantic_extra", _pydantic_extra),
    ("carrier_subclass", lambda run: SubRunV2.model_construct(**dict(run))),
    ("extra_dict_key", _with_extra_key),
    ("run_as_dict", lambda run: _construct(run, run=run.run.model_dump())),
    ("lifecycle_subclass_item", _subclass_item),
    ("retained_run_extra_key", lambda run: _construct(run, run=_with_extra_key(run.run))),
    ("record_over_depth", _deep_record),
    ("forged_enum_object", _forged_enum),
    ("forged_lifecycle_event_enum", lambda run: _lifecycle_field(run, event=_forged_event())),
]


@pytest.mark.parametrize(
    ("name", "forge"), CARRIER_FORGERIES, ids=[c[0] for c in CARRIER_FORGERIES]
)
def test_n38_forged_carriers_are_not_admitted(tmp_path: Path, name: str, forge: Forge) -> None:
    """N38: each forged carrier shape is refused before anything else reads it."""
    del name
    retained = write(tmp_path, plan(tmp_path))
    assert check(retained).findings == ()
    assert check(forge(retained)).findings == (F.CARRIER_NOT_ADMITTED,)


def test_n38_forged_enum_object_is_not_a_member() -> None:
    """N38 precondition: the forged enum object has the enum type but is no member."""
    forged = object.__new__(schema.ColdPhaseKind)
    assert type(forged) is schema.ColdPhaseKind
    assert not any(forged is member for member in schema.ColdPhaseKind)


SPY_CALLS: list[str] = []


class SpyStr(str):
    """A ``str`` subclass whose hash collides with its field name and which counts use."""

    def __hash__(self) -> int:
        SPY_CALLS.append("hash")
        return str.__hash__(self)

    def __eq__(self, other: object) -> bool:
        SPY_CALLS.append("eq")
        return str.__eq__(self, other)


class SpyMeta(type):
    """A metaclass counting equality, hashing and instance checks."""

    def __eq__(cls, other: object) -> bool:
        SPY_CALLS.append("meta_eq")
        return type.__eq__(cls, other)

    def __hash__(cls) -> int:
        SPY_CALLS.append("meta_hash")
        return type.__hash__(cls)

    def __instancecheck__(cls, instance: object) -> bool:
        SPY_CALLS.append("meta_instancecheck")
        return False


class SpyModel(metaclass=SpyMeta):
    """An instance of a spy-metaclass class."""


class Spy:
    """A value object counting every protocol the checker must never invoke."""

    def __eq__(self, other: object) -> bool:
        SPY_CALLS.append("eq")
        return False

    def __hash__(self) -> int:
        SPY_CALLS.append("hash")
        return 0

    def __bool__(self) -> bool:
        SPY_CALLS.append("bool")
        return True

    def __len__(self) -> int:
        SPY_CALLS.append("len")
        return 0

    def __iter__(self) -> typing.Iterator[object]:
        SPY_CALLS.append("iter")
        return iter(())

    def __str__(self) -> str:
        SPY_CALLS.append("str")
        return ""

    def __repr__(self) -> str:
        SPY_CALLS.append("repr")
        return ""

    def __index__(self) -> int:
        SPY_CALLS.append("index")
        return 0

    def __float__(self) -> float:
        SPY_CALLS.append("float")
        return 0.0

    def __getattribute__(self, name: str) -> typing.Any:
        SPY_CALLS.append("getattribute")
        return object.__getattribute__(self, name)


def _spy_key(model: pydantic.BaseModel, name: str) -> pydantic.BaseModel:
    copy = model.model_copy()
    data = object.__getattribute__(copy, "__dict__")
    data[SpyStr(name)] = data.pop(name)
    return copy


def _spy_lifecycle_key(run: reader.ColdRetainedRunV2) -> object:
    first = _spy_key(run.lifecycle[0], "event")
    return _construct(run, lifecycle=(first, *run.lifecycle[1:]))


def _spy_device_key(run: reader.ColdRetainedRunV2) -> object:
    tick = _first_tick(run)
    assert tick.device is not None
    forged = tick.model_copy(update={"device": _spy_key(tick.device, "driver")})
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, forged)


def _spy_known_key(run: reader.ColdRetainedRunV2) -> object:
    identity = run.run.headers[0].identity
    known: dict[typing.Any, typing.Any] = dict(identity.known)
    known[SpyStr("run_id")] = known.pop("run_id")
    return _replace_identity(run, identity.model_copy(update={"known": known}))


def _vendor(run: reader.ColdRetainedRunV2, vendor: object) -> object:
    tick = _first_tick(run)
    assert tick.device is not None
    device = tick.device.model_copy(update={"raw_vendor_data": vendor})
    return _replace_record(
        run, OFF, schema.ColdEvidenceStream.TICK, tick.model_copy(update={"device": device})
    )


def _spy_metaclass(run: reader.ColdRetainedRunV2) -> object:
    tick = _first_tick(run)
    return _replace_record(
        run, OFF, schema.ColdEvidenceStream.TICK, tick.model_copy(update={"device": SpyModel()})
    )


def _spy_record_field(run: reader.ColdRetainedRunV2) -> object:
    tick = _first_tick(run)
    forged = tick.model_copy(update={"recorded_at_utc": Spy()})
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, forged)


def _spy_identity_leaf(run: reader.ColdRetainedRunV2) -> object:
    identity = run.run.headers[0].identity
    known = {**identity.known, "spy": Spy()}
    return _replace_identity(run, identity.model_copy(update={"known": known}))


def _self_reference(run: reader.ColdRetainedRunV2) -> object:
    loop: dict[str, object] = {"packet": "abc"}
    loop["self"] = loop
    return _vendor(run, loop)


HOSTILE_CARRIERS: list[tuple[str, Forge]] = [
    ("v2_dict_key", lambda run: _spy_key(run, "run")),
    ("lifecycle_dict_key", _spy_lifecycle_key),
    ("nested_device_dict_key", _spy_device_key),
    ("identity_known_key", _spy_known_key),
    ("deep_vendor_key", lambda run: _vendor(run, {"packet": {"deep": {SpyStr("x"): 1}}})),
    ("metaclass_in_model_slot", _spy_metaclass),
    ("value_in_lifecycle_state", lambda run: _construct(run, lifecycle_state=Spy())),
    ("metaclass_in_lifecycle_field", lambda run: _lifecycle_field(run, session_id=SpyModel())),
    ("value_in_record_field", _spy_record_field),
    ("value_in_identity_leaf", _spy_identity_leaf),
    ("self_referencing_vendor_dict", _self_reference),
]


@pytest.mark.parametrize(("name", "forge"), HOSTILE_CARRIERS, ids=[c[0] for c in HOSTILE_CARRIERS])
def test_n39_hostile_carriers_run_no_caller_code(tmp_path: Path, name: str, forge: Forge) -> None:
    """N39: hostile keys, metaclasses and values are refused with zero spy calls."""
    del name
    hostile = forge(write(tmp_path, plan(tmp_path)))
    SPY_CALLS.clear()
    result = check(hostile)
    calls = list(SPY_CALLS)
    assert calls == []
    assert result.findings == (F.CARRIER_NOT_ADMITTED,)


def test_n40_admitted_carrier_with_reordered_headers_refuses_interpretation(
    tmp_path: Path,
) -> None:
    """N40: a validly constructed carrier that interpretation refuses."""
    retained = write(tmp_path, plan(tmp_path))
    run = reader.ColdRetainedRun(
        run_id=retained.run.run_id,
        manifest_sha256=retained.run.manifest_sha256,
        headers=tuple(reversed(retained.run.headers)),
        streams=retained.run.streams,
    )
    carrier = reader.ColdRetainedRunV2(
        run=run, lifecycle_state=retained.lifecycle_state, lifecycle=retained.lifecycle
    )
    assert check(carrier).findings == (F.INTERPRETATION_REFUSED,)


@pytest.fixture(scope="module")
def genuine(tmp_path_factory: pytest.TempPathFactory) -> reader.ColdRetainedRunV2:
    """One conforming retained run shared by the read-only admission forgeries."""
    tmp_path = tmp_path_factory.mktemp("genuine")
    retained = write(tmp_path, plan(tmp_path))
    assert check(retained).findings == ()
    return retained


def _nest_leaf(levels: int, leaf: object) -> object:
    value = leaf
    for _ in range(levels):
        value = {"n": value}
    return value


_AGGREGATE = schema.MAX_INPUT_AGGREGATE_BYTES
_IDENTITY_BUDGET: int = conformance._IDENTITY_TEXT_BUDGET  # pyright: ignore[reportPrivateUsage]


def _host_sample(run: reader.ColdRetainedRunV2) -> schema.ColdHostSample:
    return interpreted(run).rebound.phases[0].hosts[0].sample


#: Forged v1 records: ``(id, record factory)``; each is also refused by ``validate_record``.
_V = schema.ColdEvidenceFailure
#: ``(id, record factory, frozen validator failure)``; each member is the validator's own
#: refusal on its extraction path, pinned independently of the checker under test.
RECORD_REFUSALS: list[
    tuple[str, typing.Callable[[reader.ColdRetainedRunV2], object], schema.ColdEvidenceFailure]
] = [
    ("record_extra_key", lambda run: _with_extra_key(_first_tick(run)), _V.RECORD_NOT_VALIDATED),
    (
        "model_at_depth_limit",
        lambda run: _raw_tick(run, _nest_leaf(6, _host_sample(run))),
        _V.RECORD_NOT_VALIDATED,
    ),
    (
        "key_over_limit",
        lambda run: _raw_tick(run, {"k" * (schema.MAX_JSON_KEY_BYTES + 1): 1}),
        _V.JSON_KEY_INVALID,
    ),
    (
        "int_over_bound",
        lambda run: _raw_tick(run, {"big": 10**schema.MAX_INT_DIGITS}),
        _V.RECORD_NOT_VALIDATED,
    ),
    (
        "int_under_bound",
        lambda run: _raw_tick(run, {"small": -(10**schema.MAX_INT_DIGITS)}),
        _V.RECORD_NOT_VALIDATED,
    ),
    ("non_finite_float", lambda run: _raw_tick(run, {"x": math.inf}), _V.JSON_VALUE_NOT_FINITE),
    ("lone_surrogate", lambda run: _raw_tick(run, {"x": "\ud800"}), _V.RECORD_NOT_VALIDATED),
    (
        "text_length_over_aggregate",
        lambda run: _raw_tick(run, {"x": "x" * (_AGGREGATE + 1)}),
        _V.RECORD_TOO_LARGE,
    ),
    (
        "text_bytes_over_aggregate",
        lambda run: _raw_tick(run, {"x": "é" * (_AGGREGATE // 2 + 1)}),
        _V.RECORD_TOO_LARGE,
    ),
    (
        "aggregate_after_primitives",
        lambda run: _raw_tick(run, {"s": "x" * (_AGGREGATE - 3000), "n": [0] * 200}),
        _V.RECORD_TOO_LARGE,
    ),
    (
        "node_overflow",
        lambda run: _raw_tick(run, _node_vendor(schema.MAX_COLLECTION_LENGTH)),
        _V.JSON_NODE_LIMIT_EXCEEDED,
    ),
    ("tuple_value", lambda run: _raw_tick(run, {"x": (1, 2)}), _V.RECORD_NOT_VALIDATED),
]


def _raw_tick(run: reader.ColdRetainedRunV2, vendor: object) -> schema.ColdTickRecord:
    tick = _first_tick(run)
    assert tick.device is not None
    return tick.model_copy(
        update={"device": tick.device.model_copy(update={"raw_vendor_data": vendor})}
    )


@pytest.mark.parametrize(
    ("name", "make", "failure"), RECORD_REFUSALS, ids=[c[0] for c in RECORD_REFUSALS]
)
def test_admission_refuses_records_the_validator_refuses(
    genuine: reader.ColdRetainedRunV2,
    name: str,
    make: typing.Callable[[reader.ColdRetainedRunV2], object],
    failure: schema.ColdEvidenceFailure,
) -> None:
    """Each forged record the frozen validator refuses is refused before it runs."""
    del name
    record = make(genuine)
    with pytest.raises(schema.ColdEvidenceError) as raised:
        schema.validate_record(typing.cast(schema.ColdEvidenceRecord, record))
    assert raised.value.failure is failure
    forged = _replace_record(genuine, OFF, schema.ColdEvidenceStream.TICK, record)
    assert check(forged).findings == (F.CARRIER_NOT_ADMITTED,)


def test_undeclared_model_edge_is_left_to_the_validator(genuine: reader.ColdRetainedRunV2) -> None:
    """A package model below the depth limit in an undeclared slot passes the pre-walk.

    The declared-edge check stays with ``validate_record`` (no tighter than it), so
    interpretation refuses the carrier instead; it never conforms.
    """
    record = _raw_tick(genuine, _nest_leaf(5, _host_sample(genuine)))
    with pytest.raises(schema.ColdEvidenceError) as raised:
        schema.validate_record(record)
    assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    forged = _replace_record(genuine, OFF, schema.ColdEvidenceStream.TICK, record)
    assert check(forged).findings == (F.INTERPRETATION_REFUSED,)


class DictSubclass(dict[str, object]):
    """A ``__dict__`` replacement whose exact type is not ``dict``."""


def _dict_subclass(run: reader.ColdRetainedRunV2) -> object:
    copy = run.model_copy()
    object.__setattr__(copy, "__dict__", DictSubclass(object.__getattribute__(copy, "__dict__")))
    return copy


def _identity(run: reader.ColdRetainedRunV2, **update: object) -> object:
    return _replace_identity(run, run.run.headers[0].identity.model_copy(update=update))


def _known(run: reader.ColdRetainedRunV2, **extra: object) -> object:
    return _identity(run, known={**run.run.headers[0].identity.known, **extra})


def _run(run: reader.ColdRetainedRunV2, **update: object) -> object:
    return run.model_copy(update={"run": run.run.model_copy(update=update)})


def _header_pair(run: reader.ColdRetainedRunV2, **update: object) -> object:
    pair = run.run.headers[0].model_copy(update=update)
    return _run(run, headers=(pair, *run.run.headers[1:]))


def _stream(run: reader.ColdRetainedRunV2, **update: object) -> object:
    return _replace_stream(run, 0, run.run.streams[0].model_copy(update=update))


CARRIER_REFUSALS: list[tuple[str, Forge]] = [
    ("dunder_dict_subclass", _dict_subclass),
    ("renamed_field_same_count", lambda run: _renamed_field(run)),
    (
        "identity_extra_key",
        lambda run: _replace_identity(run, _with_extra_key(run.run.headers[0].identity)),
    ),
    ("identity_run_id_not_str", lambda run: _identity(run, run_id=1)),
    ("identity_known_not_dict", lambda run: _identity(run, known=[])),
    ("identity_too_deep", lambda run: _known(run, deep=_nest(10))),
    ("identity_too_many_nodes", lambda run: _known(run, many=[[0] * 1000 for _ in range(5)])),
    ("identity_bad_leaf", lambda run: _known(run, bad=(1,))),
    ("identity_long_key", lambda run: _known(run, **{"k" * 300: 1})),
    (
        "identity_text_budget",
        lambda run: _known(run, zz=[[0] * 1000, "x" * (_IDENTITY_BUDGET - 10_000)]),
    ),
    (
        "run_id_over_text_bound",
        lambda run: _run(run, run_id="x" * (schema.MAX_TEXT_FIELD_BYTES + 1)),
    ),
    ("headers_as_list", lambda run: _run(run, headers=list(run.run.headers))),
    ("header_item_not_a_pair", lambda run: _run(run, headers=(object(),))),
    ("header_slot_holds_a_tick", lambda run: _header_pair(run, header=_first_tick(run))),
    ("identity_slot_holds_a_dict", lambda run: _header_pair(run, identity={})),
    (
        "header_record_extra_key",
        lambda run: _header_pair(run, header=_with_extra_key(run.run.headers[0].header)),
    ),
    ("stream_records_as_list", lambda run: _stream(run, records=list(run.run.streams[0].records))),
    ("stream_item_not_a_stream", lambda run: _replace_stream(run, 0, object())),
    ("stream_record_unknown_class", lambda run: _stream(run, records=(object(),))),
    ("stream_kind_is_raw_text", lambda run: _stream(run, stream="header")),
    (
        "stream_phase_wrong_enum_type",
        lambda run: _stream(run, phase=schema.ColdEvidenceStream.TICK),
    ),
]


@pytest.mark.parametrize(("name", "forge"), CARRIER_REFUSALS, ids=[c[0] for c in CARRIER_REFUSALS])
def test_admission_refuses_each_carrier_position(
    genuine: reader.ColdRetainedRunV2, name: str, forge: Forge
) -> None:
    """Every fixed carrier position and identity bound refuses its forged value."""
    del name
    assert check(forge(genuine)).findings == (F.CARRIER_NOT_ADMITTED,)


class StopNow(KeyboardInterrupt):
    """A ``BaseException`` that must propagate."""


def test_n41_rule_exceptions_are_internal_failures_and_base_exceptions_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N41: an exception in a rule group never conforms and retains no content."""
    retained = write(tmp_path, plan(tmp_path))

    def raising(*args: object, **kwargs: object) -> typing.NoReturn:
        raise ValueError(CANARY)

    monkeypatch.setattr(conformance, "evaluate_tick", raising)
    result = check(retained)
    assert result.findings == (F.CHECKER_INTERNAL_FAILURE,)
    assert result.outcome is Outcome.NOT_CONFORMANT
    assert CANARY not in repr(result) and CANARY not in result.model_dump_json()

    def interrupting(*args: object, **kwargs: object) -> typing.NoReturn:
        raise StopNow

    monkeypatch.setattr(conformance, "evaluate_tick", interrupting)
    with pytest.raises(StopNow):
        check(retained)


def test_n41_admission_exceptions_refuse_the_carrier(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception raised inside admission is a refusal, never conformance."""

    def raising(run: object) -> typing.NoReturn:
        raise ValueError(CANARY)

    monkeypatch.setattr(conformance, "_admit", raising)
    assert check(object()).findings == (F.CARRIER_NOT_ADMITTED,)


_Result = conformance.ColdConformanceResult


@pytest.mark.parametrize(
    "values",
    [
        {"policy_version": True, "outcome": Outcome.PRE_ADVISORY_CONFORMANT, "findings": ()},
        {"policy_version": 1.0, "outcome": Outcome.PRE_ADVISORY_CONFORMANT, "findings": ()},
        {"policy_version": 2, "outcome": Outcome.PRE_ADVISORY_CONFORMANT, "findings": ()},
        {
            "policy_version": 1,
            "outcome": Outcome.PRE_ADVISORY_CONFORMANT,
            "findings": (F.ABORT_RECORDED,),
        },
        {"policy_version": 1, "outcome": Outcome.NOT_CONFORMANT, "findings": ()},
        {
            "policy_version": 1,
            "outcome": Outcome.NOT_CONFORMANT,
            "findings": (F.ABORT_RECORDED, F.ABORT_RECORDED),
        },
        {
            "policy_version": 1,
            "outcome": Outcome.NOT_CONFORMANT,
            "findings": (F.TICKS_ABSENT, F.PHASE_MISSING),
        },
        {"policy_version": 1, "outcome": "not_conformant", "findings": (F.ABORT_RECORDED,)},
    ],
)
def test_n42_result_model_is_closed(values: dict[str, object]) -> None:
    """N42: exact version, unique ordered findings, and conformance iff empty."""
    with pytest.raises(pydantic.ValidationError):
        _Result.model_validate(values)


def test_n42_valid_results_and_closed_vocabularies() -> None:
    """The two valid shapes construct; the vocabularies are closed plain enums."""
    _Result(policy_version=1, outcome=Outcome.PRE_ADVISORY_CONFORMANT, findings=())
    _Result(
        policy_version=1, outcome=Outcome.NOT_CONFORMANT, findings=(F.PHASE_MISSING, F.TICKS_ABSENT)
    )
    assert [member.name for member in Outcome] == ["PRE_ADVISORY_CONFORMANT", "NOT_CONFORMANT"]
    assert all(member.value == member.name.lower() for member in F)
    assert len(F) == 37
    members = list(F)
    position = members.index(F.HOST_EVIDENCE_MISMATCH)
    assert members[position + 1] is F.HOST_BOUND_VALUE_NOT_ADMITTED
    assert F.CARRIER_NOT_ADMITTED is list(F)[0] and F.CHECKER_INTERNAL_FAILURE is list(F)[-1]
    for vocabulary in (Outcome, F):
        assert type(vocabulary) is type(Phase) and not issubclass(vocabulary, str)


def test_n43_private_session_and_driver_text_never_reaches_the_result(tmp_path: Path) -> None:
    """N43: credential-shaped session ids and driver stay out of the result."""
    driver = "sk-live-Driver0123456789AbCdEfGh"
    run = plan(tmp_path, s_off=CANARY, s_on=CANARY + "-on", driver=driver)
    result = check(write(tmp_path, run))
    assert result.findings == ()
    for text in (repr(result), result.model_dump_json(), str(result)):
        assert CANARY not in text and driver not in text


# ------------------------------------------------------------- N44 seams, fences

SOURCE_PATH = Path(conformance.__file__)
TREE = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
_COLD = "roastpilot_agent.cold_characterisation."
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    _COLD + "acceptance": frozenset(
        {
            "interpret_retained_run",
            "ColdCheckOutcome",
            "D191_N_LIMIT",
            "D191_X_LIMIT_MS",
            "ColdInterpretation",
            "ColdReboundPhase",
            "ColdPhaseInterpretation",
            "ColdD191Metrics",
        }
    ),
    _COLD + "evidence_reader": frozenset(
        {"ColdRetainedRunV2", "ColdRetainedRun", "ColdRetainedHeader", "ColdRetainedStream"}
    ),
    _COLD + "evidence_lifecycle": frozenset(
        {
            "ColdLifecycleRecord",
            "ADMITTED_ENUM_TYPES",
            "validate_lifecycle_record",
            "ColdLifecycleSequence",
            "COLD_TRANSITION_BUDGET_SECONDS",
            *(member.__name__ for member in lifecycle.ADMITTED_ENUM_TYPES),
        }
    ),
    _COLD + "evidence_schema": frozenset(
        {
            *(model.__name__ for model in schema._MODEL_FIELDS),  # pyright: ignore[reportPrivateUsage]
            "ColdPhaseKind",
            "ADMITTED_ENUM_TYPES",
            "validate_record",
            "walk_json_value",
            "ColdEvidenceStream",
            "ColdEvidenceRecord",
            "ColdEvidenceError",
            "ColdEvidenceFailure",
            *(name for name in dir(schema) if name.startswith("MAX_")),
        }
    ),
    _COLD + "evidence_store": frozenset(
        {
            "ColdRetainedIdentityV1",
            "ColdBindingState",
            "check_record_binding",
            "check_lifecycle_binding",
            "canonical_json",
            "load_strict_json",
            "ColdEvidenceStoreFailure",
        }
    ),
    _COLD + "mcp": frozenset(
        {
            "finalisation_is_clean",
            "finalisation_has_required_safety_evidence",
            "SessionFinalisationResult",
        }
    ),
    _COLD + "engine_policy": frozenset({"evaluate_tick", "ColdTickDecision"}),
    _COLD + "host_policy": frozenset(
        {
            "HOST_MIN_FREE_BYTES_DURING",
            "free_bytes_meets_floor",
            "mem_available_is_admitted",
            "parse_retained_throttle_hex",
            "soc_temp_below_limit",
            "throttle_word_is_clear",
        }
    ),
}


def _identifiers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword):
            names.add(node.arg or "")
    return names


def test_n44_alias_seams_are_the_private_tuples() -> None:
    """N44: each public alias is the existing private tuple, unchanged."""
    assert schema.ADMITTED_ENUM_TYPES is schema._ADMITTED_ENUM_TYPES  # pyright: ignore[reportPrivateUsage]
    assert lifecycle.ADMITTED_ENUM_TYPES is lifecycle._ADMITTED_ENUM_TYPES  # pyright: ignore[reportPrivateUsage]


def test_n44_imports_stay_inside_the_ratified_allow_list() -> None:
    """N44: stdlib ``enum``/``math``/``typing``, pydantic, and the allowed cold names only."""
    plain: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in ALLOWED_IMPORTS, node.module
            names = {alias.name for alias in node.names}
            assert names <= ALLOWED_IMPORTS[node.module], names - ALLOWED_IMPORTS[node.module]
    assert plain == {"enum", "math", "typing", "pydantic"}


def test_n44_nothing_imports_the_checker() -> None:
    """N44: no production module imports the checker (actual import nodes)."""
    package = SOURCE_PATH.parents[1]
    module = _COLD + "conformance"
    consumers: list[str] = []
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if module in imported:
                consumers.append(path.relative_to(package).as_posix())
    assert consumers == []


def test_n44_no_tolerant_reads_actuators_or_outcome_vocabulary() -> None:
    """N44: no tolerant/runtime attribute, actuator, policy field or verdict identifier."""
    forbidden = {
        "active",
        "elapsed_monotonic_seconds",
        "first_crack_status",
        "device_state",
        "set_heat",
        "set_fan",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "mark_first_crack",
        "call_tool",
        "finalise_session",
        "safe_zero",
        "non_zero_dimensions",
        "heat_level_percent",
        "roast_fan_level_percent",
        "main_fan_level_percent",
        "command_streaming_required",
    }
    attributes = {node.attr for node in ast.walk(TREE) if isinstance(node, ast.Attribute)}
    assert attributes & forbidden == set()
    lowered = {name.lower() for name in _identifiers(TREE)}
    assert {
        name for name in lowered if any(t in name for t in ("qualif", "ready", "verdict"))
    } == set()


def test_n44_limits_are_imported_and_the_d195_policy_is_called() -> None:
    """N44: comparisons use the imported limits; D195 predicates and replay are called."""
    compared: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Compare):
            compared |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert {"D191_N_LIMIT", "D191_X_LIMIT_MS", "COLD_TRANSITION_BUDGET_SECONDS"} <= compared
    called = {
        node.func.id
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "finalisation_is_clean",
        "finalisation_has_required_safety_evidence",
        "evaluate_tick",
        "interpret_retained_run",
        "validate_record",
        "validate_lifecycle_record",
    } <= called
    assigned = {
        ast.unparse(target)
        for node in ast.walk(TREE)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    assert assigned.isdisjoint(
        {"D191_N_LIMIT", "D191_X_LIMIT_MS", "COLD_TRANSITION_BUDGET_SECONDS"}
    )
    numbers = {
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float)
    }
    assert numbers.isdisjoint({1800, 60, 200})


def test_a2_checker_copies_no_host_threshold_mask_or_regex() -> None:
    """A2 fence: host bounds come only from host_policy; the checker never imports host."""
    source = SOURCE_PATH.read_text(encoding="utf-8")
    for text in ("80.0", "0x000F000F", "0x000f000f", "2**20", "2**30", "2 ** 20", "2 ** 30"):
        assert text not in source, text
    modules = {node.module for node in ast.walk(TREE) if isinstance(node, ast.ImportFrom)}
    assert _COLD + "host" not in modules and _COLD + "host_policy" in modules
    plain = {alias.name for n in ast.walk(TREE) if isinstance(n, ast.Import) for alias in n.names}
    assert "re" not in plain
    numbers = {
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float)
    }
    assert numbers.isdisjoint({80.0, 0x000F000F, 2**20, 2**30})


# ------------------------------------------- A2: retained host values (AC15 bounds)


def test_a2_safe_host_baseline_lies_strictly_inside_every_bound() -> None:
    """The local baseline is strictly within the live reader's public AC15 bounds."""
    sample = safe_host_sample()
    assert sample.soc_temp_c < host.HOST_MAX_TEMP_C
    assert sample.throttled_word_hex == "0x0" and int(sample.throttled_word_hex, 16) == 0
    assert sample.mem_available_bytes > host.HOST_MIN_MEM_AVAILABLE_BYTES
    assert sample.free_bytes > host.HOST_MIN_FREE_BYTES_BEFORE > host.HOST_MIN_FREE_BYTES_DURING


def test_a2_safe_host_baseline_conforms(tmp_path: Path) -> None:
    """Positive control: every retained host sample is the safe baseline and conforms."""
    retained = write(tmp_path, plan(tmp_path))
    samples = [
        record.sample.model_dump(mode="json")
        for phase in interpreted(retained).rebound.phases
        for record in phase.hosts
    ]
    assert len(samples) == 6
    for sample in samples:
        assert {name: sample[name] for name in ("soc_temp_c", "throttled_word_hex")} == {
            "soc_temp_c": 45.5,
            "throttled_word_hex": "0x0",
        }
        assert (sample["mem_available_bytes"], sample["free_bytes"]) == (1024 * 2**20, 4 * 2**30)
    result = check(retained)
    assert (result.outcome, result.findings) == (Outcome.PRE_ADVISORY_CONFORMANT, ())


_MEM_FLOOR = host.HOST_MIN_MEM_AVAILABLE_BYTES
_RUN_DISK_FLOOR = host.HOST_MIN_FREE_BYTES_DURING
_START_DISK_FLOOR = host.HOST_MIN_FREE_BYTES_BEFORE
#: H-N: one unsafe or malformed retained value; exactly the host-value finding.
UNSAFE_HOST_VALUES: list[tuple[str, dict[str, object]]] = [
    ("temperature_at_limit", {"soc_temp_c": 80.0}),
    ("temperature_above_limit", {"soc_temp_c": 80.5}),
    *[
        (f"throttle_bit_{bit:#x}", {"throttled_word_hex": f"{bit:#x}"})
        for bit in (0x1, 0x2, 0x4, 0x8, 0x10000, 0x20000, 0x40000, 0x80000)
    ],
    ("memory_below_floor", {"mem_available_bytes": _MEM_FLOOR - 1}),
    ("disk_below_run_floor", {"free_bytes": _RUN_DISK_FLOOR - 1}),
    *[
        (f"malformed_throttle_{index}", {"throttled_word_hex": spelling})
        for index, spelling in enumerate(("", "0x", "0X0", "0xA", "0x000000000", "-0x1", " 0x0"))
    ],
]
#: H-P: admitted boundary values; the run stays conformant.
SAFE_HOST_BOUNDARIES: list[tuple[str, dict[str, object]]] = [
    ("temperature_just_below_limit", {"soc_temp_c": math.nextafter(80.0, -math.inf)}),
    ("temperature_negative", {"soc_temp_c": -5.0}),
    ("memory_at_floor", {"mem_available_bytes": _MEM_FLOOR}),
    ("disk_at_run_floor", {"free_bytes": _RUN_DISK_FLOOR}),
    ("disk_below_start_floor", {"free_bytes": _START_DISK_FLOOR - 1}),
    ("throttle_zero", {"throttled_word_hex": "0x0"}),
    ("throttle_zero_padded", {"throttled_word_hex": "0x00000000"}),
    ("throttle_unmasked_bits", {"throttled_word_hex": "0xfff0fff0"}),
]


def _host_case(tmp_path: Path, phase: Phase, values: dict[str, object], index: int = 0) -> Findings:
    run = plan(tmp_path)
    run.host_overrides[(phase, index)] = values
    retained = write(tmp_path, run)
    hosts = interpreted(retained).rebound.phases[run.phases.index(phase)].hosts
    retained_sample = hosts[index].sample
    for name, value in values.items():
        assert getattr(retained_sample, name) == value, "the writer retained the exact value"
    result = check(retained)
    assert (result.outcome is Outcome.PRE_ADVISORY_CONFORMANT) == (result.findings == ())
    return result.findings


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("phase", [OFF, ON])
def test_a2_every_retained_host_sample_is_checked(tmp_path: Path, phase: Phase, index: int) -> None:
    """Only one later host sample is unsafe; every other retained sample stays admitted."""
    run = plan(tmp_path)
    run.host_overrides[(phase, index)] = {"soc_temp_c": 80.0}
    retained = write(tmp_path, run)
    for rebound in interpreted(retained).rebound.phases:
        temperatures = [record.sample.soc_temp_c for record in rebound.hosts]
        expected = [45.5, 45.5, 45.5]
        if rebound.phase is phase:
            expected[index] = 80.0
        assert temperatures == expected
    result = check(retained)
    assert (result.outcome, result.findings) == (
        Outcome.NOT_CONFORMANT,
        (F.HOST_BOUND_VALUE_NOT_ADMITTED,),
    )


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(
    ("name", "values"), UNSAFE_HOST_VALUES, ids=[c[0] for c in UNSAFE_HOST_VALUES]
)
def test_a2_unsafe_retained_host_value_is_a_finding(
    tmp_path: Path, phase: Phase, name: str, values: dict[str, object]
) -> None:
    """H-N: one unsafe or malformed value in one host sample of one phase."""
    del name
    result = _host_case(tmp_path / "run", phase, values)
    assert result == (F.HOST_BOUND_VALUE_NOT_ADMITTED,)


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(
    ("name", "values"), SAFE_HOST_BOUNDARIES, ids=[c[0] for c in SAFE_HOST_BOUNDARIES]
)
def test_a2_admitted_host_boundaries_conform(
    tmp_path: Path, phase: Phase, name: str, values: dict[str, object]
) -> None:
    """H-P: each inclusive or unmasked boundary value in one phase stays conformant."""
    del name
    assert _host_case(tmp_path / "run", phase, values) == ()


def _renamed_field(run: reader.ColdRetainedRunV2) -> object:
    """Exact ``str`` keys with the declared count, but one declared name is missing."""
    copy = run.model_copy()
    data = object.__getattribute__(copy, "__dict__")
    data["rum"] = data.pop("run")
    return copy


# -------------------------------------- R1: subtree bounds before scanning or listing


class Seams:
    """Record every container handed to the two admission scan/list seams.

    This is a structural ordering proof: the seams are the only places the pre-walk
    scans a mapping's keys or lists a sequence's items, so a container that never
    reaches them was refused before any work proportional to its size.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.seen: list[object] = []
        scan = conformance._exact_str_keys  # pyright: ignore[reportPrivateUsage]
        items = conformance._items  # pyright: ignore[reportPrivateUsage]

        def spy_scan(mapping: dict[object, object]) -> bool:
            self.seen.append(mapping)
            return scan(mapping)

        def spy_items(values: list[object]) -> list[object]:
            self.seen.append(values)
            return items(values)

        monkeypatch.setattr(conformance, "_exact_str_keys", spy_scan)
        monkeypatch.setattr(conformance, "_items", spy_items)

    def touched(self, target: object) -> bool:
        """Whether one exact container object reached a scan or list seam."""
        return any(item is target for item in self.seen)


_IDENTITY_NODES = schema.MAX_JSON_NODES + 8


def _oversized_model_data(run: reader.ColdRetainedRunV2) -> tuple[object, object]:
    tick = _first_tick(run)
    assert tick.device is not None
    device = tick.device.model_copy()
    data = object.__getattribute__(device, "__dict__")
    data.update({f"extra{index}": 0 for index in range(5000)})
    forged = tick.model_copy(update={"device": device})
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, forged), data


def _oversized_record_list(run: reader.ColdRetainedRunV2) -> tuple[object, object]:
    target = [0] * schema.MAX_COLLECTION_LENGTH
    vendor = _node_vendor(0) | {"d": target}
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, _raw_tick(run, vendor)), target


def _overlong_record_list(run: reader.ColdRetainedRunV2) -> tuple[object, object]:
    target = [0] * (schema.MAX_COLLECTION_LENGTH + 1)
    forged = _raw_tick(run, {"items": target})
    return _replace_record(run, OFF, schema.ColdEvidenceStream.TICK, forged), target


def _oversized_identity_list(run: reader.ColdRetainedRunV2) -> tuple[object, object]:
    target = [0] * (_IDENTITY_NODES + 1)
    return _known(run, huge=target), target


def _oversized_identity_dict(run: reader.ColdRetainedRunV2) -> tuple[object, object]:
    target = {f"k{index}": 0 for index in range(_IDENTITY_NODES + 1)}
    return _known(run, huge=target), target


OVERSIZED: list[tuple[str, typing.Callable[[reader.ColdRetainedRunV2], tuple[object, object]]]] = [
    ("model_data_over_field_count", _oversized_model_data),
    ("record_list_over_node_budget", _oversized_record_list),
    ("record_list_over_collection_length", _overlong_record_list),
    ("identity_list_over_node_budget", _oversized_identity_list),
    ("identity_dict_over_node_budget", _oversized_identity_dict),
]


@pytest.mark.parametrize(("name", "forge"), OVERSIZED, ids=[c[0] for c in OVERSIZED])
def test_r1_oversized_subtrees_are_refused_before_scanning_or_listing(
    genuine: reader.ColdRetainedRunV2,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    forge: typing.Callable[[reader.ColdRetainedRunV2], tuple[object, object]],
) -> None:
    """R1: an oversized exact list, dict or model ``__dict__`` never reaches a seam."""
    del name
    carrier, target = forge(genuine)
    seams = Seams(monkeypatch)
    assert check(carrier).findings == (F.CARRIER_NOT_ADMITTED,)
    assert seams.seen, "the seams observed the walk"
    assert not seams.touched(target)


def test_r1_seams_observe_genuine_containers(
    genuine: reader.ColdRetainedRunV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 control: admitted containers do reach the seams, so a miss is meaningful."""
    carrier, _target = _oversized_record_list(genuine)
    vendor = typing.cast(dict[str, object], _first_tick(genuine).model_dump()["device"])
    assert vendor["raw_vendor_data"]
    seams = Seams(monkeypatch)
    assert check(genuine).findings == ()
    assert any(type(item) is list for item in seams.seen)
    assert any(type(item) is dict for item in seams.seen)
    seams.seen.clear()
    check(carrier)
    full = schema.MAX_COLLECTION_LENGTH
    lists: list[object] = [
        item
        for item in seams.seen
        if type(item) is list and len(typing.cast(list[object], item)) == full
    ]
    assert lists, "the earlier full-length lists in the same subtree were listed"


# -------------------------------------------- R2: every causal and binding relation


def _lifecycle_event_at(phase: Phase, event: lifecycle.ColdLifecycleEvent, instant: float) -> Edit:
    def edit(run: Plan) -> None:
        entry(run, phase, event).ev = instant

    return edit


def _header_at(phase: Phase, instant: float) -> Edit:
    def edit(run: Plan) -> None:
        run.headers[phase] = instant

    return edit


def _result_at(phase: Phase, instant: float) -> Edit:
    def edit(run: Plan) -> None:
        run.results[phase][0] = (run.results[phase][0][0], instant)

    return edit


_UP, _DOWN = math.inf, -math.inf
#: One case per enforced causal relation; every other instant stays admissible.
CAUSAL_CASES: list[tuple[str, Edit]] = [
    ("h_off_after_a_off", _header_at(OFF, math.nextafter(10.0, _UP))),
    ("h_on_after_a_on", _header_at(ON, math.nextafter(1830.0, _UP))),
    (
        "f_off_before_e_off",
        _lifecycle_event_at(OFF, Event.FINALISATION_RETURNED, math.nextafter(1810.0, _DOWN)),
    ),
    (
        "cs_off_before_f_off",
        _lifecycle_event_at(OFF, Event.CHILD_STOPPED, math.nextafter(1811.0, _DOWN)),
    ),
    (
        "ct_off_before_cs_off",
        _lifecycle_event_at(OFF, Event.CHILD_STARTED, math.nextafter(1812.0, _DOWN)),
    ),
    ("h_on_before_ct_off", _header_at(ON, math.nextafter(1813.0, _DOWN))),
    (
        "f_on_before_e_on",
        _lifecycle_event_at(ON, Event.FINALISATION_RETURNED, math.nextafter(3630.0, _DOWN)),
    ),
    (
        "cs_on_before_f_on",
        _lifecycle_event_at(ON, Event.CHILD_STOPPED, math.nextafter(3631.0, _DOWN)),
    ),
    (
        "z_before_cs_on",
        _lifecycle_event_at(ON, Event.RUN_TERMINATED, math.nextafter(3632.0, _DOWN)),
    ),
    ("fin_off_before_e_off", _result_at(OFF, math.nextafter(1810.0, _DOWN))),
    ("fin_on_before_e_on", _result_at(ON, math.nextafter(3630.0, _DOWN))),
    ("fin_off_after_f_off_recording", _result_at(OFF, math.nextafter(1811.5, _UP))),
    ("fin_on_after_f_on_recording", _result_at(ON, math.nextafter(3631.5, _UP))),
]


@pytest.mark.parametrize(("name", "edit"), CAUSAL_CASES, ids=[c[0] for c in CAUSAL_CASES])
def test_r2_each_causal_relation_is_enforced(tmp_path: Path, name: str, edit: Edit) -> None:
    """R2: moving one instant just across one inclusive bound yields only that finding."""
    del name
    run = plan(tmp_path)
    edit(run)
    assert findings(tmp_path, run) == (F.CAUSAL_ORDER_VIOLATED,)


def _transition_event_after_activation(run: Plan) -> None:
    measured = entry(run, ON, Event.TRANSITION_MEASURED)
    measured.ev = measured.rec = math.nextafter(1830.0, _UP)


BINDING_CASES: list[tuple[str, Edit, Findings]] = [
    (
        "transition_event_not_activation",
        _transition_event_after_activation,
        (F.TRANSITION_NOT_BOUND,),
    ),
    (
        "off_return_session",
        _set(OFF, Event.FINALISATION_RETURNED, session_id="session-other"),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    (
        "on_return_session",
        _set(ON, Event.FINALISATION_RETURNED, session_id="session-other"),
        (F.SESSION_BINDING_MISMATCH,),
    ),
    (
        "transition_current_session",
        _set(ON, Event.TRANSITION_MEASURED, session_id="session-other"),
        (F.SESSION_BINDING_MISMATCH,),
    ),
]


@pytest.mark.parametrize(
    ("name", "edit", "expected"), BINDING_CASES, ids=[c[0] for c in BINDING_CASES]
)
def test_r2_transition_and_session_bindings(
    tmp_path: Path, name: str, edit: Edit, expected: Findings
) -> None:
    """R2: each transition-instant and session binding is independently enforced."""
    del name
    run = plan(tmp_path)
    edit(run)
    assert findings(tmp_path, run) == expected


# ------------------------------------- R3: reachable identity depth and node maxima


def _identity_admission(doc: Json) -> schema.ColdEvidenceFailure | None:
    """Envelope, header and identity-v1 validators as the independent oracle."""
    try:
        snapshot = schema.validate_record(header_of(doc, OFF, 1.0))
        assert type(snapshot) is schema.ColdRunHeader
        store.read_identity_v1(snapshot.identity)
    except schema.ColdEvidenceError as error:
        return error.failure
    return None


def _with_extras(doc: Json, extras: Json) -> Json:
    copy = json.loads(json.dumps(doc))
    copy["runtime_config"].update(extras)
    return copy


def _identity_run(tmp_path: Path, extras: Json) -> Findings:
    run = plan(tmp_path)
    for phase in (OFF, ON):
        run.documents[phase] = _with_extras(run.documents[phase], extras)
    return findings(tmp_path, run)


def test_r3_deepest_admitted_identity_extra_conforms(tmp_path: Path) -> None:
    """R3: the deepest tolerant identity extra the frozen validators admit conforms."""
    doc = plan(tmp_path / "probe").documents[OFF]
    levels = 0
    while _identity_admission(_with_extras(doc, {"deep": _nest(levels + 1)})) is None:
        levels += 1
    assert _identity_admission(_with_extras(doc, {"deep": _nest(levels + 1)})) is (
        schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED
    )
    assert levels == schema.MAX_JSON_DEPTH - 2
    assert _identity_run(tmp_path / "run", {"deep": _nest(levels)}) == ()


def _wide(tail: int) -> Json:
    full = [0] * schema.MAX_COLLECTION_LENGTH
    return {"e0": full, "e1": list(full), "e2": list(full), "e3": [0] * tail}


def test_r3_widest_admitted_identity_extra_conforms(tmp_path: Path) -> None:
    """R3: the identity holding the envelope's maximum node count conforms."""
    doc = plan(tmp_path / "probe").documents[OFF]
    low, high = 0, schema.MAX_COLLECTION_LENGTH
    assert _identity_admission(_with_extras(doc, _wide(low))) is None
    assert _identity_admission(_with_extras(doc, _wide(high))) is not None
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (
            (middle, high)
            if _identity_admission(_with_extras(doc, _wide(middle))) is None
            else (low, middle)
        )
    assert _identity_admission(_with_extras(doc, _wide(low + 1))) is (
        schema.ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED
    )
    assert _identity_run(tmp_path / "run", _wide(low)) == ()


# ----------------------------- R4: replay never receives a negative since-activation


def test_r4_replay_skips_ticks_before_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: before-activation ticks in both phases are refused and never replayed."""
    run = plan(tmp_path)
    run.ticks[OFF][0].mono = math.nextafter(10.0, _DOWN)
    run.ticks[ON][0].mono = math.nextafter(1830.0, _DOWN)
    retained = write(tmp_path, run)
    received: list[float] = []
    real = engine_policy.evaluate_tick

    def spy(record: schema.ColdTickRecord, **kwargs: typing.Any) -> typing.Any:
        received.append(kwargs["since_activation_seconds"])
        return real(record, **kwargs)

    monkeypatch.setattr(conformance, "evaluate_tick", spy)
    assert check(retained).findings == (F.TICK_OUTSIDE_WINDOW,)
    assert len(received) == 4
    assert min(received) >= 0.0


# ------------------------------------------------ R5: host count and window variants


def _host(phase: Phase, position: int, mono: float) -> Edit:
    def edit(run: Plan) -> None:
        run.hosts[phase][position] = mono

    return edit


def _late_on_host(run: Plan) -> None:
    def make(header: schema.ColdRunHeader) -> schema.ColdEvidenceRecord:
        return host_record(header, 3634.0)

    run.late.append((ON, make))


HOST_CASES: list[tuple[str, Edit, Findings]] = [
    ("excess_host", lambda run: _append_excess_host(run), (F.HOST_EVIDENCE_MISMATCH,)),
    ("off_host_at_activation", _host(OFF, 0, 10.0), ()),
    ("off_host_at_elapsed", _host(OFF, 2, 1810.0), ()),
    ("on_host_at_activation", _host(ON, 0, 1830.0), ()),
    ("on_host_at_elapsed", _host(ON, 2, 3630.0), ()),
    ("on_tick_at_activation", _tick_at(ON, 0, 1830.0), ()),
    ("on_tick_at_elapsed", _tick_at(ON, 2, 3630.0), ()),
    (
        "off_host_before_activation",
        _host(OFF, 0, math.nextafter(10.0, _DOWN)),
        (F.TICK_OUTSIDE_WINDOW,),
    ),
    ("on_host_after_elapsed", _host(ON, 2, math.nextafter(3630.0, _UP)), (F.TICK_OUTSIDE_WINDOW,)),
    (
        "on_host_after_terminal",
        _late_on_host,
        (F.TICK_OUTSIDE_WINDOW, F.HOST_EVIDENCE_MISMATCH, F.RECORD_AFTER_TERMINATION),
    ),
]


@pytest.mark.parametrize(("name", "edit", "expected"), HOST_CASES, ids=[c[0] for c in HOST_CASES])
def test_r5_host_count_and_window(
    tmp_path: Path, name: str, edit: Edit, expected: Findings
) -> None:
    """R5: host count equality, inclusive host/tick window edges, and a post-terminal host."""
    del name
    run = plan(tmp_path)
    edit(run)
    assert findings(tmp_path, run) == expected


def _append_excess_host(run: Plan) -> None:
    """One host more than ticks, appended in clock order and inside the OFF window."""
    last = run.hosts[OFF][-1]
    run.hosts[OFF].append(13.5)
    assert run.hosts[OFF][-1] >= last


# ------------------------------------- A3: per-stream retained clock order (ticks, hosts)

_ACTIVATION: typing.Final[dict[Phase, float]] = {OFF: 10.0, ON: 1830.0}


def _tick_instants(phase: Phase, *offsets: float) -> Edit:
    """Set the three tick instants of one phase; indices and heartbeat stay unchanged."""

    def edit(run: Plan) -> None:
        for tick, offset in zip(run.ticks[phase], offsets, strict=True):
            tick.mono = _ACTIVATION[phase] + offset

    return edit


def _host_instants(phase: Phase, *offsets: float) -> Edit:
    """Set the three host instants of one phase; ticks stay unchanged."""

    def edit(run: Plan) -> None:
        run.hosts[phase] = [_ACTIVATION[phase] + offset for offset in offsets]

    return edit


CLOCK_ORDER_NEGATIVES: list[tuple[str, typing.Callable[[Phase], Edit]]] = [
    ("ticks_first_pair_swapped", lambda phase: _tick_instants(phase, 2.0, 1.0, 3.0)),
    ("ticks_later_pair_swapped", lambda phase: _tick_instants(phase, 1.0, 3.0, 2.0)),
    ("hosts_first_pair_swapped", lambda phase: _host_instants(phase, 2.0, 1.0, 3.0)),
    ("hosts_later_pair_swapped", lambda phase: _host_instants(phase, 1.0, 3.0, 2.0)),
]
CLOCK_ORDER_TIES: list[tuple[str, typing.Callable[[Phase], Edit]]] = [
    ("tick_one_equals_tick_zero", lambda phase: _tick_instants(phase, 1.0, 1.0, 3.0)),
    ("tick_two_equals_tick_one", lambda phase: _tick_instants(phase, 1.0, 2.0, 2.0)),
    ("host_one_equals_host_zero", lambda phase: _host_instants(phase, 1.0, 1.0, 3.0)),
    ("host_two_equals_host_one", lambda phase: _host_instants(phase, 1.0, 2.0, 2.0)),
]


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(
    ("name", "make"), CLOCK_ORDER_NEGATIVES, ids=[c[0] for c in CLOCK_ORDER_NEGATIVES]
)
def test_a3_a_regressed_retained_stream_instant_is_a_causal_finding(
    tmp_path: Path, phase: Phase, name: str, make: typing.Callable[[Phase], Edit]
) -> None:
    """A3: one adjacent tick or host pair regressing in one phase; heartbeat still rises."""
    del name
    run = plan(tmp_path)
    make(phase)(run)
    assert [tick.elapsed for tick in run.ticks[phase]] == [1.0, 2.0, 3.0]
    assert [tick.index for tick in run.ticks[phase]] == [0, 1, 2]
    assert findings(tmp_path, run) == (F.CAUSAL_ORDER_VIOLATED,)


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(("name", "make"), CLOCK_ORDER_TIES, ids=[c[0] for c in CLOCK_ORDER_TIES])
def test_a3_tied_retained_stream_instants_conform(
    tmp_path: Path, phase: Phase, name: str, make: typing.Callable[[Phase], Edit]
) -> None:
    """A3: equal adjacent tick or host instants are admitted (engine admits ties)."""
    del name
    run = plan(tmp_path)
    make(phase)(run)
    assert [tick.elapsed for tick in run.ticks[phase]] == [1.0, 2.0, 3.0]
    assert findings(tmp_path, run) == ()


def test_a3_tied_on_ticks_at_both_window_edges_conform(tmp_path: Path) -> None:
    """A3 boundary: ON ticks at activation, a tie at activation, then the elapsed instant."""
    run = plan(tmp_path)
    for tick, mono in zip(run.ticks[ON], (1830.0, 1830.0, 3630.0), strict=True):
        tick.mono = mono
    assert entry(run, ON, Event.PHASE_ACTIVATED).ev == 1830.0
    assert entry(run, ON, Event.OBSERVATION_WINDOW_ELAPSED).ev == 3630.0
    assert findings(tmp_path, run) == ()


def test_a3_regression_outside_the_window_keeps_both_findings(tmp_path: Path) -> None:
    """A3 combined: OFF tick 1 before activation is out of window and regresses."""
    run = plan(tmp_path)
    run.ticks[OFF][1].mono = math.nextafter(10.0, -math.inf)
    assert [tick.mono for tick in run.ticks[OFF]] == [11.0, math.nextafter(10.0, -math.inf), 13.0]
    assert findings(tmp_path, run) == (F.TICK_OUTSIDE_WINDOW, F.CAUSAL_ORDER_VIOLATED)
