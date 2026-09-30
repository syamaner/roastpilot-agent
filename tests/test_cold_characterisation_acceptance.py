"""Behavioural and fail-closed tests for cold-characterisation interpretation (#954, 3B-ii-c-i).

Runs are written through the real writer, sealed, and read back with the strict
reader under ``tmp_path``.  Inadmissible identities use the bypassed-header
pattern.  Finalisation results are the committed fixture, re-parsed and changed
in memory; the fixture file is never edited and is never treated as qualifying.
Private evaluators are called directly only for defensive cases that strict
snapshots cannot reach.
"""

import ast
import builtins
import collections
import copy
import enum
import functools
import inspect
import io
import json
import math
import os
import re
import types
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.appliance.model_manifest import REVISION as PINNED_MODEL_REVISION
from roastpilot_agent.cold_characterisation import acceptance
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation import identity as live
from roastpilot_agent.cold_characterisation.mcp import SessionFinalisationResult
from tests.test_cold_characterisation_evidence_builders import (
    RUN_ID,
    abort_for,
    advisory_for,
    audio_payload,
    device_state,
    finalisation_payload,
    header_for,
    host_for,
    observation,
)
from tests.test_cold_characterisation_evidence_reader import envelope_of, identity_document, read
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    open_writer,
    run_dir,
    snapshot_tree,
    write_full_run,
)

F = acceptance.ColdCheckFailure
Check = acceptance.ColdCheck
Outcome = acceptance.ColdCheckOutcome
Refusal = acceptance.ColdInterpretationFailure
Stream = schema.ColdEvidenceStream
Json = dict[str, typing.Any]
Mutation = typing.Callable[[Json], None]
TOKEN = acceptance._REBIND_TOKEN  # pyright: ignore[reportPrivateUsage]
qualify = acceptance._qualify_identity_v1  # pyright: ignore[reportPrivateUsage]
evaluate_runtime = acceptance._evaluate_inference_runtime  # pyright: ignore[reportPrivateUsage]
evaluate_counters = acceptance._evaluate_audio_counters  # pyright: ignore[reportPrivateUsage]
evaluate_duration = acceptance._evaluate_inference_duration  # pyright: ignore[reportPrivateUsage]
derive_d191 = acceptance._derive_d191  # pyright: ignore[reportPrivateUsage]
SOURCE_PATH = Path(acceptance.__file__)
SOURCE = SOURCE_PATH.read_text()
TREE = ast.parse(SOURCE)
CANARY = "sk-live-Canary0123456789AbCdEf"
ON_ROLES = ("primary_wav", "recording_sidecar", "annotation_session_sidecar")
LONG_SERIAL_PATH = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10KZP4H-if00-port0"


# ------------------------------------------------------------------- payloads


def tick_audio(**overrides: object) -> Json:
    """One active, pending per-tick audio payload (with one unknown key)."""
    return {**audio_payload(), **overrides}


def default_ticks() -> tuple[Json, ...]:
    """Three active ticks whose monotonic counters rise."""
    return tuple(
        tick_audio(emitted_window_count=count, processed_window_count=count) for count in (1, 2, 3)
    )


def status(**overrides: object) -> Json:
    """One active, pending first-crack status with clean counters."""
    value: Json = {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "allow_manual_override": False,
        "reason": None,
        "audio_running": True,
        "queued_window_count": 0,
        "emitted_window_count": 10,
        "dropped_window_count": 0,
        "processed_window_count": 10,
        "mic_peak_dbfs": -3.0,
        "mic_rms_dbfs": -12.0,
        "overflow_count_last_minute": 0,
        "estimated_lost_audio_ms_last_minute": 0.0,
        "total_overflow_count": 0,
        "max_consecutive_overflow_count": 0,
        "last_inference_duration_ms": 2.0,
        "max_inference_duration_ms": 3.0,
        "inference_overrun_count": 0,
    }
    return {**value, **overrides}


def final_status(**overrides: object) -> Json:
    """The frozen post-stop status: capture stopped, counters at their final values."""
    return status(
        **{"audio_running": False, "emitted_window_count": 11, "processed_window_count": 11}
        | overrides
    )


def runtime(final: Json | None = None, **overrides: object) -> Json:
    """One clean first-crack runtime stop."""
    value: Json = {
        "outcome": "stopped",
        "stop_error": None,
        "capture_running_after_stop": False,
        "final_status": final_status() if final is None else final,
    }
    return {**value, **overrides}


def artefact(role: str, **overrides: object) -> Json:
    """One stat-only recording artefact."""
    value: Json = {
        "role": role,
        "filename": f"{role}.bin",
        "path": f"/synthetic/recordings/{role}.bin",
        "exists": True,
        "size_bytes": 4096,
    }
    return {**value, **overrides}


def recording_on(artifacts: list[Json] | None = None, **overrides: object) -> Json:
    """Finalised recording-on evidence with the exact role trio."""
    value: Json = {
        "expected": True,
        "outcome": "finalised",
        "reason": None,
        "artifacts": [artefact(role) for role in ON_ROLES] if artifacts is None else artifacts,
    }
    return {**value, **overrides}


def recording_off(**overrides: object) -> Json:
    """Recording-off evidence: not configured, no artefacts."""
    value: Json = {"expected": False, "outcome": "not_configured", "reason": None, "artifacts": []}
    return {**value, **overrides}


def result_for(phase: schema.ColdPhaseKind, **changes: object) -> Json:
    """The committed fixture changed in memory into active, clean evidence for one phase."""
    payload = finalisation_payload()
    payload["pre_finalisation_first_crack_status"] = status()
    payload["first_crack_runtime"] = runtime()
    payload["stages"][1]["status"] = "completed"
    if phase is ON:
        payload["recording"] = recording_on()
        payload["stages"][2]["status"] = "completed"
    return {**payload, **changes}


# ------------------------------------------------------------------- run writer


class Phase(typing.NamedTuple):
    """One phase to write; ``None`` fields take the active, clean defaults."""

    ticks: tuple[Json, ...] | None = None
    results: tuple[Json, ...] | None = None
    document: Mutation | None = None
    extras: bool = False


def bypassed_header(document: Json, phase: schema.ColdPhaseKind) -> schema.ColdRunHeader:
    """Build a header around any v1 identity document, bypassing ``build_run_header``."""
    envelope = envelope_of(document)
    return schema.ColdRunHeader(
        schema_version=1,
        stream="header",
        run_id=RUN_ID,
        phase=phase,
        recorded_at_utc="2026-09-26T12:00:00Z",
        monotonic_seconds=1.0,
        identity_sha256=envelope.sha256,
        identity=envelope,
    )


def tick_record(header: schema.ColdRunHeader, index: int, audio: Json) -> schema.ColdTickRecord:
    """Build one tick through the real builder."""
    return builders.build_tick_record(
        header=header,
        tick=index,
        recorded_at_utc="2026-09-26T12:00:01Z",
        monotonic_seconds=2.0 + index,
        observation=observation(device_state(), audio=audio),
    )


def finalisation_record(
    header: schema.ColdRunHeader, payload: Json, index: int
) -> schema.ColdFinalisationRecord:
    """Build one finalisation record from an in-memory result through the real builder."""
    return builders.build_finalisation_record(
        header=header,
        result=SessionFinalisationResult.model_validate_json(json.dumps(payload)),
        recorded_at_utc="2026-09-26T12:10:00Z",
        monotonic_seconds=600.0 + index,
    )


def write_run(
    tmp_path: Path, phases: dict[schema.ColdPhaseKind, Phase], name: str = "pi"
) -> reader.ColdRetainedRun:
    """Write, seal, and strictly read back one run holding the given phases."""
    writer, root = open_writer(tmp_path, name)
    for phase in schema.ColdPhaseKind:
        spec = phases.get(phase)
        if spec is None:
            continue
        if spec.document is None:
            header = header_for(tmp_path, root, phase)
        else:
            document = identity_document(tmp_path, root)
            spec.document(document)
            header = bypassed_header(document, phase)
        writer.append(header)
        ticks = default_ticks() if spec.ticks is None else spec.ticks
        for index, audio in enumerate(ticks):
            writer.append(tick_record(header, index, audio))
        if spec.extras:
            writer.append(host_for(header))
            writer.append(advisory_for(header))
        results = (result_for(phase),) if spec.results is None else spec.results
        for index, payload in enumerate(results):
            writer.append(finalisation_record(header, payload, index))
        if spec.extras:
            writer.append(abort_for(header))
    sealed = writer.seal()
    return read(root, sealed.manifest_sha256)


def interpret_phase(
    tmp_path: Path, phase: schema.ColdPhaseKind = OFF, **spec: typing.Any
) -> acceptance.ColdPhaseInterpretation:
    """Write one phase, interpret it, and return that phase's interpretation."""
    return acceptance.interpret_retained_run(write_run(tmp_path, {phase: Phase(**spec)})).phases[0]


def failures_of(
    item: acceptance.ColdPhaseInterpretation, check: acceptance.ColdCheck
) -> tuple[F, ...]:
    """Return one check's failures."""
    return next(result for result in item.results if result.check is check).failures


