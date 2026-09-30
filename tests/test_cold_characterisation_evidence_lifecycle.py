"""Behavioural and fail-closed tests for v2 cold lifecycle evidence (#954 slice 4g-a)."""

import ast
import enum
import functools
import json
import math
import traceback
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import engine, engine_policy
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from tests.test_cold_characterisation_evidence_builders import (
    COLD_PACKAGE,
    RUN_ID,
    _identifiers,  # pyright: ignore[reportPrivateUsage]
    _token_violations,  # pyright: ignore[reportPrivateUsage]
    header_for,
    make_identity,
    tick_for,
)
from tests.test_cold_characterisation_evidence_reader import line_of, rewrite
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    expect,
    on_header,
    open_writer,
    run_dir,
    write_full_run,
)

Event = lifecycle.ColdLifecycleEvent
Admission = lifecycle.ColdLifecycleSessionAdmission
Result = lifecycle.ColdLifecycleFinalisationResult
Stop = lifecycle.ColdLifecycleChildStop
Start = lifecycle.ColdLifecycleChildStart
Termination = lifecycle.ColdRunTermination
Reason = lifecycle.ColdRunTerminationReason
Order = lifecycle.ColdLifecycleFailure
State = lifecycle.ColdLifecycleEvidenceState
Failure = store.ColdEvidenceStoreFailure
NOT_VALIDATED = schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
T0 = "2026-09-26T12:00:00Z"
LIFECYCLE_OFF = "records/recording_off/lifecycle.jsonl"
LIFECYCLE_ON = "records/recording_on/lifecycle.jsonl"
OFF_SESSION = "session-recording-off"
ON_SESSION = "session-recording-on"
Spec = typing.Callable[[schema.ColdRunHeader, int], lifecycle.ColdLifecycleRecord]


def lc(
    header: schema.ColdRunHeader,
    sequence: int,
    event: lifecycle.ColdLifecycleEvent,
    *,
    at: float = 10.0,
    event_at: float | None = None,
    **fields: typing.Any,
) -> lifecycle.ColdLifecycleRecord:
    """Build one lifecycle record through the real builder."""
    return builders.build_lifecycle_record(
        header=header,
        sequence=sequence,
        event=event,
        event_utc=T0,
        event_monotonic_seconds=at if event_at is None else event_at,
        recorded_at_utc=T0,
        monotonic_seconds=at,
        **fields,
    )


def activated(session: str = OFF_SESSION, at: float = 10.0) -> Spec:
    """Spec one ``PHASE_ACTIVATED`` record."""
    return lambda header, sequence: lc(
        header, sequence, Event.PHASE_ACTIVATED, at=at, session_id=session
    )


def elapsed(session: str, scheduled_end: float) -> Spec:
    """Spec one ``OBSERVATION_WINDOW_ELAPSED`` record at its scheduled end."""
    return lambda header, sequence: lc(
        header,
        sequence,
        Event.OBSERVATION_WINDOW_ELAPSED,
        at=scheduled_end + 1.0,
        event_at=scheduled_end,
        session_id=session,
        scheduled_end_monotonic=scheduled_end,
        tick_count=1800,
    )


def finalised(result: lifecycle.ColdLifecycleFinalisationResult, session: str, at: float) -> Spec:
    """Spec one ``FINALISATION_RETURNED`` record."""
    return lambda header, sequence: lc(
        header,
        sequence,
        Event.FINALISATION_RETURNED,
        at=at,
        session_id=session,
        finalisation_result=result,
    )


def stopped(stop: lifecycle.ColdLifecycleChildStop, at: float) -> Spec:
    """Spec one ``CHILD_STOPPED`` record."""
    return lambda header, sequence: lc(
        header, sequence, Event.CHILD_STOPPED, at=at, child_stop=stop
    )


def started(start: lifecycle.ColdLifecycleChildStart, at: float) -> Spec:
    """Spec one ``CHILD_STARTED`` record."""
    return lambda header, sequence: lc(
        header, sequence, Event.CHILD_STARTED, at=at, child_start=start
    )


def aborted(session: str | None, deadline: bool, at: float) -> Spec:
    """Spec one ``PHASE_ABORTED_NOT_FINALISED`` record."""
    return lambda header, sequence: lc(
        header,
        sequence,
        Event.PHASE_ABORTED_NOT_FINALISED,
        at=at,
        session_id=session,
        activation_deadline_exceeded=deadline,
    )


def transition(start: float, end: float, at: float | None = None) -> Spec:
    """Spec one ``TRANSITION_MEASURED`` record from ``start`` to activation ``end``."""
    return lambda header, sequence: lc(
        header,
        sequence,
        Event.TRANSITION_MEASURED,
        at=end if at is None else at,
        event_at=end,
        session_id=ON_SESSION,
        previous_phase_session_id=OFF_SESSION,
        transition_start_monotonic=start,
    )


def terminated(
    termination: lifecycle.ColdRunTermination,
    at: float,
    reason: lifecycle.ColdRunTerminationReason | None = None,
) -> Spec:
    """Spec one ``RUN_TERMINATED`` record."""
    return lambda header, sequence: lc(
        header,
        sequence,
        Event.RUN_TERMINATED,
        at=at,
        termination=termination,
        termination_reason=reason,
    )


