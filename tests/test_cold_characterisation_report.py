"""Behavioural and fail-closed tests for the sanitised cold-characterisation report.

#954, unit 3B-ii-c-ii.  Runs are written through the real writer, sealed, and read
back with the strict reader under ``tmp_path``; the report is then built and
rendered from that run.  Inadmissible identities use the bypassed-header pattern.
Finalisation results are the committed fixture, re-parsed and changed in memory;
the fixture file is never edited and is never treated as qualifying.  Private
helpers are called directly only for defensive cases that strict snapshots cannot
reach.
"""

import ast
import builtins
import copy
import enum
import hashlib
import inspect
import io
import itertools
import json
import os
import re
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import acceptance, report
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    SessionFinalisationResult,
)
from tests.test_cold_characterisation_acceptance import (
    artefact,
    at,
    default_ticks,
    final_status,
    recording_on,
    result_for,
    runtime,
    status,
    tick_audio,
)
from tests.test_cold_characterisation_evidence_builders import (
    FIXTURE_PATH,
    RUN_ID,
    abort_for,
    device_state,
    finalisation_payload,
    host_sample,
)
from tests.test_cold_characterisation_evidence_reader import envelope_of, identity_document
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    make_root,
    run_dir,
    snapshot_tree,
    write_full_run,
)

Json = dict[str, typing.Any]
Mutation = typing.Callable[[Json], None]
F = acceptance.ColdCheckFailure
Check = acceptance.ColdCheck
Outcome = acceptance.ColdCheckOutcome
Refusal = acceptance.ColdInterpretationFailure
Failure = report.ColdReportFailure
Kind = schema.ColdAdvisorFailureKind
Domain = schema.ColdAbortDomain
Stream = schema.ColdEvidenceStream
TOKEN = acceptance._REBIND_TOKEN  # pyright: ignore[reportPrivateUsage]
cell = report._cell  # pyright: ignore[reportPrivateUsage]
host_extremes_of = report._host_extremes  # pyright: ignore[reportPrivateUsage]
g17_maximum_of = report._g17_maximum  # pyright: ignore[reportPrivateUsage]
SOURCE = Path(report.__file__).read_text()
TREE = ast.parse(SOURCE)
REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = "2026-09-26T12:00:00Z"
RUN_TAG = b"rp954-cold-run-id-v1\x00"
SESSION_TAG = b"rp954-cold-session-id-v1\x00"
FIXTURE_SESSION = typing.cast(str, finalisation_payload()["session_id"])
CANARY = "sk-live-ReportCanary0123456789AbCdEf"
BOOT_ID_CANARY = "5e1ec7ed-cafe-4bad-b0de-0123456789ab"
ON_ROLES = ("primary_wav", "recording_sidecar", "annotation_session_sidecar")
OPERATOR_STOP = schema.ColdOperatorAbortReason.OPERATOR_STOP

#: The five ratified protocol-intent sentences, copied independently of the module.
PROTOCOL_INTENT = (
    (
        "Cold, empty-roaster load characterisation summary; not detector-accuracy, "
        "deployment-acoustics, Pi-readiness or live-roast evidence."
    ),
    (
        "The mode is designed to issue no actuator or control command and to permit only "
        "D187 all-zero protocol frames; this report does not evaluate command-state, "
        "safe-zero, serial-write or physical-response evidence and makes no finding that "
        "actuation did or did not occur."
    ),
    (
        "Per-tick software observation covers heat, roast fan and cooling only; all six "
        "command dimensions appear only in D195 finalisation evidence, which this report "
        "does not evaluate."
    ),
    (
        "This report renders per-check results and states no run verdict; D191 limits are "
        "shown, not compared; the recorded identity is committed by digest and is not a "
        "complete D192 identity."
    ),
    (
        "The manifest digest is carried from the supplied run and is not re-verified here; "
        "this report does not establish completeness, provenance or independent storage, "
        "and MCP-reported fields are recorded values, not evaluated here."
    ),
)
FORBIDDEN_PHRASES = (
    "no roaster actuation occurred",
    "zero-actuation protocol frames were written",
    "no serial writes occurred",
    "production-ready",
    "hardware-ready",
    "validated on hardware",
    "qualified",
    "passed characterisation",
    "fully autonomous",
    "%",
)
#: The nine fixed Markdown heading notes in rendering order, copied independently of the module.
SECTION_NOTES = (
    (
        "locked_limits",
        "shown, not compared by the report projection; G17 already compares inference "
        "duration against the fixed seven-second hop; these recorded limits are not "
        "compared against the recorded profile",
    ),
    (
        "checks",
        "per-check results, not a run verdict; a per-check pass is not evidence of "
        "sustained inference, run duration or run qualification",
    ),
    ("d191", "derived, not compared"),
    ("counters", "final snapshot, plus the series maximum inference duration; null if unavailable"),
    ("aborts", "closed classifications"),
    ("advisor_failure_counts", "closed classifications"),
    ("host_extremes", "recorded, not compared"),
    ("recording_artefacts", "stat only"),
    ("identity_facts", "shown only when identity check v1 has no failure"),
)
RUN_SECTION = f"## `{SECTION_NOTES[0][0]}` ({SECTION_NOTES[0][1]})"
PHASE_SECTIONS = tuple(f"### `{name}` ({note})" for name, note in SECTION_NOTES[1:])
#: Every heading a report may render: the title, run and phase headings, and the notes.
FIXED_HEADINGS = frozenset(
    {
        "# Cold characterisation summary (report schema 1)",
        "## Run",
        "## Phase `recording_off`",
        "## Phase `recording_on`",
        RUN_SECTION,
        *PHASE_SECTIONS,
    }
)


# ------------------------------------------------------------------- helpers


def tagged(tag: bytes, value: str) -> str:
    """Independently compute one tagged identifier digest."""
    return hashlib.sha256(tag + value.encode("utf-8")).hexdigest()


def rendered(error: BaseException) -> str:
    """Every text channel of one error."""
    return f"{error!s}{error!r}{error.args}"


def expect_report_error(
    failure: report.ColdReportFailure, call: typing.Callable[[], object]
) -> report.ColdReportError:
    """Assert one call raises exactly one closed, content-free, chain-free report error."""
    with pytest.raises(report.ColdReportError) as raised:
        call()
    error = raised.value
    assert error.failure is failure
    assert error.args == ("Cold report failed.",)
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


def header_of(
    document: Json, phase: schema.ColdPhaseKind, run_id: str, text: str = UTC
) -> schema.ColdRunHeader:
    """Build a header around any v1 identity document, bypassing ``build_run_header``."""
    envelope = envelope_of(document)
    return schema.ColdRunHeader(
        schema_version=1,
        stream="header",
        run_id=run_id,
        phase=phase,
        recorded_at_utc=text,
        monotonic_seconds=1.0,
        identity_sha256=envelope.sha256,
        identity=envelope,
    )


def tick_record(
    header: schema.ColdRunHeader,
    index: int,
    audio: Json,
    seconds: float,
    *,
    text: str = UTC,
    vendor: Json | None = None,
) -> schema.ColdTickRecord:
    """Build one tick through the real builder."""
    state = device_state() if vendor is None else device_state(raw_vendor_data=vendor)
    return builders.build_tick_record(
        header=header,
        tick=index,
        recorded_at_utc=text,
        monotonic_seconds=seconds,
        device_state=state,
        projection=schema.project_tick_audio(audio),
    )


def finalisation_record(
    header: schema.ColdRunHeader, payload: Json, index: int, text: str = UTC
) -> schema.ColdFinalisationRecord:
    """Build one finalisation record from an in-memory result through the real builder."""
    return builders.build_finalisation_record(
        header=header,
        result=SessionFinalisationResult.model_validate_json(json.dumps(payload)),
        recorded_at_utc=text,
        monotonic_seconds=600.0 + index,
    )


def advisory_record(
    header: schema.ColdRunHeader,
    failure: schema.ColdAdvisorFailureKind | None,
    *,
    rule: str = "observation_only",
    reason: str = "observation only",
    text: str = UTC,
) -> schema.ColdAdvisoryRecord:
    """Build one observation-only advisory record with a chosen failure kind."""
    return schema.ColdAdvisoryRecord(
        schema_version=1,
        stream="advisory",
        run_id=header.run_id,
        phase=header.phase,
        recorded_at_utc=text,
        monotonic_seconds=3.0,
        identity_sha256=header.identity_sha256,
        requested_heat=0,
        requested_fan=0,
        should_drop=False,
        confidence=0.5,
        latency_seconds=0.25,
        evaluation=schema.ColdSafetyEvaluation(
            rule=rule,
            verdict=schema.ColdSafetyVerdict.REJECT,
            input_heat=0,
            input_fan=0,
            adjusted_heat=None,
            adjusted_fan=None,
            reason=reason,
        ),
        failure=failure,
    )


def host(**overrides: typing.Any) -> HostBoundSample:
    """One validated host-bound sample with chosen values."""
    return HostBoundSample(**(host_sample().model_dump() | overrides))


class Spec(typing.NamedTuple):
    """One phase to write; ``None`` ticks or results take the active, clean defaults."""

    ticks: tuple[Json, ...] | None = None
    seconds: tuple[float, ...] | None = None
    results: tuple[Json, ...] | None = None
    hosts: tuple[HostBoundSample, ...] = ()
    advisories: tuple[schema.ColdAdvisorFailureKind | None, ...] = ()
    aborts: tuple[tuple[schema.ColdAbortDomain, typing.Any], ...] = ()
    document: Mutation | None = None


def write(
    tmp_path: Path,
    phases: dict[schema.ColdPhaseKind, Spec],
    *,
    name: str = "pi",
    run_id: str = RUN_ID,
) -> reader.ColdRetainedRun:
    """Write, seal, and strictly read back one run holding the given phases."""
    root = make_root(tmp_path, name)
    writer = store.open_run(store.admit_evidence_root(root), run_id)
    for phase in schema.ColdPhaseKind:
        spec = phases.get(phase)
        if spec is None:
            continue
        document = identity_document(tmp_path, root)
        document["run_id"] = run_id
        if spec.document is not None:
            spec.document(document)
        header = header_of(document, phase, run_id)
        writer.append(header)
        ticks = default_ticks() if spec.ticks is None else spec.ticks
        seconds = spec.seconds or tuple(2.0 + index for index in range(len(ticks)))
        for index, (audio, second) in enumerate(zip(ticks, seconds, strict=True)):
            writer.append(tick_record(header, index, audio, second))
        for sample in spec.hosts:
            writer.append(
                builders.build_host_record(
                    header=header, sample=sample, recorded_at_utc=UTC, monotonic_seconds=2.0
                )
            )
        for failure in spec.advisories:
            writer.append(advisory_record(header, failure))
        results = (result_for(phase),) if spec.results is None else spec.results
        for index, payload in enumerate(results):
            writer.append(finalisation_record(header, payload, index))
        for domain, reason in spec.aborts:
            writer.append(abort_for(header, domain, reason))
    sealed = writer.seal()
    return reader.read_retained_run(
        root, run_id=run_id, expected_manifest_sha256=sealed.manifest_sha256
    )


def phase_of(
    tmp_path: Path, phase: schema.ColdPhaseKind = OFF, **spec: typing.Any
) -> report.ColdReportPhase:
    """Write one phase, build its report, and return that phase's projection."""
    return report.build_sanitised_report(write(tmp_path, {phase: Spec(**spec)})).phases[0]


def rebuilt(run: reader.ColdRetainedRun, **changes: typing.Any) -> reader.ColdRetainedRun:
    """Construct a caller-built run from a genuine one without validation."""
    values: dict[str, typing.Any] = {
        "run_id": run.run_id,
        "manifest_sha256": run.manifest_sha256,
        "headers": run.headers,
        "streams": run.streams,
    }
    return reader.ColdRetainedRun.model_construct(**(values | changes))


def values_of(model: pydantic.BaseModel) -> dict[str, typing.Any]:
    """One model's field values, keeping nested instances."""
    return {name: getattr(model, name) for name in type(model).model_fields}


def full_spec() -> dict[schema.ColdPhaseKind, Spec]:
    """Both phases, with every record kind in recording-off."""
    return {
        OFF: Spec(
            hosts=(host(),),
            advisories=(None, Kind.TIMEOUT),
            aborts=((Domain.OPERATOR, OPERATOR_STOP),),
        ),
        ON: Spec(),
    }


@pytest.fixture(scope="module")
def clean_report(tmp_path_factory: pytest.TempPathFactory) -> report.ColdSanitisedReport:
    """One report over a clean two-phase run holding every record kind."""
    return report.build_sanitised_report(write(tmp_path_factory.mktemp("clean"), full_spec()))