def expect_refusal(
    failure: acceptance.ColdInterpretationFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, content-free, chain-free refusal."""
    with pytest.raises(acceptance.ColdInterpretationError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold interpretation failed.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def genuine(tmp_path: Path) -> tuple[reader.ColdRetainedRun, list[schema.ColdEvidenceRecord]]:
    """Return one genuine two-phase ``write_full_run`` run and the records written."""
    root, sealed, records = write_full_run(tmp_path)
    return read(root, sealed.manifest_sha256), records


def rebuilt(run: reader.ColdRetainedRun, **changes: typing.Any) -> reader.ColdRetainedRun:
    """Construct a caller-built run from a genuine one without validation."""
    values: dict[str, typing.Any] = {
        "run_id": run.run_id,
        "manifest_sha256": run.manifest_sha256,
        "headers": run.headers,
        "streams": run.streams,
    }
    return reader.ColdRetainedRun.model_construct(**(values | changes))


def replaced(
    run: reader.ColdRetainedRun,
    phase: schema.ColdPhaseKind,
    stream: schema.ColdEvidenceStream,
    item: object | None,
) -> tuple[object, ...]:
    """Return the run's streams with one container replaced, or dropped when ``item`` is None."""
    streams: list[object] = []
    for current in run.streams:
        if current.phase is phase and current.stream is stream:
            if item is not None:
                streams.append(item)
        else:
            streams.append(current)
    return tuple(streams)


def container(
    run: reader.ColdRetainedRun, phase: schema.ColdPhaseKind, stream: schema.ColdEvidenceStream
) -> reader.ColdRetainedStream:
    """Return one stream container of a run."""
    return next(item for item in run.streams if item.phase is phase and item.stream is stream)


def at(path: str, value: object) -> Mutation:
    """Return a mutation setting one dotted identity-document path."""
    *parents, leaf = path.split(".")

    def mutate(document: Json) -> None:
        target = document
        for key in parents:
            target = target[key]
        target[leaf] = value

    return mutate


def mutations(*items: Mutation) -> Mutation:
    """Compose several identity-document mutations."""

    def mutate(document: Json) -> None:
        for item in items:
            item(document)

    return mutate


# ------------------------------------------------------------ R1-R6 rebinding


def test_genuine_two_phase_run_interprets_into_fresh_snapshots(tmp_path: Path) -> None:
    """R1: a genuine run binds once; ``run.headers`` is compared, never bound."""
    run, records = genuine(tmp_path)
    interpretation = acceptance.interpret_retained_run(run)
    rebound = interpretation.rebound
    assert (rebound.run_id, rebound.manifest_sha256) == (run.run_id, run.manifest_sha256)
    off, on = rebound.phases
    assert (off.phase, on.phase) == (OFF, ON)
    assert (off.header, on.header) == (records[0], records[5])
    assert (off.ticks, off.hosts, off.advisories) == (
        (records[1], records[2]),
        (records[3],),
        (records[4],),
    )
    assert (off.finalisations, off.aborts, off.finalisation, off.finalisation_ambiguous) == (
        (),
        (),
        None,
        False,
    )
    assert (on.ticks, on.hosts, on.finalisations, on.aborts) == (
        (records[6],),
        (records[7],),
        (records[8],),
        (records[9],),
    )
    assert on.finalisation == SessionFinalisationResult.model_validate_json(
        json.dumps(finalisation_payload())
    )
    assert on.finalisation_ambiguous is False
    retained = [record for item in run.streams for record in item.records]
    assert not any(
        snapshot is original for snapshot in (*off.ticks, on.header) for original in retained
    )
    assert [item.phase for item in interpretation.phases] == [OFF, ON]
    assert [item.identity_sha256 for item in interpretation.phases] == [
        records[0].identity_sha256,
        records[5].identity_sha256,
    ]
    for item in interpretation.phases:
        assert tuple(result.check for result in item.results) == tuple(Check)


def test_t_b14_engine_abort_rebinds_into_its_phase_without_a_verdict(tmp_path: Path) -> None:
    """T-B14: a retained ENGINE abort rebinds into its phase; nothing carries a verdict."""
    writer, root = open_writer(tmp_path)
    header = header_for(tmp_path, root, OFF)
    tick = tick_record(header, 0, tick_audio())
    engine_abort = builders.build_abort_record(
        header=header,
        domain=schema.ColdAbortDomain.ENGINE,
        reason=schema.ColdEngineAbortReason.SESSION_CLOCK_STALLED,
        recorded_at_utc="2026-09-26T12:05:00Z",
        monotonic_seconds=300.0,
    )
    for record in (header, tick, engine_abort):
        writer.append(record)
    run = read(root, writer.seal().manifest_sha256)
    (off,) = acceptance.interpret_retained_run(run).rebound.phases
    assert off.phase is OFF
    assert off.ticks == (tick,)
    assert off.aborts == (engine_abort,)
    assert off.aborts[0].domain is schema.ColdAbortDomain.ENGINE
    assert off.aborts[0].reason is schema.ColdEngineAbortReason.SESSION_CLOCK_STALLED
    names = [name for name in dir(off) if not name.startswith("_")]
    for model in (schema.ColdAbortRecord, schema.ColdTickRecord, schema.ColdTickSessionEvidence):
        names.extend(model.model_fields)
    assert "aborts" in names and "session" in names
    for name in names:
        assert not any(token in name for token in ("verdict", "qualif", "clean")), name


class SubRun(reader.ColdRetainedRun):
    """A subclassed run container."""


class SubHeader(reader.ColdRetainedHeader):
    """A subclassed header container."""


class SubStream(reader.ColdRetainedStream):
    """A subclassed stream container."""


Forge = typing.Callable[[reader.ColdRetainedRun], object]


def forge(function: Forge) -> Forge:
    """Type one parametrized container forgery."""
    return function


def _two_record_header(run: reader.ColdRetainedRun) -> object:
    item = container(run, OFF, Stream.HEADER)
    doubled = reader.ColdRetainedStream.model_construct(
        phase=OFF, stream=Stream.HEADER, records=item.records * 2
    )
    return rebuilt(run, streams=replaced(run, OFF, Stream.HEADER, doubled))


def _tick_in_host(run: reader.ColdRetainedRun) -> object:
    tick = container(run, OFF, Stream.TICK).records[0]
    host = reader.ColdRetainedStream.model_construct(phase=OFF, stream=Stream.HOST, records=(tick,))
    return rebuilt(run, streams=replaced(run, OFF, Stream.HOST, host))


def _off_record_in_on(run: reader.ColdRetainedRun) -> object:
    tick = container(run, OFF, Stream.TICK).records[0]
    ticks = reader.ColdRetainedStream.model_construct(phase=ON, stream=Stream.TICK, records=(tick,))
    return rebuilt(run, streams=replaced(run, ON, Stream.TICK, ticks))


def _stream_with(run: reader.ColdRetainedRun, **changes: typing.Any) -> object:
    item = container(run, OFF, Stream.TICK)
    values: dict[str, typing.Any] = {
        "phase": item.phase,
        "stream": item.stream,
        "records": item.records,
    }
    forged = reader.ColdRetainedStream.model_construct(**(values | changes))
    return rebuilt(run, streams=replaced(run, OFF, Stream.TICK, forged))


CONTAINER_FORGERIES: list[tuple[str, Forge]] = [
    ("not-a-run", forge(lambda run: run.model_dump())),
    ("subclassed-run", forge(lambda run: SubRun.model_construct(**dict(run)))),
    (
        "subclassed-header",
        forge(
            lambda run: rebuilt(
                run,
                headers=tuple(
                    SubHeader.model_construct(header=item.header, identity=item.identity)
                    for item in run.headers
                ),
            )
        ),
    ),
    (
        "state-pairs-for-headers",
        forge(lambda run: rebuilt(run, headers=tuple((i.header, i.identity) for i in run.headers))),
    ),
    (
        "subclassed-stream",
        forge(
            lambda run: rebuilt(
                run,
                streams=tuple(
                    SubStream.model_construct(phase=i.phase, stream=i.stream, records=i.records)
                    for i in run.streams
                ),
            )
        ),
    ),
    ("headers-list", forge(lambda run: rebuilt(run, headers=list(run.headers)))),
    ("streams-list", forge(lambda run: rebuilt(run, streams=list(run.streams)))),
    (
        "records-list",
        forge(
            lambda run: _stream_with(run, records=list(container(run, OFF, Stream.TICK).records))
        ),
    ),
    ("empty-records", forge(lambda run: _stream_with(run, records=()))),
    ("phase-as-text", forge(lambda run: _stream_with(run, phase="recording_off"))),
    ("stream-as-text", forge(lambda run: _stream_with(run, stream="tick"))),
    (
        "stream-without-records",
        forge(
            lambda run: rebuilt(
                run,
                streams=(reader.ColdRetainedStream.model_construct(phase=OFF, stream=Stream.TICK),),
            )
        ),
    ),
    (
        "duplicate-phase-stream",
        forge(lambda run: rebuilt(run, streams=(*run.streams, container(run, OFF, Stream.TICK)))),
    ),
    (
        "phase-without-header",
        forge(lambda run: rebuilt(run, streams=replaced(run, OFF, Stream.HEADER, None))),
    ),
    ("two-record-header", forge(_two_record_header)),
    ("tick-in-host-container", forge(_tick_in_host)),
    ("off-record-in-on-container", forge(_off_record_in_on)),
    ("run-id-traversal", forge(lambda run: rebuilt(run, run_id="../" + CANARY))),
    ("run-id-uppercase", forge(lambda run: rebuilt(run, run_id=RUN_ID.upper()))),
    ("run-id-not-text", forge(lambda run: rebuilt(run, run_id=20260926))),
    (
        "digest-uppercase",
        forge(lambda run: rebuilt(run, manifest_sha256=run.manifest_sha256.upper())),
    ),
    ("digest-63", forge(lambda run: rebuilt(run, manifest_sha256=run.manifest_sha256[:63]))),
    ("digest-not-text", forge(lambda run: rebuilt(run, manifest_sha256=None))),
    (
        "run-without-streams-field",
        forge(
            lambda run: reader.ColdRetainedRun.model_construct(
                run_id=run.run_id, manifest_sha256=run.manifest_sha256, headers=run.headers
            )
        ),
    ),
]


@pytest.mark.parametrize(
    "make", [item[1] for item in CONTAINER_FORGERIES], ids=[item[0] for item in CONTAINER_FORGERIES]
)
def test_forged_or_relabelled_containers_are_malformed(tmp_path: Path, make: Forge) -> None:
    """R2: exact types, identifiers, uniqueness and container consistency fail closed."""
    run, _records = genuine(tmp_path)
    forged = make(run)
    expect_refusal(
        Refusal.CONTAINER_MALFORMED,
        lambda: acceptance.interpret_retained_run(typing.cast(reader.ColdRetainedRun, forged)),
    )


def test_a_run_without_streams_has_no_phase(tmp_path: Path) -> None:
    """R2: no streams (and so no bound header) is refused as no phase present."""
    run, _records = genuine(tmp_path)
    expect_refusal(
        Refusal.NO_PHASE_PRESENT,
        lambda: acceptance.interpret_retained_run(rebuilt(run, headers=(), streams=())),
    )


def _foreign_run_id(run: reader.ColdRetainedRun) -> object:
    return rebuilt(run, run_id="20260926T120000Z-foreign")


def _roast_fan_forge(
    tick: schema.ColdTickRecord, outcome: schema.ColdTickRoastFanOutcome, level: int | None
) -> schema.ColdTickRecord:
    """Copy a tick with an unvalidated nested roast-fan pair (no top-level key added)."""
    forged_fan = tick.roast_fan.model_copy(
        update={"outcome": outcome, "roast_fan_level_percent": level}
    )
    return tick.model_copy(update={"roast_fan": forged_fan})


def _invalid_tick_record(run: reader.ColdRetainedRun) -> schema.ColdTickRecord:
    tick = typing.cast(schema.ColdTickRecord, container(run, OFF, Stream.TICK).records[0])
    return _roast_fan_forge(tick, schema.ColdTickRoastFanOutcome.OBSERVED, 101)


def _invalid_tick(run: reader.ColdRetainedRun) -> object:
    forged = _invalid_tick_record(run)
    return rebuilt(run, streams=replaced(run, OFF, Stream.TICK, _single(OFF, Stream.TICK, forged)))


def test_invalid_tick_forge_fails_for_the_pairing_reason(tmp_path: Path) -> None:
    """T-E15: the forge is a D197 pairing breach, not an unknown top-level key."""
    run, _records = genuine(tmp_path)
    forged = _invalid_tick_record(run)
    assert set(forged.__dict__) == set(schema.ColdTickRecord.model_fields)
    with pytest.raises(schema.ColdEvidenceError) as raised:
        schema.validate_record(forged)
    assert raised.value.failure is schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
    tick = typing.cast(schema.ColdTickRecord, container(run, OFF, Stream.TICK).records[0])
    control = _roast_fan_forge(tick, schema.ColdTickRoastFanOutcome.NOT_ELIGIBLE, None)
    assert set(control.__dict__) == set(schema.ColdTickRecord.model_fields)
    assert isinstance(schema.validate_record(control), schema.ColdTickRecord)


def _misbound_tick(run: reader.ColdRetainedRun) -> object:
    tick = typing.cast(schema.ColdTickRecord, container(run, OFF, Stream.TICK).records[0])
    forged = tick.model_copy(update={"identity_sha256": "0" * 64})
    return rebuilt(run, streams=replaced(run, OFF, Stream.TICK, _single(OFF, Stream.TICK, forged)))


def _single(
    phase: schema.ColdPhaseKind, stream: schema.ColdEvidenceStream, record: object
) -> reader.ColdRetainedStream:
    return reader.ColdRetainedStream.model_construct(phase=phase, stream=stream, records=(record,))


@pytest.mark.parametrize(
    "make",
    [forge(_foreign_run_id), forge(_invalid_tick), forge(_misbound_tick)],
    ids=["foreign-run-id", "record-not-valid", "record-digest-unbound"],
)
def test_records_that_do_not_rebind_are_refused(tmp_path: Path, make: Forge) -> None:
    """R2: every record is revalidated and rebound; a failure is closed and chain-free."""
    run, _records = genuine(tmp_path)
    forged = typing.cast(reader.ColdRetainedRun, make(run))
    expect_refusal(Refusal.RECORD_REBIND_FAILED, lambda: acceptance.interpret_retained_run(forged))


def test_refusal_errors_carry_no_input_content(tmp_path: Path) -> None:
    """R2: a canary in a refused run id reaches no error channel."""
    run, _records = genuine(tmp_path)
    with pytest.raises(acceptance.ColdInterpretationError) as raised:
        acceptance.interpret_retained_run(rebuilt(run, run_id="../" + CANARY))
    rendered = f"{raised.value!s}{raised.value!r}{raised.value.args}"
    assert CANARY not in rendered


Relabel = typing.Callable[
    [schema.ColdEvidenceRecord, reader.ColdRetainedRun], schema.ColdEvidenceRecord
]


def _host_for_every_tick(
    snapshot: schema.ColdEvidenceRecord, run: reader.ColdRetainedRun
) -> schema.ColdEvidenceRecord:
    """Replace every tick snapshot with a same-phase, bindable host snapshot (a wrong class)."""
    if type(snapshot) is schema.ColdTickRecord:
        return host_for(next(i.header for i in run.headers if i.header.phase is snapshot.phase))
    return snapshot


def _on_ticks_as_off(
    snapshot: schema.ColdEvidenceRecord, run: reader.ColdRetainedRun
) -> schema.ColdEvidenceRecord:
    """Relabel every recording-on tick snapshot as recording-off (a changed phase)."""
    del run
    if type(snapshot) is schema.ColdTickRecord and snapshot.phase is ON:
        return snapshot.model_copy(update={"phase": OFF})
    return snapshot


@pytest.mark.parametrize(
    "relabel", [_host_for_every_tick, _on_ticks_as_off], ids=["wrong-class", "changed-phase"]
)
def test_step_two_refuses_a_snapshot_relabelled_after_step_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relabel: Relabel
) -> None:
    """R2: a snapshot whose class or phase differs from its checked container is refused.

    Both phases share one identity digest, so the relabelled snapshot would bind;
    only the step-2 guard refuses it.
    """
    run = write_run(tmp_path, {OFF: Phase(), ON: Phase()})
    assert run.headers[0].header.identity_sha256 == run.headers[1].header.identity_sha256
    assert len(acceptance.interpret_retained_run(run).phases) == 2

    def relabelling(record: schema.ColdEvidenceRecord) -> schema.ColdEvidenceRecord:
        return relabel(schema.validate_record(record), run)

    monkeypatch.setattr(acceptance, "validate_record", relabelling)
    expect_refusal(Refusal.RECORD_REBIND_FAILED, lambda: acceptance.interpret_retained_run(run))


def _with_identity(
    run: reader.ColdRetainedRun, index: int, **changes: object
) -> tuple[object, ...]:
    item = run.headers[index]
    changed = reader.ColdRetainedHeader.model_construct(
        header=item.header, identity=item.identity.model_copy(update=changes)
    )
    return tuple(changed if position == index else h for position, h in enumerate(run.headers))


def _with_known(run: reader.ColdRetainedRun, mutate: Mutation) -> tuple[object, ...]:
    known = copy.deepcopy(run.headers[0].identity.known)
    mutate(known)
    return _with_identity(run, 0, known=known)


def _with_header(run: reader.ColdRetainedRun, **changes: object) -> tuple[object, ...]:
    item = run.headers[0]
    changed = reader.ColdRetainedHeader.model_construct(
        header=item.header.model_copy(update=changes), identity=item.identity
    )
    return (changed, *run.headers[1:])


def _renamed_key(document: Json) -> None:
    document["renamed_key"] = document.pop("boot_id")


class SubIdentity(store.ColdRetainedIdentityV1):
    """A subclassed retained identity."""


def _with_subclassed_identity(run: reader.ColdRetainedRun) -> tuple[object, ...]:
    item = run.headers[0]
    equal = SubIdentity.model_construct(**dict(item.identity))
    changed = reader.ColdRetainedHeader.model_construct(header=item.header, identity=equal)
    return (changed, *run.headers[1:])


HEADER_FORGERIES: list[tuple[str, Forge]] = [
    ("dropped", forge(lambda run: rebuilt(run, headers=run.headers[:1]))),
    ("reordered", forge(lambda run: rebuilt(run, headers=tuple(reversed(run.headers))))),
    ("extra", forge(lambda run: rebuilt(run, headers=(*run.headers, run.headers[0])))),
    (
        "header-altered",
        forge(lambda run: rebuilt(run, headers=_with_header(run, monotonic_seconds=9.0))),
    ),
    ("header-invalid", forge(lambda run: rebuilt(run, headers=_with_header(run, run_id="x")))),
    (
        "header-not-a-header",
        forge(
            lambda run: rebuilt(
                run,
                headers=(
                    reader.ColdRetainedHeader.model_construct(
                        header=container(run, OFF, Stream.TICK).records[0],
                        identity=run.headers[0].identity,
                    ),
                    run.headers[1],
                ),
            )
        ),
    ),
    (
        "identity-root-altered",
        forge(
            lambda run: rebuilt(run, headers=_with_identity(run, 1, pi_evidence_root="/elsewhere"))
        ),
    ),
    (
        "identity-extras-altered",
        forge(
            lambda run: rebuilt(run, headers=_with_identity(run, 0, server_info_extras={"k": 1}))
        ),
    ),
    (
        "identity-bool-for-false",
        forge(
            lambda run: rebuilt(
                run, headers=_with_known(run, at("build_provenance.source_tree_dirty", 0))
            )
        ),
    ),
    (
        "identity-int-for-float",
        forge(lambda run: rebuilt(run, headers=_with_known(run, at("controller_tick_seconds", 1)))),
    ),
    (
        "identity-extra-key",
        forge(lambda run: rebuilt(run, headers=_with_known(run, at("unexpected", "x")))),
    ),
    (
        "identity-renamed-key",
        forge(lambda run: rebuilt(run, headers=_with_known(run, _renamed_key))),
    ),
    (
        "identity-list-shortened",
        forge(
            lambda run: rebuilt(
                run, headers=_with_known(run, at("device_config.recording_devices", []))
            )
        ),
    ),
    (
        "identity-missing-field",
        forge(
            lambda run: rebuilt(
                run,
                headers=(
                    reader.ColdRetainedHeader.model_construct(
                        header=run.headers[0].header,
                        identity=store.ColdRetainedIdentityV1.model_construct(run_id=RUN_ID),
                    ),
                    run.headers[1],
                ),
            )
        ),
    ),
    ("headers-without-streams", forge(lambda run: rebuilt(run, streams=()))),
    (
        "identity-run-id-altered",
        forge(
            lambda run: rebuilt(
                run, headers=_with_identity(run, 0, run_id="20260926T120000Z-other")
            )
        ),
    ),
    (
        "identity-runtime-extras-altered",
        forge(
            lambda run: rebuilt(run, headers=_with_identity(run, 0, runtime_config_extras={"k": 1}))
        ),
    ),
    (
        "identity-subclassed-equal",
        forge(lambda run: rebuilt(run, headers=_with_subclassed_identity(run))),
    ),
]