def seal_run(
    tmp_path: Path, off: list[Spec], on: list[Spec] | None = None
) -> tuple[str, str, list[lifecycle.ColdLifecycleRecord]]:
    """Write and seal one run with v1 records plus the specified lifecycle records."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off_header = header_for(tmp_path, root, OFF)
    writer.append(off_header)
    writer.append(tick_for(off_header, 0))
    written: list[lifecycle.ColdLifecycleRecord] = []
    for spec in off:
        record = spec(off_header, len(written))
        writer.append_lifecycle(record)
        written.append(record)
    if on is not None:
        on_bound = on_header(tmp_path, root)
        writer.append(on_bound)
        writer.append(tick_for(on_bound, 0))
        for spec in on:
            record = spec(on_bound, len(written))
            writer.append_lifecycle(record)
            written.append(record)
    return root, writer.seal().manifest_sha256, written


def read2(root: str, digest: str) -> reader.ColdRetainedRunV2:
    """Read the shared test run through the v2 reader."""
    return reader.read_retained_run_v2(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def completed_run() -> tuple[list[Spec], list[Spec]]:
    """Return a full two-phase ``COMPLETED`` lifecycle plan."""
    return (
        [
            activated(OFF_SESSION, 10.0),
            elapsed(OFF_SESSION, 1810.0),
            finalised(Result.CLEAN_RECORDED, OFF_SESSION, 1812.0),
            stopped(Stop.CONFIRMED, 1813.0),
            started(Start.STARTED, 1814.0),
        ],
        [
            activated(ON_SESSION, 1830.0),
            transition(1810.0, 1830.0, 1831.0),
            elapsed(ON_SESSION, 3630.0),
            finalised(Result.NOT_CLEAN_RECORDED, ON_SESSION, 3632.0),
            terminated(Termination.COMPLETED, 3633.0),
        ],
    )


def expect_evidence(
    failure: schema.ColdEvidenceFailure, call: typing.Callable[[], object]
) -> schema.ColdEvidenceError:
    """Assert one call raises exactly one closed, chain-free evidence failure."""
    with pytest.raises(schema.ColdEvidenceError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold evidence admission failed.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def expect_order(
    failure: lifecycle.ColdLifecycleFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, chain-free lifecycle failure."""
    with pytest.raises(lifecycle.ColdLifecycleError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold lifecycle evidence refused.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def forged(
    record: lifecycle.ColdLifecycleRecord, **update: object
) -> lifecycle.ColdLifecycleRecord:
    """Return an unvalidated ``model_copy`` of a record with raw field updates."""
    return record.model_copy(update=update)


def refused(record: lifecycle.ColdLifecycleRecord) -> None:
    """Assert revalidation refuses a record as ``RECORD_NOT_VALIDATED``."""
    expect_evidence(NOT_VALIDATED, lambda: lifecycle.validate_lifecycle_record(record))


def samples(tmp_path: Path) -> dict[lifecycle.ColdLifecycleEvent, lifecycle.ColdLifecycleRecord]:
    """Return one valid builder-made record per event."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = str(tmp_path.resolve())
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    return {
        Event.PHASE_ACTIVATED: activated()(off, 0),
        Event.OBSERVATION_WINDOW_ELAPSED: elapsed(OFF_SESSION, 1810.0)(off, 0),
        Event.PHASE_ABORTED_NOT_FINALISED: aborted(None, True, 5.0)(off, 0),
        Event.FINALISATION_RETURNED: finalised(Result.CLEAN_RECORDED, OFF_SESSION, 5.0)(off, 0),
        Event.CHILD_STOPPED: stopped(Stop.CONFIRMED, 5.0)(off, 0),
        Event.CHILD_STARTED: started(Start.STARTED, 5.0)(off, 0),
        Event.TRANSITION_MEASURED: transition(1810.0, 1830.0)(on, 0),
        Event.RUN_TERMINATED: terminated(Termination.FAILED, 5.0, Reason.RESPAWN_FAILED)(off, 0),
    }


# ------------------------------------------------------------ L-T1 round trip


def test_every_event_round_trips_across_separate_runs(tmp_path: Path) -> None:
    """L-T1: completed, failed, and unterminated runs read back equal and in order."""
    off, on = completed_run()
    runs = {
        "completed": seal_run(tmp_path / "completed", off, on),
        "failed": seal_run(
            tmp_path / "failed",
            [
                aborted(None, True, 20.0),
                finalised(Result.FAILED_WITHOUT_RESULT, OFF_SESSION, 21.0),
                stopped(Stop.UNCONFIRMED, 22.0),
                started(Start.FAILED, 22.0),
                terminated(Termination.FAILED, 23.0, Reason.CHILD_STOP_UNCONFIRMED),
            ],
        ),
        "unterminated": seal_run(
            tmp_path / "unterminated",
            [
                activated(OFF_SESSION, 10.0),
                aborted(OFF_SESSION, False, 11.0),
                finalised(Result.RECORD_NOT_RETAINED, OFF_SESSION, 12.0),
            ],
        ),
    }
    seen: dict[str, set[object]] = {
        name: set() for name in ("event", "admission", "result", "stop", "start")
    }
    for root, digest, written in runs.values():
        retained = read2(root, digest)
        assert retained.lifecycle_state is State.PRESENT
        assert list(retained.lifecycle) == written
        assert [record.sequence for record in retained.lifecycle] == list(range(len(written)))
        assert retained.run.run_id == RUN_ID
        assert retained.run.manifest_sha256 == digest
        for record in retained.lifecycle:
            seen["event"].add(record.event)
            seen["admission"].add(record.session_admission)
            seen["result"].add(record.finalisation_result)
            seen["stop"].add(record.child_stop)
            seen["start"].add(record.child_start)
    assert seen["event"] == set(Event)
    assert seen["admission"] - {None} == set(Admission)
    assert seen["result"] - {None} == set(Result)
    assert seen["stop"] - {None} == set(Stop)
    assert seen["start"] - {None} == set(Start)
    terminal = [read2(*runs[name][:2]).lifecycle[-1] for name in ("completed", "failed")]
    assert terminal[0].termination is Termination.COMPLETED
    assert terminal[0].termination_reason is None
    assert terminal[1].termination is Termination.FAILED
    assert terminal[1].termination_reason is Reason.CHILD_STOP_UNCONFIRMED
    unterminated = read2(*runs["unterminated"][:2]).lifecycle
    assert all(record.event is not Event.RUN_TERMINATED for record in unterminated)


def test_lifecycle_lines_are_canonical_and_in_their_phase_file(tmp_path: Path) -> None:
    """L-T1: each phase file holds exactly its canonical LF-terminated lines."""
    off, on = completed_run()
    root, _digest, written = seal_run(tmp_path, off, on)
    for path, phase in ((LIFECYCLE_OFF, OFF), (LIFECYCLE_ON, ON)):
        expected = b"".join(
            line_of(record.model_dump(mode="json")) for record in written if record.phase is phase
        )
        assert (run_dir(root) / path).read_bytes() == expected


# -------------------------------------------------- L-T2 / L-T15 v1 compatibility


def test_v1_reader_refuses_a_lifecycle_tree(tmp_path: Path) -> None:
    """L-T2: the unchanged v1 reader refuses ``lifecycle.jsonl`` as an unknown entry."""
    root, digest, _written = seal_run(tmp_path, [activated()])
    expect(
        Failure.ENTRY_PATH_INVALID,
        lambda: reader.read_retained_run(root, run_id=RUN_ID, expected_manifest_sha256=digest),
    )


def test_v2_reader_marks_a_v1_tree_absent_with_an_identical_run(tmp_path: Path) -> None:
    """L-T2: a v1 tree reads as ``ABSENT`` with no records and the v1 reader's run."""
    root, sealed, _records = write_full_run(tmp_path)
    retained = read2(root, sealed.manifest_sha256)
    assert retained.lifecycle_state is State.ABSENT
    assert retained.lifecycle == ()
    assert retained.run == reader.read_retained_run(
        root, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )


def test_v2_run_is_the_v1_run_type_for_both_phases(tmp_path: Path) -> None:
    """L-T2: v2 returns the v1 run type with both headers and every v1 stream."""
    off, on = completed_run()
    root, digest, _written = seal_run(tmp_path, off, on)
    retained = read2(root, digest)
    assert type(retained.run) is reader.ColdRetainedRun
    assert [item.header.phase for item in retained.run.headers] == [OFF, ON]
    assert [(item.phase, item.stream.value) for item in retained.run.streams] == [
        (OFF, "header"),
        (OFF, "tick"),
        (ON, "header"),
        (ON, "tick"),
    ]


def test_retained_run_v2_state_must_match_its_records(tmp_path: Path) -> None:
    """L-T15: ``PRESENT`` needs records, ``ABSENT`` forbids them, and extras are refused."""
    root, digest, written = seal_run(tmp_path, [activated()])
    run = read2(root, digest).run
    with pytest.raises(pydantic.ValidationError):
        reader.ColdRetainedRunV2(run=run, lifecycle_state=State.PRESENT, lifecycle=())
    with pytest.raises(pydantic.ValidationError):
        reader.ColdRetainedRunV2(run=run, lifecycle_state=State.ABSENT, lifecycle=tuple(written))
    with pytest.raises(pydantic.ValidationError):
        reader.ColdRetainedRunV2.model_validate(
            {"run": run, "lifecycle_state": State.ABSENT, "lifecycle": (), "extra": 1}
        )
    assert (
        reader.ColdRetainedRunV2(run=run, lifecycle_state=State.ABSENT, lifecycle=()).lifecycle
        == ()
    )


# -------------------------------------------------------------- L-T4 matrix


_REQUIRED: dict[lifecycle.ColdLifecycleEvent, frozenset[str]] = {
    Event.PHASE_ACTIVATED: frozenset(
        {"session_admission", "session_id", "scheduled_end_monotonic"}
    ),
    Event.OBSERVATION_WINDOW_ELAPSED: frozenset(
        {"session_admission", "session_id", "scheduled_end_monotonic", "tick_count"}
    ),
    Event.PHASE_ABORTED_NOT_FINALISED: frozenset(
        {"session_admission", "activation_deadline_exceeded"}
    ),
    Event.FINALISATION_RETURNED: frozenset(
        {"session_admission", "session_id", "finalisation_result"}
    ),
    Event.CHILD_STOPPED: frozenset({"child_stop"}),
    Event.CHILD_STARTED: frozenset({"child_start"}),
    Event.TRANSITION_MEASURED: frozenset(
        {
            "session_admission",
            "session_id",
            "previous_phase_session_id",
            "transition_start_monotonic",
            "transition_end_monotonic",
            "transition_seconds",
            "transition_budget_seconds",
            "transition_within_budget",
        }
    ),
    Event.RUN_TERMINATED: frozenset({"termination", "termination_reason"}),
}
_SAMPLE_VALUES: dict[str, object] = {
    "session_admission": Admission.ADMITTED,
    "session_id": "session-sample",
    "previous_phase_session_id": "session-previous",
    "scheduled_end_monotonic": 1.0,
    "tick_count": 0,
    "activation_deadline_exceeded": False,
    "finalisation_result": Result.CLEAN_RECORDED,
    "child_stop": Stop.CONFIRMED,
    "child_start": Start.STARTED,
    "transition_start_monotonic": 1.0,
    "transition_end_monotonic": 1.0,
    "transition_seconds": 0.0,
    "transition_budget_seconds": 60.0,
    "transition_within_budget": True,
    "termination": Termination.COMPLETED,
    "termination_reason": Reason.UNEXPECTED_FAILURE,
}


def test_matrix_fields_are_exactly_the_optional_record_fields() -> None:
    """The test oracle covers exactly the 16 optional fields of the 27-field record."""
    names = list(lifecycle.ColdLifecycleRecord.model_fields)
    assert len(names) == 27
    assert names[11:] == list(_SAMPLE_VALUES)
    assert all(field.is_required() for field in lifecycle.ColdLifecycleRecord.model_fields.values())


@pytest.mark.parametrize("event", list(Event))
def test_each_forbidden_field_and_each_missing_required_field_refuses(
    tmp_path: Path, event: lifecycle.ColdLifecycleEvent
) -> None:
    """L-T4: every forbidden field set, and every required field nulled, is refused."""
    record = samples(tmp_path)[event]
    assert lifecycle.validate_lifecycle_record(record) == record
    required = _REQUIRED[event]
    for name, value in _SAMPLE_VALUES.items():
        if name in required:
            if getattr(record, name) is not None:
                refused(forged(record, **{name: None}))
        else:
            refused(forged(record, **{name: value}))


def test_admission_and_termination_pairings_are_exact(tmp_path: Path) -> None:
    """L-T4: admission and session id pair; a reason exists exactly for ``FAILED``."""
    sample = samples(tmp_path)
    abort = sample[Event.PHASE_ABORTED_NOT_FINALISED]
    assert abort.session_admission is Admission.NOT_ADMITTED
    refused(forged(abort, session_admission=Admission.ADMITTED))
    refused(forged(abort, session_id="session-x"))
    assert (
        lifecycle.validate_lifecycle_record(
            forged(abort, session_admission=Admission.ADMITTED, session_id="session-x")
        ).session_id
        == "session-x"
    )
    for event in (
        Event.PHASE_ACTIVATED,
        Event.OBSERVATION_WINDOW_ELAPSED,
        Event.FINALISATION_RETURNED,
        Event.TRANSITION_MEASURED,
    ):
        refused(forged(sample[event], session_admission=Admission.NOT_ADMITTED))
    failed = sample[Event.RUN_TERMINATED]
    refused(forged(failed, termination_reason=None))
    refused(forged(failed, termination=Termination.COMPLETED))
    completed = forged(failed, termination=Termination.COMPLETED, termination_reason=None)
    assert lifecycle.validate_lifecycle_record(completed).termination is Termination.COMPLETED


# ------------------------------------------------------- L-T5 derived exactness


def test_derived_fields_are_exact(tmp_path: Path) -> None:
    """L-T5: scheduled end and transition fields admit no tolerance or substitution."""
    sample = samples(tmp_path)
    active = sample[Event.PHASE_ACTIVATED]
    assert active.scheduled_end_monotonic == 10.0 + 1800.0
    refused(forged(active, scheduled_end_monotonic=math.nextafter(1810.0, math.inf)))
    refused(forged(active, scheduled_end_monotonic=math.nextafter(1810.0, 0.0)))
    window = sample[Event.OBSERVATION_WINDOW_ELAPSED]
    refused(forged(window, event_monotonic_seconds=math.nextafter(1810.0, 0.0)))
    moved = sample[Event.TRANSITION_MEASURED]
    assert (moved.transition_seconds, moved.transition_within_budget) == (20.0, True)
    refused(forged(moved, transition_seconds=math.nextafter(20.0, math.inf)))
    refused(forged(moved, transition_end_monotonic=1829.0))
    refused(forged(moved, transition_budget_seconds=59.0))
    refused(forged(moved, transition_within_budget=False))
    refused(forged(moved, phase=OFF))


@pytest.mark.parametrize(
    ("start", "end", "seconds", "within"),
    [
        (100.0, 50.0, -50.0, False),
        (10.0, 71.0, 61.0, False),
        (10.0, 70.0, 60.0, True),
        (0.0, 0.0, 0.0, True),
        (10.0, 70.000001, 70.000001 - 10.0, False),
    ],
)
def test_negative_and_over_budget_transitions_are_recorded_faithfully(
    tmp_path: Path, start: float, end: float, seconds: float, within: bool
) -> None:
    """L-T5: every transition is admitted and ``within`` is exactly ``0 <= s <= 60``."""
    root, digest, written = seal_run(tmp_path, [], [transition(start, end)])
    record = read2(root, digest).lifecycle[0]
    assert record == written[0]
    assert record.transition_seconds == seconds
    assert record.transition_within_budget is within
    assert record.transition_budget_seconds == lifecycle.COLD_TRANSITION_BUDGET_SECONDS == 60.0
    assert record.transition_end_monotonic == record.event_monotonic_seconds == end


def test_builder_refuses_a_caller_scheduled_end_and_an_off_phase_transition(
    tmp_path: Path,
) -> None:
    """L-T5: derived fields are never accepted from the caller."""
    root = str(tmp_path.resolve())
    off = header_for(tmp_path, root, OFF)
    expect_evidence(
        NOT_VALIDATED,
        lambda: lc(off, 0, Event.PHASE_ACTIVATED, session_id="s", scheduled_end_monotonic=1810.0),
    )
    expect_evidence(NOT_VALIDATED, lambda: transition(1.0, 2.0)(off, 0))
    expect_evidence(
        NOT_VALIDATED,
        lambda: lc(
            off,
            0,
            Event.OBSERVATION_WINDOW_ELAPSED,
            at=1809.0,
            session_id="s",
            scheduled_end_monotonic=1810.0,
            tick_count=0,
        ),
    )


# ------------------------------------------------------------ L-T6 scalars


class _Text(str):
    """A ``str`` subclass that must never pass as an exact string."""


class _Foreign(enum.Enum):
    """A foreign enum whose values collide with lifecycle values."""

    PHASE_ACTIVATED = "phase_activated"


_BAD_FIELD_VALUES: list[tuple[str, object]] = [
    ("sequence", True),
    ("sequence", -1),
    ("sequence", 1.0),
    ("tick_count", True),
    ("schema_version", True),
    ("monotonic_seconds", 11),
    ("monotonic_seconds", "11.0"),
    # Coupled: a -1.0 append time also precedes the 10.0 event (see the coupled test).
    ("monotonic_seconds", -1.0),
    # Confounded: also breaks the derived scheduled end; the isolated event-after-append
    # oracle is test_event_after_append_is_refused_at_every_boundary.
    ("event_monotonic_seconds", 20.0),
    ("scheduled_end_monotonic", -1.0),
    ("transition_seconds", 1),
    ("transition_within_budget", 1),
    ("activation_deadline_exceeded", 0),
    ("session_id", _Text("session")),
    ("session_id", ""),
    ("session_id", "   "),
    ("session_id", "a" * 2049),
    ("session_id", "é" * 1025),
    ("session_id", ["session"]),
    ("recorded_at_utc", "2026-09-26T12:00:00+01:00"),
    ("recorded_at_utc", "2026-09-26T12:00:00"),
    ("recorded_at_utc", "not an instant"),
    ("recorded_at_utc", "Z" * 2049),
    ("event_utc", _Text(T0)),
    ("run_id", 5),
    ("run_id", "not-a-run-id"),
    ("identity_sha256", "Z" * 64),
    ("stream", _Text("lifecycle")),
    ("event", _Foreign.PHASE_ACTIVATED),
    ("session_admission", Stop.CONFIRMED),
    ("phase", schema.ColdEvidenceStream.TICK),
]


@pytest.mark.parametrize(("name", "value"), _BAD_FIELD_VALUES)
def test_non_exact_scalars_are_refused(tmp_path: Path, name: str, value: object) -> None:
    """L-T6: exact native scalars only; no coercion, non-finite, or sign violation."""
    record = samples(tmp_path)[Event.PHASE_ACTIVATED]
    if name in {"tick_count", "transition_seconds", "transition_within_budget"}:
        record = samples(tmp_path)[
            Event.OBSERVATION_WINDOW_ELAPSED if name == "tick_count" else Event.TRANSITION_MEASURED
        ]
    if name == "activation_deadline_exceeded":
        record = samples(tmp_path)[Event.PHASE_ABORTED_NOT_FINALISED]
    if name == "scheduled_end_monotonic":
        record = samples(tmp_path)[Event.OBSERVATION_WINDOW_ELAPSED]
    refused(forged(record, **{name: value}))


@pytest.mark.parametrize(
    ("name", "value", "failure"),
    [
        ("monotonic_seconds", math.nan, schema.ColdEvidenceFailure.JSON_VALUE_NOT_FINITE),
        ("monotonic_seconds", math.inf, schema.ColdEvidenceFailure.JSON_VALUE_NOT_FINITE),
        ("transition_seconds", -math.inf, schema.ColdEvidenceFailure.JSON_VALUE_NOT_FINITE),
        ("session_id", "\ud800", schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED),
        ("event_utc", "\ud800", schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED),
    ],
)
def test_walker_refusals_keep_their_own_closed_member(
    tmp_path: Path, name: str, value: object, failure: schema.ColdEvidenceFailure
) -> None:
    """L-T6: non-finite and surrogate values are refused by the shared walker's member."""
    event = Event.TRANSITION_MEASURED if name == "transition_seconds" else Event.PHASE_ACTIVATED
    record = samples(tmp_path)[event]
    expect_evidence(
        failure, lambda: lifecycle.validate_lifecycle_record(forged(record, **{name: value}))
    )
    assert not lifecycle.is_admissible_session_id("\ud800")
    assert not lifecycle.is_admissible_utc_instant("\ud800")


def test_session_id_and_instant_boundaries_are_admitted(tmp_path: Path) -> None:
    """L-T6: a 2048-byte multibyte id, equal instants, and ``+00:00`` are admitted."""
    record = samples(tmp_path)[Event.PHASE_ACTIVATED]
    for value in ("é" * 1024, "a" * 2048, " s "):
        assert (
            lifecycle.validate_lifecycle_record(forged(record, session_id=value)).session_id
            == value
        )
    zero = forged(record, recorded_at_utc="2026-09-26T12:00:00+00:00")
    assert lifecycle.validate_lifecycle_record(zero) == zero


def test_walker_bound_failures_propagate_as_their_own_member(tmp_path: Path) -> None:
    """L-T4: the shared walker's own refusal is not collapsed into a generic one."""
    record = samples(tmp_path)[Event.OBSERVATION_WINDOW_ELAPSED]
    expect_evidence(
        schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED,
        lambda: lifecycle.validate_lifecycle_record(forged(record, tick_count=10**40)),
    )


_INSTANT_CORPUS: tuple[object, ...] = (
    T0,
    "2026-09-26T12:00:00+00:00",
    "2026-09-26T12:00:00.123456Z",
    "2026-09-26T12:00:00-00:00",
    "2026-09-26T12:00:00+01:00",
    "2026-09-26T12:00:00",
    "2026-09-26",
    "",
    " ",
    "not an instant",
    "\ud800",
    "Z" * 2049,
    None,
    1.0,
    b"2026-09-26T12:00:00Z",
    _Text(T0),
)


@pytest.mark.parametrize("value", _INSTANT_CORPUS)
def test_instant_rule_agrees_with_the_engine(value: object) -> None:
    """L-T6: the lifecycle instant rule equals the engine's admission on a fixed corpus."""
    expected = engine._instant_is_admissible(0.0, value, None)  # pyright: ignore[reportPrivateUsage]
    assert lifecycle.is_admissible_utc_instant(value) is expected


def test_monotonic_rule_is_exact() -> None:
    """L-T6: only an exact finite non-negative float is an absolute monotonic value."""
    assert lifecycle.is_admissible_monotonic(0.0)
    for value in (-1.0, math.nan, math.inf, 1, True, None, "1.0"):
        assert not lifecycle.is_admissible_monotonic(value)
    assert not lifecycle.is_admissible_session_id(None)


def test_a_record_subclass_is_refused(tmp_path: Path) -> None:
    """L-T6: only the exact record class is admitted."""

    class _Sub(lifecycle.ColdLifecycleRecord):
        """A record subclass."""

    record = samples(tmp_path)[Event.CHILD_STOPPED]
    sub = _Sub.model_validate(record.model_dump(), strict=True)
    expect_evidence(NOT_VALIDATED, lambda: lifecycle.validate_lifecycle_record(sub))


# -------------------------------------------------------------- L-T7 forgery


def _constructed(
    record: lifecycle.ColdLifecycleRecord, **changes: object
) -> lifecycle.ColdLifecycleRecord:
    """Return a ``model_construct`` forgery of a record's fields with changes."""
    values: dict[str, typing.Any] = {**dict(record), **changes}
    for name, value in changes.items():
        if value is _MISSING:
            del values[name]
    return lifecycle.ColdLifecycleRecord.model_construct(**values)


_MISSING = object()


def _forgeries(tmp_path: Path) -> list[lifecycle.ColdLifecycleRecord]:
    """Return forged records of every documented kind."""
    sample = samples(tmp_path)
    stop = sample[Event.CHILD_STOPPED]
    extra_key = _constructed(stop)
    object.__getattribute__(extra_key, "__dict__")["smuggled"] = 1
    extra = _constructed(stop)
    object.__setattr__(extra, "__pydantic_extra__", {"smuggled": 1})
    odd_extra = _constructed(stop)
    object.__setattr__(odd_extra, "__pydantic_extra__", [])

    class _Dict(dict[str, object]):
        """A dict subclass smuggled in as the instance dictionary."""

    odd_dict = _constructed(stop)
    object.__setattr__(odd_dict, "__dict__", _Dict(dict(stop)))
    return [
        extra_key,
        extra,
        odd_extra,
        odd_dict,
        _constructed(stop, event_utc=_MISSING),
        _constructed(stop, sequence=True),
        _constructed(stop, tick_count=0),
        forged(sample[Event.PHASE_ACTIVATED], scheduled_end_monotonic=1811.0),
        forged(stop, child_stop=None),
    ]


def test_forged_records_are_refused_by_validation_and_append(tmp_path: Path) -> None:
    """L-T7a: ``model_construct``/``model_copy`` forgeries never persist, nor poison."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    for record in _forgeries(tmp_path / "samples"):
        refused(record)
        expect_evidence(NOT_VALIDATED, functools.partial(writer.append_lifecycle, record))
    writer.append_lifecycle(activated()(off, 0))
    assert (run_dir(root) / LIFECYCLE_OFF).read_bytes().count(b"\n") == 1


def test_forged_pydantic_extra_is_refused(tmp_path: Path) -> None:
    """A1-6: a smuggled ``__pydantic_extra__`` is refused rather than silently dropped."""
    record = samples(tmp_path)[Event.CHILD_STARTED]
    forged_record = record.model_copy()
    object.__setattr__(forged_record, "__pydantic_extra__", {"smuggled": 1})
    refused(forged_record)


class _HeaderSub(schema.ColdRunHeader):
    """A header subclass."""


def _forged_builder_inputs(off: schema.ColdRunHeader) -> list[dict[str, typing.Any]]:
    """Return builder keyword sets that must each be refused."""
    base: dict[str, typing.Any] = {
        "header": off,
        "sequence": 0,
        "event": Event.PHASE_ACTIVATED,
        "event_utc": T0,
        "event_monotonic_seconds": 10.0,
        "recorded_at_utc": T0,
        "monotonic_seconds": 10.0,
        "session_id": "session",
    }
    stop = {**base, "event": Event.CHILD_STOPPED, "session_id": None, "child_stop": Stop.CONFIRMED}
    construct = typing.cast(typing.Any, schema.ColdRunHeader.model_construct)
    header_fields: dict[str, object] = dict(off)
    return [
        {**base, "header": _HeaderSub.model_validate(off.model_dump())},
        {**base, "header": construct(**{**header_fields, "identity_sha256": "z" * 64})},
        {**base, "header": construct(**{**header_fields, "run_id": "bad"})},
        {**base, "sequence": True},
        {**base, "sequence": -1},
        {**base, "event_monotonic_seconds": 1},
        {**base, "event_monotonic_seconds": math.nan},
        {**base, "monotonic_seconds": math.inf},
        {**base, "event": _Foreign.PHASE_ACTIVATED},
        {**base, "event": None},
        {**base, "session_id": _Text("session")},
        {**base, "session_id": None},
        {**base, "event_utc": "2026-09-26T12:00:00+02:00"},
        {**base, "previous_phase_session_id": " "},
        {**stop, "session_id": "session"},
        {**stop, "child_stop": Start.STARTED},
        {**stop, "tick_count": True},
        {**stop, "tick_count": -1},
        {**stop, "activation_deadline_exceeded": 1},
        {**stop, "scheduled_end_monotonic": -1.0},
        {**stop, "transition_start_monotonic": 1},
    ]


def test_forged_builder_inputs_are_refused(tmp_path: Path) -> None:
    """L-T7b: forged headers and every non-exact raw input are refused by the builder."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    for kwargs in _forged_builder_inputs(off):
        expect_evidence(NOT_VALIDATED, functools.partial(builders.build_lifecycle_record, **kwargs))


class _ArithmeticTrap:
    """A non-float input that records any arithmetic and raises an inert canary."""

    calls = 0

    def __add__(self, _other: object) -> float:
        """Record the invocation and refuse."""
        _ArithmeticTrap.calls += 1
        raise RuntimeError("arithmetic trap")

    __radd__ = __add__
    __sub__ = __add__
    __rsub__ = __add__


def test_raw_admission_precedes_any_derivation(tmp_path: Path) -> None:
    """LM26b: an untrusted object is refused before any derivation arithmetic runs."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    _ArithmeticTrap.calls = 0
    trap = typing.cast(typing.Any, _ArithmeticTrap())
    for event, extra in (
        (Event.PHASE_ACTIVATED, {"event_monotonic_seconds": trap}),
        (
            Event.TRANSITION_MEASURED,
            {"event_monotonic_seconds": 1.0, "transition_start_monotonic": trap},
        ),
    ):
        kwargs: dict[str, typing.Any] = {
            "header": off,
            "sequence": 0,
            "event": event,
            "event_utc": T0,
            "event_monotonic_seconds": 1.0,
            "recorded_at_utc": T0,
            "monotonic_seconds": 10.0,
            "session_id": "session",
            **extra,
        }
        expect_evidence(NOT_VALIDATED, functools.partial(builders.build_lifecycle_record, **kwargs))
    assert _ArithmeticTrap.calls == 0


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one module-level function definition."""
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


class _DerivationProbe:
    """Stand-in for the phase-length constant; any use records entry into derivation."""

    calls = 0

    def __radd__(self, _other: object) -> float:
        """Record the derivation arithmetic and refuse with an inert canary."""
        _DerivationProbe.calls += 1
        raise RuntimeError("derivation entered")

    __add__ = __radd__


@pytest.mark.parametrize("missing", ["event_monotonic_seconds", "monotonic_seconds"])
@pytest.mark.parametrize("event", [Event.PHASE_ACTIVATED, Event.CHILD_STOPPED])
def test_required_timestamps_refuse_none_before_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    event: lifecycle.ColdLifecycleEvent,
) -> None:
    """A ``None`` required timestamp is refused before derivation or construction.

    Probe evidence (not a general proof): the builder's module-global phase-length
    constant is read only inside the derivation block, so replacing it with a trap
    records whether derivation was entered; a spy records whether construction was.
    """
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    fields: dict[str, typing.Any] = (
        {"session_id": "session"}
        if event is Event.PHASE_ACTIVATED
        else {"child_stop": Stop.CONFIRMED}
    )
    kwargs: dict[str, typing.Any] = {
        "header": off,
        "sequence": 0,
        "event": event,
        "event_utc": T0,
        "event_monotonic_seconds": 10.0,
        "recorded_at_utc": T0,
        "monotonic_seconds": 10.0,
        **fields,
    }
    assert builders.build_lifecycle_record(**kwargs).event is event
    constructed: list[object] = []
    real = builders._construct_lifecycle  # pyright: ignore[reportPrivateUsage]

    def spy(build: typing.Callable[[], lifecycle.ColdLifecycleRecord]) -> object:
        constructed.append(build)
        return real(build)

    _DerivationProbe.calls = 0
    monkeypatch.setattr(builders, "COLD_PHASE_OBSERVATION_SECONDS", _DerivationProbe())
    monkeypatch.setattr(builders, "_construct_lifecycle", spy)
    expect_evidence(
        NOT_VALIDATED,
        functools.partial(builders.build_lifecycle_record, **{**kwargs, missing: None}),
    )
    assert _DerivationProbe.calls == 0
    assert constructed == []