# ------------------------------------------------------------- builder content


def test_a_genuine_run_builds_the_report_from_its_interpretation(tmp_path: Path) -> None:
    """Builder: schema v1 over both phases, each projecting its interpretation exactly."""
    run = write(tmp_path, full_spec())
    interpretation = acceptance.interpret_retained_run(run)
    built = report.build_sanitised_report(run)
    assert built.report_schema_version == 1
    assert built.run_id_sha256 == tagged(RUN_TAG, RUN_ID)
    assert built.manifest_sha256 == run.manifest_sha256
    assert built.locked_limits == report.ColdReportLockedLimits(
        label="not compared", n=1, x_ms=200.0, fatal_streak=30, hop_seconds=7.0
    )
    assert [item.phase for item in built.phases] == [OFF, ON]
    assert built.phases_absent == ()
    for item, phase in zip(interpretation.phases, built.phases, strict=True):
        assert phase.phase is item.phase
        assert phase.identity_sha256 == item.identity_sha256
        assert [(c.check, c.outcome, c.failures) for c in phase.checks] == [
            (r.check, r.outcome, r.failures) for r in item.results
        ]
        assert [c.outcome for c in phase.checks] == [Outcome.PASS] * 5
        assert (phase.d191, phase.identity_facts) == (item.d191, item.identity_facts)
        assert phase.identity_facts is not None and phase.d191 is not None
    off, on = built.phases
    assert (off.tick_count, off.observed_tick_span_seconds) == (3, 2.0)
    assert off.recording_artefacts == ()
    assert [(a.role, a.size_bytes) for a in on.recording_artefacts or ()] == [
        (role, 4096) for role in ON_ROLES
    ]