@pytest.mark.parametrize(
    "make", [item[1] for item in HEADER_FORGERIES], ids=[item[0] for item in HEADER_FORGERIES]
)
def test_container_headers_must_equal_the_bound_pairs(tmp_path: Path, make: Forge) -> None:
    """R3: count, order, and exact header and identity values must agree."""
    run, _records = genuine(tmp_path)
    forged = typing.cast(reader.ColdRetainedRun, make(run))
    expect_refusal(Refusal.HEADER_SET_MISMATCHED, lambda: acceptance.interpret_retained_run(forged))


def test_header_agreement_compares_values_not_objects(tmp_path: Path) -> None:
    """R3: equal-valued copies of every container header and identity agree."""
    run, _records = genuine(tmp_path)
    copies = tuple(
        reader.ColdRetainedHeader(
            header=item.header.model_copy(), identity=item.identity.model_copy(deep=True)
        )
        for item in run.headers
    )
    assert len(acceptance.interpret_retained_run(rebuilt(run, headers=copies)).phases) == 2


def _function(name: str) -> ast.FunctionDef:
    """Return one module-level function definition from the module source."""
    return next(
        node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _loads_of(function: ast.FunctionDef, name: str) -> list[ast.Name]:
    """Return every load of one name inside a function."""
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    ]


def test_run_is_read_only_while_rebinding() -> None:
    """R4: ``run`` is passed to rebinding once and only step 1 dereferences that name.

    Later rebinding steps read the containers step 1 captured; nothing else takes a
    run parameter, and evaluation reads only the capability's fresh snapshots.
    """
    for function_name, callee in (
        ("interpret_retained_run", "_rebind"),
        ("_rebind", "_check_containers"),
    ):
        function = _function(function_name)
        loads = _loads_of(function, "run")
        assert len(loads) == 1, function_name
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
            and any(argument is loads[0] for argument in node.args)
        ]
        assert len(calls) == 1, function_name
    readers = {
        node.name
        for node in TREE.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(argument.annotation, ast.Name)
            and argument.annotation.id == "ColdRetainedRun"
            for argument in node.args.args
        )
    }
    assert readers == {"interpret_retained_run", "_rebind", "_check_containers"}


EVALUATORS = (
    "_evaluate_inference_runtime",
    "_evaluate_audio_counters",
    "_evaluate_inference_duration",
    "_evaluate_recording_artefacts",
    "_derive_d191",
    "_series_of",
    "_interpret_phase",
)


@pytest.mark.parametrize("name", EVALUATORS)
def test_private_evaluators_accept_only_the_capability(name: str) -> None:
    """R4 and G16 symmetry: each evaluator takes one rebound phase and no phase kind."""
    signature = inspect.signature(getattr(acceptance, name))
    assert [
        (parameter.name, parameter.annotation) for parameter in signature.parameters.values()
    ] == [("rebound", acceptance.ColdReboundPhase)]


CapabilityType = (
    type[acceptance.ColdReboundRun]
    | type[acceptance.ColdReboundPhase]
    | type[acceptance.ColdInterpretation]
)
CAPABILITY_FIELDS: dict[CapabilityType, tuple[str, ...]] = {
    acceptance.ColdReboundRun: ("run_id", "manifest_sha256", "phases"),
    acceptance.ColdReboundPhase: (
        "phase",
        "header",
        "ticks",
        "hosts",
        "advisories",
        "finalisations",
        "aborts",
        "finalisation",
        "finalisation_ambiguous",
        "_identity",
    ),
    acceptance.ColdInterpretation: ("rebound", "phases"),
}


def _capabilities(tmp_path: Path) -> dict[CapabilityType, object]:
    run, _records = genuine(tmp_path)
    interpretation = acceptance.interpret_retained_run(run)
    return {
        acceptance.ColdReboundRun: interpretation.rebound,
        acceptance.ColdReboundPhase: interpretation.rebound.phases[0],
        acceptance.ColdInterpretation: interpretation,
    }


def test_minted_capabilities_expose_the_contract_attributes(tmp_path: Path) -> None:
    """R5: minted objects expose their fields and hold no instance dictionary."""
    minted = _capabilities(tmp_path)
    for kind, fields in CAPABILITY_FIELDS.items():
        instance = minted[kind]
        assert set(kind.__slots__) == set(fields)
        assert all(hasattr(instance, name) for name in fields)
        assert not hasattr(instance, "__dict__")
    interpretation = typing.cast(
        acceptance.ColdInterpretation, minted[acceptance.ColdInterpretation]
    )
    assert interpretation.rebound is minted[acceptance.ColdReboundRun]
    phase = interpretation.rebound.phases[0]
    assert type(phase.header) is schema.ColdRunHeader
    assert all(type(tick) is schema.ColdTickRecord for tick in phase.ticks)
    assert type(phase.ticks) is tuple and type(interpretation.phases) is tuple


def test_capabilities_refuse_foreign_tokens(tmp_path: Path) -> None:
    """R5: construction without the private token is refused."""
    run, _records = genuine(tmp_path)
    rebound = acceptance.interpret_retained_run(run).rebound
    phase = rebound.phases[0]
    expect_refusal(
        Refusal.CAPABILITY_INVALID,
        lambda: acceptance.ColdReboundRun(
            token=object(), run_id=RUN_ID, manifest_sha256="0" * 64, phases=()
        ),
    )
    expect_refusal(
        Refusal.CAPABILITY_INVALID,
        lambda: acceptance.ColdInterpretation(token=None, rebound=rebound, phases=()),
    )
    expect_refusal(
        Refusal.CAPABILITY_INVALID,
        lambda: acceptance.ColdReboundPhase(
            token=object(),
            phase=OFF,
            header=phase.header,
            ticks=(),
            hosts=(),
            advisories=(),
            finalisations=(),
            aborts=(),
            finalisation=None,
            finalisation_ambiguous=False,
            identity=store.ColdRetainedIdentityV1.model_construct(),
        ),
    )


def test_capabilities_refuse_reinitialisation_assignment_and_deletion(tmp_path: Path) -> None:
    """R5: minted fields never change through ordinary mutation."""
    minted = _capabilities(tmp_path)
    rebound = typing.cast(acceptance.ColdReboundRun, minted[acceptance.ColdReboundRun])
    expect_refusal(
        Refusal.CAPABILITY_INVALID,
        lambda: rebound.__init__(token=TOKEN, run_id="other", manifest_sha256="0" * 64, phases=()),
    )
    assert rebound.run_id == RUN_ID
    for kind, fields in CAPABILITY_FIELDS.items():
        instance = minted[kind]
        for name in (*fields, "unexpected"):
            expect_refusal(
                Refusal.CAPABILITY_INVALID, functools.partial(setattr, instance, name, None)
            )
        for name in fields:
            expect_refusal(Refusal.CAPABILITY_INVALID, functools.partial(delattr, instance, name))
            assert hasattr(instance, name)


def test_truncated_containers_still_interpret(tmp_path: Path) -> None:
    """R6 honest limit: deletion or truncation inside a caller-built run is not detected."""
    run, _records = genuine(tmp_path)
    ticks = container(run, OFF, Stream.TICK)
    shortened = reader.ColdRetainedStream(phase=OFF, stream=Stream.TICK, records=ticks.records[:1])
    truncated = acceptance.interpret_retained_run(
        rebuilt(run, streams=replaced(run, OFF, Stream.TICK, shortened))
    )
    assert len(truncated.rebound.phases[0].ticks) == 1
    dropped = acceptance.interpret_retained_run(
        rebuilt(run, streams=replaced(run, OFF, Stream.TICK, None))
    )
    assert dropped.rebound.phases[0].ticks == ()
    assert failures_of(dropped.phases[0], Check.INFERENCE_RUNTIME)[0] is F.TICK_EVIDENCE_ABSENT
    assert acceptance.__doc__ is not None
    assert "Deleting or truncating" in acceptance.__doc__


# ------------------------------------------------------ Q1-Q11 qualification


def qualification(item: acceptance.ColdPhaseInterpretation) -> acceptance.ColdCheckResult:
    """Return the qualification result of one phase."""
    return item.results[0]


def test_admissible_identity_passes_and_yields_facts(tmp_path: Path) -> None:
    """Q: an admissible identity passes every gate and its facts are copied exactly."""
    item = interpret_phase(tmp_path, OFF)
    assert qualification(item).outcome is Outcome.PASS and qualification(item).failures == ()
    assert item.identity_facts == acceptance.ColdIdentityFacts(
        mcp_version="0.2.2",
        temperature_unit="celsius",
        first_crack_mode="audio",
        model_precision="int8",
        recording_device_count=1,
        source_tree_dirty=False,
        source_revision="b" * 40,
        artefact_kind="wheel",
        artefact_sha256="a" * 64,
        profile_source_sha256="c" * 64,
        profile_source_byte_length=100,
        first_crack_onnx_threads=2,
        first_crack_min_positive_windows=3,
        first_crack_confirmation_window_seconds=30.0,
        audio_sample_rate=16000,
        audio_window_seconds=10.0,
        audio_overlap=0.3,
        audio_hop_seconds=None,
        session_ror_window_seconds=60,
        session_ror_min_sample_seconds=10,
    )


def test_minimum_positive_profile_counts_qualify_as_facts(tmp_path: Path) -> None:
    """Q11: one ONNX thread and one positive window are admissible and copied exactly."""
    minimum: dict[str, object] = {
        "effective_mcp_profile.first_crack_onnx_threads": 1,
        "effective_mcp_profile.first_crack_min_positive_windows": 1,
    }
    item = interpret_phase(tmp_path, OFF, document=applying(minimum))
    assert qualification(item).outcome is Outcome.PASS and qualification(item).failures == ()
    assert item.identity_facts is not None
    assert item.identity_facts.first_crack_onnx_threads == 1
    assert item.identity_facts.first_crack_min_positive_windows == 1


# 32 [A-Za-z0-9] characters: 8 symbols twice and 4 symbols four times, which is
# exactly 3.5 bits in binary floating point (2.0 + 1.5, both exact).
EXACT_LIMIT_TOKEN = "ABCDEFGH" * 2 + "wxyz" * 4


def test_exact_entropy_limit_token_is_refused_by_frozen_and_live_screens(
    tmp_path: Path,
) -> None:
    """Q7: a token scoring exactly 3.5 is refused by the frozen copy and the live screen."""
    text = f"note {EXACT_LIMIT_TOKEN} end"
    frozen_entropy = acceptance._shannon_entropy(EXACT_LIMIT_TOKEN)  # pyright: ignore[reportPrivateUsage]
    live_entropy = live._shannon_entropy(EXACT_LIMIT_TOKEN)  # pyright: ignore[reportPrivateUsage]
    assert frozen_entropy == live_entropy == 3.5
    assert acceptance._CREDENTIAL_SHAPE_PATTERN.search(text) is None  # pyright: ignore[reportPrivateUsage]
    assert not acceptance._operator_text_is_safe(text)  # pyright: ignore[reportPrivateUsage]
    assert not live._operator_text_is_safe(text)  # pyright: ignore[reportPrivateUsage]
    below = "note " + "ABCDEFGH" * 3 + " end"
    assert acceptance._operator_text_is_safe(below)  # pyright: ignore[reportPrivateUsage]
    assert live._operator_text_is_safe(below)  # pyright: ignore[reportPrivateUsage]
    item = interpret_phase(tmp_path, OFF, document=applying({"operator_psu_notes": text}))
    assert qualification(item).failures == (F.Q_OPERATOR_TEXT,)
    assert item.identity_facts is None


def test_editable_source_without_a_digest_qualifies(tmp_path: Path) -> None:
    """Q9: an editable source with a null artefact digest passes; its facts keep both."""
    unpackaged: dict[str, object] = {
        "build_provenance.artefact_kind": "editable_source",
        "build_provenance.artefact_sha256": None,
    }
    item = interpret_phase(tmp_path, OFF, document=applying(unpackaged))
    assert qualification(item).outcome is Outcome.PASS and qualification(item).failures == ()
    assert item.identity_facts is not None
    assert (item.identity_facts.artefact_kind, item.identity_facts.artefact_sha256) == (
        "editable_source",
        None,
    )


@pytest.mark.parametrize(
    ("changes", "fact", "expected"),
    [
        ({"device_config.fc_confidence_threshold": 0.0}, None, None),
        ({"device_config.fc_confidence_threshold": 1.0}, None, None),
        ({"effective_mcp_profile.audio_overlap": 0.0}, "audio_overlap", 0.0),
        ({"effective_mcp_profile.source_byte_length": 0}, "profile_source_byte_length", 0),
    ],
    ids=["threshold-0.0", "threshold-1.0", "overlap-0.0", "source-byte-length-0"],
)
def test_inclusive_q_boundaries_qualify(
    tmp_path: Path, changes: dict[str, object], fact: str | None, expected: object
) -> None:
    """Q8/Q11: each inclusive bound passes, and the facts keep the exact boundary value."""
    item = interpret_phase(tmp_path, OFF, document=applying(changes))
    assert qualification(item).outcome is Outcome.PASS and qualification(item).failures == ()
    assert item.identity_facts is not None
    if fact is not None:
        actual = getattr(item.identity_facts, fact)
        assert (type(actual), actual) == (type(expected), expected)


NOTE_FIELDS = (
    "stimulus_block",
    "operator_host_notes",
    "operator_psu_notes",
    "operator_cooling_notes",
)
UNSAFE_NOTES = {
    "credential-shaped": "calibrated, token: abc123",
    "high-entropy": "note Zq8Xv3Lm9Pw2Rt7Yk4Nb6Hc1Jd5Gf0Ae end",
    "2001-characters": "a" * 2001,
    "non-printable": "tab\there",
}
DEVICE_TEXT_FIELDS = (
    "serial_port",
    "roaster_driver",
    "audio_input_device",
    "mcp_yaml_source_path",
    "ambient_device",
)