def test_required_event_time_none_refuses_for_a_transition(tmp_path: Path) -> None:
    """Non-discriminating closed-error regression for a ``None`` transition event instant.

    Before the required-timestamp fix this path also ended in the closed error (via the
    derivation handler), so it does not discriminate that fix; the four trap and
    constructor-probe cases in test_required_timestamps_refuse_none_before_derivation do.
    """
    on = on_header(tmp_path, str(tmp_path.resolve()))
    expect_evidence(
        NOT_VALIDATED,
        lambda: builders.build_lifecycle_record(
            header=on,
            sequence=0,
            event=Event.TRANSITION_MEASURED,
            event_utc=T0,
            event_monotonic_seconds=typing.cast(typing.Any, None),
            recorded_at_utc=T0,
            monotonic_seconds=1830.0,
            session_id=ON_SESSION,
            previous_phase_session_id=OFF_SESSION,
            transition_start_monotonic=1810.0,
        ),
    )


def test_optional_timestamps_stay_optional(tmp_path: Path) -> None:
    """Optional ``None`` timestamps remain admitted where the event matrix forbids them."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = builders.build_lifecycle_record(
        header=off,
        sequence=0,
        event=Event.CHILD_STOPPED,
        event_utc=T0,
        event_monotonic_seconds=5.0,
        recorded_at_utc=T0,
        monotonic_seconds=5.0,
        child_stop=Stop.CONFIRMED,
        scheduled_end_monotonic=None,
        transition_start_monotonic=None,
    )
    assert (record.scheduled_end_monotonic, record.transition_start_monotonic) == (None, None)


def test_derivation_handler_catches_exactly_three_exception_classes() -> None:
    """LM28 (structural): the clock-free derivation handler names exactly three classes."""
    tree = ast.parse((COLD_PACKAGE / "evidence_builders.py").read_text(encoding="utf-8"))
    handlers = [
        node
        for node in ast.walk(_function(tree, "build_lifecycle_record"))
        if isinstance(node, ast.ExceptHandler)
    ]
    assert [ast.unparse(handler.type) for handler in handlers if handler.type is not None] == [
        "(ArithmeticError, TypeError, ValueError)"
    ]
    assert all(handler.type is not None for handler in handlers)
    construct = [
        node
        for node in ast.walk(_function(tree, "_construct_lifecycle"))
        if isinstance(node, ast.ExceptHandler)
    ]
    assert [ast.unparse(handler.type) for handler in construct if handler.type] == [
        "pydantic.ValidationError"
    ]


def test_construction_propagates_cancellation_unchanged() -> None:
    """A ``BaseException`` from construction is never converted into a refusal."""

    class _Stop(KeyboardInterrupt):
        """An inert interrupt canary."""

    def build() -> lifecycle.ColdLifecycleRecord:
        raise _Stop

    with pytest.raises(_Stop):
        builders._construct_lifecycle(build)  # pyright: ignore[reportPrivateUsage]


# ------------------------------------------------------ L-T8 writer ordering


def test_writer_ordering_refusals_do_not_poison(tmp_path: Path) -> None:
    """L-T8: gaps, repeats, stale phases, regressions, and post-terminal appends refuse."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append_lifecycle(activated(at=10.0)(off, 0))
    expect_order(
        Order.SEQUENCE_NOT_CONTIGUOUS,
        lambda: writer.append_lifecycle(stopped(Stop.CONFIRMED, 11.0)(off, 2)),
    )
    expect_order(
        Order.SEQUENCE_NOT_CONTIGUOUS,
        lambda: writer.append_lifecycle(stopped(Stop.CONFIRMED, 11.0)(off, 0)),
    )
    expect_order(
        Order.RECORDING_TIME_REGRESSED,
        lambda: writer.append_lifecycle(stopped(Stop.CONFIRMED, 9.0)(off, 1)),
    )
    on = on_header(tmp_path, root)
    expect(
        Failure.HEADER_MISSING, lambda: writer.append_lifecycle(activated(ON_SESSION, 11.0)(on, 1))
    )
    writer.append_lifecycle(stopped(Stop.CONFIRMED, 10.0)(off, 1))
    writer.append(on)
    expect_order(
        Order.PHASE_NOT_LATEST,
        lambda: writer.append_lifecycle(started(Start.STARTED, 12.0)(off, 2)),
    )
    writer.append_lifecycle(activated(ON_SESSION, 12.0)(on, 2))
    writer.append_lifecycle(terminated(Termination.COMPLETED, 12.0)(on, 3))
    expect_order(
        Order.APPENDED_AFTER_TERMINATION,
        lambda: writer.append_lifecycle(terminated(Termination.COMPLETED, 13.0)(on, 4)),
    )
    retained = read2(root, writer.seal().manifest_sha256)
    assert [record.sequence for record in retained.lifecycle] == [0, 1, 2, 3]