def test_a_genuine_writer_built_run_reports(tmp_path: Path) -> None:
    """Builder: the shared ``write_full_run`` run (real headers) reports both phases."""
    root, sealed, records = write_full_run(tmp_path)
    run = reader.read_retained_run(
        root, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    built = report.build_sanitised_report(run)
    assert [item.identity_sha256 for item in built.phases] == [
        records[0].identity_sha256,
        records[5].identity_sha256,
    ]
    off, on = built.phases
    assert off.session_id_sha256 is None and off.counters.emitted is None
    assert on.session_id_sha256 == tagged(SESSION_TAG, FIXTURE_SESSION)


def test_absent_phases_are_listed_in_phase_order(tmp_path: Path) -> None:
    """Builder: a missing phase is listed as absent, never reported as present."""
    only_on = report.build_sanitised_report(write(tmp_path, {ON: Spec()}, name="on"))
    assert [item.phase for item in only_on.phases] == [ON]
    assert only_on.phases_absent == (OFF,)
    only_off = report.build_sanitised_report(write(tmp_path, {OFF: Spec()}, name="off"))
    assert only_off.phases_absent == (ON,)


def test_identifiers_are_rendered_only_as_tagged_digests(tmp_path: Path) -> None:
    """Digests: the run and session ids are domain-tagged SHA-256, never raw text."""
    run_id = "20260926T120000Z-raw-run-kiwi"
    session = "raw-session-kiwi"
    run = write(tmp_path, {ON: Spec(results=(result_for(ON, session_id=session),))}, run_id=run_id)
    document, markdown = report.render_sanitised_report(run)
    built = report.build_sanitised_report(run)
    assert built.run_id_sha256 == tagged(RUN_TAG, run_id)
    assert built.phases[0].session_id_sha256 == tagged(SESSION_TAG, session)
    assert built.run_id_sha256 != hashlib.sha256(run_id.encode()).hexdigest()
    assert tagged(RUN_TAG, session) != tagged(SESSION_TAG, session)
    for raw in (run_id, session, "raw-run-kiwi"):
        assert raw not in markdown
        assert raw.encode() not in document


def _streaming(payload: Json) -> Json:
    """Mark a payload's final driver evidence as requiring command streaming."""
    payload["final_driver_evidence"]["evidence"]["command_streaming_required"] = True
    return payload


def _finalisation_view(phase: report.ColdReportPhase) -> tuple[object, ...]:
    """The finalisation-derived values of one reported phase."""
    return (
        phase.session_id_sha256,
        phase.mcp_reported_finalisation_status,
        phase.mcp_reported_clean,
        phase.observed_command_streaming_required,
        phase.applied_branch,
        [(item.role, item.size_bytes) for item in phase.recording_artefacts or ()],
        phase.counters.emitted,
    )


def test_finalisation_fields_come_from_the_last_bound_record(tmp_path: Path) -> None:
    """Selection: same-session retries are reported from the last record in file order."""
    early = _streaming(
        result_for(
            ON,
            status="partial",
            clean=False,
            recording=recording_on([artefact(role, size_bytes=1) for role in ON_ROLES]),
            first_crack_runtime=runtime(
                final_status(emitted_window_count=12, processed_window_count=12)
            ),
        )
    )
    late = result_for(
        ON,
        recording=recording_on(
            [
                artefact(role, size_bytes=size)
                for role, size in zip(ON_ROLES, (10, 20, 30), strict=True)
            ]
        ),
    )
    forward = report.build_sanitised_report(
        write(tmp_path, {ON: Spec(results=(early, late))}, name="forward")
    ).phases[0]
    backward = report.build_sanitised_report(
        write(tmp_path, {ON: Spec(results=(late, early))}, name="backward")
    ).phases[0]
    session = tagged(SESSION_TAG, FIXTURE_SESSION)
    status_value = schema.ColdFinalisationStatus
    branch = schema.ColdCapabilityBranch
    assert _finalisation_view(forward) == (
        session,
        status_value.CLEAN,
        True,
        False,
        branch.NON_STREAMING,
        list(zip(ON_ROLES, (10, 20, 30), strict=True)),
        11,
    )
    assert _finalisation_view(backward) == (
        session,
        status_value.PARTIAL,
        False,
        True,
        branch.STREAMING,
        [(role, 1) for role in ON_ROLES],
        12,
    )


def test_untrusted_capability_evidence_is_recorded_as_null(tmp_path: Path) -> None:
    """MCP fields: without trusted driver evidence the capability fields stay ``None``."""
    payload = result_for(OFF)
    payload["final_driver_evidence"] = {
        **payload["final_driver_evidence"],
        "outcome": "unreadable",
        "error": "driver_state_unreadable",
        "evidence": None,
    }
    phase = phase_of(tmp_path, OFF, results=(payload,))
    assert phase.session_id_sha256 == tagged(SESSION_TAG, FIXTURE_SESSION)
    assert phase.mcp_reported_finalisation_status is schema.ColdFinalisationStatus.CLEAN
    assert (phase.observed_command_streaming_required, phase.applied_branch) == (None, None)


#: One legal reason per abort domain, not every legal domain and reason pair.
ALL_ABORTS: tuple[tuple[schema.ColdAbortDomain, typing.Any], ...] = (
    (Domain.HOST, schema.ColdHostAbortReason.THERMAL_EXCEEDED),
    (Domain.IDENTITY, schema.ColdIdentityAbortReason.BOOT_ID_MALFORMED),
    (Domain.EVIDENCE, schema.ColdEvidenceFailure.RECORD_TOO_LARGE),
    (Domain.MCP, schema.ColdMcpAbortReason.EMERGENCY_STOP),
    (Domain.ADVISOR, Kind.PROVIDER_ERROR),
    (Domain.OPERATOR, OPERATOR_STOP),
)


def test_aborts_advisory_counts_and_host_extremes_come_from_the_bound_records(
    tmp_path: Path,
) -> None:
    """Records: aborts in file order, per-kind advisory counts, and host extremes."""
    hosts = (
        host(soc_temp_c=41.0, mem_available_bytes=900, free_bytes=7000),
        host(soc_temp_c=52.5, mem_available_bytes=1500, free_bytes=4000),
        host(soc_temp_c=-2.5, mem_available_bytes=1200, free_bytes=9000),
    )
    phase = phase_of(
        tmp_path,
        OFF,
        hosts=hosts,
        advisories=(None, Kind.TIMEOUT, Kind.UNSAFE_OUTPUT, Kind.TIMEOUT),
        aborts=ALL_ABORTS,
    )
    assert [(item.domain, item.reason) for item in phase.aborts] == list(ALL_ABORTS)
    assert phase.advisory_record_count == 4
    assert [(item.kind, item.count) for item in phase.advisor_failure_counts] == [
        (Kind.TIMEOUT, 2),
        (Kind.PROVIDER_ERROR, 0),
        (Kind.MALFORMED_OUTPUT, 0),
        (Kind.UNSAFE_OUTPUT, 1),
    ]
    assert phase.host_extremes == report.ColdReportHostExtremes(
        max_soc_temp_c=52.5, min_mem_available_bytes=900, min_free_bytes=4000
    )


def test_a_phase_without_advisories_or_aborts_counts_zero_of_each(tmp_path: Path) -> None:
    """Records: zero advisories is an observed count, one entry per failure kind."""
    phase = phase_of(tmp_path, OFF)
    assert phase.advisory_record_count == 0 and phase.aborts == ()
    assert [(item.kind, item.count) for item in phase.advisor_failure_counts] == [
        (kind, 0) for kind in Kind
    ]


@pytest.mark.parametrize(
    "hosts",
    [
        (),
        (host(mem_available_bytes=-1),),
        (host(), host(free_bytes=-1)),
    ],
    ids=["no-host-record", "negative-memory", "negative-free-bytes"],
)
def test_host_extremes_are_null_without_hosts_or_with_a_negative_byte_count(
    tmp_path: Path, hosts: tuple[HostBoundSample, ...]
) -> None:
    """Hosts: no host record, or any negative byte count, leaves the extremes ``None``."""
    assert phase_of(tmp_path, OFF, hosts=hosts).host_extremes is None


def forged_phase(**fields: typing.Any) -> acceptance.ColdReboundPhase:
    """Mint one rebound phase with the private token, bypassing strict rebinding.

    Only defensive projection tests use it, to place values that strict rebinding
    never yields; it leaves the rebinding code itself untouched.
    """
    values: dict[str, typing.Any] = {
        "phase": OFF,
        "header": schema.ColdRunHeader.model_construct(),
        "ticks": (),
        "hosts": (),
        "advisories": (),
        "finalisations": (),
        "aborts": (),
        "finalisation": None,
        "finalisation_ambiguous": False,
        "identity": store.ColdRetainedIdentityV1.model_construct(),
    }
    return acceptance.ColdReboundPhase(token=TOKEN, **(values | fields))


def test_host_extremes_never_admit_a_malformed_byte_count() -> None:
    """Hosts (defensive): a ``bool`` byte count is unavailable, never an ``int``."""

    def forged_host(**changes: typing.Any) -> acceptance.ColdReboundPhase:
        sample = schema.ColdHostSample.model_construct(**(host_sample().model_dump() | changes))
        return forged_phase(hosts=(schema.ColdHostRecord.model_construct(sample=sample),))

    assert host_extremes_of(forged_host()) is not None
    assert host_extremes_of(forged_host(mem_available_bytes=True)) is None


#: Readings strict rebinding never yields: refused (``inf``, ``bool``) or stored as ``float``.
MALFORMED_DURATIONS: tuple[tuple[str, object], ...] = (
    ("inf", float("inf")),
    ("bool", True),
    ("int", 9000),
)


@pytest.mark.parametrize(
    "value",
    [case[1] for case in MALFORMED_DURATIONS],
    ids=[case[0] for case in MALFORMED_DURATIONS],
)
def test_strict_rebinding_never_yields_a_malformed_duration_reading(
    tmp_path: Path, value: object
) -> None:
    """Precondition: normal rebinding is strict and unchanged, so the report never sees these.

    A tick carrying an ``inf`` or ``bool`` duration fails rebinding and an ``int`` is
    stored as ``float``; the strict mirror that parses the pre-finalisation and final
    snapshots does the same.
    """
    run = write(tmp_path, {OFF: Spec()})
    ticks = next(item for item in run.streams if item.phase is OFF and item.stream is Stream.TICK)
    last = typing.cast(schema.ColdTickRecord, ticks.records[-1])
    audio = last.audio.model_copy(update={"max_inference_duration_ms": value})
    stream = reader.ColdRetainedStream.model_construct(
        phase=OFF,
        stream=Stream.TICK,
        records=(*ticks.records[:-1], last.model_copy(update={"audio": audio})),
    )
    forged = rebuilt(run, streams=tuple(stream if item is ticks else item for item in run.streams))
    snapshot = status(max_inference_duration_ms=value)
    if type(value) is int:
        rebound = acceptance.interpret_retained_run(forged).rebound.phases[0]
        stored = rebound.ticks[-1].audio.max_inference_duration_ms
        mirrored = FinalisationFirstCrackStatus.model_validate(snapshot).max_inference_duration_ms
        assert [(type(item), item) for item in (stored, mirrored)] == [(float, 9000.0)] * 2
    else:
        expect_report_error(Failure.REBIND_FAILED, lambda: report.build_sanitised_report(forged))
        with pytest.raises(pydantic.ValidationError):
            FinalisationFirstCrackStatus.model_validate(snapshot)


@pytest.mark.parametrize(
    "value",
    [case[1] for case in MALFORMED_DURATIONS],
    ids=[case[0] for case in MALFORMED_DURATIONS],
)
@pytest.mark.parametrize("placement", ["tick", "pre-finalisation", "final"])
def test_a_malformed_duration_reading_is_unavailable_never_admitted(
    tmp_path: Path, placement: str, value: object
) -> None:
    """G17 maximum (defensive): an ``inf``, ``bool`` or ``int`` reading in ``S`` is ``None``.

    A forged capability places the reading in the last tick, the pre-finalisation
    snapshot or the final snapshot; the same forgery over the genuine series is the
    control, so the ``None`` comes from the malformed reading alone.
    """
    genuine = acceptance.interpret_retained_run(write(tmp_path, {OFF: Spec()})).rebound.phases[0]
    result = genuine.finalisation
    assert result is not None
    pre, evidence = result.pre_finalisation_first_crack_status, result.first_crack_runtime
    assert pre is not None and evidence is not None
    assert g17_maximum_of(forged_phase(ticks=genuine.ticks, finalisation=result)) == 3.0
    update = {"max_inference_duration_ms": value}
    ticks, finalisation = genuine.ticks, result
    if placement == "tick":
        audio = ticks[-1].audio.model_copy(update=update)
        ticks = (*ticks[:-1], ticks[-1].model_copy(update={"audio": audio}))
    elif placement == "pre-finalisation":
        changed = {"pre_finalisation_first_crack_status": pre.model_copy(update=update)}
        finalisation = result.model_copy(update=changed)
    else:
        final = evidence.final_status.model_copy(update=update)
        changed = {"first_crack_runtime": evidence.model_copy(update={"final_status": final})}
        finalisation = result.model_copy(update=changed)
    assert g17_maximum_of(forged_phase(ticks=ticks, finalisation=finalisation)) is None


def test_counters_come_from_the_final_snapshot(tmp_path: Path) -> None:
    """Counters: the five named counters are read from the frozen final snapshot."""
    final = final_status(
        emitted_window_count=21,
        processed_window_count=20,
        dropped_window_count=2,
        inference_overrun_count=3,
        total_overflow_count=4,
    )
    phase = phase_of(tmp_path, OFF, results=(result_for(OFF, first_crack_runtime=runtime(final)),))
    counters = phase.counters
    assert (
        counters.emitted,
        counters.processed,
        counters.dropped,
        counters.inference_overruns,
        counters.total_overflows,
    ) == (21, 20, 2, 3, 4)


MAXIMUM_CASES: list[tuple[str, dict[str, typing.Any]]] = [
    (
        "tick",
        {
            "ticks": (
                *default_ticks()[:2],
                tick_audio(
                    max_inference_duration_ms=6500.0,
                    emitted_window_count=3,
                    processed_window_count=3,
                ),
            )
        },
    ),
    (
        "pre-finalisation",
        {
            "results": (
                result_for(
                    OFF,
                    pre_finalisation_first_crack_status=status(max_inference_duration_ms=6500.0),
                ),
            )
        },
    ),
    (
        "final",
        {
            "results": (
                result_for(
                    OFF,
                    first_crack_runtime=runtime(final_status(max_inference_duration_ms=6500.0)),
                ),
            )
        },
    ),
]


@pytest.mark.parametrize(
    "spec", [case[1] for case in MAXIMUM_CASES], ids=[case[0] for case in MAXIMUM_CASES]
)
def test_the_maximum_inference_duration_is_taken_over_every_element(
    tmp_path: Path, spec: dict[str, typing.Any]
) -> None:
    """G17 maximum: the largest reading anywhere in ``S``, not a snapshot alone."""
    assert phase_of(tmp_path, OFF, **spec).counters.max_inference_duration_ms == 6500.0


NULLABLE = (
    "emitted",
    "processed",
    "dropped",
    "inference_overruns",
    "total_overflows",
    "max_inference_duration_ms",
    "session_id_sha256",
    "mcp_reported_finalisation_status",
    "mcp_reported_clean",
    "observed_command_streaming_required",
    "applied_branch",
    "recording_artefacts",
    "d191",
    "observed_tick_span_seconds",
)
COUNTERS = frozenset(NULLABLE[:6])
FINALISATION = frozenset(NULLABLE[6:11]) | {"recording_artefacts"}


def nullable_view(phase: report.ColdReportPhase) -> dict[str, object]:
    """Every value the report may leave ``None``, by name."""
    values = {**values_of(phase.counters), **values_of(phase)}
    return {name: values[name] for name in NULLABLE}


UNAVAILABLE_CASES: list[tuple[str, dict[str, typing.Any], frozenset[str]]] = [
    ("no-finalisation", {"results": ()}, COUNTERS | FINALISATION | {"d191"}),
    (
        "ambiguous-finalisation",
        {"results": (result_for(OFF), result_for(OFF, session_id="another-session"))},
        COUNTERS | FINALISATION | {"d191"},
    ),
    (
        "no-pre-finalisation",
        {"results": (result_for(OFF, pre_finalisation_first_crack_status=None),)},
        frozenset({"max_inference_duration_ms", "d191"}),
    ),
    (
        "no-runtime",
        {"results": (result_for(OFF, first_crack_runtime=None),)},
        COUNTERS | {"d191"},
    ),
    (
        "no-recording",
        {"results": (result_for(OFF, recording=None),)},
        frozenset({"recording_artefacts"}),
    ),
    (
        "no-ticks",
        {"ticks": ()},
        frozenset({"max_inference_duration_ms", "d191", "observed_tick_span_seconds"}),
    ),
    (
        "negative-final-counter",
        {
            "results": (
                result_for(OFF, first_crack_runtime=runtime(final_status(emitted_window_count=-1))),
            )
        },
        frozenset({"emitted"}),
    ),
    (
        "negative-tick-duration",
        {"ticks": (*default_ticks()[:2], tick_audio(max_inference_duration_ms=-1.0))},
        frozenset({"max_inference_duration_ms"}),
    ),
    (
        "negative-tick-lost-audio",
        {"ticks": (*default_ticks()[:2], tick_audio(estimated_lost_audio_ms_last_minute=-1.0))},
        frozenset({"d191"}),
    ),
]


@pytest.mark.parametrize(
    ("spec", "unavailable"),
    [case[1:] for case in UNAVAILABLE_CASES],
    ids=[case[0] for case in UNAVAILABLE_CASES],
)
def test_absent_or_negative_values_are_none_never_zero(
    tmp_path: Path, spec: dict[str, typing.Any], unavailable: frozenset[str]
) -> None:
    """Unavailable: absent or negative evidence is ``None``; everything else is present."""
    view = nullable_view(phase_of(tmp_path, OFF, **spec))
    assert {name for name, value in view.items() if value is None} == unavailable


#: Non-zero final D191 inputs, so a voided or zero-filled derivation cannot pass unseen.
D191_FINAL: Json = {
    "max_consecutive_overflow_count": 2,
    "total_overflow_count": 5,
    "estimated_lost_audio_ms_last_minute": 40.0,
}


def d191_result(snapshot: str = "final", **reading: object) -> Json:
    """A recording-off result with non-zero D191 inputs and optional replaced readings."""
    pre = status(**(reading if snapshot == "pre" else {}))
    final = final_status(**(D191_FINAL | (reading if snapshot == "final" else {})))
    return result_for(
        OFF, pre_finalisation_first_crack_status=pre, first_crack_runtime=runtime(final)
    )


#: One negative reading per case: the five final counters, then both snapshot durations.
NEGATIVE_READINGS: list[tuple[str, str, str, float, str]] = [
    ("final-emitted", "final", "emitted_window_count", -1, "emitted"),
    ("final-processed", "final", "processed_window_count", -1, "processed"),
    ("final-dropped", "final", "dropped_window_count", -1, "dropped"),
    ("final-overruns", "final", "inference_overrun_count", -1, "inference_overruns"),
    ("final-total-overflow", "final", "total_overflow_count", -1, "total_overflows"),
    ("pre-duration", "pre", "max_inference_duration_ms", -1.0, "max_inference_duration_ms"),
    ("final-duration", "final", "max_inference_duration_ms", -1.0, "max_inference_duration_ms"),
]


@pytest.mark.parametrize(
    ("snapshot", "source", "value", "reported"),
    [case[1:] for case in NEGATIVE_READINGS],
    ids=[case[0] for case in NEGATIVE_READINGS],
)
def test_a_negative_snapshot_reading_is_null_and_only_total_overflow_voids_d191(
    tmp_path: Path, snapshot: str, source: str, value: float, reported: str
) -> None:
    """Unavailable: a negative final counter or snapshot duration is ``None``, never zero.

    The merged D191 derivation reads both overflow counters and the trailing lost
    audio over ``S``, so a negative final total overflow voids it; the other four
    final counters and the durations are not D191 inputs, so the metrics stand.
    """
    clean_run = write(tmp_path, {OFF: Spec(results=(d191_result(),))}, name="clean")
    negative_result = d191_result(snapshot, **{source: value})
    negative_run = write(tmp_path, {OFF: Spec(results=(negative_result,))}, name="negative")
    clean = report.build_sanitised_report(clean_run).phases[0]
    negative = report.build_sanitised_report(negative_run).phases[0]
    assert clean.d191 == acceptance.ColdD191Metrics(
        max_consecutive_overflow_count=2, peak_trailing_lost_audio_ms=40.0
    )
    assert None not in values_of(clean.counters).values()
    assert negative.counters == clean.counters.model_copy(update={reported: None})
    assert negative.d191 == (None if source == "total_overflow_count" else clean.d191)


def test_recording_artefact_sizes_are_null_when_absent_or_negative(tmp_path: Path) -> None:
    """Recording: roles in order; a missing or negative size is ``None``, zero stays zero."""
    artifacts = [
        artefact("primary_wav", size_bytes=None),
        artefact("recording_sidecar", size_bytes=-1),
        artefact("annotation_session_sidecar", size_bytes=0),
        artefact("additional_wav", size_bytes=7),
    ]
    phase = phase_of(tmp_path, ON, results=(result_for(ON, recording=recording_on(artifacts)),))
    assert [(item.role, item.size_bytes) for item in phase.recording_artefacts or ()] == [
        ("primary_wav", None),
        ("recording_sidecar", None),
        ("annotation_session_sidecar", 0),
        ("additional_wav", 7),
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        ((2.0, 3.0, 4.5), 2.5),
        ((2.0, 100.0, 3.0), 1.0),
        ((5.0, 5.0), 0.0),
        ((9.0, 4.0, 1.0), None),
        ((3.0,), None),
        ((), None),
    ],
    ids=["rising", "last-minus-first", "zero", "negative", "one-tick", "no-ticks"],
)
def test_the_tick_span_is_last_minus_first(
    tmp_path: Path, seconds: tuple[float, ...], expected: float | None
) -> None:
    """Span: last minus first monotonic seconds; ``None`` below two ticks or when negative."""
    ticks = tuple(
        tick_audio(emitted_window_count=index + 1, processed_window_count=index + 1)
        for index in range(len(seconds))
    )
    phase = phase_of(tmp_path, OFF, ticks=ticks, seconds=seconds)
    assert (phase.tick_count, phase.observed_tick_span_seconds) == (len(seconds), expected)


def test_identity_facts_appear_exactly_when_qualification_passes(tmp_path: Path) -> None:
    """Facts: present with an admissible identity; ``None`` after any Q failure."""
    clean = report.build_sanitised_report(write(tmp_path, {OFF: Spec()}, name="clean"))
    facts = clean.phases[0].identity_facts
    assert clean.phases[0].checks[0].outcome is Outcome.PASS
    assert facts is not None and facts.source_revision == "b" * 40
    for name, mutate, failure in (
        ("dirty", at("build_provenance.source_tree_dirty", True), F.Q_SOURCE_TREE_DIRTY),
        ("fahrenheit", at("runtime_config.temperature_unit", "fahrenheit"), F.Q_TEMPERATURE_UNIT),
    ):
        run = write(tmp_path, {OFF: Spec(document=mutate)}, name=name)
        phase = report.build_sanitised_report(run).phases[0]
        assert phase.checks[0].failures == (failure,)
        assert phase.identity_facts is None