QCase = tuple[str, dict[str, object], F]
Q_CASES: list[QCase] = [
    ("q1-0.2.0", {"coffee_roaster_mcp_version": "0.2.0"}, F.Q_MCP_VERSION),
    ("q1-0.2.1", {"coffee_roaster_mcp_version": "0.2.1"}, F.Q_MCP_VERSION),
    ("q1-0.2.20", {"coffee_roaster_mcp_version": "0.2.20"}, F.Q_MCP_VERSION),
    ("q1-leading-space", {"coffee_roaster_mcp_version": " 0.2.2"}, F.Q_MCP_VERSION),
    ("q2-capitalised", {"runtime_config.temperature_unit": "Celsius"}, F.Q_TEMPERATURE_UNIT),
    ("q2-trailing-space", {"runtime_config.temperature_unit": "celsius "}, F.Q_TEMPERATURE_UNIT),
    ("q2-fahrenheit", {"runtime_config.temperature_unit": "fahrenheit"}, F.Q_TEMPERATURE_UNIT),
    ("q2-empty", {"runtime_config.temperature_unit": ""}, F.Q_TEMPERATURE_UNIT),
    ("q3-no-device", {"device_config.recording_devices": []}, F.Q_RECORDING_DEVICE_NOT_SINGLE),
    (
        "q3-two-devices",
        {"device_config.recording_devices": ["a", "b"]},
        F.Q_RECORDING_DEVICE_NOT_SINGLE,
    ),
    ("q3-unset", {"device_config.recording_devices": None}, F.Q_RECORDING_DEVICE_NOT_SINGLE),
    ("q4-disabled", {"runtime_config.first_crack_mode": "disabled"}, F.Q_INFERENCE_NOT_ACTIVE),
    ("q4-manual", {"runtime_config.first_crack_mode": "manual"}, F.Q_INFERENCE_NOT_ACTIVE),
    ("q4-fp32", {"runtime_config.model_precision": "fp32"}, F.Q_INFERENCE_NOT_ACTIVE),
    ("q5-other-credential", {"credential_env_var_name": "OPENAI_API_KEY"}, F.Q_CREDENTIAL_NAME),
    ("q6-uppercase", {"boot_id": "123E4567-E89B-12D3-A456-426614174000"}, F.Q_BOOT_ID),
    ("q6-truncated", {"boot_id": "123e4567-e89b-12d3-a456-42661417400"}, F.Q_BOOT_ID),
    *[
        (f"q7-{field}-{kind}", {field: text}, F.Q_OPERATOR_TEXT)
        for field in NOTE_FIELDS
        for kind, text in UNSAFE_NOTES.items()
    ],
    *[
        (f"q8-credential-{field}", {f"device_config.{field}": "/dev/token=abc"}, F.Q_DEVICE_VALUE)
        for field in DEVICE_TEXT_FIELDS
    ],
    (
        "q8-credential-recording-device",
        {"device_config.recording_devices": ["sk-" + "a" * 20]},
        F.Q_DEVICE_VALUE,
    ),
    ("q8-non-printable-device", {"device_config.serial_port": "/dev/tty\x00"}, F.Q_DEVICE_VALUE),
    ("q8-513-character-device", {"device_config.ambient_device": "d" * 513}, F.Q_DEVICE_VALUE),
    ("q8-threshold-1.1", {"device_config.fc_confidence_threshold": 1.1}, F.Q_DEVICE_VALUE),
    ("q8-threshold-negative", {"device_config.fc_confidence_threshold": -0.1}, F.Q_DEVICE_VALUE),
    ("q8-zero-auto-t0", {"device_config.auto_t0_drop_threshold_c": 0.0}, F.Q_DEVICE_VALUE),
    ("q8-zero-poll", {"device_config.ambient_poll_interval_seconds": 0.0}, F.Q_DEVICE_VALUE),
    ("q9-wheel-without-digest", {"build_provenance.artefact_sha256": None}, F.Q_PROVENANCE_DIGEST),
    (
        "q9-sdist-without-digest",
        {"build_provenance.artefact_kind": "sdist", "build_provenance.artefact_sha256": None},
        F.Q_PROVENANCE_DIGEST,
    ),
    (
        "q9-editable-with-digest",
        {"build_provenance.artefact_kind": "editable_source"},
        F.Q_PROVENANCE_DIGEST,
    ),
    ("q10-dirty-source", {"build_provenance.source_tree_dirty": True}, F.Q_SOURCE_TREE_DIRTY),
    *[
        (f"q11-{label}", {f"effective_mcp_profile.{key}": value}, F.Q_PROFILE_VALUE)
        for label, key, value in (
            ("overlap-1.0", "audio_overlap", 1.0),
            ("overlap-negative", "audio_overlap", -0.1),
            ("zero-window", "audio_window_seconds", 0.0),
            ("zero-threads", "first_crack_onnx_threads", 0),
            ("zero-hop", "audio_hop_seconds", 0.0),
            ("zero-min-windows", "first_crack_min_positive_windows", 0),
            ("zero-confirmation", "first_crack_confirmation_window_seconds", 0.0),
            ("zero-sample-rate", "audio_sample_rate", 0),
            ("negative-byte-length", "source_byte_length", -1),
            ("zero-ror-window", "session_ror_window_seconds", 0),
            ("zero-ror-sample", "session_ror_min_sample_seconds", 0),
            ("sk-revision", "first_crack_revision", "sk-AbCdEfGhIjKlMnOpQr"),
            ("ghp-revision", "first_crack_revision", "ghp_" + "a1B2c3D4e5F6g7H8i9J0"),
            ("129-character-revision", "first_crack_revision", "r" * 129),
            ("revision-grammar", "first_crack_revision", "rev/1"),
            ("empty-revision", "first_crack_revision", ""),
        )
    ],
]


def applying(changes: dict[str, object]) -> Mutation:
    """Return one mutation applying every dotted-path change."""
    return mutations(*(at(path, value) for path, value in changes.items()))


@pytest.mark.parametrize(
    ("changes", "failure"),
    [case[1:] for case in Q_CASES],
    ids=[case[0] for case in Q_CASES],
)
def test_each_frozen_gate_fails_closed(
    tmp_path: Path, changes: dict[str, object], failure: F
) -> None:
    """Q1-Q11: each inadmissible retained value fails exactly its gate; no facts."""
    item = interpret_phase(tmp_path, OFF, document=applying(changes))
    assert qualification(item).outcome is Outcome.FAIL
    assert qualification(item).failures == (failure,)
    assert item.identity_facts is None


def test_retained_v1_identity_with_mcp_021_is_losslessly_read_then_fails_q1(
    tmp_path: Path,
) -> None:
    """T-C13: historical v1 identity bytes remain readable but cannot qualify under v2."""
    document = identity_document(tmp_path)
    document["coffee_roaster_mcp_version"] = "0.2.1"
    envelope = envelope_of(document)
    retained = store.read_identity_v1(envelope)
    assert retained.known["coffee_roaster_mcp_version"] == "0.2.1"
    assert envelope.canonical_json == store.canonical_json(document)

    item = interpret_phase(
        tmp_path,
        OFF,
        document=applying({"coffee_roaster_mcp_version": "0.2.1"}),
    )
    assert qualification(item).outcome is Outcome.FAIL
    assert qualification(item).failures == (F.Q_MCP_VERSION,)


def test_every_gate_row_has_a_negative_case() -> None:
    """Q: every gate row, and every Q failure, is exercised by a failing case."""
    rows = acceptance._Q_RULES  # pyright: ignore[reportPrivateUsage]
    assert len(rows) == 32
    row_paths = {
        (f"{section}.{key}" if section else key, failure) for section, key, failure, _ in rows
    }
    case_paths = {(path, case[2]) for case in Q_CASES for path in case[1]}
    assert row_paths <= case_paths
    qualification_failures = {member for member in F if member.name[:2] == "Q_"}
    assert {case[2] for case in Q_CASES} == qualification_failures - {F.Q_SHAPE_UNEXPECTED}
    assert len({case[0] for case in Q_CASES}) == len(Q_CASES)


def test_q2_never_converts_or_modifies_the_retained_identity(tmp_path: Path) -> None:
    """Q2: a non-Celsius unit fails closed and the retained bytes are unchanged."""
    run = write_run(
        tmp_path, {OFF: Phase(document=at("runtime_config.temperature_unit", "Celsius"))}
    )
    header_file = run_dir(str(tmp_path.resolve() / "pi")) / "records/recording_off/header.jsonl"
    before_file = header_file.read_bytes()
    before_known = copy.deepcopy(run.headers[0].identity.known)
    before_envelope = run.headers[0].header.identity.canonical_json
    item = acceptance.interpret_retained_run(run).phases[0]
    assert qualification(item).failures == (F.Q_TEMPERATURE_UNIT,)
    assert run.headers[0].identity.known == before_known
    retained_runtime = typing.cast(Json, run.headers[0].identity.known["runtime_config"])
    assert retained_runtime["temperature_unit"] == "Celsius"
    assert run.headers[0].header.identity.canonical_json == before_envelope
    assert header_file.read_bytes() == before_file


def test_revision_and_device_values_are_never_score_screened(tmp_path: Path) -> None:
    """Q trap: a pinned 40-hex revision and a long real serial path pass Q8 and Q11."""
    item = interpret_phase(
        tmp_path,
        OFF,
        document=mutations(
            at("effective_mcp_profile.first_crack_revision", PINNED_MODEL_REVISION),
            at("device_config.serial_port", LONG_SERIAL_PATH),
            at("device_config.recording_devices", [LONG_SERIAL_PATH]),
        ),
    )
    assert qualification(item).outcome is Outcome.PASS
    operator_screen = acceptance._operator_text_is_safe  # pyright: ignore[reportPrivateUsage]
    assert operator_screen(PINNED_MODEL_REVISION) is False
    assert operator_screen(LONG_SERIAL_PATH) is False


def _identity_with(mutate: Mutation, tmp_path: Path) -> store.ColdRetainedIdentityV1:
    """Construct an unvalidated retained identity around a mutated admissible document."""
    document = identity_document(tmp_path)
    known = store.read_identity_v1(envelope_of(document)).known
    changed = copy.deepcopy(known)
    mutate(changed)
    return store.ColdRetainedIdentityV1.model_construct(
        run_id=RUN_ID,
        pi_evidence_root="/synthetic/pi",
        known=changed,
        runtime_config_extras={},
        server_info_extras={},
    )


def _delete(path: str) -> Mutation:
    *parents, leaf = path.split(".")

    def mutate(document: Json) -> None:
        target = document
        for key in parents:
            target = target[key]
        del target[leaf]

    return mutate


SHAPE_CASES: list[tuple[str, Mutation]] = [
    ("missing-top-level-key", _delete("boot_id")),
    ("missing-section", _delete("effective_mcp_profile")),
    ("section-not-a-dict", at("runtime_config", ["celsius"])),
    ("bool-for-int", at("effective_mcp_profile.source_byte_length", True)),
    ("int-for-float", at("controller_tick_seconds", 1)),
    ("infinite-runtime-float", at("runtime_config.command_interval_seconds", float("inf"))),
    ("nan-runtime-float", at("runtime_config.sample_interval_seconds", float("nan"))),
    ("infinite-auto-t0", at("runtime_config.auto_t0_drop_threshold_c", float("-inf"))),
    ("text-for-bool", at("build_provenance.source_tree_dirty", "false")),
    ("device-list-item", at("device_config.recording_devices", [1])),
    ("device-tuple", at("device_config.recording_devices", ("mic",))),
    ("revision-hex-uppercase", at("build_provenance.source_revision", "B" * 40)),
    ("unknown-artefact-kind", at("build_provenance.artefact_kind", "zip")),
    ("digest-short", at("build_provenance.artefact_sha256", "a" * 63)),
    ("int-profile-float", at("effective_mcp_profile.audio_overlap", 0)),
]


@pytest.mark.parametrize(
    "mutate", [case[1] for case in SHAPE_CASES], ids=[case[0] for case in SHAPE_CASES]
)
def test_unexpected_shapes_fail_closed_without_raising(tmp_path: Path, mutate: Mutation) -> None:
    """Q shape: exact-type accessors record ``Q_SHAPE_UNEXPECTED`` and never raise."""
    result, facts = qualify(_identity_with(mutate, tmp_path))
    assert result.outcome is Outcome.FAIL
    assert result.failures == (F.Q_SHAPE_UNEXPECTED,)
    assert facts is None


@pytest.mark.parametrize(
    "identity",
    [
        store.ColdRetainedIdentityV1.model_construct(),
        store.ColdRetainedIdentityV1.model_construct(known=[]),
        store.ColdRetainedIdentityV1.model_construct(known={}),
    ],
    ids=["no-known", "known-not-a-dict", "known-empty"],
)
def test_a_missing_identity_body_is_one_shape_failure(
    identity: store.ColdRetainedIdentityV1,
) -> None:
    """Q shape: with nothing readable, only the shape failure is recorded."""
    assert qualify(identity)[0].failures == (F.Q_SHAPE_UNEXPECTED,)


def test_d4_packaged_constants_and_extras_change_nothing(tmp_path: Path) -> None:
    """Q boundary: history, extras, and a shadow unit key never alter qualification."""

    def historical(document: Json) -> None:
        document["agent_version"] = "0.0.1-historical"
        document["model_revision"] = "0" * 40
        document["model_repo_id"] = "other/repo"
        document["model_manifest"] = [{"relative_path": "x.onnx", "sha256": "d" * 64}]
        document["runtime_config"][CANARY] = {"nested": [CANARY]}
        document["server_info"]["temperature_unit"] = "fahrenheit"

    run = write_run(tmp_path, {OFF: Phase(document=historical)})
    assert run.headers[0].identity.server_info_extras == {"temperature_unit": "fahrenheit"}
    item = acceptance.interpret_retained_run(run).phases[0]
    assert qualification(item).outcome is Outcome.PASS
    assert CANARY not in item.model_dump_json()


def test_d4_live_constants_are_never_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q boundary: monkeypatched live constants change no qualification result."""
    run = write_run(tmp_path, {OFF: Phase()})
    expected = acceptance.interpret_retained_run(run).phases[0]
    monkeypatch.setattr(live, "REQUIRED_MCP_VERSION", "9.9.9")
    monkeypatch.setattr(live, "_ALLOWED_CELSIUS_TOKENS", frozenset({"fahrenheit"}))
    monkeypatch.setattr(live, "_ALLOWED_INFERENCE_MODES", frozenset({"manual"}))
    monkeypatch.setattr(live, "_ALLOWED_CREDENTIAL_ENV_NAMES", frozenset({"OTHER"}))
    monkeypatch.setattr(live, "__version__", "9.9.9")
    monkeypatch.setattr(live, "REVISION", "f" * 40)
    monkeypatch.setattr(live, "REPO_ID", "other/repo")
    assert acceptance.interpret_retained_run(run).phases[0] == expected


def _identifiers(tree: ast.AST) -> set[str]:
    """Return every name, attribute, and imported alias in a syntax tree."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