def test_writer_binding_refusals_do_not_poison(tmp_path: Path) -> None:
    """L-T8: a foreign run id or identity digest refuses; the writer stays usable."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    other_run = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-other-run")
    expect(Failure.RUN_ID_MISMATCHED, lambda: writer.append_lifecycle(activated()(other_run, 0)))
    other_identity = builders.build_run_header(
        identity=make_identity(tmp_path, pi_root=root, audio_device="USB microphone two"),
        phase=OFF,
        recorded_at_utc=T0,
        monotonic_seconds=1.0,
    )
    expect(
        Failure.IDENTITY_DIGEST_MISMATCHED,
        lambda: writer.append_lifecycle(activated()(other_identity, 0)),
    )
    writer.append_lifecycle(activated()(off, 0))


def test_lifecycle_binding_never_mutates_state(tmp_path: Path) -> None:
    """A1-4: lifecycle binding reads state only, on success and on refusal."""
    root = str(tmp_path.resolve())
    off = header_for(tmp_path, root, OFF)
    state = store.ColdBindingState(RUN_ID)
    store.check_record_binding(state, off, writer_root=None)
    before = state.headers
    store.check_lifecycle_binding(state, activated()(off, 0))
    assert state.headers == before
    on = on_header(tmp_path, root)
    expect(Failure.HEADER_MISSING, lambda: store.check_lifecycle_binding(state, activated()(on, 0)))
    assert state.headers == before
    other = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-other-run")
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: store.check_lifecycle_binding(state, activated()(other, 0)),
    )
    assert state.headers == before


def test_sequence_state_refuses_phase_regression_directly(tmp_path: Path) -> None:
    """L-T8: the pure order state refuses a phase that precedes the last committed one."""
    root = str(tmp_path.resolve())
    order = lifecycle.ColdLifecycleSequence()
    on_record = activated(ON_SESSION, 10.0)(on_header(tmp_path, root), 0)
    order.check(on_record)
    order.commit(on_record)
    assert (order.next_sequence, order.terminated) == (1, False)
    off_record = activated(OFF_SESSION, 10.0)(header_for(tmp_path, root, OFF), 1)
    expect_order(Order.PHASE_REGRESSED, lambda: order.check(off_record))
    assert order.next_sequence == 1


def test_unexpected_check_interrupt_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected ``BaseException`` during binding poisons, matching ``append``."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)

    def interrupt(*_args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "check_lifecycle_binding", interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.append_lifecycle(activated()(off, 0))
    expect(Failure.WRITER_POISONED, lambda: writer.append(tick_for(off, 0)))


# -------------------------------------------------------- L-T11 write faults


def test_lifecycle_write_fault_poisons_without_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-T11: a failed write poisons every later operation; order never advances."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append_lifecycle(activated()(off, 0))

    def fail(_descriptor: int, _data: memoryview) -> int:
        raise OSError("disk")

    monkeypatch.setattr(store, "_write_chunk", fail)
    expect(
        Failure.WRITE_FAILED, lambda: writer.append_lifecycle(stopped(Stop.CONFIRMED, 11.0)(off, 1))
    )
    order = writer._lifecycle  # pyright: ignore[reportPrivateUsage]
    assert (order.next_sequence, order.terminated) == (1, False)
    expect(Failure.WRITER_POISONED, lambda: writer.append(tick_for(off, 0)))
    expect(
        Failure.WRITER_POISONED,
        lambda: writer.append_lifecycle(stopped(Stop.CONFIRMED, 11.0)(off, 1)),
    )
    expect(Failure.WRITER_POISONED, writer.seal)
    assert not (run_dir(root) / store.MANIFEST_JSON_NAME).exists()


def test_interrupted_lifecycle_write_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-T11: a ``BaseException`` during the write poisons and propagates unchanged."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)

    def interrupt(_descriptor: int, _data: memoryview) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "_write_chunk", interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.append_lifecycle(activated()(off, 0))
    assert writer._lifecycle.next_sequence == 0  # pyright: ignore[reportPrivateUsage]
    expect(Failure.WRITER_POISONED, lambda: writer.append_lifecycle(activated()(off, 0)))


def test_sealed_writer_refuses_lifecycle(tmp_path: Path) -> None:
    """L-T11: a sealed writer refuses a lifecycle append."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.seal()
    expect(Failure.WRITER_SEALED, lambda: writer.append_lifecycle(activated()(off, 0)))