# ------------------------------------------------------------------- rendering


def assert_key_sets(model: pydantic.BaseModel, document: object) -> int:
    """Assert each nested object's keys equal its model's fields; return the objects walked."""
    assert type(document) is dict
    parsed = typing.cast(Json, document)
    names = tuple(type(model).model_fields)
    assert set(parsed) == set(names)
    walked = 1
    for name in names:
        value: object = getattr(model, name)
        if isinstance(value, pydantic.BaseModel):
            walked += assert_key_sets(value, parsed[name])
        elif isinstance(value, tuple):
            items = typing.cast(tuple[object, ...], value)
            children = typing.cast(list[object], parsed[name])
            assert len(children) == len(items)
            walked += sum(
                assert_key_sets(item, child)
                for item, child in zip(items, children, strict=True)
                if isinstance(item, pydantic.BaseModel)
            )
    return walked


def test_the_json_is_the_canonical_report_and_round_trips(tmp_path: Path) -> None:
    """Renderer: canonical UTF-8 bytes of the built report; nested key sets are the fields.

    One legal reason per abort domain (six domain and reason pairs, not every legal
    pair) round-trips through the strict JSON parse.
    """
    off = Spec(hosts=(host(),), advisories=(None, Kind.TIMEOUT), aborts=ALL_ABORTS)
    run = write(tmp_path, {OFF: off, ON: Spec()})
    document, markdown = report.render_sanitised_report(run)
    built = report.build_sanitised_report(run)
    assert document == store.canonical_json(built.model_dump(mode="json")).encode("utf-8")
    assert report.ColdSanitisedReport.model_validate_json(document) == built
    assert [(item.domain, item.reason) for item in built.phases[0].aborts] == list(ALL_ABORTS)
    # Root and limits, then per phase: itself, five checks, D191, counters, aborts,
    # four advisor counts, host extremes, artefacts and facts (20 off, 16 on).
    assert assert_key_sets(built, json.loads(document)) == 38
    assert report.render_sanitised_report(run) == (document, markdown)


def test_markdown_rows_render_the_report_values(tmp_path: Path) -> None:
    """Markdown: fixed rows over the report values; null and absent sections are marked."""
    run = write(
        tmp_path, {OFF: Spec(hosts=(host(),), results=(finalisation_payload(),)), ON: Spec()}
    )
    built = report.build_sanitised_report(run)
    lines = report.render_sanitised_report(run)[1].splitlines()
    off = built.phases[0]
    for line in (
        "# Cold characterisation summary (report schema 1)",
        "## Run",
        f"| run_id_sha256 | `{built.run_id_sha256}` |",
        f"| manifest_sha256 | `{built.manifest_sha256}` |",
        "| phases_absent | none |",
        RUN_SECTION,
        "| label | `not compared` |",
        "| n | 1 |",
        "| x_ms | 200.0 |",
        "| fatal_streak | 30 |",
        "| hop_seconds | 7.0 |",
        "## Phase `recording_off`",
        f"| identity_sha256 | `{off.identity_sha256}` |",
        "| tick_count | 3 |",
        "| observed_tick_span_seconds | 2.0 |",
        "| mcp_reported_finalisation_status | `clean` |",
        "| mcp_reported_clean | true |",
        "| observed_command_streaming_required | false |",
        "| applied_branch | `non_streaming` |",
        "| `identity_qualification_v1` | `pass` | none |",
        "| `inference_runtime` | `fail` | `inference_not_active`, `no_processed_window`, "
        "`microphone_or_fatal_error` |",
        "### `d191` (derived, not compared)",
        "| emitted | 0 |",
        "| max_inference_duration_ms | 3.0 |",
        "| `timeout` | 0 |",
        "| max_soc_temp_c | 45.5 |",
        "| audio_hop_seconds | `null` |",
        "## Phase `recording_on`",
        "| `primary_wav` | 4096 |",
    ):
        assert line in lines, line
    d191 = lines.index("### `d191` (derived, not compared)")
    assert lines[d191 + 2] == "| Field | Value |"
    assert off.d191 is not None


def test_absent_sections_are_marked_null_and_empty_ones_none(tmp_path: Path) -> None:
    """Markdown: a ``None`` section reads as null (unavailable); an empty tuple as none."""
    run = write(tmp_path, {OFF: Spec(results=(), document=at("boot_id", "not-a-boot-id"))})
    lines = report.render_sanitised_report(run)[1].splitlines()
    for heading, body in (
        ("### `d191` (derived, not compared)", "`null` (unavailable)"),
        ("### `host_extremes` (recorded, not compared)", "`null` (unavailable)"),
        ("### `recording_artefacts` (stat only)", "`null` (unavailable)"),
        (
            "### `identity_facts` (shown only when identity check v1 has no failure)",
            "`null` (unavailable)",
        ),
        ("### `aborts` (closed classifications)", "none"),
    ):
        assert lines[lines.index(heading) + 2] == body, heading


RUN_SHAPES: list[tuple[str, typing.Callable[[], dict[schema.ColdPhaseKind, Spec]]]] = [
    ("both-phases-every-record", full_spec),
    (
        "phase-one-aborted",
        lambda: {OFF: Spec(results=(), aborts=((Domain.OPERATOR, OPERATOR_STOP),))},
    ),
    (
        "ambiguous-finalisation",
        lambda: {ON: Spec(results=(result_for(ON), result_for(ON, session_id="other")))},
    ),
    (
        "disabled-fixture",
        lambda: {
            OFF: Spec(results=(finalisation_payload(),)),
            ON: Spec(results=(finalisation_payload(),)),
        },
    ),
    ("no-ticks-no-finalisation", lambda: {ON: Spec(ticks=(), results=())}),
    (
        "inadmissible-identity",
        lambda: {OFF: Spec(document=at("build_provenance.source_tree_dirty", True))},
    ),
]
CELL = re.compile(
    r"\A(?:`[a-z0-9_. ]+`(?:, `[a-z0-9_. ]+`)*|-?[0-9][0-9.e+-]*"
    r"|true|false|none|Field|Value|[a-z][a-z0-9_]*)\Z"
)


@pytest.mark.parametrize(
    "shape", [case[1] for case in RUN_SHAPES], ids=[case[0] for case in RUN_SHAPES]
)
def test_the_text_is_fixed_protocol_intent_and_never_a_claim(
    tmp_path: Path, shape: typing.Callable[[], dict[schema.ColdPhaseKind, Spec]]
) -> None:
    """Text: five verbatim sentences, fixed headings and cells, and no forbidden phrase."""
    document, markdown = report.render_sanitised_report(write(tmp_path, shape()))
    for sentence in PROTOCOL_INTENT:
        assert markdown.count(sentence) == 1, sentence
    lowered = f"{document.decode('utf-8')}\n{markdown}".lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in lowered, phrase
    for line in filter(None, markdown.splitlines()):
        if line.startswith("#"):
            assert line in FIXED_HEADINGS, line
        elif line.startswith("|"):
            if re.fullmatch(r"\|(?:---\|)+", line) is None:
                assert all(CELL.fullmatch(item) for item in line[2:-2].split(" | ")), line
        else:
            assert line in (*PROTOCOL_INTENT, "none", "`null` (unavailable)"), line


def test_the_heading_notes_are_exactly_the_nine_fixed_notes_in_order(tmp_path: Path) -> None:
    """Text: the nine fixed heading notes, in order; an added overall-pass note fails.

    The checks note says a per-check pass is not evidence of sustained inference, run
    duration or run qualification.  The locked-limits note says G17 already compares
    against the fixed seven-second hop and no recorded limit is compared against the
    recorded profile; the JSON label stays ``not compared``.
    """
    assert report._SECTION_NOTES == SECTION_NOTES  # pyright: ignore[reportPrivateUsage]
    document, markdown = report.render_sanitised_report(write(tmp_path, full_spec()))
    sections = [line for line in markdown.splitlines() if re.match(r"#{2,3} `", line)]
    assert sections == [RUN_SECTION, *PHASE_SECTIONS, *PHASE_SECTIONS]
    assert json.loads(document)["locked_limits"]["label"] == "not compared"


#: Module-doc disclosures: the meaning of "not compared", the per-check caveat, and the
#: two named privacy residuals (hex commitments, linkable and guessable digests).
MODULE_DISCLOSURES = (
    '"Not compared" means not compared by this report projection: the G17 '
    "inference-duration check already compares against the fixed seven-second hop, and "
    "no rendered limit is compared against the recorded profile.",
    "A per-check pass is not evidence of sustained inference, run duration or run qualification.",
    "Named privacy residuals, disclosed rather than waived: when identity qualification "
    "passes, the caller-asserted hexadecimal commitments (the 40-character source revision "
    "and the 64-character artefact and profile-source digests) are rendered.",
    "They are accepted by shape alone, so any of them could be a hex-shaped secret, and "
    "nothing here proves that they are real commits or digests.",
    "The run and session identifiers are rendered only as tagged SHA-256 digests, but "
    "those digests are deterministic: equal identifiers remain linkable across reports, "
    "and a predictable identifier can be guessed by hashing candidates, so the digests "
    "do not keep identifiers secret.",
)


def test_module_docs_disclose_the_limit_meaning_and_the_privacy_residuals() -> None:
    """Docs: the module states each disclosure verbatim, whitespace aside."""
    text = " ".join((report.__doc__ or "").split())
    for sentence in MODULE_DISCLOSURES:
        assert sentence in text, sentence


def test_every_markdown_value_passes_through_the_scalar_renderer() -> None:
    """Markdown (defensive): the cell renderer refuses anything but a report scalar."""
    assert [cell(value) for value in (None, True, False, 3, 2.5, "ab", (), (OFF, ON))] == [
        "`null`",
        "true",
        "false",
        "3",
        "2.5",
        "`ab`",
        "none",
        "`recording_off`, `recording_on`",
    ]
    for value in (
        object(),
        b"bytes",
        report.ColdReportAdvisorFailureCount(kind=Kind.TIMEOUT, count=0),
    ):
        with pytest.raises(TypeError):
            cell(value)


# ---------------------------------------------------------------- fail closed


def _misbound_tick(run: reader.ColdRetainedRun) -> object:
    """Replace the recording-off ticks with one whose identity digest does not bind."""
    ticks = next(item for item in run.streams if item.phase is OFF and item.stream is Stream.TICK)
    tick = typing.cast(schema.ColdTickRecord, ticks.records[0])
    forged = reader.ColdRetainedStream.model_construct(
        phase=OFF,
        stream=Stream.TICK,
        records=(tick.model_copy(update={"identity_sha256": "0" * 64}),),
    )
    return rebuilt(run, streams=tuple(forged if item is ticks else item for item in run.streams))


REBIND_FORGERIES: list[
    tuple[
        str, acceptance.ColdInterpretationFailure, typing.Callable[[reader.ColdRetainedRun], object]
    ]
] = [
    (
        "container-malformed",
        Refusal.CONTAINER_MALFORMED,
        lambda run: rebuilt(run, run_id="../" + CANARY),
    ),
    ("record-rebind-failed", Refusal.RECORD_REBIND_FAILED, _misbound_tick),
    (
        "header-set-mismatched",
        Refusal.HEADER_SET_MISMATCHED,
        lambda run: rebuilt(run, headers=run.headers[:1]),
    ),
    (
        "no-phase-present",
        Refusal.NO_PHASE_PRESENT,
        lambda run: rebuilt(run, headers=(), streams=()),
    ),
]