def test_d4_module_never_imports_identity_or_packaged_constants() -> None:
    """Q boundary: no live identity import and no packaged-constant name."""
    imported = {node.module for node in ast.walk(TREE) if isinstance(node, ast.ImportFrom)} | {
        alias.name
        for node in ast.walk(TREE)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(module is not None and module.endswith(".identity") for module in imported)
    assert _identifiers(TREE) & {"__version__", "REPO_ID", "REVISION", "MANIFEST_FILES"} == set()


def test_frozen_literals_equal_their_live_counterparts() -> None:
    """Parity: each frozen literal equals its live counterpart; drift forces a v2 policy."""
    frozen = typing.cast(typing.Any, acceptance)
    current = typing.cast(typing.Any, live)
    assert frozen._REQUIRED_MCP_VERSION == current.REQUIRED_MCP_VERSION
    for name in (
        "_ALLOWED_CELSIUS_TOKENS",
        "_ALLOWED_INFERENCE_MODES",
        "_ALLOWED_CREDENTIAL_ENV_NAMES",
    ):
        assert getattr(frozen, name) == getattr(current, name), name
    for name in (
        "_BOOT_ID_PATTERN",
        "_PRINTABLE_TEXT_PATTERN",
        "_CREDENTIAL_SHAPE_PATTERN",
        "_REVISION_SECRET_SHAPE_PATTERN",
        "_HIGH_ENTROPY_TOKEN_PATTERN",
    ):
        mine, theirs = getattr(frozen, name), getattr(current, name)
        assert (mine.pattern, mine.flags) == (theirs.pattern, theirs.flags), name
    assert frozen._MAX_OPERATOR_TEXT_LENGTH == current._MAX_OPERATOR_TEXT_LENGTH
    assert acceptance.QUALIFICATION_POLICY_VERSION == 2


SCREEN_CORPUS = (
    "",
    "tap",
    "Room 21 C, lid closed.\nFan at rest.",
    "a" * 128,
    "a" * 129,
    "a" * 512,
    "a" * 513,
    "a" * 2000,
    "a" * 2001,
    "tab\there",
    "bell\x07",
    "café",
    "sk-" + "A1b2C3d4E5f6G7h8",
    "API_KEY = hunter2",
    "password:  x",
    "token=abc",
    "secret : shh",
    "Zq8Xv3Lm9Pw2Rt7Yk4Nb6Hc1Jd5Gf0Ae",
    "a" * 30,
    "abab" * 8,
    PINNED_MODEL_REVISION,
    LONG_SERIAL_PATH,
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0",
    "glpat-" + "a1B2c3D4e5F6g7H8i9J0",
    "xoxb-" + "a1B2c3D4e5F6g7H8i9J0",
    "AKIA" + "ABCDEFGHIJ012345",
    "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl",
    "v1.2.3-rc_4",
    "rev/1",
    "/dev/ttyUSB0",
    "aabcdefghijkaabcdefghijk",
    "abcdefghijklabcdefghijkl",
)
Q7_BELOW_LIMIT = "aabcdefghijkaabcdefghijk"
Q7_ABOVE_LIMIT = "abcdefghijklabcdefghijkl"


def _live_profile_admits(revision: str) -> bool:
    """Whether the live effective-profile model admits one revision."""
    try:
        live.EffectiveMCPProfile(
            source_sha256="c" * 64,
            source_byte_length=100,
            first_crack_onnx_threads=2,
            first_crack_min_positive_windows=3,
            first_crack_confirmation_window_seconds=30.0,
            first_crack_revision=revision,
            audio_sample_rate=16000,
            audio_window_seconds=10.0,
            audio_overlap=0.3,
            audio_hop_seconds=None,
            session_ror_window_seconds=60,
            session_ror_min_sample_seconds=10,
        )
    except (pydantic.ValidationError, live.ColdIdentityError):
        return False
    return True


def test_frozen_screens_agree_with_the_live_screens() -> None:
    """Parity: the text, device, and revision screens agree over a fixed corpus."""
    frozen = typing.cast(typing.Any, acceptance)
    current = typing.cast(typing.Any, live)
    pairs = (
        (frozen._operator_text_is_safe, current._operator_text_is_safe),
        (frozen._device_text_is_safe, current._managed_device_text_is_safe),
        (frozen._revision_is_admissible, _live_profile_admits),
    )
    for mine, theirs in pairs:
        outcomes = [mine(text) for text in SCREEN_CORPUS]
        assert outcomes == [theirs(text) for text in SCREEN_CORPUS]
        assert set(outcomes) == {True, False}


def _independent_token_score(token: str) -> float:
    """Score one token's character entropy independently of both screens under test."""
    counts = collections.Counter(token)
    return -sum((count / len(token)) * math.log2(count / len(token)) for count in counts.values())


def test_q7_token_score_boundary_outcomes_are_exact() -> None:
    """Q7: 24-character tokens either side of 3.5 are admitted and refused by both screens."""
    frozen = typing.cast(typing.Any, acceptance)
    current = typing.cast(typing.Any, live)
    assert frozen._ENTROPY_LIMIT == 3.5
    assert [len(Q7_BELOW_LIMIT), len(Q7_ABOVE_LIMIT)] == [24, 24]
    assert round(_independent_token_score(Q7_BELOW_LIMIT), 3) == 3.418
    assert round(_independent_token_score(Q7_ABOVE_LIMIT), 3) == 3.585
    for screen in (frozen._operator_text_is_safe, current._operator_text_is_safe):
        assert screen(Q7_BELOW_LIMIT) is True
        assert screen(Q7_ABOVE_LIMIT) is False


def _inline_token_score_limits(source: str) -> list[object]:
    """Return each constant ``LIMIT`` in a ``_shannon_entropy(...) < LIMIT`` comparison."""
    return [
        node.comparators[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "_shannon_entropy"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Lt)
        and isinstance(node.comparators[0], ast.Constant)
    ]


def test_live_inline_token_score_limit_is_the_frozen_limit() -> None:
    """Q7 parity: the live screen's inline limit is pinned to the frozen one; drift is caught.

    The drift checks run on bounded in-memory copies of the live function's text; the
    live module itself is only read.
    """
    source = inspect.getsource(live._operator_text_is_safe)  # pyright: ignore[reportPrivateUsage]
    frozen_limit = acceptance._ENTROPY_LIMIT  # pyright: ignore[reportPrivateUsage]
    assert _inline_token_score_limits(source) == [frozen_limit] == [3.5]
    assert source.count("< 3.5") == 1
    for drifted in (3.4, 3.6):
        text = source.replace("< 3.5", f"< {drifted}")
        assert _inline_token_score_limits(text) == [drifted] != [frozen_limit]


# --------------------------------------------------------------- G16 runtime


def both_phases(
    ticks: tuple[Json, ...] | None = None, **changes: object
) -> dict[schema.ColdPhaseKind, Phase]:
    """Two phases with the same tick series and per-phase results changed alike."""
    return {
        phase: Phase(ticks=ticks, results=(result_for(phase, **changes),)) for phase in (OFF, ON)
    }


def test_active_pending_evidence_passes_every_check_in_both_phases(tmp_path: Path) -> None:
    """G16-G18: clean active evidence passes in both phases, with metrics and facts."""
    run = write_run(tmp_path, {OFF: Phase(extras=True), ON: Phase(extras=True)})
    for item in acceptance.interpret_retained_run(run).phases:
        assert [(r.check, r.outcome, r.failures) for r in item.results] == [
            (check, Outcome.PASS, ()) for check in Check
        ]
        assert item.d191 == acceptance.ColdD191Metrics(
            max_consecutive_overflow_count=0, peak_trailing_lost_audio_ms=0.0
        )
        assert item.identity_facts is not None


def test_the_committed_disabled_fixture_is_never_active_in_either_phase(tmp_path: Path) -> None:
    """G16: the bare-harness disabled fixture fails the runtime check in each phase."""
    run = write_run(
        tmp_path,
        {phase: Phase(results=(finalisation_payload(),)) for phase in (OFF, ON)},
    )
    for item in acceptance.interpret_retained_run(run).phases:
        assert failures_of(item, Check.INFERENCE_RUNTIME) == (
            F.INFERENCE_NOT_ACTIVE,
            F.NO_PROCESSED_WINDOW,
            F.MICROPHONE_OR_FATAL_ERROR,
        )


def _runtime_failures(
    tmp_path: Path, phases: dict[schema.ColdPhaseKind, Phase]
) -> set[tuple[F, ...]]:
    """Interpret both phases and return the distinct runtime-failure tuples."""
    run = write_run(tmp_path, phases)
    return {
        failures_of(item, Check.INFERENCE_RUNTIME)
        for item in acceptance.interpret_retained_run(run).phases
    }


@pytest.mark.parametrize(
    ("status_value", "failure"),
    [
        ("detected", F.FIRST_CRACK_CONFIRMED),
        ("faulted", F.MICROPHONE_OR_FATAL_ERROR),
        ("unavailable", F.MICROPHONE_OR_FATAL_ERROR),
        ("disabled", F.INFERENCE_NOT_ACTIVE),
        ("manual", F.INFERENCE_NOT_ACTIVE),
    ],
)
def test_each_live_status_maps_to_its_exact_failure(
    tmp_path: Path, status_value: str, failure: F
) -> None:
    """G16: every non-pending live status maps to one member, symmetrically."""
    ticks = (
        *default_ticks()[:2],
        tick_audio(status=status_value, emitted_window_count=3, processed_window_count=3),
    )
    assert _runtime_failures(tmp_path, both_phases(ticks)) == {(failure,)}


RUNTIME_CASES: list[tuple[str, tuple[Json, ...] | None, dict[str, object], tuple[F, ...]]] = [
    (
        "tick-detected-at",
        (tick_audio(detected_at_utc="2026-09-26T12:00:05Z"),),
        {},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "tick-detected-monotonic",
        (tick_audio(detected_monotonic_seconds=5.0),),
        {},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "first-tick-not-running",
        (tick_audio(audio_running=False), *default_ticks()[1:]),
        {},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "middle-tick-not-running",
        (
            default_ticks()[0],
            tick_audio(audio_running=False, emitted_window_count=2, processed_window_count=2),
            default_ticks()[2],
        ),
        {},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "last-tick-not-running",
        (
            *default_ticks()[:2],
            tick_audio(audio_running=False, emitted_window_count=3, processed_window_count=3),
        ),
        {},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    ("tick-mode-manual", (tick_audio(mode="manual"),), {}, (F.INFERENCE_NOT_ACTIVE,)),
    (
        "tick-live-reason",
        (tick_audio(reason="stream overflowed"),),
        {},
        (F.MICROPHONE_OR_FATAL_ERROR,),
    ),
    (
        "pre-not-running",
        None,
        {"pre_finalisation_first_crack_status": status(audio_running=False)},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "pre-live-reason",
        None,
        {"pre_finalisation_first_crack_status": status(reason="device lost")},
        (F.MICROPHONE_OR_FATAL_ERROR,),
    ),
    (
        "pre-detected",
        None,
        {"pre_finalisation_first_crack_status": status(status="detected")},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "pre-detection-field",
        None,
        {"pre_finalisation_first_crack_status": status(detected_monotonic_seconds=5.0)},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "pre-absent",
        None,
        {"pre_finalisation_first_crack_status": None},
        (F.PRE_FINALISATION_EVIDENCE_ABSENT,),
    ),
    (
        "pre-processed-zero",
        None,
        {
            "pre_finalisation_first_crack_status": status(
                processed_window_count=0, emitted_window_count=3
            )
        },
        (F.NO_PROCESSED_WINDOW,),
    ),
    (
        "outcome-not-active",
        None,
        {"first_crack_runtime": runtime(outcome="not_active")},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "final-mode-disabled",
        None,
        {"first_crack_runtime": runtime(final_status(mode="disabled"))},
        (F.INFERENCE_NOT_ACTIVE,),
    ),
    (
        "final-detected",
        None,
        {"first_crack_runtime": runtime(final_status(status="detected"))},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "final-detection-field",
        None,
        {"first_crack_runtime": runtime(final_status(detected_at_utc="2026-09-26T12:09:00Z"))},
        (F.FIRST_CRACK_CONFIRMED,),
    ),
    (
        "final-audio-running",
        None,
        {"first_crack_runtime": runtime(final_status(audio_running=True))},
        (F.POST_STOP_AUDIO_RUNNING,),
    ),
    ("runtime-absent", None, {"first_crack_runtime": None}, (F.FINALISATION_EVIDENCE_ABSENT,)),
    ("no-ticks", (), {}, (F.TICK_EVIDENCE_ABSENT,)),
]


@pytest.mark.parametrize(
    ("ticks", "changes", "expected"),
    [case[1:] for case in RUNTIME_CASES],
    ids=[case[0] for case in RUNTIME_CASES],
)
def test_runtime_failures_are_phase_symmetric(
    tmp_path: Path,
    ticks: tuple[Json, ...] | None,
    changes: dict[str, object],
    expected: tuple[F, ...],
) -> None:
    """G16: each runtime rule fails identically in recording-off and recording-on."""
    assert _runtime_failures(tmp_path, both_phases(ticks, **changes)) == {expected}


def test_one_processed_window_is_enough(tmp_path: Path) -> None:
    """G16: pre-finalisation processed ``1`` passes; there is no duration-derived floor."""
    pre = status(processed_window_count=1, emitted_window_count=3)
    assert _runtime_failures(tmp_path, both_phases(pre_finalisation_first_crack_status=pre)) == {()}


@pytest.mark.parametrize(
    ("check", "pre", "expected"),
    [
        (
            Check.INFERENCE_RUNTIME,
            status(audio_running=False),
            (F.TICK_EVIDENCE_ABSENT, F.INFERENCE_NOT_ACTIVE),
        ),
        (
            Check.AUDIO_COUNTERS,
            status(dropped_window_count=1),
            (F.TICK_EVIDENCE_ABSENT, F.DROPPED_WINDOW),
        ),
    ],
    ids=["runtime-pre-not-running", "counters-pre-dropped"],
)
def test_missing_ticks_never_hide_failures_in_present_evidence(
    tmp_path: Path, check: acceptance.ColdCheck, pre: Json, expected: tuple[F, ...]
) -> None:
    """G16: a presence failure never short-circuits the rules over the evidence present."""
    run = write_run(tmp_path, both_phases((), pre_finalisation_first_crack_status=pre))
    phases = acceptance.interpret_retained_run(run).phases
    assert {failures_of(item, check) for item in phases} == {expected}


@pytest.mark.parametrize(
    "stop",
    [
        runtime(outcome="stop_failed", stop_error="join timed out"),
        runtime(outcome="capture_still_running", capture_running_after_stop=True),
        runtime(final_status(status="faulted", reason="stream closed after stop")),
    ],
    ids=["stop-failed", "capture-still-running", "final-faulted-with-reason"],
)
def test_post_stop_cleanliness_is_not_judged_here(tmp_path: Path, stop: Json) -> None:
    """G16 boundary: stop cleanliness and post-stop status or reason belong to G5."""
    assert _runtime_failures(tmp_path, both_phases(first_crack_runtime=stop)) == {()}


def test_missing_or_ambiguous_finalisation_is_a_presence_failure(tmp_path: Path) -> None:
    """G16: no finalisation, or differing session ids, never pass."""
    assert _runtime_failures(tmp_path, {phase: Phase(results=()) for phase in (OFF, ON)}) == {
        (F.FINALISATION_EVIDENCE_ABSENT,)
    }
    ambiguous = {
        phase: Phase(results=(result_for(phase), result_for(phase, session_id="other-session")))
        for phase in (OFF, ON)
    }
    item = acceptance.interpret_retained_run(write_run(tmp_path, ambiguous, name="two")).phases[1]
    for check in (
        Check.INFERENCE_RUNTIME,
        Check.AUDIO_COUNTERS,
        Check.INFERENCE_DURATION,
        Check.RECORDING_ARTEFACTS,
    ):
        assert failures_of(item, check) == (F.FINALISATION_SESSION_AMBIGUOUS,)
    assert item.d191 is None


def test_absent_finalisation_is_only_absence_in_both_phases(tmp_path: Path) -> None:
    """Presence: with no finalisation record, each evidence check reports only absence.

    In particular, G18 never reads absent evidence as a not-configured recording.
    """
    run = write_run(tmp_path, {phase: Phase(results=()) for phase in (OFF, ON)})
    phases = acceptance.interpret_retained_run(run).phases
    assert [item.phase for item in phases] == [OFF, ON]
    for item in phases:
        assert [failures_of(item, check) for check in tuple(Check)[1:]] == [
            (F.FINALISATION_EVIDENCE_ABSENT,)
        ] * 4
        assert item.d191 is None


def test_same_session_retries_are_decided_by_the_last_record(tmp_path: Path) -> None:
    """Selection: the last same-session finalisation record in file order decides."""
    failing = result_for(OFF, pre_finalisation_first_crack_status=status(processed_window_count=0))
    passing = result_for(OFF)
    last_passes = write_run(tmp_path, {OFF: Phase(results=(failing, passing))})
    phase = acceptance.interpret_retained_run(last_passes).rebound.phases[0]
    assert len(phase.finalisations) == 2 and phase.finalisation_ambiguous is False
    assert (
        failures_of(
            acceptance.interpret_retained_run(last_passes).phases[0], Check.INFERENCE_RUNTIME
        )
        == ()
    )
    last_fails = write_run(tmp_path, {OFF: Phase(results=(passing, failing))}, name="two")
    item = acceptance.interpret_retained_run(last_fails).phases[0]
    assert failures_of(item, Check.INFERENCE_RUNTIME) == (F.NO_PROCESSED_WINDOW,)


def test_runtime_rules_read_no_phase() -> None:
    """G16 symmetry: only the recording-artefacts check reads the phase."""
    readers = {
        node.name
        for node in TREE.body
        if isinstance(node, ast.FunctionDef)
        and any(
            (isinstance(child, ast.Attribute) and child.attr == "phase")
            or (isinstance(child, ast.Name) and child.id == "ColdPhaseKind")
            for child in ast.walk(node)
        )
    }
    assert readers == {
        "_check_containers",
        "_bind_records",
        "_mint_phase",
        "_rebind",
        "_evaluate_recording_artefacts",
        "_interpret_phase",
    }


# --------------------------------------------------------------- counters (B5)

READINGS = (
    "queued_window_count",
    "emitted_window_count",
    "dropped_window_count",
    "processed_window_count",
    "total_overflow_count",
    "max_consecutive_overflow_count",
    "inference_overrun_count",
    "estimated_lost_audio_ms_last_minute",
    "max_inference_duration_ms",
)
MONOTONIC = (
    "total_overflow_count",
    "emitted_window_count",
    "processed_window_count",
    "max_inference_duration_ms",
    "max_consecutive_overflow_count",
)


def _counter_failures(
    tmp_path: Path, ticks: tuple[Json, ...] | None = None, **changes: object
) -> tuple[F, ...]:
    """Interpret one recording-off phase and return its counter failures."""
    item = interpret_phase(tmp_path, OFF, ticks=ticks, results=(result_for(OFF, **changes),))
    return failures_of(item, Check.AUDIO_COUNTERS)


def _high(field: str) -> object:
    """Return a value above every baseline value of one monotonic field."""
    return 50.0 if field == "max_inference_duration_ms" else 50


def _ticks_with(position: int, **overrides: object) -> tuple[Json, ...]:
    """Return the default ticks with one tick changed."""
    ticks = list(default_ticks())
    ticks[position] = {**ticks[position], **overrides}
    return tuple(ticks)


def test_a_clean_series_passes_and_negative_dbfs_is_not_read(tmp_path: Path) -> None:
    """Counters: clean evidence passes; legitimately negative dBFS is never a measurement."""
    ticks = tuple(
        {**tick, "mic_peak_dbfs": -90.0, "mic_rms_dbfs": -96.0} for tick in default_ticks()
    )
    assert _counter_failures(tmp_path, ticks) == ()


@pytest.mark.parametrize("where", ["tick", "pre", "final"])
@pytest.mark.parametrize(
    ("field", "failure"),
    [("dropped_window_count", F.DROPPED_WINDOW), ("inference_overrun_count", F.INFERENCE_OVERRUN)],
)
def test_dropped_windows_and_overruns_fail_in_every_element(
    tmp_path: Path, where: str, field: str, failure: F
) -> None:
    """Counters: one dropped window or overrun in a tick or either snapshot fails."""
    if where == "tick":
        assert _counter_failures(tmp_path, _ticks_with(1, **{field: 1})) == (failure,)
    elif where == "pre":
        assert _counter_failures(
            tmp_path, pre_finalisation_first_crack_status=status(**{field: 1})
        ) == (failure,)
    else:
        assert _counter_failures(
            tmp_path, first_crack_runtime=runtime(final_status(**{field: 1}))
        ) == (failure,)


@pytest.mark.parametrize("transition", ["tick-to-tick", "tick-to-pre", "pre-to-final"])
@pytest.mark.parametrize("field", MONOTONIC)
def test_a_decrease_in_any_lifetime_counter_is_a_capture_restart(
    tmp_path: Path, field: str, transition: str
) -> None:
    """Counters: any decrease across consecutive elements of ``S`` is a restart."""
    if transition == "tick-to-tick":
        failures = _counter_failures(tmp_path, _ticks_with(0, **{field: _high(field)}))
    elif transition == "tick-to-pre":
        failures = _counter_failures(tmp_path, _ticks_with(2, **{field: _high(field)}))
    else:
        failures = _counter_failures(
            tmp_path, pre_finalisation_first_crack_status=status(**{field: _high(field)})
        )
    assert failures == (F.CAPTURE_RESTART,)


@pytest.mark.parametrize(
    ("queued", "expected"),
    [((0, 1, 2), (F.QUEUE_GROWING,)), ((0, 1, 0, 1), ()), ((3, 3, 3), ()), ((0, 2, 1, 3), ())],
    ids=["0-1-2", "0-1-0-1", "flat", "non-consecutive-highs"],
)
def test_queue_growth_needs_consecutive_new_highs(
    tmp_path: Path, queued: tuple[int, ...], expected: tuple[F, ...]
) -> None:
    """Counters: queued above its running high-water mark twice in a row is growth."""
    ticks = tuple(
        tick_audio(
            queued_window_count=value,
            emitted_window_count=index + 1,
            processed_window_count=index + 1,
        )
        for index, value in enumerate(queued)
    )
    assert _counter_failures(tmp_path, ticks) == expected


def test_queue_growth_reads_the_pre_finalisation_snapshot(tmp_path: Path) -> None:
    """Counters: ``L`` includes the pre-finalisation snapshot."""
    ticks = tuple(
        tick_audio(
            queued_window_count=value,
            emitted_window_count=value + 1,
            processed_window_count=value + 1,
        )
        for value in (0, 0, 1)
    )
    pre = status(queued_window_count=2)
    assert _counter_failures(tmp_path, ticks, pre_finalisation_first_crack_status=pre) == (
        F.QUEUE_GROWING,
    )


@pytest.mark.parametrize(("queued", "expected"), [(0, ()), (1, (F.QUEUE_NOT_DRAINED,))])
def test_the_final_snapshot_must_be_drained(
    tmp_path: Path, queued: int, expected: tuple[F, ...]
) -> None:
    """Counters (C1): the final snapshot requires zero queued windows."""
    stop = runtime(final_status(queued_window_count=queued))
    pre = status(queued_window_count=1)
    assert (
        _counter_failures(
            tmp_path, first_crack_runtime=stop, pre_finalisation_first_crack_status=pre
        )
        == expected
    )


@pytest.mark.parametrize("field", READINGS)
def test_every_read_field_rejects_a_negative_value(tmp_path: Path, field: str) -> None:
    """Counters: a negative reading is never read as clean or as zero."""
    value: object = (
        -1.0
        if field in ("estimated_lost_audio_ms_last_minute", "max_inference_duration_ms")
        else -1
    )
    assert _counter_failures(tmp_path, _ticks_with(1, **{field: value})) == (
        F.NEGATIVE_MEASUREMENT,
    )


def minted_phase(
    samples: tuple[schema.ColdTickAudioSample, ...],
    result: SessionFinalisationResult | None,
    phase: schema.ColdPhaseKind = OFF,
) -> acceptance.ColdReboundPhase:
    """Mint a capability around unvalidated samples, for defensive cases only."""
    return acceptance.ColdReboundPhase(
        token=TOKEN,
        phase=phase,
        header=schema.ColdRunHeader.model_construct(),
        ticks=tuple(schema.ColdTickRecord.model_construct(audio=sample) for sample in samples),
        hosts=(),
        advisories=(),
        finalisations=(),
        aborts=(),
        finalisation=result,
        finalisation_ambiguous=False,
        identity=store.ColdRetainedIdentityV1.model_construct(),
    )


def clean_result() -> SessionFinalisationResult:
    """One strictly parsed, active, clean recording-off result."""
    return SessionFinalisationResult.model_validate_json(json.dumps(result_for(OFF)))


@pytest.mark.parametrize(
    "value",
    [True, "1", float("inf"), float("nan"), None],
    ids=["bool", "text", "inf", "nan", "none"],
)
@pytest.mark.parametrize("field", ["dropped_window_count", "max_inference_duration_ms"])
def test_unexpected_measurement_shapes_fail_closed(field: str, value: object) -> None:
    """Counters (defensive): wrong exact types or non-finite readings are never read."""
    values: Json = {**tick_audio(), field: value}
    sample = schema.ColdTickAudioSample.model_construct(**values)
    rebound = minted_phase((sample,), clean_result())
    result = evaluate_counters(rebound)
    assert result.outcome is Outcome.FAIL
    assert F.MEASUREMENT_SHAPE_UNEXPECTED in result.failures


def test_a_missing_reading_is_a_shape_failure() -> None:
    """Counters (defensive): a sample without a counter attribute fails closed."""
    values = tick_audio()
    del values["emitted_window_count"]
    sample = schema.ColdTickAudioSample.model_construct(**values)
    assert evaluate_counters(minted_phase((sample,), clean_result())).failures == (
        F.MEASUREMENT_SHAPE_UNEXPECTED,
    )


def test_an_unknown_live_status_fails_closed() -> None:
    """G16 (defensive): a status outside the closed grammar is never read as pending."""
    values: Json = {**tick_audio(), "status": "future_status"}
    sample = schema.ColdTickAudioSample.model_construct(**values)
    assert evaluate_runtime(minted_phase((sample,), clean_result())).failures == (
        F.INFERENCE_NOT_ACTIVE,
    )


def test_a_malformed_duration_reading_fails_closed() -> None:
    """G17 (defensive): a malformed duration is a shape failure, never a pass."""
    values: Json = {**tick_audio(), "max_inference_duration_ms": True}
    sample = schema.ColdTickAudioSample.model_construct(**values)
    assert evaluate_duration(minted_phase((sample,), clean_result())).failures == (
        F.MEASUREMENT_SHAPE_UNEXPECTED,
    )


# ------------------------------------------------------------------------ G17


def _duration_failures(
    tmp_path: Path,
    ticks: tuple[Json, ...] | None = None,
    document: Mutation | None = None,
    **changes: typing.Any,
) -> tuple[F, ...]:
    """Interpret one phase and return its inference-duration failures."""
    item = interpret_phase(
        tmp_path, OFF, ticks=ticks, document=document, results=(result_for(OFF, **changes),)
    )
    return failures_of(item, Check.INFERENCE_DURATION)


def _durations(value: float) -> dict[str, typing.Any]:
    """Snapshot changes putting one duration in both snapshots."""
    return {
        "pre_finalisation_first_crack_status": status(max_inference_duration_ms=value),
        "first_crack_runtime": runtime(final_status(max_inference_duration_ms=value)),
    }


def test_inference_duration_is_strictly_below_the_seven_second_hop(tmp_path: Path) -> None:
    """G17: 6999.999 ms passes and 7000.0 ms fails."""
    assert _duration_failures(tmp_path, **_durations(6999.999)) == ()
    assert _duration_failures(tmp_path / "at", **_durations(7000.0)) == (
        F.INFERENCE_DURATION_AT_OR_ABOVE_HOP,
    )


def test_inference_duration_takes_the_maximum_over_every_element(tmp_path: Path) -> None:
    """G17: a tick maximum above both snapshots, or a final-only maximum, fails."""
    assert _duration_failures(tmp_path, _ticks_with(0, max_inference_duration_ms=7500.0)) == (
        F.INFERENCE_DURATION_AT_OR_ABOVE_HOP,
    )
    final_only = runtime(final_status(max_inference_duration_ms=7000.0))
    assert _duration_failures(tmp_path / "final", first_crack_runtime=final_only) == (
        F.INFERENCE_DURATION_AT_OR_ABOVE_HOP,
    )


def test_inference_duration_rejects_negative_or_absent_readings(tmp_path: Path) -> None:
    """G17: a negative reading fails; with no evidence only presence failures remain."""
    assert _duration_failures(tmp_path, _ticks_with(1, max_inference_duration_ms=-1.0)) == (
        F.NEGATIVE_MEASUREMENT,
    )
    item = interpret_phase(tmp_path / "empty", OFF, ticks=(), results=())
    assert failures_of(item, Check.INFERENCE_DURATION) == (
        F.TICK_EVIDENCE_ABSENT,
        F.FINALISATION_EVIDENCE_ABSENT,
    )


def test_the_hop_is_the_fixed_seven_seconds_whatever_the_profile(tmp_path: Path) -> None:
    """G17 residual: a 0.7-overlap profile is still judged against 7.0 s."""
    overlap = at("effective_mcp_profile.audio_overlap", 0.7)
    assert _duration_failures(tmp_path, document=overlap, **_durations(5000.0)) == ()
    assert _duration_failures(tmp_path / "at", document=overlap, **_durations(7000.0)) == (
        F.INFERENCE_DURATION_AT_OR_ABOVE_HOP,
    )
    assert acceptance.EFFECTIVE_HOP_SECONDS == 7.0
    hop_users = {
        function.name
        for function in TREE.body
        if isinstance(function, ast.FunctionDef)
        and "EFFECTIVE_HOP_SECONDS" in _identifiers(function)
    }
    assert hop_users == {"_evaluate_inference_duration"}


# ------------------------------------------------------------------------ G18


def _recording_failures(
    tmp_path: Path, phase: schema.ColdPhaseKind, recording: Json | None
) -> tuple[F, ...]:
    """Interpret one phase with the given recording evidence."""
    item = interpret_phase(tmp_path, phase, results=(result_for(phase, recording=recording),))
    return failures_of(item, Check.RECORDING_ARTEFACTS)


def _without(role: str) -> list[Json]:
    return [artefact(name) for name in ON_ROLES if name != role]


ON_CASES: list[tuple[str, Json | None, tuple[F, ...]]] = [
    *[
        (f"missing-{role}", recording_on(_without(role)), (F.RECORDING_ARTEFACT_SET_UNEXPECTED,))
        for role in ON_ROLES
    ],
    (
        "two-primaries",
        recording_on([*[artefact(r) for r in ON_ROLES], artefact("primary_wav")]),
        (F.RECORDING_ARTEFACT_SET_UNEXPECTED,),
    ),
    (
        "additional-wav",
        recording_on([*[artefact(r) for r in ON_ROLES], artefact("additional_wav")]),
        (F.RECORDING_ARTEFACT_SET_UNEXPECTED,),
    ),
    (
        "not-exists",
        recording_on([artefact("primary_wav", exists=False), *_without("primary_wav")]),
        (F.RECORDING_ARTEFACT_EMPTY,),
    ),
    (
        "size-none",
        recording_on(
            [artefact("recording_sidecar", size_bytes=None), *_without("recording_sidecar")]
        ),
        (F.RECORDING_ARTEFACT_EMPTY,),
    ),
    (
        "size-zero",
        recording_on([artefact("primary_wav", size_bytes=0), *_without("primary_wav")]),
        (F.RECORDING_ARTEFACT_EMPTY,),
    ),
    (
        "size-negative",
        recording_on(
            [
                artefact("annotation_session_sidecar", size_bytes=-1),
                *_without("annotation_session_sidecar"),
            ]
        ),
        (F.RECORDING_ARTEFACT_EMPTY,),
    ),
    ("not-started", recording_on(outcome="not_started"), (F.RECORDING_NOT_FINALISED,)),
    ("failed", recording_on(outcome="failed"), (F.RECORDING_NOT_FINALISED,)),
    ("reason", recording_on(reason="writer closed late"), (F.RECORDING_NOT_FINALISED,)),
    ("not-expected", recording_on(expected=False), (F.RECORDING_NOT_FINALISED,)),
    (
        "off-evidence-in-on",
        recording_off(),
        (F.RECORDING_NOT_FINALISED, F.RECORDING_ARTEFACT_SET_UNEXPECTED),
    ),
    ("absent", None, (F.FINALISATION_EVIDENCE_ABSENT,)),
    (
        "same-count-additional-for-sidecar",
        recording_on(
            [
                artefact("primary_wav"),
                artefact("additional_wav"),
                artefact("annotation_session_sidecar"),
            ]
        ),
        (F.RECORDING_ARTEFACT_SET_UNEXPECTED,),
    ),
    (
        "same-count-duplicate-primary",
        recording_on(
            [
                artefact("primary_wav"),
                artefact("primary_wav"),
                artefact("annotation_session_sidecar"),
            ]
        ),
        (F.RECORDING_ARTEFACT_SET_UNEXPECTED,),
    ),
]
OFF_CASES: list[tuple[str, Json | None, tuple[F, ...]]] = [
    (
        "artefact",
        recording_off(artifacts=[artefact("primary_wav")]),
        (F.RECORDING_UNEXPECTEDLY_CONFIGURED,),
    ),
    ("expected", recording_off(expected=True), (F.RECORDING_UNEXPECTEDLY_CONFIGURED,)),
    ("finalised", recording_off(outcome="finalised"), (F.RECORDING_UNEXPECTEDLY_CONFIGURED,)),
    ("on-evidence-in-off", recording_on(), (F.RECORDING_UNEXPECTEDLY_CONFIGURED,)),
    ("absent", None, (F.FINALISATION_EVIDENCE_ABSENT,)),
]


@pytest.mark.parametrize(
    ("recording", "expected"), [case[1:] for case in ON_CASES], ids=[case[0] for case in ON_CASES]
)
def test_recording_on_requires_the_exact_non_empty_trio(
    tmp_path: Path, recording: Json | None, expected: tuple[F, ...]
) -> None:
    """G18: finalised, exactly one of each role, no additional WAV, non-empty stats."""
    assert _recording_failures(tmp_path, ON, recording) == expected


@pytest.mark.parametrize(
    ("recording", "expected"), [case[1:] for case in OFF_CASES], ids=[case[0] for case in OFF_CASES]
)
def test_recording_off_requires_nothing_configured(
    tmp_path: Path, recording: Json | None, expected: tuple[F, ...]
) -> None:
    """G18: recording-off is not expected, not configured, and has no artefacts."""
    assert _recording_failures(tmp_path, OFF, recording) == expected


def test_recording_checks_never_read_paths_or_filenames(tmp_path: Path) -> None:
    """G18: canary paths and filenames change nothing and are never read."""
    artifacts = [artefact(role, path=f"/{CANARY}/{role}", filename=CANARY) for role in ON_ROLES]
    assert _recording_failures(tmp_path, ON, recording_on(artifacts)) == ()
    attributes = {node.attr for node in ast.walk(TREE) if isinstance(node, ast.Attribute)}
    assert {"path", "filename"} & attributes == set()


def test_recording_off_pass_never_relaxes_the_runtime_check(tmp_path: Path) -> None:
    """G18/G16: a clean recording-off result does not excuse an inactive detector."""
    disabled = finalisation_payload()
    item = interpret_phase(tmp_path, OFF, results=(disabled,))
    assert failures_of(item, Check.RECORDING_ARTEFACTS) == ()
    assert F.INFERENCE_NOT_ACTIVE in failures_of(item, Check.INFERENCE_RUNTIME)


# ------------------------------------------------------------------------ D191


def _metrics(
    tmp_path: Path, ticks: tuple[Json, ...] | None = None, **changes: object
) -> acceptance.ColdD191Metrics | None:
    """Interpret one phase and return its derived D191 metrics."""
    return interpret_phase(tmp_path, OFF, ticks=ticks, results=(result_for(OFF, **changes),)).d191


def test_x_is_the_trailing_peak_over_every_element(tmp_path: Path) -> None:
    """D191: X peaks at the earliest tick, the pre snapshot, or the final snapshot.

    The trailing gauge falling from 150 to 0 is not a counter restart.
    """
    lost = "estimated_lost_audio_ms_last_minute"
    earliest = interpret_phase(
        tmp_path, OFF, ticks=_ticks_with(0, **{lost: 150.0}), results=(result_for(OFF),)
    )
    assert earliest.d191 == acceptance.ColdD191Metrics(
        max_consecutive_overflow_count=0, peak_trailing_lost_audio_ms=150.0
    )
    assert failures_of(earliest, Check.AUDIO_COUNTERS) == ()
    pre = _metrics(tmp_path / "pre", pre_finalisation_first_crack_status=status(**{lost: 120.0}))
    assert pre is not None and pre.peak_trailing_lost_audio_ms == 120.0
    final = _metrics(tmp_path / "final", first_crack_runtime=runtime(final_status(**{lost: 90.0})))
    assert final is not None and final.peak_trailing_lost_audio_ms == 90.0


def test_n_is_the_final_snapshot_value(tmp_path: Path) -> None:
    """D191: N is taken from the frozen final counter snapshot."""
    streak = "max_consecutive_overflow_count"
    ticks = tuple(
        {**tick, streak: value} for tick, value in zip(default_ticks(), (1, 2, 2), strict=True)
    )
    metrics = _metrics(
        tmp_path,
        ticks,
        pre_finalisation_first_crack_status=status(**{streak: 3}),
        first_crack_runtime=runtime(final_status(**{streak: 4})),
    )
    assert metrics == acceptance.ColdD191Metrics(
        max_consecutive_overflow_count=4, peak_trailing_lost_audio_ms=0.0
    )


UNAVAILABLE_CASES: list[tuple[str, tuple[Json, ...] | None, dict[str, object]]] = [
    ("no-ticks", (), {}),
    ("pre-absent", None, {"pre_finalisation_first_crack_status": None}),
    ("runtime-absent", None, {"first_crack_runtime": None}),
    ("negative-lost-audio", _ticks_with(1, estimated_lost_audio_ms_last_minute=-1.0), {}),
    ("negative-streak", _ticks_with(1, max_consecutive_overflow_count=-1), {}),
    ("streak-restart", _ticks_with(0, max_consecutive_overflow_count=5), {}),
    (
        "total-restart",
        None,
        {"pre_finalisation_first_crack_status": status(total_overflow_count=9)},
    ),
    (
        "final-streak-decrease",
        None,
        {
            "pre_finalisation_first_crack_status": status(max_consecutive_overflow_count=3),
            "first_crack_runtime": runtime(final_status(max_consecutive_overflow_count=0)),
        },
    ),
    (
        "final-negative-streak",
        None,
        {"first_crack_runtime": runtime(final_status(max_consecutive_overflow_count=-1))},
    ),
]


@pytest.mark.parametrize(
    ("ticks", "changes"),
    [case[1:] for case in UNAVAILABLE_CASES],
    ids=[case[0] for case in UNAVAILABLE_CASES],
)
def test_unavailable_metrics_are_none_never_zero(
    tmp_path: Path, ticks: tuple[Json, ...] | None, changes: dict[str, object]
) -> None:
    """D191: absent, ambiguous, negative, or restarted evidence yields ``None``."""
    assert _metrics(tmp_path, ticks, **changes) is None


def test_metrics_are_unavailable_without_finalisation_or_when_ambiguous(tmp_path: Path) -> None:
    """D191: a missing or ambiguous finalisation yields ``None``."""
    assert interpret_phase(tmp_path, OFF, results=()).d191 is None
    ambiguous = (result_for(OFF), result_for(OFF, session_id="other-session"))
    assert interpret_phase(tmp_path / "two", OFF, results=ambiguous).d191 is None


def test_malformed_readings_make_metrics_unavailable() -> None:
    """D191 (defensive): a malformed reading yields ``None``."""
    values: Json = {**tick_audio(), "estimated_lost_audio_ms_last_minute": float("inf")}
    sample = schema.ColdTickAudioSample.model_construct(**values)
    assert derive_d191(minted_phase((sample,), clean_result())) is None


def test_limits_are_declared_and_never_compared() -> None:
    """D191: the locked limits exist for rendering and are never a comparison operand."""
    assert (acceptance.D191_N_LIMIT, acceptance.D191_X_LIMIT_MS) == (1, 200.0)
    assert acceptance.PRODUCTION_FATAL_STREAK == 30
    limits = {"D191_N_LIMIT", "D191_X_LIMIT_MS", "PRODUCTION_FATAL_STREAK"}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Compare):
            assert _identifiers(node) & limits == set()
    uses = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Name) and node.id in limits and isinstance(node.ctx, ast.Load)
    ]
    assert uses == []