# ------------------------------------------------------ L-T9 reader ordering


def _lines(*records: lifecycle.ColdLifecycleRecord) -> bytes:
    """Return canonical lifecycle lines for records."""
    return b"".join(line_of(record.model_dump(mode="json")) for record in records)


def _two_phase(tmp_path: Path) -> tuple[str, schema.ColdRunHeader, schema.ColdRunHeader]:
    """Seal a two-phase lifecycle run and return its root and headers."""
    root, _digest, _written = seal_run(tmp_path, [activated()], [activated(ON_SESSION, 1830.0)])
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    return root, off, on


def test_reader_refuses_broken_order(tmp_path: Path) -> None:
    """L-T9: gaps, cross-phase swaps, duplicate or trailing terminals, and regressions."""
    root, off, on = _two_phase(tmp_path)
    cases: list[tuple[bytes, bytes, lifecycle.ColdLifecycleFailure]] = [
        (
            _lines(activated()(off, 0), stopped(Stop.CONFIRMED, 11.0)(off, 2)),
            _lines(),
            Order.SEQUENCE_NOT_CONTIGUOUS,
        ),
        (
            _lines(activated()(off, 1)),
            _lines(activated(ON_SESSION)(on, 0)),
            Order.SEQUENCE_NOT_CONTIGUOUS,
        ),
        (
            _lines(
                terminated(Termination.COMPLETED, 10.0)(off, 0),
                terminated(Termination.COMPLETED, 10.0)(off, 1),
            ),
            _lines(),
            Order.APPENDED_AFTER_TERMINATION,
        ),
        (
            _lines(terminated(Termination.COMPLETED, 10.0)(off, 0)),
            _lines(activated(ON_SESSION, 11.0)(on, 1)),
            Order.APPENDED_AFTER_TERMINATION,
        ),
        (
            _lines(activated(at=10.0)(off, 0), stopped(Stop.CONFIRMED, 9.0)(off, 1)),
            _lines(),
            Order.RECORDING_TIME_REGRESSED,
        ),
    ]
    for off_bytes, on_bytes, failure in cases:
        rewrite(root, LIFECYCLE_OFF, off_bytes)
        if on_bytes:
            digest = rewrite(root, LIFECYCLE_ON, on_bytes)
        else:
            (run_dir(root) / LIFECYCLE_ON).unlink(missing_ok=True)
            digest = rewrite(root, LIFECYCLE_OFF, off_bytes)
        expect_order(failure, functools.partial(read2, root, digest))