@pytest.mark.parametrize(
    ("refusal", "forge"),
    [case[1:] for case in REBIND_FORGERIES],
    ids=[case[0] for case in REBIND_FORGERIES],
)
def test_each_interpretation_refusal_is_a_content_free_rebind_failure(
    tmp_path: Path,
    refusal: acceptance.ColdInterpretationFailure,
    forge: typing.Callable[[reader.ColdRetainedRun], object],
) -> None:
    """Refusal: every closed interpretation refusal becomes ``REBIND_FAILED``, chain-free."""
    root, sealed, _records = write_full_run(tmp_path)
    run = reader.read_retained_run(
        root, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    forged = typing.cast(reader.ColdRetainedRun, forge(run))
    with pytest.raises(acceptance.ColdInterpretationError) as raised:
        acceptance.interpret_retained_run(forged)
    assert raised.value.failure is refusal
    for call in (report.build_sanitised_report, report.render_sanitised_report):
        error = expect_report_error(Failure.REBIND_FAILED, lambda call=call: call(forged))
        assert CANARY not in rendered(error)


def test_a_truncated_run_still_reports_under_the_honest_limit(tmp_path: Path) -> None:
    """R6: removed ticks go undetected; the digest is carried and sentence 5 is stated."""
    run = write(tmp_path, {OFF: Spec()})
    ticks = next(item for item in run.streams if item.phase is OFF and item.stream is Stream.TICK)
    shortened = reader.ColdRetainedStream(phase=OFF, stream=Stream.TICK, records=ticks.records[:1])
    truncated = rebuilt(
        run, streams=tuple(shortened if item is ticks else item for item in run.streams)
    )
    built = report.build_sanitised_report(truncated)
    assert built.phases[0].tick_count == 1
    assert built.manifest_sha256 == run.manifest_sha256
    assert PROTOCOL_INTENT[4] in report.render_sanitised_report(truncated)[1]


def test_a_non_finite_tick_span_is_not_admitted(tmp_path: Path) -> None:
    """Refusal: finite tick times whose span overflows are refused, never rendered."""
    run = write(tmp_path, {OFF: Spec(ticks=default_ticks()[:2], seconds=(-1.7e308, 1.7e308))})
    expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda: report.build_sanitised_report(run))
    expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda: report.render_sanitised_report(run))


def _raise_value_error(_run: reader.ColdRetainedRun) -> typing.NoReturn:
    raise ValueError(CANARY)


def _raise_type_error(_run: reader.ColdRetainedRun) -> typing.NoReturn:
    raise TypeError(CANARY)


def _raise_overflow(_run: reader.ColdRetainedRun) -> typing.NoReturn:
    raise OverflowError(CANARY)


def _raise_validation_error(_run: reader.ColdRetainedRun) -> typing.NoReturn:
    report.ColdReportCounters.model_validate({"emitted": CANARY})
    raise AssertionError("unreachable")  # pragma: no cover - validation raises first


@pytest.mark.parametrize(
    "raising",
    [_raise_value_error, _raise_type_error, _raise_overflow, _raise_validation_error],
    ids=["value-error", "type-error", "overflow", "pydantic-validation"],
)
def test_value_errors_during_interpretation_are_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raising: typing.Callable[[reader.ColdRetainedRun], typing.NoReturn],
) -> None:
    """Containment: interpretation value errors become ``VALUE_NOT_ADMITTED`` without text."""
    run = write(tmp_path, {OFF: Spec()})
    monkeypatch.setattr(report, "interpret_retained_run", raising)
    for call in (report.build_sanitised_report, report.render_sanitised_report):
        error = expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda call=call: call(run))
        assert CANARY not in rendered(error)


def test_projection_errors_are_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Containment: a value the report schema refuses becomes ``VALUE_NOT_ADMITTED``."""
    run = write(tmp_path, {OFF: Spec()})

    def infinite(_rebound: acceptance.ColdReboundPhase) -> float:
        return float("inf")

    monkeypatch.setattr(report, "_tick_span", infinite)
    expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda: report.build_sanitised_report(run))


def test_interpreted_and_rebound_phases_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment (defensive): a misaligned interpretation is refused, never reported.

    Recording-on results paired with recording-off evidence would otherwise build a
    well-ordered report; a phase-count mismatch is refused as well.
    """
    run = write(tmp_path, {OFF: Spec()}, name="off")
    rebound = acceptance.interpret_retained_run(run).rebound
    on_items = acceptance.interpret_retained_run(write(tmp_path, {ON: Spec()}, name="on")).phases
    both = acceptance.interpret_retained_run(write(tmp_path, full_spec(), name="both")).phases
    for phases in (on_items, both):
        forged = acceptance.ColdInterpretation(token=TOKEN, rebound=rebound, phases=phases)

        def misaligned(
            _run: reader.ColdRetainedRun, forged: acceptance.ColdInterpretation = forged
        ) -> acceptance.ColdInterpretation:
            return forged

        monkeypatch.setattr(report, "interpret_retained_run", misaligned)
        expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda: report.build_sanitised_report(run))


def test_markdown_errors_are_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Containment: a Markdown rendering error becomes ``VALUE_NOT_ADMITTED`` without text."""
    run = write(tmp_path, {OFF: Spec()})

    def refuse(_report: report.ColdSanitisedReport) -> typing.NoReturn:
        raise TypeError(CANARY)

    monkeypatch.setattr(report, "_markdown", refuse)
    error = expect_report_error(
        Failure.VALUE_NOT_ADMITTED, lambda: report.render_sanitised_report(run)
    )
    assert CANARY not in rendered(error)


def _set_extra(document: Json) -> None:
    document["extra"] = CANARY


def _drop_label(document: Json) -> None:
    del document["locked_limits"]["label"]


def _limits_scalar(document: Json) -> None:
    document["locked_limits"] = 1


def _phases_object(document: Json) -> None:
    document["phases"] = {}


def _phases_shortened(document: Json) -> None:
    document["phases"] = document["phases"][:1]


def _version_object(document: Json) -> None:
    document["report_schema_version"] = {}


def _digest_array(document: Json) -> None:
    document["manifest_sha256"] = []


def _check_extra(document: Json) -> None:
    document["phases"][0]["checks"][0][CANARY] = 1


def _fact_dropped(document: Json) -> None:
    del document["phases"][1]["identity_facts"]["mcp_version"]


TAMPERS: list[Mutation] = [
    _set_extra,
    _drop_label,
    _limits_scalar,
    _phases_object,
    _phases_shortened,
    _version_object,
    _digest_array,
    _check_extra,
    _fact_dropped,
]


@pytest.mark.parametrize("tamper", TAMPERS, ids=[tamper.__name__.strip("_") for tamper in TAMPERS])
def test_a_tampered_egress_key_set_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: Mutation
) -> None:
    """Egress: a re-parsed key set that differs from its model is refused, content-free."""
    run = write(tmp_path, {OFF: Spec(), ON: Spec()})
    real = store.canonical_json

    def tampered(value: object) -> str:
        document = copy.deepcopy(typing.cast(Json, value))
        tamper(document)
        return real(document)

    monkeypatch.setattr(report, "canonical_json", tampered)
    error = expect_report_error(
        Failure.EGRESS_KEYSET_MISMATCH, lambda: report.render_sanitised_report(run)
    )
    assert CANARY not in rendered(error)
    assert report.build_sanitised_report(run).report_schema_version == 1


def test_unparseable_egress_is_not_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Egress: bytes that do not re-parse are refused as ``VALUE_NOT_ADMITTED``."""
    run = write(tmp_path, {OFF: Spec()})

    def truncated(_value: object) -> str:
        return "{" + CANARY

    monkeypatch.setattr(report, "canonical_json", truncated)
    error = expect_report_error(
        Failure.VALUE_NOT_ADMITTED, lambda: report.render_sanitised_report(run)
    )
    assert CANARY not in rendered(error)


# ------------------------------------------------------------- canary sweep


#: Identity strings a qualifying v1 identity must keep; every other string is a canary.
IDENTITY_KEEP: frozenset[tuple[str, ...]] = frozenset(
    {
        ("run_id",),
        ("pi_evidence_root",),
        ("coffee_roaster_mcp_version",),
        ("credential_env_var_name",),
        ("boot_id",),
        ("runtime_config", "temperature_unit"),
        ("runtime_config", "first_crack_mode"),
        ("runtime_config", "model_precision"),
        ("device_config", "fc_mode"),
        ("device_config", "ambient_mode"),
        ("build_provenance", "source_revision"),
        ("build_provenance", "artefact_kind"),
        ("build_provenance", "artefact_sha256"),
        ("effective_mcp_profile", "source_sha256"),
    }
)


class Canaries:
    """Mint unique, non-hexadecimal canaries and remember every one minted."""

    def __init__(self, *, credential_shaped: bool) -> None:
        """Choose plain or ``sk-``-shaped canaries."""
        self.credential_shaped = credential_shaped
        self.minted: list[str] = []

    def __call__(self) -> str:
        """Return the next canary."""
        index = len(self.minted)
        text = (
            f"sk-zqx{index:04d}qzxAbCdEfGhIjKlMn"
            if self.credential_shaped
            else f"zqx{index:04d}qzx"
        )
        self.minted.append(text)
        return text


def replace_strings(value: typing.Any, path: tuple[str, ...], mint: Canaries) -> typing.Any:
    """Replace every string leaf outside ``IDENTITY_KEEP`` with a fresh canary."""
    if type(value) is str:
        return value if path in IDENTITY_KEEP else mint()
    if type(value) is dict:
        mapping = typing.cast(Json, value)
        return {key: replace_strings(child, (*path, key), mint) for key, child in mapping.items()}
    if type(value) is list:
        return [
            replace_strings(child, path, mint) for child in typing.cast(list[typing.Any], value)
        ]
    return value


def canaried_identity(document: Json, mint: Canaries) -> Json:
    """A v1 identity whose every free-text value and extras key or value is a canary."""
    for key in ("serial_port", "roaster_driver", "audio_input_device", "mcp_yaml_source_path"):
        document["device_config"][key] = "text"
    document["device_config"]["ambient_device"] = "text"
    document["runtime_config"]["config_source"] = "text"
    changed = typing.cast(Json, replace_strings(document, (), mint))
    changed["boot_id"] = BOOT_ID_CANARY
    changed["runtime_config"][mint()] = mint()
    changed["server_info"][mint()] = {mint(): mint()}
    return changed


def string_paths(
    value: typing.Any, path: tuple[str | int, ...] = ()
) -> list[tuple[str | int, ...]]:
    """Every path to a string leaf of one JSON value."""
    if type(value) is str:
        return [path]
    if type(value) is dict:
        mapping = typing.cast(Json, value)
        return [p for key, child in mapping.items() for p in string_paths(child, (*path, key))]
    if type(value) is list:
        items = typing.cast(list[typing.Any], value)
        return [p for index, child in enumerate(items) for p in string_paths(child, (*path, index))]
    return []


def set_path(document: typing.Any, path: tuple[str | int, ...], value: str) -> None:
    """Set one nested leaf."""
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value


def canaried_result(phase: schema.ColdPhaseKind, mint: Canaries) -> Json:
    """A finalisation result whose every string the strict mirror admits freely is a canary."""
    payload = result_for(phase)
    payload["failures"] = [
        {
            "stage": "recording",
            "code": "text",
            "message": "text",
            "attempt_number": 1,
            "recorded_at_utc": "text",
        }
    ]
    for stage in payload["stages"]:
        stage["detail"] = "text"
    payload["first_crack_runtime"]["stop_error"] = "text"
    payload["sampler"]["last_error"] = "text"
    payload["disconnect"]["last_error"] = "text"
    admission = payload["admission_driver_evidence"]
    admission["error"] = "text"
    admission["evidence"]["non_zero_dimensions"] = ["text"]
    for snapshot in (
        payload["pre_finalisation_first_crack_status"],
        payload["first_crack_runtime"]["final_status"],
    ):
        snapshot["reason"] = "text"
        snapshot["detected_at_utc"] = "text"
    if phase is ON:
        payload["recording"]["reason"] = "text"
    for path in string_paths(payload):
        probe = copy.deepcopy(payload)
        set_path(probe, path, "probe-text")
        try:
            SessionFinalisationResult.model_validate_json(json.dumps(probe))
        except pydantic.ValidationError:
            continue
        set_path(payload, path, mint())
    return payload


def write_canary_run(
    tmp_path: Path, mint: Canaries, run_id: str
) -> tuple[str, reader.ColdRetainedRun]:
    """Write both phases with a canary in every omitted source; return the root and run."""
    root = make_root(tmp_path, "pi")
    writer = store.open_run(store.admit_evidence_root(root), run_id)
    for phase in schema.ColdPhaseKind:
        document = identity_document(tmp_path, root)
        document["run_id"] = run_id
        header = header_of(canaried_identity(document, mint), phase, run_id, text=mint())
        writer.append(header)
        for index in range(2):
            audio = {
                **tick_audio(
                    emitted_window_count=index + 1,
                    processed_window_count=index + 1,
                    detected_at_utc=mint(),
                    reason=mint(),
                ),
                mint(): {mint(): [mint(), 1.5]},
            }
            writer.append(
                tick_record(header, index, audio, 2.0 + index, text=mint(), vendor={mint(): mint()})
            )
        writer.append(
            builders.build_host_record(
                header=header,
                sample=host(captured_at_utc=mint(), throttled_word_hex=mint()),
                recorded_at_utc=mint(),
                monotonic_seconds=2.0,
            )
        )
        writer.append(
            advisory_record(header, Kind.PROVIDER_ERROR, rule=mint(), reason=mint(), text=mint())
        )
        writer.append(finalisation_record(header, canaried_result(phase, mint), 0, text=mint()))
        writer.append(abort_for(header))
    sealed = writer.seal()
    return root, reader.read_retained_run(
        root, run_id=run_id, expected_manifest_sha256=sealed.manifest_sha256
    )