# --------------------------------------------------------- models and results


def test_check_results_are_closed_ordered_and_consistent() -> None:
    """Results: ``PASS`` exactly without failures; failures unique, in declaration order."""
    acceptance.ColdCheckResult(check=Check.AUDIO_COUNTERS, outcome=Outcome.PASS, failures=())
    acceptance.ColdCheckResult(
        check=Check.AUDIO_COUNTERS,
        outcome=Outcome.FAIL,
        failures=(F.DROPPED_WINDOW, F.CAPTURE_RESTART),
    )
    for outcome, failures in (
        (Outcome.PASS, (F.DROPPED_WINDOW,)),
        (Outcome.FAIL, ()),
        (Outcome.FAIL, (F.CAPTURE_RESTART, F.DROPPED_WINDOW)),
        (Outcome.FAIL, (F.DROPPED_WINDOW, F.DROPPED_WINDOW)),
    ):
        with pytest.raises(pydantic.ValidationError):
            acceptance.ColdCheckResult(
                check=Check.AUDIO_COUNTERS, outcome=outcome, failures=failures
            )
    with pytest.raises(pydantic.ValidationError):
        acceptance.ColdCheckResult.model_validate(
            {"check": "audio_counters", "outcome": "pass", "failures": ()}
        )


def test_phase_interpretations_hold_exactly_the_five_checks(tmp_path: Path) -> None:
    """Results: the five checks in order, with facts exactly when qualification passed."""
    item = interpret_phase(tmp_path, OFF)
    values: dict[str, object] = {
        "phase": item.phase,
        "identity_sha256": item.identity_sha256,
        "results": item.results,
        "d191": item.d191,
        "identity_facts": item.identity_facts,
    }
    model = acceptance.ColdPhaseInterpretation
    assert model.model_validate(values) == item
    failed = acceptance.ColdCheckResult(
        check=Check.IDENTITY_QUALIFICATION_V1, outcome=Outcome.FAIL, failures=(F.Q_BOOT_ID,)
    )
    for change in (
        {"results": item.results[::-1]},
        {"results": item.results[:4]},
        {"identity_facts": None},
        {"results": (failed, *item.results[1:])},
        {"identity_sha256": "A" * 64},
    ):
        with pytest.raises(pydantic.ValidationError):
            model.model_validate(values | change)