def test_reader_refuses_broken_binding(tmp_path: Path) -> None:
    """L-T9: a lifecycle file without a header, or foreign run or identity, refuses."""
    root, _digest, _written = seal_run(tmp_path, [activated()])
    on = on_header(tmp_path, root)
    digest = rewrite(root, LIFECYCLE_ON, _lines(activated(ON_SESSION, 11.0)(on, 1)))
    expect(Failure.HEADER_MISSING, lambda: read2(root, digest))
    (run_dir(root) / LIFECYCLE_ON).unlink()
    (run_dir(root) / LIFECYCLE_ON).parent.rmdir()
    other_run = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-other-run")
    digest = rewrite(root, LIFECYCLE_OFF, _lines(activated()(other_run, 0)))
    expect(Failure.RUN_ID_MISMATCHED, lambda: read2(root, digest))
    other_identity = builders.build_run_header(
        identity=make_identity(tmp_path, pi_root=root, audio_device="USB microphone two"),
        phase=OFF,
        recorded_at_utc=T0,
        monotonic_seconds=1.0,
    )
    digest = rewrite(root, LIFECYCLE_OFF, _lines(activated()(other_identity, 0)))
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: read2(root, digest))


def test_equal_instants_and_phase_one_only_runs_read(tmp_path: Path) -> None:
    """A1-7: equal recording instants are valid; a phase-1-only run reads its lifecycle."""
    root, digest, written = seal_run(
        tmp_path, [activated(at=10.0), stopped(Stop.CONFIRMED, 10.0), started(Start.STARTED, 10.0)]
    )
    retained = read2(root, digest)
    assert list(retained.lifecycle) == written
    assert [item.header.phase for item in retained.run.headers] == [OFF]


# ------------------------------------------------------ L-T10 reader hardening


def _document(tmp_path: Path) -> tuple[str, dict[str, typing.Any]]:
    """Seal a one-record lifecycle run and return its root and the record document."""
    root, _digest, written = seal_run(tmp_path, [activated(at=10.5)])
    return root, written[0].model_dump(mode="json")


def _canonical(document: dict[str, typing.Any]) -> str:
    """Return canonical text for one document."""
    return store.canonical_json(document)