def retained_text(root: str, run_id: str) -> str:
    """All retained evidence bytes of one run, decoded."""
    directory = Path(root) / run_id
    return "".join(
        path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*")) if path.is_file()
    )


@pytest.mark.parametrize("credential_shaped", [False, True], ids=["screen-passing", "sk-shaped"])
def test_no_omitted_source_reaches_the_json_or_the_markdown(
    tmp_path: Path, credential_shaped: bool
) -> None:
    """Canary: every omitted source is retained privately yet absent from both outputs."""
    mint = Canaries(credential_shaped=credential_shaped)
    suffix = "sk-zqx9999qzxabcdefghijklmn" if credential_shaped else "zqx9999qzx"
    run_id = f"20260926T120000Z-{suffix}"
    root, run = write_canary_run(tmp_path, mint, run_id)
    document, markdown = report.render_sanitised_report(run)
    built = report.build_sanitised_report(run)
    assert all((item.identity_facts is None) is credential_shaped for item in built.phases)
    assert all(item.session_id_sha256 is not None for item in built.phases)
    assert len(mint.minted) > 150
    private = retained_text(root, run_id)
    secrets = (*mint.minted, run_id, suffix, BOOT_ID_CANARY, root, str(tmp_path))
    assert all(secret in private for secret in (*mint.minted, BOOT_ID_CANARY, root))
    public = document.decode("utf-8")
    for secret in secrets:
        assert secret not in public, secret
        assert secret not in markdown, secret


def test_report_errors_carry_no_canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Canary: every error kind raised over a canary-laden run is content-free."""
    mint = Canaries(credential_shaped=True)
    _root, run = write_canary_run(tmp_path, mint, "20260926T120000Z-zqx9998qzx")
    errors = [
        expect_report_error(
            Failure.REBIND_FAILED,
            lambda: report.render_sanitised_report(rebuilt(run, run_id="../" + mint())),
        )
    ]
    real = store.canonical_json

    def keyed(value: object) -> str:
        return real({**typing.cast(Json, value), mint(): mint()})

    monkeypatch.setattr(report, "canonical_json", keyed)
    errors.append(
        expect_report_error(
            Failure.EGRESS_KEYSET_MISMATCH, lambda: report.render_sanitised_report(run)
        )
    )
    monkeypatch.undo()
    secrets = " ".join(mint.minted)

    def raising(_run: reader.ColdRetainedRun) -> typing.NoReturn:
        raise ValueError(secrets)

    monkeypatch.setattr(report, "interpret_retained_run", raising)
    errors.append(
        expect_report_error(Failure.VALUE_NOT_ADMITTED, lambda: report.build_sanitised_report(run))
    )
    for error in errors:
        text = f"{rendered(error)}{error.failure!r}"
        assert not any(secret in text for secret in mint.minted)


# ------------------------------------------------------------ schema closure

REPORT_MODELS: tuple[type[pydantic.BaseModel], ...] = (
    report.ColdSanitisedReport,
    report.ColdReportLockedLimits,
    report.ColdReportPhase,
    report.ColdReportCheck,
    report.ColdReportCounters,
    report.ColdReportAbort,
    report.ColdReportAdvisorFailureCount,
    report.ColdReportHostExtremes,
    report.ColdReportRecordingArtefact,
)


def schema_nodes(node: object) -> typing.Iterator[dict[str, object]]:
    """Every mapping in a compiled pydantic core schema."""
    if type(node) is dict:
        mapping = typing.cast(dict[str, object], node)
        yield mapping
        for child in mapping.values():
            yield from schema_nodes(child)
    elif type(node) is list:
        for child in typing.cast(list[object], node):
            yield from schema_nodes(child)


def test_every_string_leaf_is_an_enum_a_literal_or_an_anchored_hex_digest() -> None:
    """Closure: reflecting on the compiled schema, no string leaf is free text."""
    nodes = list(schema_nodes(report.ColdSanitisedReport.__pydantic_core_schema__))
    kinds = {node.get("type") for node in nodes}
    patterns = {node.get("pattern") for node in nodes if node.get("type") == "str"}
    assert patterns == {r"\A[0-9a-f]{64}\z", r"\A[0-9a-f]{40}\z"}
    assert {"enum", "literal", "model"} <= kinds
    assert not kinds & {"any", "dict", "bytes", "json", "url", "callable", "is-instance"}
    strings = [
        node
        for node in schema_nodes(report.ColdSanitisedReport.model_json_schema())
        if node.get("type") == "string"
    ]
    assert strings
    assert all({"pattern", "enum", "const"} & set(node) for node in strings)


@pytest.mark.parametrize("model", REPORT_MODELS, ids=[model.__name__ for model in REPORT_MODELS])
def test_report_models_are_strict_frozen_closed_and_finite(
    clean_report: report.ColdSanitisedReport, model: type[pydantic.BaseModel]
) -> None:
    """Closure: each model pins its config, refuses unknown fields, and refuses assignment."""
    config = model.model_config
    assert (
        config.get("strict"),
        config.get("frozen"),
        config.get("extra"),
        config.get("allow_inf_nan"),
    ) == (True, True, "forbid", False)
    off = clean_report.phases[0]
    instances: dict[type[pydantic.BaseModel], pydantic.BaseModel] = {
        report.ColdSanitisedReport: clean_report,
        report.ColdReportLockedLimits: clean_report.locked_limits,
        report.ColdReportPhase: off,
        report.ColdReportCheck: off.checks[0],
        report.ColdReportCounters: off.counters,
        report.ColdReportAbort: off.aborts[0],
        report.ColdReportAdvisorFailureCount: off.advisor_failure_counts[0],
        report.ColdReportHostExtremes: typing.cast(
            report.ColdReportHostExtremes, off.host_extremes
        ),
        report.ColdReportRecordingArtefact: typing.cast(
            tuple[report.ColdReportRecordingArtefact, ...],
            clean_report.phases[1].recording_artefacts,
        )[0],
    }
    instance = instances[model]
    values = values_of(instance)
    assert model.model_validate(values) == instance
    with pytest.raises(pydantic.ValidationError):
        model.model_validate({**values, "unexpected": 1})
    name = next(iter(values))
    with pytest.raises(pydantic.ValidationError):
        setattr(instance, name, values[name])


Invalid = typing.Callable[[report.ColdSanitisedReport], tuple[type[pydantic.BaseModel], Json]]


def _phase_with(**changes: typing.Any) -> Invalid:
    return lambda built: (report.ColdReportPhase, values_of(built.phases[0]) | changes)


def _report_with(**changes: typing.Any) -> Invalid:
    return lambda built: (report.ColdSanitisedReport, values_of(built) | changes)


def _limits_with(**changes: typing.Any) -> Invalid:
    return lambda built: (report.ColdReportLockedLimits, values_of(built.locked_limits) | changes)


def _check(outcome: acceptance.ColdCheckOutcome, *failures: acceptance.ColdCheckFailure) -> Invalid:
    return lambda _built: (
        report.ColdReportCheck,
        {"check": Check.AUDIO_COUNTERS, "outcome": outcome, "failures": failures},
    )


def _plain(model: type[pydantic.BaseModel], **values: typing.Any) -> Invalid:
    return lambda _built: (model, values)


def _counters_with(**changes: typing.Any) -> Invalid:
    unavailable = dict.fromkeys(report.ColdReportCounters.model_fields)
    return _plain(report.ColdReportCounters, **(unavailable | changes))


FAILED_Q = report.ColdReportCheck(
    check=Check.IDENTITY_QUALIFICATION_V1, outcome=Outcome.FAIL, failures=(F.Q_BOOT_ID,)
)
INVALID_CASES: list[tuple[str, Invalid]] = [
    ("check-pass-with-failure", _check(Outcome.PASS, F.DROPPED_WINDOW)),
    ("check-fail-without-failure", _check(Outcome.FAIL)),
    ("check-failures-unordered", _check(Outcome.FAIL, F.CAPTURE_RESTART, F.DROPPED_WINDOW)),
    ("check-failures-repeated", _check(Outcome.FAIL, F.DROPPED_WINDOW, F.DROPPED_WINDOW)),
    ("phase-checks-reordered", lambda b: _phase_with(checks=b.phases[0].checks[::-1])(b)),
    ("phase-checks-missing", lambda b: _phase_with(checks=b.phases[0].checks[:4])(b)),
    (
        "phase-facts-without-pass",
        lambda b: _phase_with(checks=(FAILED_Q, *b.phases[0].checks[1:]))(b),
    ),
    ("phase-pass-without-facts", _phase_with(identity_facts=None)),
    (
        "phase-advisor-kinds-reordered",
        lambda b: _phase_with(advisor_failure_counts=b.phases[0].advisor_failure_counts[::-1])(b),
    ),
    (
        "phase-advisor-kind-missing",
        lambda b: _phase_with(advisor_failure_counts=b.phases[0].advisor_failure_counts[:3])(b),
    ),
    ("phase-identity-uppercase", lambda b: _phase_with(identity_sha256="A" * 64)(b)),
    ("phase-session-raw", _phase_with(session_id_sha256=FIXTURE_SESSION)),
    ("phase-negative-ticks", _phase_with(tick_count=-1)),
    ("phase-infinite-span", _phase_with(observed_tick_span_seconds=float("inf"))),
    ("report-phases-reordered", lambda b: _report_with(phases=b.phases[::-1])(b)),
    (
        "report-phases-repeated",
        lambda b: _report_with(phases=(b.phases[0], b.phases[0]), phases_absent=(ON,))(b),
    ),
    ("report-no-phase", _report_with(phases=(), phases_absent=(OFF, ON))),
    ("report-absent-not-complement", _report_with(phases_absent=(ON,))),
    ("report-raw-run-id", _report_with(run_id_sha256=RUN_ID)),
    ("report-schema-v2", _report_with(report_schema_version=2)),
    ("limits-n", _limits_with(n=2)),
    ("limits-x", _limits_with(x_ms=200.5)),
    ("limits-fatal-streak", _limits_with(fatal_streak=29)),
    ("limits-hop", _limits_with(hop_seconds=7.5)),
    ("limits-label", _limits_with(label="compared")),
    ("abort-unpaired", _plain(report.ColdReportAbort, domain=Domain.HOST, reason=OPERATOR_STOP)),
    (
        "abort-raw-text",
        _plain(report.ColdReportAbort, domain=Domain.OPERATOR, reason="operator_stop"),
    ),
    ("advisor-negative", _plain(report.ColdReportAdvisorFailureCount, kind=Kind.TIMEOUT, count=-1)),
    ("advisor-raw-kind", _plain(report.ColdReportAdvisorFailureCount, kind="timeout", count=0)),
    ("counters-negative", _counters_with(dropped=-1)),
    ("counters-bool", _counters_with(emitted=True)),
    ("counters-nan", _counters_with(max_inference_duration_ms=float("nan"))),
    (
        "host-infinite-temperature",
        _plain(
            report.ColdReportHostExtremes,
            max_soc_temp_c=float("inf"),
            min_mem_available_bytes=0,
            min_free_bytes=0,
        ),
    ),
    (
        "host-negative-bytes",
        _plain(
            report.ColdReportHostExtremes,
            max_soc_temp_c=40.0,
            min_mem_available_bytes=-1,
            min_free_bytes=0,
        ),
    ),
    ("artefact-role", _plain(report.ColdReportRecordingArtefact, role="wav", size_bytes=1)),
    (
        "artefact-negative",
        _plain(report.ColdReportRecordingArtefact, role="primary_wav", size_bytes=-1),
    ),
]


@pytest.mark.parametrize(
    "invalid", [case[1] for case in INVALID_CASES], ids=[case[0] for case in INVALID_CASES]
)
def test_report_validators_fail_closed(
    clean_report: report.ColdSanitisedReport, invalid: Invalid
) -> None:
    """Closure: inconsistent, raw, negative, non-finite or unpinned values are refused."""
    model, values = invalid(clean_report)
    with pytest.raises(pydantic.ValidationError):
        model.model_validate(values)


def test_report_failures_are_a_plain_closed_enum_on_a_runtime_error() -> None:
    """Errors: three closed members of a plain ``Enum``; the error is a ``RuntimeError``."""
    assert issubclass(Failure, enum.Enum) and not issubclass(Failure, str)
    assert [(member.name, member.value) for member in Failure] == [
        ("REBIND_FAILED", "rebind_failed"),
        ("VALUE_NOT_ADMITTED", "value_not_admitted"),
        ("EGRESS_KEYSET_MISMATCH", "egress_keyset_mismatch"),
    ]
    assert issubclass(report.ColdReportError, RuntimeError)
    error = report.ColdReportError(Failure.VALUE_NOT_ADMITTED)
    assert (error.failure, error.args) == (Failure.VALUE_NOT_ADMITTED, ("Cold report failed.",))


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
    """No verdict: no report field or public callable names a verdict or an aggregate."""
    names: set[str] = set(report.__all__)
    for model in (*REPORT_MODELS, acceptance.ColdD191Metrics, acceptance.ColdIdentityFacts):
        names.update(model.model_fields)
    lowered = {name.lower() for name in names}
    assert not any(token in name for name in lowered for token in FORBIDDEN_NAME_TOKENS)
    assert [name for name in report.__all__ if inspect.isfunction(getattr(report, name))] == [
        "build_sanitised_report",
        "render_sanitised_report",
    ]
    assert [model.__name__ for model in REPORT_MODELS if "outcome" in model.model_fields] == [
        "ColdReportCheck"
    ]


#: Every model the report projects, with its exact field order, written independently.
PROJECTED_FIELDS: dict[type[pydantic.BaseModel], tuple[str, ...]] = {
    report.ColdSanitisedReport: (
        "report_schema_version",
        "run_id_sha256",
        "manifest_sha256",
        "locked_limits",
        "phases",
        "phases_absent",
    ),
    report.ColdReportLockedLimits: ("label", "n", "x_ms", "fatal_streak", "hop_seconds"),
    report.ColdReportPhase: (
        "phase",
        "identity_sha256",
        "checks",
        "d191",
        "tick_count",
        "observed_tick_span_seconds",
        "counters",
        "session_id_sha256",
        "mcp_reported_finalisation_status",
        "mcp_reported_clean",
        "observed_command_streaming_required",
        "applied_branch",
        "aborts",
        "advisory_record_count",
        "advisor_failure_counts",
        "host_extremes",
        "recording_artefacts",
        "identity_facts",
    ),
    report.ColdReportCheck: ("check", "outcome", "failures"),
    report.ColdReportCounters: (
        "emitted",
        "processed",
        "dropped",
        "inference_overruns",
        "total_overflows",
        "max_inference_duration_ms",
    ),
    report.ColdReportAbort: ("domain", "reason"),
    report.ColdReportAdvisorFailureCount: ("kind", "count"),
    report.ColdReportHostExtremes: ("max_soc_temp_c", "min_mem_available_bytes", "min_free_bytes"),
    report.ColdReportRecordingArtefact: ("role", "size_bytes"),
    acceptance.ColdD191Metrics: ("max_consecutive_overflow_count", "peak_trailing_lost_audio_ms"),
    acceptance.ColdIdentityFacts: (
        "mcp_version",
        "temperature_unit",
        "first_crack_mode",
        "model_precision",
        "recording_device_count",
        "source_tree_dirty",
        "source_revision",
        "artefact_kind",
        "artefact_sha256",
        "profile_source_sha256",
        "profile_source_byte_length",
        "first_crack_onnx_threads",
        "first_crack_min_positive_windows",
        "first_crack_confirmation_window_seconds",
        "audio_sample_rate",
        "audio_window_seconds",
        "audio_overlap",
        "audio_hop_seconds",
        "session_ror_window_seconds",
        "session_ror_min_sample_seconds",
    ),
}


def reachable_models(root: type[pydantic.BaseModel]) -> set[type[pydantic.BaseModel]]:
    """Every model class reachable through field annotations, the root included."""
    found: set[type[pydantic.BaseModel]] = set()
    pending: list[object] = [root]
    while pending:
        current = pending.pop()
        if isinstance(current, type) and issubclass(current, pydantic.BaseModel):
            if current not in found:
                found.add(current)
                pending.extend(field.annotation for field in current.model_fields.values())
        else:
            pending.extend(typing.get_args(current))
    return found


def test_every_projected_model_has_exactly_its_pinned_ordered_fields() -> None:
    """Allow-list: the report reaches exactly eleven models, each with its pinned field order.

    The nine report models and the embedded D191 metrics and identity facts are pinned
    independently, so a numeric private value, a pass or failure count, a
    qualification flag or a run outcome cannot enter the projection unseen.  All
    eleven are strict, frozen, closed and finite.
    """
    assert reachable_models(report.ColdSanitisedReport) == set(PROJECTED_FIELDS)
    assert set(REPORT_MODELS) < set(PROJECTED_FIELDS)
    for model, fields in PROJECTED_FIELDS.items():
        assert tuple(model.model_fields) == fields, model.__name__
        config = model.model_config
        settings = (
            config.get("strict"),
            config.get("frozen"),
            config.get("extra"),
            config.get("allow_inf_nan"),
        )
        assert settings == (True, True, "forbid", False), model.__name__


#: The nested fields rendered under their own headings; every other field is a table row.
SECTIONED = frozenset(name for name, _note in SECTION_NOTES)
MarkdownTable = tuple[str, ...]


def spelled(value: object) -> str:
    """One report value's fixed Markdown cell, spelled independently of the module."""
    if value is None:
        return "`null`"
    if type(value) is tuple:
        return ", ".join(spelled(item) for item in typing.cast(tuple[object, ...], value)) or "none"
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, enum.Enum):
        return f"`{value.value}`"
    if type(value) is str:
        return f"`{value}`"
    if type(value) is int or type(value) is float:
        return str(value)
    raise TypeError(type(value).__name__)