def test_metrics_and_facts_models_are_strict_and_closed() -> None:
    """Models: negative metrics, non-qualifying facts, and extras are refused."""
    with pytest.raises(pydantic.ValidationError):
        acceptance.ColdD191Metrics(
            max_consecutive_overflow_count=-1, peak_trailing_lost_audio_ms=0.0
        )
    with pytest.raises(pydantic.ValidationError):
        acceptance.ColdD191Metrics(
            max_consecutive_overflow_count=0, peak_trailing_lost_audio_ms=float("inf")
        )
    facts = admissible_facts()
    for change in (
        {"recording_device_count": 2},
        {"source_tree_dirty": True},
        {"temperature_unit": "fahrenheit"},
        {"source_revision": "B" * 40},
        {"audio_overlap": 1.0},
        {"unexpected": 1},
    ):
        with pytest.raises(pydantic.ValidationError):
            acceptance.ColdIdentityFacts.model_validate(facts | change)


def admissible_facts() -> dict[str, object]:
    """Return one admissible set of identity-fact values."""
    return {
        "mcp_version": "0.2.2",
        "temperature_unit": "celsius",
        "first_crack_mode": "audio",
        "model_precision": "int8",
        "recording_device_count": 1,
        "source_tree_dirty": False,
        "source_revision": "b" * 40,
        "artefact_kind": "wheel",
        "artefact_sha256": "a" * 64,
        "profile_source_sha256": "c" * 64,
        "profile_source_byte_length": 100,
        "first_crack_onnx_threads": 2,
        "first_crack_min_positive_windows": 3,
        "first_crack_confirmation_window_seconds": 30.0,
        "audio_sample_rate": 16000,
        "audio_window_seconds": 10.0,
        "audio_overlap": 0.3,
        "audio_hop_seconds": None,
        "session_ror_window_seconds": 60,
        "session_ror_min_sample_seconds": 10,
    }