_STORE_CASES: list[
    tuple[str, typing.Callable[[dict[str, typing.Any]], bytes], store.ColdEvidenceStoreFailure]
] = [
    ("version 1", lambda d: line_of({**d, "schema_version": 1}), Failure.SCHEMA_VERSION_UNKNOWN),
    ("version 3", lambda d: line_of({**d, "schema_version": 3}), Failure.SCHEMA_VERSION_UNKNOWN),
    (
        "version true",
        lambda d: line_of({**d, "schema_version": True}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    (
        "version missing",
        lambda d: line_of({k: v for k, v in d.items() if k != "schema_version"}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    ("wrong stream", lambda d: line_of({**d, "stream": "tick"}), Failure.LINE_MALFORMED),
    ("unknown event", lambda d: line_of({**d, "event": "phase_exploded"}), Failure.LINE_MALFORMED),
    ("extra field", lambda d: line_of({**d, "zzz": 1}), Failure.LINE_MALFORMED),
    (
        "missing field",
        lambda d: line_of({k: v for k, v in d.items() if k != "event_utc"}),
        Failure.LINE_MALFORMED,
    ),
    ("int for float", lambda d: line_of({**d, "monotonic_seconds": 11}), Failure.LINE_MALFORMED),
    ("true for int", lambda d: line_of({**d, "sequence": True}), Failure.LINE_MALFORMED),
    (
        "string for float",
        lambda d: line_of({**d, "monotonic_seconds": "10.5"}),
        Failure.LINE_MALFORMED,
    ),
    ("wrong phase", lambda d: line_of({**d, "phase": "recording_on"}), Failure.LINE_MALFORMED),
    ("not an object", lambda _d: b"[]\n", Failure.LINE_MALFORMED),
    (
        "reordered keys",
        lambda d: (
            json.dumps(
                dict(reversed(list(d.items()))), separators=(",", ":"), ensure_ascii=False
            ).encode()
            + b"\n"
        ),
        Failure.LINE_NOT_CANONICAL,
    ),
    (
        "whitespace",
        lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False).encode() + b"\n",
        Failure.LINE_NOT_CANONICAL,
    ),
    (
        "trailing zero",
        lambda d: (
            _canonical(d).replace('"monotonic_seconds":10.5', '"monotonic_seconds":10.50').encode()
            + b"\n"
        ),
        Failure.LINE_NOT_CANONICAL,
    ),
    (
        "exponent",
        lambda d: (
            _canonical(d).replace('"monotonic_seconds":10.5', '"monotonic_seconds":105e-1').encode()
            + b"\n"
        ),
        Failure.LINE_NOT_CANONICAL,
    ),
    (
        "escaped text",
        lambda d: (
            _canonical(d).replace('"stream":"lifecycle"', '"stream":"\\u006cifecycle"').encode()
            + b"\n"
        ),
        Failure.LINE_NOT_CANONICAL,
    ),
    (
        "duplicate key",
        lambda d: (_canonical(d)[:-1] + ',"sequence":0}').encode() + b"\n",
        Failure.JSON_DUPLICATE_KEY,
    ),
    (
        "nan literal",
        lambda d: (
            _canonical(d).replace('"monotonic_seconds":10.5', '"monotonic_seconds":NaN').encode()
            + b"\n"
        ),
        Failure.JSON_NOT_FINITE,
    ),
    ("oversize", lambda _d: b"x" * (reader.MAX_LINE_BYTES + 1) + b"\n", Failure.LINE_TOO_LARGE),
    ("torn", lambda d: _canonical(d).encode(), Failure.LINE_MALFORMED),
    ("empty file", lambda _d: b"", Failure.LINE_MALFORMED),
    ("blank line", lambda d: line_of(d) + b"\n", Failure.LINE_MALFORMED),
]


@pytest.mark.parametrize(
    ("label", "make", "failure"), _STORE_CASES, ids=[case[0] for case in _STORE_CASES]
)
def test_reader_refuses_each_malformed_lifecycle_line(
    tmp_path: Path,
    label: str,
    make: typing.Callable[[dict[str, typing.Any]], bytes],
    failure: store.ColdEvidenceStoreFailure,
) -> None:
    """L-T10/A1-3: each malformed or non-canonical line maps to its exact failure."""
    root, document = _document(tmp_path)
    assert _canonical(document).count('"monotonic_seconds":10.5') == 1, label
    digest = rewrite(root, LIFECYCLE_OFF, make(document))
    expect(failure, lambda: read2(root, digest))


def test_reader_walks_lines_before_version_dispatch(tmp_path: Path) -> None:
    """L-T10: a depth breach is refused by the shared walker."""
    root, document = _document(tmp_path)
    nested: object = 1
    for _ in range(10):
        nested = [nested]
    digest = rewrite(root, LIFECYCLE_OFF, line_of({**document, "zzz": nested}))
    expect_evidence(schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED, lambda: read2(root, digest))


def test_v1_stream_version_two_stays_unknown_in_v2(tmp_path: Path) -> None:
    """L-T10: v2 does not widen the v1 stream version set."""
    root, _digest, _written = seal_run(tmp_path, [activated()])
    tick = json.loads((run_dir(root) / "records/recording_off/tick.jsonl").read_bytes())
    digest = rewrite(
        root, "records/recording_off/tick.jsonl", line_of({**tick, "schema_version": 2})
    )
    expect(Failure.SCHEMA_VERSION_UNKNOWN, lambda: read2(root, digest))


@pytest.mark.parametrize(
    "relative_path",
    [
        "records/recording_off/lifecycle.jsonl.bak",
        "records/recording_off/lifecycle2.jsonl",
        "records/lifecycle.jsonl",
        "records/recording_sideways/lifecycle.jsonl",
    ],
)
def test_v2_reader_refuses_unknown_record_files(tmp_path: Path, relative_path: str) -> None:
    """L-T10: an unknown ``records/`` entry is refused, never ignored."""
    root, _digest, _written = seal_run(tmp_path, [activated()])
    digest = rewrite(root, relative_path, b"{}\n")
    expect(Failure.ENTRY_PATH_INVALID, lambda: read2(root, digest))


def test_non_record_artefacts_stay_verified_but_unparsed(tmp_path: Path) -> None:
    """Entries outside ``records/`` are integrity-checked and never decoded."""
    root, _digest, written = seal_run(tmp_path, [activated()])
    digest = rewrite(root, "artefacts/primary.bin", b"\x00\xff not json")
    assert list(read2(root, digest).lifecycle) == written


def test_tampered_lifecycle_is_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-T10/LM5: a flipped lifecycle byte refuses at verification; nothing is decoded."""
    calls: list[bytes] = []
    real = reader._read_lifecycle_line  # pyright: ignore[reportPrivateUsage]

    def spy(line: bytes, **kwargs: typing.Any) -> lifecycle.ColdLifecycleRecord:
        calls.append(line)
        return real(line, **kwargs)

    monkeypatch.setattr(reader, "_read_lifecycle_line", spy)
    root, digest, _written = seal_run(tmp_path, [activated()])
    read2(root, digest)
    assert len(calls) == 1
    calls.clear()
    path = run_dir(root) / LIFECYCLE_OFF
    data = bytearray(path.read_bytes())
    data[10] ^= 0x01
    path.write_bytes(bytes(data))
    expect(Failure.FILE_DIGEST_MISMATCHED, lambda: read2(root, digest))
    assert calls == []


# ------------------------------------------- repair: sign, budget, int-for-float


def _record_samples(
    tmp_path: Path,
) -> tuple[
    lifecycle.ColdLifecycleRecord, lifecycle.ColdLifecycleRecord, lifecycle.ColdLifecycleRecord
]:
    """Return valid CHILD_STOPPED, OBSERVATION_WINDOW_ELAPSED, and TRANSITION_MEASURED records."""
    sample = samples(tmp_path)
    stop = sample[Event.CHILD_STOPPED]
    window = sample[Event.OBSERVATION_WINDOW_ELAPSED]
    moved = sample[Event.TRANSITION_MEASURED]
    assert (stop.event_monotonic_seconds, stop.monotonic_seconds) == (5.0, 5.0)
    assert (window.scheduled_end_monotonic, window.event_monotonic_seconds) == (1810.0, 1810.0)
    assert (moved.transition_start_monotonic, moved.transition_end_monotonic) == (1810.0, 1830.0)
    return stop, window, moved


def test_negative_event_time_alone_is_refused(tmp_path: Path) -> None:
    """Repair 1: a -1.0 event with a positive append time is refused only by its sign rule.

    Every cross-field rule holds (event <= append, no matrix field touched), so the
    refusal is attributable to the event-instant sign guard alone.
    """
    stop, _window, _moved = _record_samples(tmp_path)
    consistent = forged(stop, event_monotonic_seconds=0.0)
    assert lifecycle.validate_lifecycle_record(consistent) == consistent
    refused(forged(stop, event_monotonic_seconds=-1.0))


def test_negative_transition_start_alone_is_refused(tmp_path: Path) -> None:
    """Repair 1: a negative start with a non-negative end, exact delta, and flag is refused.

    ``transition_seconds == end - start`` and ``within`` stay exactly consistent, so
    only the start sign guard explains the refusal.
    """
    _stop, _window, moved = _record_samples(tmp_path)
    event = moved.event_monotonic_seconds
    near = forged(
        moved,
        transition_start_monotonic=0.0,
        transition_seconds=event - 0.0,
        transition_within_budget=False,
    )
    assert lifecycle.validate_lifecycle_record(near) == near
    start = event - 30.0 - 1830.0
    assert start < 0.0 <= event
    negative = forged(
        moved,
        transition_start_monotonic=start,
        transition_seconds=event - start,
        transition_within_budget=0.0 <= event - start <= 60.0,
    )
    refused(negative)


def test_negative_scheduled_end_alone_is_refused(tmp_path: Path) -> None:
    """Repair 1: an elapsed window with a negative scheduled end is refused by its sign rule.

    The event instant still follows the scheduled end, so the ordering rule holds.
    """
    _stop, window, _moved = _record_samples(tmp_path)
    zero = forged(window, scheduled_end_monotonic=0.0)
    assert lifecycle.validate_lifecycle_record(zero) == zero
    refused(forged(window, scheduled_end_monotonic=-1.0))


def test_coupled_negative_append_and_transition_end_are_refused(tmp_path: Path) -> None:
    """Repair 1: negative append time and negative transition end are refused.

    These are coupled cases, not isolated oracles: a negative append time requires a
    non-positive event instant (event <= append), and a negative transition end must
    equal an equally negative event instant.  Each refusal is therefore also explained
    by the event-instant sign guard; they cannot prove their own redundant guard alone.
    """
    stop, _window, moved = _record_samples(tmp_path)
    refused(forged(stop, monotonic_seconds=-1.0, event_monotonic_seconds=-1.0))
    refused(forged(stop, monotonic_seconds=-1.0, event_monotonic_seconds=-2.0))
    refused(
        forged(
            moved,
            event_monotonic_seconds=-1.0,
            transition_end_monotonic=-1.0,
            transition_start_monotonic=0.0,
            transition_seconds=-1.0,
            transition_within_budget=False,
        )
    )


def _stopped_document(tmp_path: Path) -> tuple[str, dict[str, typing.Any]]:
    """Seal a run holding one CHILD_STOPPED record and return its root and document."""
    root, digest, written = seal_run(tmp_path, [stopped(Stop.CONFIRMED, 5.0)])
    assert list(read2(root, digest).lifecycle) == written
    return root, written[0].model_dump(mode="json")


def _transition_document(tmp_path: Path) -> tuple[str, dict[str, typing.Any]]:
    """Seal a two-phase run and rewrite its recording-on file with a valid transition."""
    root, _off, on = _two_phase(tmp_path)
    record = transition(1810.0, 1830.0)(on, 1)
    document = record.model_dump(mode="json")
    digest = rewrite(root, LIFECYCLE_ON, line_of(document))
    retained = read2(root, digest)
    assert retained.lifecycle[-1] == record
    assert type(retained.lifecycle[-1].transition_budget_seconds) is float
    return root, document


def test_reader_refuses_a_negative_event_time(tmp_path: Path) -> None:
    """Repair 1: a retained CHILD_STOPPED line with a -1.0 event instant is malformed."""
    root, document = _stopped_document(tmp_path)
    digest = rewrite(root, LIFECYCLE_OFF, line_of({**document, "event_monotonic_seconds": -1.0}))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, digest))


def test_reader_refuses_a_negative_transition_start(tmp_path: Path) -> None:
    """Repair 1: a retained transition with a negative start and exact delta is malformed."""
    root, document = _transition_document(tmp_path)
    changed = {
        **document,
        "transition_start_monotonic": -10.0,
        "transition_seconds": 1840.0,
        "transition_within_budget": False,
    }
    assert changed["transition_end_monotonic"] - changed["transition_start_monotonic"] == 1840.0
    digest = rewrite(root, LIFECYCLE_ON, line_of(changed))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, digest))


@pytest.mark.parametrize("budget", [60, True], ids=["int60", "true"])
def test_fixed_budget_refuses_non_float_natives(tmp_path: Path, budget: object) -> None:
    """Repair 2: only the exact float budget is admitted; ``60`` and ``True`` are refused.

    Revalidation and the real writer path both refuse; the float baseline round-trips.
    """
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    on = on_header(tmp_path, root)
    writer.append(on)
    moved = transition(1810.0, 1830.0)(on, 0)
    assert type(moved.transition_budget_seconds) is float
    forged_record = forged(moved, transition_budget_seconds=budget)
    refused(forged_record)
    expect_evidence(NOT_VALIDATED, lambda: writer.append_lifecycle(forged_record))
    writer.append_lifecycle(moved)
    retained = read2(root, writer.seal().manifest_sha256)
    assert list(retained.lifecycle) == [moved]
    assert type(retained.lifecycle[0].transition_budget_seconds) is float


@pytest.mark.parametrize("budget", [60, True], ids=["int60", "true"])
def test_reader_refuses_a_non_float_budget(tmp_path: Path, budget: object) -> None:
    """Repair 2: a retained budget of JSON ``60`` or ``true`` is malformed."""
    root, document = _transition_document(tmp_path)
    digest = rewrite(root, LIFECYCLE_ON, line_of({**document, "transition_budget_seconds": budget}))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, digest))


def test_absolute_fields_refuse_integers_with_valid_values(tmp_path: Path) -> None:
    """Repair 3: each absolute field refuses an int even when its value keeps every rule."""
    stop, window, moved = _record_samples(tmp_path)
    cases: list[tuple[lifecycle.ColdLifecycleRecord, dict[str, object]]] = [
        (stop, {"monotonic_seconds": 5}),
        (stop, {"event_monotonic_seconds": 5}),
        (window, {"scheduled_end_monotonic": 1810}),
        (moved, {"transition_start_monotonic": 1810}),
        (moved, {"transition_end_monotonic": 1830}),
        (moved, {"transition_seconds": 20}),
    ]
    for record, update in cases:
        ((name, value),) = update.items()
        assert getattr(record, name) == value
        refused(forged(record, **update))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("event_monotonic_seconds", 1830),
        ("monotonic_seconds", 1830),
        ("transition_start_monotonic", 1810),
        ("transition_end_monotonic", 1830),
    ],
)
def test_reader_refuses_integer_absolute_fields(tmp_path: Path, name: str, value: int) -> None:
    """Repair 3: a retained absolute field written as a JSON integer is malformed."""
    root, document = _transition_document(tmp_path)
    assert document[name] == value
    digest = rewrite(root, LIFECYCLE_ON, line_of({**document, name: value}))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, digest))


def test_reader_refuses_an_integer_scheduled_end(tmp_path: Path) -> None:
    """Repair 3: a retained elapsed window with an integer scheduled end is malformed."""
    root, digest, written = seal_run(tmp_path, [elapsed(OFF_SESSION, 1810.0)])
    assert list(read2(root, digest).lifecycle) == written
    document = written[0].model_dump(mode="json")
    digest = rewrite(root, LIFECYCLE_OFF, line_of({**document, "scheduled_end_monotonic": 1810}))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, digest))


# ---------------------------------------------------------- L-T12 canaries


def _canary() -> str:
    """Assemble a secret-shaped canary at runtime."""
    return "".join(["sk-", "live-", "Qv7Lc9", "Zt41Xw"])


def _assert_contained(error: BaseException, canary: str) -> None:
    """Assert a canary is absent from every rendering and the chain is empty."""
    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), repr(error.args), rendered):
        assert canary not in text
    assert error.__cause__ is None
    assert error.__context__ is None


def test_refusals_never_carry_session_ids_or_instants(tmp_path: Path) -> None:
    """L-T12: canaries in refused records never reach any error rendering."""
    canary = _canary()
    instant = "".join(["2031-07-19T03:41:", "59+05:00"])
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append_lifecycle(activated()(off, 0))
    valid = activated(canary)(off, 1)
    calls: list[typing.Callable[[], object]] = [
        lambda: lc(off, 0, Event.CHILD_STOPPED, child_stop=Stop.CONFIRMED, session_id=canary),
        lambda: lc(off, 0, Event.PHASE_ACTIVATED, session_id="s", previous_phase_session_id=canary),
        lambda: builders.build_lifecycle_record(
            header=off,
            sequence=0,
            event=Event.CHILD_STOPPED,
            event_utc=instant,
            event_monotonic_seconds=1.0,
            recorded_at_utc=T0,
            monotonic_seconds=1.0,
            child_stop=Stop.CONFIRMED,
        ),
        lambda: lifecycle.validate_lifecycle_record(forged(valid, session_id=canary + " \ud800")),
        lambda: lifecycle.validate_lifecycle_record(forged(valid, recorded_at_utc=instant)),
        lambda: writer.append_lifecycle(activated(canary)(off, 7)),
        lambda: writer.append_lifecycle(forged(valid, previous_phase_session_id=canary)),
    ]
    for call in calls:
        with pytest.raises(
            (schema.ColdEvidenceError, store.ColdEvidenceStoreError, lifecycle.ColdLifecycleError)
        ) as raised:
            call()
        _assert_contained(raised.value, canary)
        _assert_contained(raised.value, instant)
    document = valid.model_dump(mode="json")
    root2, _digest, _written = seal_run(tmp_path / "reader", [activated()])
    digest = rewrite(root2, LIFECYCLE_OFF, json.dumps(document, sort_keys=True).encode() + b"\n")
    with pytest.raises(store.ColdEvidenceStoreError) as raised_read:
        read2(root2, digest)
    _assert_contained(raised_read.value, canary)


# ---------------------------------------------------------- L-T13 absence


def test_not_admitted_never_proves_absence(tmp_path: Path) -> None:
    """L-T13: a missing session round-trips as ``NOT_ADMITTED`` with no identity."""
    root, digest, _written = seal_run(tmp_path, [aborted(None, True, 5.0)])
    record = read2(root, digest).lifecycle[0]
    assert record.session_admission is Admission.NOT_ADMITTED
    assert record.session_id is None
    assert "never proves absence" in (Admission.__doc__ or "")
    assert "never proves that no session exists" in (builders.build_lifecycle_record.__doc__ or "")


# ------------------------------------------------------------- L-T14 fences


_LIFECYCLE_SOURCE = (COLD_PACKAGE / "evidence_lifecycle.py").read_text(encoding="utf-8")


def test_lifecycle_module_imports_exactly_its_allow_list() -> None:
    """L-T14: the pure module imports only stdlib, pydantic, schema, and policy."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_LIFECYCLE_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module)
    cold = "roastpilot_agent.cold_characterisation."
    assert imported == {
        "enum",
        "math",
        "typing",
        "datetime",
        "pydantic",
        cold + "evidence_schema",
        cold + "engine_policy",
    }


def test_lifecycle_module_carries_no_forbidden_names() -> None:
    """L-T14: no outcome/verdict/report/evaluation/qualification token, actuator, or reads."""
    tree = ast.parse(_LIFECYCLE_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "active",
                "elapsed_monotonic_seconds",
                "device_state",
                "first_crack_status",
            }
    identifiers = _fence_identifiers(_LIFECYCLE_SOURCE)
    assert _token_violations("evidence_lifecycle.py", identifiers) == set()
    assert {name for name in identifiers if "qualif" in name.lower()} == set()
    for text in _FORBIDDEN_CAPABILITY_TEXT:
        assert text not in _LIFECYCLE_SOURCE
    assigned = {
        ast.unparse(target)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    assert "COLD_PHASE_OBSERVATION_SECONDS" not in assigned
    assert "COLD_TRANSITION_BUDGET_SECONDS" in assigned
    assert lifecycle.COLD_PHASE_OBSERVATION_SECONDS is engine_policy.COLD_PHASE_OBSERVATION_SECONDS


#: The builders test's actuator/limit/clean-conjunction text fence, plus ``subprocess``.
_FORBIDDEN_CAPABILITY_TEXT: tuple[str, ...] = (
    "set_heat",
    "set_fan",
    "drop_beans",
    "start_cooling",
    "stop_cooling",
    "emergency_stop",
    "mark_first_crack",
    "set_targets",
    "call_tool",
    "RoasterControlAdapter",
    "MAX_CONSECUTIVE_OVERFLOW",
    "LOST_AUDIO_MS",
    "FATAL_STREAK",
    "EFFECTIVE_HOP",
    "HOST_MAX",
    "HOST_MIN",
    "finalisation_is_clean",
    "subprocess",
)


def _fence_identifiers(source: str) -> set[str]:
    """Return the builders test's identifiers plus every argument and keyword name."""
    names = set(_identifiers(source))
    for node in ast.walk(ast.parse(source)):
        name = node.arg if isinstance(node, (ast.arg, ast.keyword)) else None
        if name is not None:
            names.add(name)
    return names


def test_fence_sees_argument_and_keyword_identifiers() -> None:
    """Structural evidence: the extended collector sees names the shared one does not.

    This proves only that the fence inspects argument and keyword identifiers; it says
    nothing about any runtime behaviour.
    """
    synthetic = "def f(evaluate_x):\n    g(verdict_y=1, qualified_z=2)\n"
    assert {"evaluate_x", "verdict_y", "qualified_z"} <= _fence_identifiers(synthetic)
    assert {"evaluate_x", "verdict_y", "qualified_z"}.isdisjoint(_identifiers(synthetic))
    assert _token_violations("evidence_lifecycle.py", _fence_identifiers(synthetic)) == {
        "evaluate_x",
        "verdict_y",
    }
    assert {"set_heat", "start_cooling", "RoasterControlAdapter"} <= set(_FORBIDDEN_CAPABILITY_TEXT)


def test_event_after_append_is_refused_at_every_boundary(tmp_path: Path) -> None:
    """An event instant after its append instant is refused; equal instants are admitted.

    ``CHILD_STOPPED`` carries no derived arithmetic, so apart from the event-before-append
    rule every fact stays valid at revalidation, builder, writer, and reader boundaries.
    """
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    equal = lc(off, 0, Event.CHILD_STOPPED, at=5.0, event_at=5.0, child_stop=Stop.CONFIRMED)
    assert lifecycle.validate_lifecycle_record(equal) == equal
    later = forged(equal, event_monotonic_seconds=6.0)
    refused(later)
    expect_evidence(
        NOT_VALIDATED,
        lambda: lc(off, 0, Event.CHILD_STOPPED, at=5.0, event_at=6.0, child_stop=Stop.CONFIRMED),
    )
    expect_evidence(NOT_VALIDATED, lambda: writer.append_lifecycle(later))
    writer.append_lifecycle(equal)
    digest = writer.seal().manifest_sha256
    assert list(read2(root, digest).lifecycle) == [equal]
    document = equal.model_dump(mode="json")
    tampered = rewrite(root, LIFECYCLE_OFF, line_of({**document, "event_monotonic_seconds": 6.0}))
    expect(Failure.LINE_MALFORMED, lambda: read2(root, tampered))


def test_lifecycle_enums_are_plain_closed_enums() -> None:
    """L-T14: every lifecycle enum is a plain ``Enum`` whose values are lower-case names."""
    enums = [
        value
        for value in vars(lifecycle).values()
        if isinstance(value, type)
        and issubclass(value, enum.Enum)
        and value.__module__ == lifecycle.__name__
    ]
    assert len(enums) == 9
    for kind in enums:
        assert kind.__bases__ == (enum.Enum,)
        assert not issubclass(kind, (str, int))
        assert all(member.value == member.name.lower() for member in kind)
    assert "CANCELLED" not in Reason.__members__
    assert lifecycle.ColdLifecycleError(Order.PHASE_REGRESSED).args == (
        "Cold lifecycle evidence refused.",
    )