def table_of(header: tuple[str, ...], rows: typing.Iterable[tuple[str, ...]]) -> MarkdownTable:
    """One table's exact lines: its header, its separator, then one line per row."""
    return (
        f"| {' | '.join(header)} |",
        "|" + "---|" * len(header),
        *(f"| {' | '.join(row)} |" for row in rows),
    )


def field_table(model: pydantic.BaseModel, names: typing.Iterable[str]) -> MarkdownTable:
    """A field and value table: one row per named field, labelled by that field's name."""
    return table_of(("Field", "Value"), ((name, spelled(getattr(model, name))) for name in names))


def expected_tables(model: pydantic.BaseModel, names: tuple[str, ...]) -> list[MarkdownTable]:
    """Every table one report model renders over its named fields, in order.

    The scalar fields form one field and value table.  Each section field then adds
    a field and value table for a nested model, one row per item for a non-empty
    tuple, and no table when it is ``None`` or empty.  Field orders come only from
    the independently pinned ``PROJECTED_FIELDS``.
    """
    tables = [field_table(model, (name for name in names if name not in SECTIONED))]
    for name in names:
        value: object = getattr(model, name)
        if name not in SECTIONED or value is None or value == ():
            continue
        if isinstance(value, pydantic.BaseModel):
            tables.append(field_table(value, PROJECTED_FIELDS[type(value)]))
            continue
        items = typing.cast(tuple[pydantic.BaseModel, ...], value)
        fields = PROJECTED_FIELDS[type(items[0])]
        rows = (tuple(spelled(getattr(item, field)) for field in fields) for item in items)
        tables.append(table_of(fields, rows))
    return tables


def rendered_tables(markdown: str) -> list[MarkdownTable]:
    """Every run of consecutive table lines in the rendered Markdown, in order."""
    groups = itertools.groupby(markdown.splitlines(), key=lambda line: line.startswith("|"))
    return [tuple(lines) for is_table, lines in groups if is_table]


@pytest.mark.parametrize(
    "shape", [case[1] for case in RUN_SHAPES], ids=[case[0] for case in RUN_SHAPES]
)
def test_every_markdown_table_is_exactly_the_built_report(
    tmp_path: Path, shape: typing.Callable[[], dict[schema.ColdPhaseKind, Spec]]
) -> None:
    """Markdown closure: every table's header, row labels, row count and values, in order.

    The expected tables come from the pinned field orders and section names over the
    built report, so an added scalar row (an overall outcome, an all-checks-pass
    flag), an added check row or an added table fails, even when each added cell
    would satisfy the cell grammar.
    """
    run = write(tmp_path, shape())
    built = report.build_sanitised_report(run)
    run_fields = PROJECTED_FIELDS[report.ColdSanitisedReport]
    expected = expected_tables(built, tuple(name for name in run_fields if name != "phases"))
    for phase in built.phases:
        expected += expected_tables(phase, PROJECTED_FIELDS[report.ColdReportPhase])
    assert rendered_tables(report.render_sanitised_report(run)[1]) == expected


# ------------------------------------------------------ entry points and reach


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


def _calls_of(function: ast.AST, callee: str) -> list[ast.Call]:
    """Return every call of one bare name inside a tree."""
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee
    ]


def test_public_surface_is_the_report_schema_and_two_run_only_entry_points() -> None:
    """Signature: only a retained run enters; no renderer accepts a report or interpretation."""
    public = {
        node.name
        for node in TREE.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
    }
    assigned = {
        target.id
        for node in TREE.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and not target.id.startswith("_")
    }
    assert public == set(report.__all__) and assigned == set()
    for function in (report.build_sanitised_report, report.render_sanitised_report):
        parameters = inspect.signature(function).parameters.values()
        assert [(item.name, item.kind, item.annotation) for item in parameters] == [
            ("run", inspect.Parameter.POSITIONAL_OR_KEYWORD, reader.ColdRetainedRun)
        ]
    assert inspect.signature(report.build_sanitised_report).return_annotation is (
        report.ColdSanitisedReport
    )
    assert inspect.signature(report.render_sanitised_report).return_annotation == tuple[bytes, str]


def test_each_entry_point_reads_the_run_only_as_its_single_call_argument() -> None:
    """R4: ``run`` is only the argument of one call; nothing else takes or interprets a run."""
    for function_name, callee in (
        ("build_sanitised_report", "interpret_retained_run"),
        ("render_sanitised_report", "build_sanitised_report"),
    ):
        function = _function(function_name)
        loads = _loads_of(function, "run")
        calls = _calls_of(function, callee)
        assert len(loads) == 1 and len(calls) == 1, function_name
        assert calls[0].args == [loads[0]] and calls[0].keywords == [], function_name
    readers = {
        node.name
        for node in TREE.body
        if isinstance(node, ast.FunctionDef)
        and any(
            argument.annotation is not None
            and ast.unparse(argument.annotation) == "ColdRetainedRun"
            for argument in node.args.args
        )
    }
    assert readers == {"build_sanitised_report", "render_sanitised_report"}
    assert [
        node.name
        for node in TREE.body
        if isinstance(node, ast.FunctionDef) and _calls_of(node, "interpret_retained_run")
    ] == ["build_sanitised_report"]


PROJECTIONS: dict[str, list[tuple[str, object]]] = {
    "_tick_span": [("rebound", acceptance.ColdReboundPhase)],
    "_g17_maximum": [("rebound", acceptance.ColdReboundPhase)],
    "_counters": [("rebound", acceptance.ColdReboundPhase)],
    "_host_extremes": [("rebound", acceptance.ColdReboundPhase)],
    "_recording_artefacts": [("rebound", acceptance.ColdReboundPhase)],
    "_projected_phase": [
        ("item", acceptance.ColdPhaseInterpretation),
        ("rebound", acceptance.ColdReboundPhase),
    ],
    "_report_of": [("interpretation", acceptance.ColdInterpretation)],
}


@pytest.mark.parametrize("name", PROJECTIONS)
def test_projection_helpers_accept_only_the_capability(name: str) -> None:
    """R4: every projection helper takes only the capability or its interpretation."""
    parameters = inspect.signature(getattr(report, name)).parameters.values()
    assert [(item.name, item.annotation) for item in parameters] == PROJECTIONS[name]


def test_the_builder_interprets_once_and_the_renderer_builds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: one interpretation per build, one build per render, of the caller's run."""
    run = write(tmp_path, {OFF: Spec()})
    seen: list[reader.ColdRetainedRun] = []

    def counting(argument: reader.ColdRetainedRun) -> acceptance.ColdInterpretation:
        seen.append(argument)
        return acceptance.interpret_retained_run(argument)

    monkeypatch.setattr(report, "interpret_retained_run", counting)
    report.build_sanitised_report(run)
    assert len(seen) == 1 and seen[0] is run
    report.render_sanitised_report(run)
    assert len(seen) == 2 and seen[1] is run