OUTPUT_MODELS: tuple[type[pydantic.BaseModel], ...] = (
    acceptance.ColdCheckResult,
    acceptance.ColdD191Metrics,
    acceptance.ColdIdentityFacts,
    acceptance.ColdPhaseInterpretation,
)


def _valid_output(model: type[pydantic.BaseModel]) -> tuple[dict[str, object], str, object]:
    """Return valid construction values, one field, and a replacement value to assign."""
    metrics: dict[str, object] = {
        "max_consecutive_overflow_count": 0,
        "peak_trailing_lost_audio_ms": 0.0,
    }
    if model is acceptance.ColdCheckResult:
        return (
            {"check": Check.AUDIO_COUNTERS, "outcome": Outcome.PASS, "failures": ()},
            "outcome",
            Outcome.FAIL,
        )
    if model is acceptance.ColdD191Metrics:
        return metrics, "max_consecutive_overflow_count", 5
    if model is acceptance.ColdIdentityFacts:
        return admissible_facts(), "audio_overlap", 0.5
    results = tuple(
        acceptance.ColdCheckResult(check=check, outcome=Outcome.PASS, failures=())
        for check in Check
    )
    phase: dict[str, object] = {
        "phase": OFF,
        "identity_sha256": "a" * 64,
        "results": results,
        "d191": acceptance.ColdD191Metrics.model_validate(metrics),
        "identity_facts": acceptance.ColdIdentityFacts.model_validate(admissible_facts()),
    }
    return phase, "d191", None


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=[model.__name__ for model in OUTPUT_MODELS])
def test_output_models_are_strict_frozen_closed_and_finite(
    model: type[pydantic.BaseModel],
) -> None:
    """Models: each output model pins its config, constructs, and refuses assignment."""
    config = model.model_config
    assert (
        config.get("strict"),
        config.get("frozen"),
        config.get("extra"),
        config.get("allow_inf_nan"),
    ) == (True, True, "forbid", False)
    values, field, replacement = _valid_output(model)
    instance = model.model_validate(values)
    assert model(**values) == instance
    original = getattr(instance, field)
    with pytest.raises(pydantic.ValidationError):
        setattr(instance, field, replacement)
    assert getattr(instance, field) == original
    with pytest.raises(pydantic.ValidationError):
        model.model_validate({**values, "unexpected": 1})


def test_enums_are_plain_closed_and_pinned() -> None:
    """Enums: plain ``Enum``, unique values, and the ratified member order."""
    for kind in (Check, Outcome, F, Refusal):
        assert issubclass(kind, enum.Enum) and not issubclass(kind, str)
        assert len({member.value for member in kind}) == len(kind)
    assert [member.name for member in Check] == [
        "IDENTITY_QUALIFICATION_V1",
        "INFERENCE_RUNTIME",
        "AUDIO_COUNTERS",
        "INFERENCE_DURATION",
        "RECORDING_ARTEFACTS",
    ]
    assert [member.name for member in F][21:29] == [
        "MEASUREMENT_SHAPE_UNEXPECTED",
        "NEGATIVE_MEASUREMENT",
        "DROPPED_WINDOW",
        "INFERENCE_OVERRUN",
        "QUEUE_GROWING",
        "QUEUE_NOT_DRAINED",
        "CAPTURE_RESTART",
        "INFERENCE_DURATION_AT_OR_ABOVE_HOP",
    ]
    assert len(F) == 33
    assert [member.name for member in Refusal] == [
        "CONTAINER_MALFORMED",
        "RECORD_REBIND_FAILED",
        "HEADER_SET_MISMATCHED",
        "NO_PHASE_PRESENT",
        "CAPABILITY_INVALID",
    ]


FORBIDDEN_NAME_TOKENS = (
    "verdict",
    "qualified",
    "passed",
    "is_pass",
    "aggregate",
    "overall",
    "success",
)


def test_no_verdict_qualified_pass_or_aggregate_field_or_callable() -> None:
    """No verdict: no field, slot, or public callable names a verdict or an aggregate."""
    names: set[str] = set()
    for name in acceptance.__all__:
        names.add(name)
        value = getattr(acceptance, name)
        if isinstance(value, type) and issubclass(value, pydantic.BaseModel):
            names.update(value.model_fields)
        if isinstance(value, type):
            names.update(getattr(value, "__slots__", ()))
    lowered = {name.lower() for name in names}
    assert not any(token in name for name in lowered for token in FORBIDDEN_NAME_TOKENS)
    assert [
        name for name in acceptance.__all__ if inspect.isfunction(getattr(acceptance, name))
    ] == ["interpret_retained_run"]
    assert "outcome" in acceptance.ColdCheckResult.model_fields
    assert not any("outcome" in name for name in acceptance.ColdPhaseInterpretation.model_fields)


def test_public_surface_is_exactly_the_contract_surface() -> None:
    """Scope: the public names are the §2.1-§2.2 types, constants, and entry point."""
    defined = {
        name
        for name, value in vars(acceptance).items()
        if not name.startswith("_")
        and getattr(value, "__module__", acceptance.__name__) == acceptance.__name__
        and not isinstance(value, type(acceptance))
    }
    assert set(acceptance.__all__) == defined


# ------------------------------------------------------------ structure sweeps

ALLOWED_FROM_IMPORTS: dict[str, frozenset[str]] = {
    "roastpilot_agent.cold_characterisation.evidence_schema": frozenset(
        {
            "ColdAbortRecord",
            "ColdAdvisoryRecord",
            "ColdEvidenceError",
            "ColdEvidenceStream",
            "ColdFinalisationRecord",
            "ColdHostRecord",
            "ColdPhaseKind",
            "ColdRunHeader",
            "ColdTickAudioSample",
            "ColdTickRecord",
            "validate_record",
        }
    ),
    "roastpilot_agent.cold_characterisation.evidence_store": frozenset(
        {
            "ColdBindingState",
            "ColdEvidenceStoreError",
            "ColdRetainedIdentityV1",
            "check_record_binding",
            "parse_finalisation_envelope",
            "run_id_is_valid",
        }
    ),
    "roastpilot_agent.cold_characterisation.evidence_reader": frozenset(
        {"ColdRetainedHeader", "ColdRetainedRun", "ColdRetainedStream"}
    ),
    "roastpilot_agent.cold_characterisation.mcp": frozenset(
        {
            "FinalisationFirstCrackStatus",
            "FirstCrackRuntimeFinalisationEvidence",
            "RecordingFinalisationEvidence",
            "SessionFinalisationResult",
        }
    ),
}
ALLOWED_IMPORTS = frozenset({"enum", "math", "re", "types", "typing", "pydantic"})


def test_imports_are_exactly_the_ratified_allow_list() -> None:
    """Class B: only the allow-listed modules and names are imported."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            assert all(
                alias.name in ALLOWED_IMPORTS and alias.asname is None for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in ALLOWED_FROM_IMPORTS, node.module
            names = {alias.name for alias in node.names}
            assert names <= ALLOWED_FROM_IMPORTS[node.module], node.module


DYNAMIC_CALL_NAMES = frozenset({"__import__", "import_module", "eval", "exec", "compile"})
DYNAMIC_CALL_ATTRIBUTES = frozenset({"__import__", "import_module", "eval", "exec"})


def _dynamic_calls(tree: ast.AST) -> list[str]:
    """Return each dynamic-import or evaluation call in a tree; ``re.compile`` is not one."""
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id in DYNAMIC_CALL_NAMES:
            calls.append(target.id)
        elif isinstance(target, ast.Attribute) and (
            target.attr in DYNAMIC_CALL_ATTRIBUTES
            or (
                target.attr == "compile"
                and isinstance(target.value, ast.Name)
                and target.value.id == "builtins"
            )
        ):
            calls.append(ast.unparse(target))
    return calls


DYNAMIC_CALL_SAMPLES: list[tuple[str, str, list[str]]] = [
    ("bare-import", "__import__('math')", ["__import__"]),
    ("importlib-import-module", "importlib.import_module('math')", ["importlib.import_module"]),
    ("bare-import-module", "import_module('math')", ["import_module"]),
    ("eval", "eval('1')", ["eval"]),
    ("exec", "exec('x = 1')", ["exec"]),
    ("builtin-compile", "compile('1', '<sample>', 'eval')", ["compile"]),
    ("builtins-compile", "builtins.compile('1', '<sample>', 'eval')", ["builtins.compile"]),
    ("builtins-import", "builtins.__import__('math')", ["builtins.__import__"]),
    ("dead-function", "def unused():\n    return __import__('math')\n", ["__import__"]),
    ("re-compile-permitted", "re.compile(r'\\A[a-z]+\\Z')", []),
    ("pattern-call-permitted", "_PATTERN.fullmatch(text)", []),
]


@pytest.mark.parametrize(
    ("sample", "expected"),
    [case[1:] for case in DYNAMIC_CALL_SAMPLES],
    ids=[case[0] for case in DYNAMIC_CALL_SAMPLES],
)
def test_dynamic_call_fence_classifies_bounded_samples(sample: str, expected: list[str]) -> None:
    """Class B: the fence flags dynamic import and evaluation calls, never ``re.compile``.

    Each sample is only parsed into a syntax tree; none is compiled or executed.
    """
    assert _dynamic_calls(ast.parse(sample)) == expected


def test_module_makes_no_dynamic_import_and_binds_only_allowed_modules() -> None:
    """Class B: the source has no dynamic import or evaluation call; only allowed modules bind."""
    assert _dynamic_calls(TREE) == []
    assert "re.compile(" in SOURCE
    bound = {
        name for name, value in vars(acceptance).items() if isinstance(value, types.ModuleType)
    }
    assert bound == ALLOWED_IMPORTS


def test_no_assert_and_module_level_policy_is_immutable() -> None:
    """Class A: no ``assert``; module-level policy is ``Final`` and immutable."""
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(TREE))
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            annotation = None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            annotation = ast.unparse(node.annotation)
        else:
            continue
        for name in names:
            value = getattr(acceptance, name)
            assert not isinstance(value, (dict, list, set, bytearray)), name
            if name in {"__all__", "_T", "_R"} or annotation == "typing.TypeAlias":
                continue
            assert annotation is not None and annotation.startswith("typing.Final"), name


def test_interpretation_performs_no_file_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope: interpretation needs none of eight patched file and evidence entry points.

    Only ``builtins.open``, ``io.open``, ``os.open``, ``os.scandir``, the reader's
    ``read_retained_run`` and the store's ``verify_retained_tree``,
    ``verify_retained_copies`` and ``read_verified_lines`` are patched to raise while
    an already-read run is interpreted; the evidence tree is then unchanged.
    """
    run = write_run(tmp_path, {OFF: Phase(), ON: Phase()})
    root = str(tmp_path.resolve() / "pi")
    before = snapshot_tree(root)

    def refuse(*_args: object, **_kwargs: object) -> typing.NoReturn:
        raise AssertionError("file or reader access during interpretation")

    for target, name in (
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "scandir"),
        (reader, "read_retained_run"),
        (store, "verify_retained_tree"),
        (store, "verify_retained_copies"),
        (store, "read_verified_lines"),
    ):
        monkeypatch.setattr(target, name, refuse)
    interpretation = acceptance.interpret_retained_run(run)
    monkeypatch.undo()
    assert len(interpretation.phases) == 2
    assert snapshot_tree(root) == before


FORBIDDEN_SOURCE_TEXT = (
    # C: reader, writer, verifier, builder, and clean-conjunction names.
    "read_retained_run",
    "verify_retained",
    "open_run",
    "admit_evidence_root",
    "read_verified_lines",
    "load_strict_json",
    "build_run_header",
    "build_tick_record",
    "build_host_record",
    "build_finalisation_record",
    "finalisation_is_clean",
    "derive_finalisation_index",
    # D: file and process I/O.
    "open(",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "mkdir",
    "os.",
    "subprocess",
    # E: actuator reach.
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
    # F, H, K, L: capability reads, lexical control, packaged constants, JSON output.
    ".command_streaming_required",
    "startswith",
    "endswith",
    "casefold",
    ".lower(",
    ".upper(",
    ".strip(",
    "__version__",
    "REPO_ID",
    "MANIFEST_FILES",
    "json.dumps",
    "model_dump_json",
)


def test_source_sweeps_find_no_forbidden_reach() -> None:
    """Classes C-L: no reader, I/O, actuator, lexical-control, or packaged-constant text."""
    for text in FORBIDDEN_SOURCE_TEXT:
        assert text not in SOURCE, text
    assert re.search(r"REVISION\b", SOURCE) is None


def test_token_scoring_is_confined_to_the_q7_copy() -> None:
    """Class I: the token-score screen appears only in the Q7 operator-text copy."""
    lines = SOURCE.splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith("# Q7 copy"))
    last = next(index for index, line in enumerate(lines) if line.startswith("def _device_text"))
    hits = [index for index, line in enumerate(lines) if "entropy" in line.lower()]
    assert hits and all(first <= index < last for index in hits)