def test_limits_are_rendered_and_never_compared_with_a_measurement() -> None:
    """Class J: the limits are loaded once, into the pinned tuple, compared only with the pin."""
    limits = {"D191_N_LIMIT", "D191_X_LIMIT_MS", "PRODUCTION_FATAL_STREAK", "EFFECTIVE_HOP_SECONDS"}
    compares = [node for node in ast.walk(TREE) if isinstance(node, ast.Compare)]
    names_in = [{n.id for n in ast.walk(node) if isinstance(n, ast.Name)} for node in compares]
    assert all(not names & limits for names in names_in)
    pinned = [
        node
        for node, names in zip(compares, names_in, strict=True)
        if "_LOCKED_LIMIT_VALUES" in names
    ]
    assert len(pinned) == 1
    assert ast.unparse(pinned[0]) == (
        "(self.n, self.x_ms, self.fatal_streak, self.hop_seconds) != _LOCKED_LIMIT_VALUES"
    )
    declaration = next(
        node
        for node in TREE.body
        if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "_LOCKED_LIMIT_VALUES"
    )
    loads = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Name) and node.id in limits and isinstance(node.ctx, ast.Load)
    ]
    assert sorted(node.id for node in loads) == sorted(limits)
    assert all(any(node is inner for inner in ast.walk(declaration)) for node in loads)
    uses = [n for n in ast.walk(TREE) if isinstance(n, ast.Name) and n.id == "_LOCKED_LIMIT_VALUES"]
    assert len(uses) == 3


LIMIT_ALIASES = ("n", "x_ms", "fatal_streak", "hop_seconds")


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Map every node's ``id`` to its parent node."""
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _owners(tree: ast.AST) -> dict[int, str]:
    """Map every node's ``id`` to its nearest enclosing function or class name."""
    owners: dict[int, str] = {}

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.ClassDef)) else owner
            owners[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return owners


def test_the_unpacked_limit_aliases_bind_only_their_own_locked_limit_keywords() -> None:
    """Class J: each limit alias is read once, as its own ``ColdReportLockedLimits`` keyword.

    The four aliases unpacked from the pinned tuple are never compared, rebound, passed
    on or used in arithmetic; each keyword binds the alias of the same name, and the
    rendered limit fields are read back only by the pin validator.
    """
    unpackings = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Assign) and ast.unparse(node.value) == "_LOCKED_LIMIT_VALUES"
    ]
    assert len(unpackings) == 1 and len(unpackings[0].targets) == 1
    target = unpackings[0].targets[0]
    assert isinstance(target, ast.Tuple)
    assert tuple(ast.unparse(item) for item in target.elts) == LIMIT_ALIASES
    limits_class = next(
        node
        for node in TREE.body
        if isinstance(node, ast.ClassDef) and node.name == "ColdReportLockedLimits"
    )
    declarations = [
        statement.target
        for statement in limits_class.body
        if isinstance(statement, ast.AnnAssign) and ast.unparse(statement.target) in LIMIT_ALIASES
    ]
    names = [n for n in ast.walk(TREE) if isinstance(n, ast.Name) and n.id in LIMIT_ALIASES]
    stores = {id(node) for node in names if isinstance(node.ctx, ast.Store)}
    assert stores == {id(node) for node in (*target.elts, *declarations)}
    calls = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "ColdReportLockedLimits"
    ]
    assert len(calls) == 1
    bindings: dict[str | None, ast.expr] = {item.arg: item.value for item in calls[0].keywords}
    for alias in LIMIT_ALIASES:
        value = bindings[alias]
        assert isinstance(value, ast.Name) and value.id == alias, alias
    loads = {id(node) for node in names if isinstance(node.ctx, ast.Load)}
    assert loads == {id(bindings[alias]) for alias in LIMIT_ALIASES}
    owners = _owners(TREE)
    attributes = [
        (owners[id(node)], ast.unparse(node))
        for node in ast.walk(TREE)
        if isinstance(node, ast.Attribute) and node.attr in LIMIT_ALIASES
    ]
    assert sorted(attributes) == sorted(
        ("_require_acceptance_constants", f"self.{alias}") for alias in LIMIT_ALIASES
    )


def test_no_string_names_a_limit_field_and_getattr_reads_only_by_variable() -> None:
    """Class J: no string literal names a limit field, and ``getattr`` names only by variable.

    The alias and attribute pins above see only syntactic names, so a limit read by a
    string key (``values["x_ms"]``), by ``getattr(limits, "x_ms")``, by a ``getattr``
    whose name is any literal or inline expression, or by an aliased ``getattr`` would
    bypass them.  Every remaining ``getattr`` is a direct two-argument call naming its
    field by a variable, as the generic field reads do.
    """
    parents = _parents(TREE)
    named = [
        ast.unparse(parents[id(node)])
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant) and node.value in LIMIT_ALIASES
    ]
    assert named == []
    calls = _calls_of(TREE, "getattr")
    names = [node for node in ast.walk(TREE) if isinstance(node, ast.Name) and node.id == "getattr"]
    assert calls and len(names) == len(calls)
    for call in calls:
        assert len(call.args) == 2 and isinstance(call.args[1], ast.Name), ast.unparse(call)


#: Every numeric literal in the module, by owner and immediate context: the non-negative
#: field constraints and admissions, the tick-span presence floor and sign, the schema
#: version, and subscript positions.  None of them is a limit.
NUMERIC_LITERALS = (
    ("<module>", "ge=0"),
    ("<module>", "ge=0"),
    ("ColdSanitisedReport", "typing.Literal[1]"),
    ("_count", "value >= 0"),
    ("_projected_phase", "rebound.finalisations[-1]"),
    ("_reading", "value >= 0"),
    ("_report_of", "report_schema_version=1"),
    ("_require_closed_phase", "self.checks[0]"),
    ("_section", "items[0]"),
    ("_tick_span", "len(ticks) < 2"),
    ("_tick_span", "span >= 0"),
    ("_tick_span", "ticks[-1]"),
    ("_tick_span", "ticks[0]"),
)
#: The only literal comparisons: non-negative admissions (0) and the tick-span floor (2).
LITERAL_COMPARISONS = (
    ("_count", "value >= 0"),
    ("_reading", "value >= 0"),
    ("_tick_span", "len(ticks) < 2"),
    ("_tick_span", "span >= 0"),
)


def test_every_numeric_literal_is_pinned_so_no_limit_is_introduced() -> None:
    """Class J: no new numeric literal, and no literal comparison beyond the 0 and 2 guards.

    A limit cannot enter as a literal comparison, a constraint keyword, a literal type
    or arithmetic, because every numeric literal is pinned with its owner and context.
    """
    parents = _parents(TREE)
    owners = _owners(TREE)
    found: list[tuple[str, str]] = []
    compared: list[tuple[str, str]] = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Constant) or type(node.value) not in (int, float):
            continue
        context = parents[id(node)]
        if isinstance(context, ast.UnaryOp):
            context = parents[id(context)]
        found.append((owners[id(node)], ast.unparse(context)))
        if isinstance(context, ast.Compare):
            compared.append(found[-1])
    assert sorted(found) == sorted(NUMERIC_LITERALS)
    assert sorted(compared) == sorted(LITERAL_COMPARISONS)


ALLOWED_FROM_IMPORTS: dict[str, frozenset[str]] = {
    "roastpilot_agent.cold_characterisation.acceptance": frozenset(
        {
            "ColdCheck",
            "ColdCheckFailure",
            "ColdCheckOutcome",
            "ColdD191Metrics",
            "ColdIdentityFacts",
            "ColdInterpretation",
            "ColdInterpretationError",
            "ColdPhaseInterpretation",
            "ColdReboundPhase",
            "D191_N_LIMIT",
            "D191_X_LIMIT_MS",
            "EFFECTIVE_HOP_SECONDS",
            "PRODUCTION_FATAL_STREAK",
            "interpret_retained_run",
        }
    ),
    "roastpilot_agent.cold_characterisation.evidence_schema": frozenset(
        {
            "ColdAbortDomain",
            "ColdAdvisorFailureKind",
            "ColdCapabilityBranch",
            "ColdEvidenceFailure",
            "ColdFinalisationStatus",
            "ColdHostAbortReason",
            "ColdIdentityAbortReason",
            "ColdMcpAbortReason",
            "ColdOperatorAbortReason",
            "ColdPhaseKind",
        }
    ),
    "roastpilot_agent.cold_characterisation.evidence_store": frozenset({"canonical_json"}),
    "roastpilot_agent.cold_characterisation.evidence_reader": frozenset(
        {"ColdRetainedRun", "ABORT_REASON_BY_DOMAIN"}
    ),
}
ALLOWED_IMPORTS = frozenset({"enum", "hashlib", "json", "math", "re", "typing", "pydantic"})
FORBIDDEN_MODULES = (
    "identity",
    "host",
    "mcp",
    "mcp_client",
    "config",
    "advisor",
    "appliance",
    "models",
    "safety",
    "controller",
    "api",
    "store",
    "cli",
    "live",
    "os",
    "pathlib",
    "io",
    "subprocess",
    "socket",
    "shutil",
)


def test_imports_are_within_the_ratified_allow_list() -> None:
    """Class B: only allow-listed modules and names; nothing from ``mcp`` or a forbidden module."""
    imported: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            assert all(
                alias.name in ALLOWED_IMPORTS and alias.asname is None for alias in node.names
            )
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in ALLOWED_FROM_IMPORTS, node.module
            assert {alias.name for alias in node.names} <= ALLOWED_FROM_IMPORTS[node.module]
            assert all(alias.asname is None for alias in node.names)
            imported.add(node.module)
    assert not any(module.rsplit(".", 1)[-1] in FORBIDDEN_MODULES for module in imported)
    bound = {name for name, value in vars(report).items() if inspect.ismodule(value)}
    assert bound <= ALLOWED_IMPORTS


def test_module_makes_no_dynamic_import_or_evaluation_call() -> None:
    """Class B: no dynamic import, evaluation, or attribute-reflective call reaches a module."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
        assert name not in {"__import__", "import_module", "eval", "exec", "compile"}, name


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
            value = getattr(report, name)
            assert not isinstance(value, (dict, list, set, bytearray)), name
            if name in {"__all__", "_T"} or annotation == "typing.TypeAlias":
                continue
            assert annotation is not None and annotation.startswith("typing.Final"), name


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
    # F, H, I, K, L: capability reads, lexical control, scoring, packaged constants, JSON.
    ".command_streaming_required",
    "startswith",
    "endswith",
    "casefold",
    ".lower(",
    ".upper(",
    ".strip(",
    "entropy",
    "__version__",
    "REPO_ID",
    "MANIFEST_FILES",
    "json.dumps",
    "model_dump_json",
)


def test_source_sweeps_find_no_forbidden_reach() -> None:
    """Classes C-L: no reader, I/O, actuator, lexical-control, scoring or constant text."""
    for text in FORBIDDEN_SOURCE_TEXT:
        assert text not in SOURCE, text
    assert re.search(r"REVISION\b", SOURCE) is None
    assert SOURCE.count("json.loads") == 1
    assert [
        node.name
        for node in TREE.body
        if isinstance(node, ast.FunctionDef) and "json.loads" in ast.unparse(node)
    ] == ["_egress"]


def test_rendering_performs_no_file_io_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope: building and rendering need no file or evidence entry point and write nothing."""
    run = write(tmp_path, full_spec())
    root = str(tmp_path.resolve() / "pi")
    docs = REPO_ROOT / "docs"

    def docs_paths() -> list[str]:
        return sorted(path.relative_to(docs).as_posix() for path in docs.rglob("*"))

    before = (snapshot_tree(root), docs_paths())
    touched: list[str] = []

    def refusing(name: str) -> typing.Callable[..., typing.NoReturn]:
        def refuse(*_args: object, **_kwargs: object) -> typing.NoReturn:
            touched.append(name)
            raise AssertionError("file or evidence access during reporting")

        return refuse

    for target, name in (
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "scandir"),
        (reader, "read_retained_run"),
        (store, "verify_retained_tree"),
        (store, "verify_retained_copies"),
        (store, "read_verified_lines"),
        (store, "open_run"),
        (store, "admit_evidence_root"),
    ):
        monkeypatch.setattr(target, name, refusing(name))
    built = report.build_sanitised_report(run)
    document, markdown = report.render_sanitised_report(run)
    monkeypatch.undo()
    assert touched == []
    assert len(built.phases) == 2 and document and markdown
    assert (snapshot_tree(root), docs_paths()) == before
    assert run_dir(root).is_dir()
    assert len(sorted(FIXTURE_PATH.parent.glob("*.json"))) == 15
