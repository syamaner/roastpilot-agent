"""Advisory-attempt evidence: format, sequence, builders, writer append, and V3 reader."""

import ast
import enum
import hashlib
import inspect
import json
import traceback
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.advisor import AdvisorContext, AdvisorDescriptor, AdvisorUsage, RoastDecision
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyEvaluation
from tests.test_cold_characterisation_evidence_builders import (
    COLD_PACKAGE,
    RUN_ID,
    _reachable_package_modules,  # pyright: ignore[reportPrivateUsage]
    header_for,
    make_identity,
    tick_for,
)
from tests.test_cold_characterisation_evidence_lifecycle import (
    activated,
    completed_run,
    expect_evidence,
    read2,
    seal_run,
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

Kind = advisory.ColdAdvisoryResolution
Order = advisory.ColdAdvisoryAttemptFailure
State = advisory.ColdAdvisoryAttemptEvidenceState
Rationale = advisory.ColdAdvisoryRationaleState
EvaluationState = advisory.ColdAdvisoryEvaluationState
UsageState = advisory.ColdAdvisoryUsageState
Invocation = advisory.ColdAdvisoryInvocationState
Verdict = schema.ColdSafetyVerdict
Failure = store.ColdEvidenceStoreFailure
Intent = advisory.ColdAdvisoryIntentRecord
Resolution = advisory.ColdAdvisoryResolutionRecord
Record = Intent | Resolution
NOT_VALIDATED = schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
T0 = "2026-09-26T12:00:00Z"
ADVISORY_OFF = "records/recording_off/advisory_attempt.jsonl"
ADVISORY_ON = "records/recording_on/advisory_attempt.jsonl"
PROFILE = "Cold characterisation"
CONTEXT: dict[str, object] = {
    "charge_guidance_max_c": 190.0,
    "charge_guidance_min_c": None,
    "current_bean_temp_c": 24.5,
    "phase": "preheating",
    "profile_name": PROFILE,
    "target_drop_temp_c": 205.0,
}
FAILED_KINDS = (
    Kind.RETURNED_MALFORMED_OUTPUT,
    Kind.RETURNED_PROVIDER_ERROR,
    Kind.RAISED_UNCLASSIFIED,
    Kind.ABANDONED_AFTER_BOUND,
    Kind.UNRESOLVED_AT_PHASE_END,
)
NON_DECISION_KINDS = (Kind.RETURNED_UNSAFE_OUTPUT, *FAILED_KINDS)
USAGE = advisory.ColdAdvisoryUsageReading(
    input_tokens=1200, output_tokens=80, total_tokens=1280, reasoning_tokens=None
)


def evaluation(
    heat: int = 40,
    fan: int = 60,
    verdict: schema.ColdSafetyVerdict = Verdict.REJECT,
    adjusted_heat: int | None = None,
    adjusted_fan: int | None = None,
) -> schema.ColdSafetyEvaluation:
    """Build one strict safety evaluation of a request."""
    return schema.ColdSafetyEvaluation(
        rule="cold_observation_only",
        verdict=verdict,
        input_heat=heat,
        input_fan=fan,
        adjusted_heat=adjusted_heat,
        adjusted_fan=adjusted_fan,
        reason="Advisory output is never forwarded.",
    )


def decision_payload() -> dict[str, typing.Any]:
    """Return the default returned-decision payload."""
    return {
        "requested_heat": 40,
        "requested_fan": 60,
        "should_drop": False,
        "confidence": 0.5,
        "rationale": "Hold heat while the probe settles.",
        "evaluation": evaluation(),
        "usage": USAGE,
    }


def intend(
    header: schema.ColdRunHeader, index: int = 0, *, at: float = 10.0, **overrides: typing.Any
) -> Intent:
    """Build one intent with admitted defaults, overriding any argument."""
    arguments: dict[str, typing.Any] = {
        "header": header,
        "attempt_index": index,
        "recorded_at_utc": T0,
        "monotonic_seconds": at,
        "context_tick": 0,
        "context_tick_monotonic": 2.0,
        "context": CONTEXT,
        "profile_name": PROFILE,
        "target_drop_temp_c": 205.0,
        "charge_guidance_min_c": None,
        "charge_guidance_max_c": 190.0,
        "provider": "openrouter",
        "model": "openai/gpt-4o",
        "prompt_version": "v4",
        "configured_call_bound_seconds": 30.0,
        "configured_dwell_seconds": 5.0,
    }
    arguments.update(overrides)
    return advisory.build_advisory_intent_record(**arguments)


def resolve(
    header: schema.ColdRunHeader,
    index: int = 0,
    kind: advisory.ColdAdvisoryResolution = Kind.RETURNED_DECISION,
    *,
    start: float = 10.0,
    invoked: bool = True,
    **payload: typing.Any,
) -> Resolution:
    """Build one resolution: invoked at start+1, resolved at start+2, recorded at start+3."""
    merged = {**decision_payload(), **payload} if kind is Kind.RETURNED_DECISION else payload
    return advisory.build_advisory_resolution_record(
        header=header,
        attempt_index=index,
        recorded_at_utc=T0,
        monotonic_seconds=start + 3.0,
        resolution=kind,
        invocation_utc=T0 if invoked else None,
        invocation_monotonic=start + 1.0 if invoked else None,
        resolved_utc=T0,
        resolved_monotonic=start + 2.0,
        **merged,
    )


def attempt(
    header: schema.ColdRunHeader,
    index: int,
    start: float,
    kind: advisory.ColdAdvisoryResolution = Kind.RETURNED_DECISION,
    **payload: typing.Any,
) -> list[Record]:
    """Return one intent and its resolution."""
    return [intend(header, index, at=start), resolve(header, index, kind, start=start, **payload)]


def open_off(tmp_path: Path) -> tuple[store.ColdEvidenceWriter, str, schema.ColdRunHeader]:
    """Open a run and bind its recording-off header and one tick."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off, 0))
    return writer, root, off


def bind_on(tmp_path: Path, writer: store.ColdEvidenceWriter, root: str) -> schema.ColdRunHeader:
    """Bind the recording-on header and one tick."""
    on = on_header(tmp_path, root)
    writer.append(on)
    writer.append(tick_for(on, 0))
    return on


def read3(root: str, digest: str) -> reader.ColdRetainedRunV3:
    """Read the shared test run through the V3 reader."""
    return reader.read_retained_run_v3(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def write_read(
    writer: store.ColdEvidenceWriter, root: str, records: list[Record]
) -> reader.ColdRetainedRunV3:
    """Append records, seal, and read the run back through V3."""
    for record in records:
        writer.append_advisory_attempt(record)
    return read3(root, writer.seal().manifest_sha256)


def expect_attempt(
    failure: advisory.ColdAdvisoryAttemptFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, chain-free attempt-order failure."""
    with pytest.raises(advisory.ColdAdvisoryAttemptError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold advisory attempt evidence refused.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def raw(record: pydantic.BaseModel) -> dict[str, typing.Any]:
    """Return a copy of a model's raw field values."""
    return dict(object.__getattribute__(record, "__dict__"))


def lines(records: list[Record]) -> bytes:
    """Return canonical record lines for a crafted stream file."""
    return b"".join(line_of(record.model_dump(mode="json")) for record in records)


def test_path_constants_are_enum_pinned() -> None:
    """The literal stream paths equal the closed phase values."""
    assert f"records/{OFF.value}/advisory_attempt.jsonl" == ADVISORY_OFF
    assert f"records/{ON.value}/advisory_attempt.jsonl" == ADVISORY_ON


# ------------------------------------------------------------------ V-T1 round trip


def test_returned_decision_round_trips(tmp_path: Path) -> None:
    """V-T1a: a decision with retained rationale, evaluation, and usage reads back equal."""
    writer, root, off = open_off(tmp_path)
    records = attempt(off, 0, 10.0)
    retained = write_read(writer, root, records)
    assert list(retained.advisory_attempts) == records
    assert retained.advisory_attempt_state is State.COMPLETE
    resolution = typing.cast(Resolution, retained.advisory_attempts[1])
    assert resolution.rationale_state is Rationale.RETAINED
    assert resolution.evaluation_state is EvaluationState.RECORDED
    assert resolution.usage_state is UsageState.RECORDED
    assert resolution.usage_basis is advisory.ColdAdvisoryUsageBasis.PRODUCTION_NORMALISED
    assert (run_dir(root) / ADVISORY_OFF).read_bytes() == lines(records)


def test_every_failure_kind_round_trips_and_reads_complete(tmp_path: Path) -> None:
    """V-T1b/c: every other kind, invoked and not, round-trips; all failures read COMPLETE."""
    writer, root, off = open_off(tmp_path)
    records: list[Record] = [
        *attempt(off, 0, 10.0, Kind.RETURNED_UNSAFE_OUTPUT, usage=USAGE),
        *attempt(off, 1, 20.0, Kind.RETURNED_UNSAFE_OUTPUT),
    ]
    for index, kind in enumerate(FAILED_KINDS, start=2):
        records.extend(attempt(off, index, 10.0 * index + 10.0, kind))
    records.append(intend(off, 7, at=100.0))
    records.append(resolve(off, 7, Kind.UNRESOLVED_AT_PHASE_END, start=100.0, invoked=False))
    retained = write_read(writer, root, records)
    assert list(retained.advisory_attempts) == records
    assert retained.advisory_attempt_state is State.COMPLETE
    kinds = [item.resolution for item in retained.advisory_attempts if isinstance(item, Resolution)]
    assert all(kind is not Kind.RETURNED_DECISION for kind in kinds)
    unsafe = typing.cast(Resolution, records[3])
    assert unsafe.usage_state is UsageState.NOT_RECORDED
    not_invoked = typing.cast(Resolution, records[-1])
    assert not_invoked.invocation_state is Invocation.NOT_INVOKED
    assert (not_invoked.invocation_utc, not_invoked.invocation_monotonic) == (None, None)


def test_a_run_ending_on_an_intent_reads_open_tail(tmp_path: Path) -> None:
    """V-T1d: an unterminated tail is retained data and reads ``OPEN_TAIL``."""
    writer, root, off = open_off(tmp_path)
    records = [*attempt(off, 0, 10.0), intend(off, 1, at=20.0)]
    retained = write_read(writer, root, records)
    assert list(retained.advisory_attempts) == records
    assert retained.advisory_attempt_state is State.OPEN_TAIL


def test_both_phases_restart_indices_and_keep_lifecycle(tmp_path: Path) -> None:
    """V-T1e: per-phase indices restart at 0; lifecycle is read alongside, unchanged."""
    writer, root, off = open_off(tmp_path)
    life = activated(at=5.0)(off, 0)
    writer.append_lifecycle(life)
    records = attempt(off, 0, 10.0)
    for record in records:
        writer.append_advisory_attempt(record)
    on = bind_on(tmp_path, writer, root)
    on_records = [*attempt(on, 0, 1300.0), *attempt(on, 1, 1310.0, Kind.ABANDONED_AFTER_BOUND)]
    retained = write_read(writer, root, on_records)
    assert list(retained.advisory_attempts) == [*records, *on_records]
    assert retained.advisory_attempt_state is State.COMPLETE
    assert retained.lifecycle == (life,)
    assert retained.lifecycle_state is lifecycle.ColdLifecycleEvidenceState.PRESENT
    assert (run_dir(root) / ADVISORY_ON).read_bytes() == lines(on_records)


# ------------------------------------------------------------------ V-T2 profiles


def test_v1_and_v2_refuse_an_advisory_tree(tmp_path: Path) -> None:
    """V-T2/AM1: the earlier profiles still refuse ``advisory_attempt.jsonl``."""
    writer, root, off = open_off(tmp_path)
    for record in attempt(off, 0, 10.0):
        writer.append_advisory_attempt(record)
    digest = writer.seal().manifest_sha256
    expect(
        Failure.ENTRY_PATH_INVALID,
        lambda: reader.read_retained_run(root, run_id=RUN_ID, expected_manifest_sha256=digest),
    )
    expect(Failure.ENTRY_PATH_INVALID, lambda: read2(root, digest))
    assert read3(root, digest).advisory_attempt_state is State.COMPLETE


def test_v3_reads_v1_and_v2_trees_as_absent_with_equal_runs(tmp_path: Path) -> None:
    """V-T2: v1 and v2 trees read through V3 as ``ABSENT`` with the old ``run``."""
    (tmp_path / "v1").mkdir()
    root, sealed, _records = write_full_run(tmp_path / "v1")
    v1 = reader.read_retained_run(
        root, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    v3 = read3(root, sealed.manifest_sha256)
    assert v3.run == v1
    assert (v3.advisory_attempt_state, v3.advisory_attempts, v3.lifecycle) == (State.ABSENT, (), ())
    off_plan, on_plan = completed_run()
    root2, digest2, written = seal_run(tmp_path / "v2", off_plan, on_plan)
    v2 = read2(root2, digest2)
    v3b = read3(root2, digest2)
    assert (v3b.run, v3b.lifecycle_state, list(v3b.lifecycle)) == (
        v2.run,
        v2.lifecycle_state,
        written,
    )
    assert v3b.advisory_attempt_state is State.ABSENT


# ------------------------------------------------------------------ V-T3 validator matrix


_LEVERS: dict[str, object] = {
    "requested_heat": 40,
    "requested_fan": 60,
    "should_drop": False,
    "confidence": 0.5,
}
_RECORDED_EVALUATION: dict[str, object] = {
    "evaluation_state": EvaluationState.RECORDED,
    "evaluation_rule": "rule",
    "evaluation_verdict": Verdict.REJECT,
    "evaluation_input_heat": 40,
    "evaluation_input_fan": 60,
    "evaluation_adjusted_heat": None,
    "evaluation_adjusted_fan": None,
    "evaluation_reason": "reason",
}
_RECORDED_USAGE: dict[str, object] = {
    "usage_state": UsageState.RECORDED,
    "usage_basis": advisory.ColdAdvisoryUsageBasis.PRODUCTION_NORMALISED,
    "usage_input_tokens": 1,
    "usage_output_tokens": 1,
    "usage_total_tokens": 2,
}
_NOT_INVOKED: dict[str, object] = {
    "invocation_state": Invocation.NOT_INVOKED,
    "invocation_utc": None,
    "invocation_monotonic": None,
}
_DECISION_NULLS: dict[str, object] = {
    "requested_heat": None,
    "requested_fan": None,
    "should_drop": None,
    "confidence": None,
}


def _matrix_cases() -> list[tuple[str, advisory.ColdAdvisoryResolution, dict[str, object]]]:
    """Return every presence-matrix breach as ``(label, base kind, raw update)``."""
    cases: list[tuple[str, advisory.ColdAdvisoryResolution, dict[str, object]]] = []
    for kind in NON_DECISION_KINDS:
        for name, value in _LEVERS.items():
            cases.append((f"{kind.value}-{name}", kind, {name: value}))
        cases.append(
            (
                f"{kind.value}-rationale",
                kind,
                {"rationale_state": Rationale.RETAINED, "rationale": "x"},
            )
        )
        cases.append((f"{kind.value}-evaluation", kind, {**_LEVERS, **_RECORDED_EVALUATION}))
        cases.append((f"{kind.value}-evaluation-rule", kind, {"evaluation_rule": "rule"}))
    for kind in FAILED_KINDS:
        cases.append((f"{kind.value}-usage", kind, dict(_RECORDED_USAGE)))
        cases.append((f"{kind.value}-usage-state", kind, {"usage_state": UsageState.NOT_RECORDED}))
    for kind in (Kind.RETURNED_DECISION, *NON_DECISION_KINDS[:-1]):
        cases.append((f"{kind.value}-not-invoked", kind, dict(_NOT_INVOKED)))
    decision = Kind.RETURNED_DECISION
    unsafe = Kind.RETURNED_UNSAFE_OUTPUT
    for name in _LEVERS:
        cases.append((f"decision-{name}-null", decision, {name: None}))
    named: list[tuple[str, advisory.ColdAdvisoryResolution, dict[str, object]]] = [
        ("decision-rationale-state-null", decision, {"rationale_state": None, "rationale": None}),
        ("decision-retained-null", decision, {"rationale": None}),
        (
            "decision-not-retained-text",
            decision,
            {"rationale_state": Rationale.NOT_RETAINED_OVER_BOUND},
        ),
        ("decision-evaluation-state-null", decision, {"evaluation_state": None}),
        (
            "decision-not-recorded-fields",
            decision,
            {"evaluation_state": EvaluationState.NOT_RECORDED},
        ),
        ("decision-recorded-rule-null", decision, {"evaluation_rule": None}),
        ("decision-recorded-verdict-null", decision, {"evaluation_verdict": None}),
        ("decision-recorded-reason-null", decision, {"evaluation_reason": None}),
        ("decision-usage-state-null", decision, {"usage_state": None}),
        ("decision-usage-count-null", decision, {"usage_input_tokens": None}),
        ("decision-usage-basis-null", decision, {"usage_basis": None}),
        ("decision-usage-not-recorded", decision, {"usage_state": UsageState.NOT_RECORDED}),
        ("unsafe-usage-state-null", unsafe, {"usage_state": None}),
        (
            "unsafe-reasoning-without-usage",
            unsafe,
            {"usage_state": UsageState.NOT_RECORDED, "usage_reasoning_tokens": 5},
        ),
        ("decision-invocation-utc-null", decision, {"invocation_utc": None}),
        ("decision-invocation-monotonic-null", decision, {"invocation_monotonic": None}),
        (
            "unresolved-not-invoked-with-instant",
            Kind.UNRESOLVED_AT_PHASE_END,
            {**_NOT_INVOKED, "invocation_utc": T0},
        ),
        ("invocation-after-resolution", decision, {"invocation_monotonic": 12.5}),
        ("resolution-after-recording", decision, {"resolved_monotonic": 13.5}),
        (
            "not-invoked-resolution-after-recording",
            Kind.UNRESOLVED_AT_PHASE_END,
            {**_NOT_INVOKED, "resolved_monotonic": 13.5},
        ),
    ]
    return cases + named


_MATRIX = _matrix_cases()


@pytest.mark.parametrize(("label", "kind", "update"), _MATRIX, ids=[case[0] for case in _MATRIX])
def test_presence_matrix_refuses_at_both_layers(
    tmp_path: Path, label: str, kind: advisory.ColdAdvisoryResolution, update: dict[str, object]
) -> None:
    """V-T3: direct model validation raises ``ValidationError``; the wrapper refuses closed."""
    base = resolve(header_for(tmp_path, str(tmp_path.resolve()), OFF), 0, kind)
    values = {**raw(base), **update}
    with pytest.raises(pydantic.ValidationError):
        Resolution.model_validate(values, strict=True)
    forged = base.model_copy(update=update)
    expect_evidence(NOT_VALIDATED, lambda: advisory.validate_advisory_attempt_record(forged))
    assert label


def test_not_invoked_order_is_inclusive(tmp_path: Path) -> None:
    """Lead §10: a not-invoked resolution may resolve exactly at its recording instant."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = advisory.build_advisory_resolution_record(
        header=off,
        attempt_index=0,
        recorded_at_utc=T0,
        monotonic_seconds=13.0,
        resolution=Kind.UNRESOLVED_AT_PHASE_END,
        invocation_utc=None,
        invocation_monotonic=None,
        resolved_utc=T0,
        resolved_monotonic=13.0,
    )
    assert record.invocation_state is Invocation.NOT_INVOKED
    assert advisory.validate_advisory_attempt_record(record) == record


# ------------------------------------------------------------------ V-T4 no drop


_FORBIDDEN_PAYLOADS: dict[str, dict[str, typing.Any]] = {
    "usage": {"usage": USAGE},
    "levers": {"requested_heat": 40, "requested_fan": 60, "should_drop": False, "confidence": 0.5},
    "rationale": {"rationale": "unexpected"},
    "over-bound-rationale": {"rationale": "x" * 4096},
    "evaluation": {"evaluation": evaluation()},
}


@pytest.mark.parametrize("kind", FAILED_KINDS, ids=[kind.value for kind in FAILED_KINDS])
@pytest.mark.parametrize("payload", list(_FORBIDDEN_PAYLOADS), ids=list(_FORBIDDEN_PAYLOADS))
def test_builder_carries_and_refuses_a_forbidden_payload(
    tmp_path: Path, kind: advisory.ColdAdvisoryResolution, payload: str
) -> None:
    """V-T4/AM7: a forbidden payload is carried and refused, never dropped or fabricated."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    expect_evidence(NOT_VALIDATED, lambda: resolve(off, 0, kind, **_FORBIDDEN_PAYLOADS[payload]))
    assert resolve(off, 0, kind).usage_state is None


def test_unsafe_output_carries_usage_but_refuses_levers(tmp_path: Path) -> None:
    """V-T4: unsafe output admits usage only; any lever is carried and refused."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    unsafe = Kind.RETURNED_UNSAFE_OUTPUT
    assert resolve(off, 0, unsafe, usage=USAGE).usage_input_tokens == 1200
    for payload in ("levers", "rationale", "evaluation"):
        forbidden = _FORBIDDEN_PAYLOADS[payload]
        expect_evidence(
            NOT_VALIDATED,
            lambda forbidden=forbidden: resolve(off, 0, unsafe, **forbidden),
        )


# ------------------------------------------------------------------ V-T5 rationale


def _assert_levers_kept(record: Resolution) -> None:
    """Assert a not-retained rationale kept every lever, the evaluation, and usage."""
    assert (record.requested_heat, record.requested_fan, record.should_drop, record.confidence) == (
        40,
        60,
        False,
        0.5,
    )
    assert record.evaluation_state is EvaluationState.RECORDED
    assert (record.evaluation_verdict, record.evaluation_input_heat) == (Verdict.REJECT, 40)
    assert (record.usage_state, record.usage_input_tokens) == (UsageState.RECORDED, 1200)
    assert record.rationale is None


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("x" * 2049, Rationale.NOT_RETAINED_OVER_BOUND),
        ("€" * 683, Rationale.NOT_RETAINED_OVER_BOUND),
        ("a\ud800b", Rationale.NOT_RETAINED_NOT_ENCODABLE),
        ("\ud800" + "a" * 2048, Rationale.NOT_RETAINED_OVER_BOUND),
    ],
    ids=["ascii-2049", "euro-2049-bytes", "surrogate", "length-before-encoding"],
)
def test_unretainable_rationale_keeps_everything_else(
    tmp_path: Path, text: str, state: advisory.ColdAdvisoryRationaleState
) -> None:
    """V-T5/AM8/AM9/AM12: never truncated; the bounded state says why; levers survive."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = resolve(off, 0, rationale=text)
    assert record.rationale_state is state
    _assert_levers_kept(record)


@pytest.mark.parametrize(
    "text", ["x" * 2048, "€" * 682, ""], ids=["ascii-2048", "euro-2046", "empty"]
)
def test_bounded_rationale_is_retained_exactly(tmp_path: Path, text: str) -> None:
    """V-T5: a bounded rationale, including the empty string, is retained byte for byte."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = resolve(off, 0, rationale=text)
    assert (record.rationale_state, record.rationale) == (Rationale.RETAINED, text)


def test_not_retained_rationale_round_trips(tmp_path: Path) -> None:
    """V-T5: the not-retained state and every kept field survive the writer and reader."""
    writer, root, off = open_off(tmp_path)
    records = attempt(off, 0, 10.0, rationale="x" * 3000)
    retained = write_read(writer, root, records)
    assert list(retained.advisory_attempts) == records
    _assert_levers_kept(typing.cast(Resolution, retained.advisory_attempts[1]))


# ------------------------------------------------------------------ V-T6 / V-T6a evaluation


_ADJUSTED: list[tuple[int | None, int | None]] = [
    (None, None),
    (0, 0),
    (0, 100),
    (100, 0),
    (None, 0),
    (0, None),
]


@pytest.mark.parametrize("verdict", list(Verdict), ids=[verdict.value for verdict in Verdict])
@pytest.mark.parametrize("adjusted", _ADJUSTED, ids=[f"{a}-{b}" for a, b in _ADJUSTED])
def test_every_verdict_keeps_none_and_zero_exactly(
    tmp_path: Path, verdict: schema.ColdSafetyVerdict, adjusted: tuple[int | None, int | None]
) -> None:
    """V-T6a/AM27/AM28: adjusted values follow the settled grammar; no verdict rule."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    carried = evaluation(verdict=verdict, adjusted_heat=adjusted[0], adjusted_fan=adjusted[1])
    record = resolve(off, 0, evaluation=carried)
    kept = (record.evaluation_adjusted_heat, record.evaluation_adjusted_fan)
    assert kept == adjusted
    assert [type(value) for value in kept] == [type(value) for value in adjusted]
    assert record.evaluation_verdict is verdict
    assert advisory.validate_advisory_attempt_record(record) == record


def test_none_and_zero_adjusted_values_survive_the_store(tmp_path: Path) -> None:
    """V-T6a: every verdict's ``None`` and ``0`` adjusted values reload unchanged."""
    writer, root, off = open_off(tmp_path)
    records: list[Record] = []
    for index, verdict in enumerate(Verdict):
        adjusted = _ADJUSTED[4] if index % 2 else _ADJUSTED[5]
        carried = evaluation(verdict=verdict, adjusted_heat=adjusted[0], adjusted_fan=adjusted[1])
        records.extend(attempt(off, index, 10.0 * index + 10.0, evaluation=carried))
    retained = write_read(writer, root, records)
    assert list(retained.advisory_attempts) == records
    reloaded = [item for item in retained.advisory_attempts if isinstance(item, Resolution)]
    assert [(item.evaluation_adjusted_heat, item.evaluation_adjusted_fan) for item in reloaded] == [
        _ADJUSTED[4] if index % 2 else _ADJUSTED[5] for index in range(len(Verdict))
    ]


def carrier(**overrides: typing.Any) -> schema.ColdSafetyEvaluation:
    """Construct an unvalidated evaluation carrier with raw overrides."""
    values: dict[str, typing.Any] = {**raw(evaluation()), **overrides}
    return schema.ColdSafetyEvaluation.model_construct(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"adjusted_heat": 101},
        {"adjusted_fan": -1},
        {"adjusted_heat": True},
        {"adjusted_fan": 1.0},
        {"input_heat": 41},
        {"input_fan": 61},
        {"rule": ""},
        {"reason": ""},
        {"rule": "r" * 2049},
    ],
    ids=[
        "adjusted-101",
        "adjusted-negative",
        "adjusted-bool",
        "adjusted-float",
        "input-heat",
        "input-fan",
        "empty-rule",
        "empty-reason",
        "long-rule",
    ],
)
def test_evaluation_values_outside_the_grammar_refuse(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """V-T6/V-T6a: ranges, exact ints, non-empty text, and the request binding hold."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    expect_evidence(NOT_VALIDATED, lambda: resolve(off, 0, evaluation=carrier(**overrides)))


@pytest.mark.parametrize(
    "update",
    [
        {"evaluation_adjusted_heat": 101},
        {"evaluation_input_heat": 41},
        {"evaluation_input_fan": 61},
    ],
    ids=["adjusted-101", "input-heat", "input-fan"],
)
def test_evaluation_record_layers_refuse_independently(
    tmp_path: Path, update: dict[str, object]
) -> None:
    """Lead §10: direct model validation raises; the wrapper refuses closed."""
    record = resolve(header_for(tmp_path, str(tmp_path.resolve()), OFF), 0)
    with pytest.raises(pydantic.ValidationError):
        Resolution.model_validate({**raw(record), **update}, strict=True)
    forged = record.model_copy(update=update)
    expect_evidence(NOT_VALIDATED, lambda: advisory.validate_advisory_attempt_record(forged))


class _EvaluationSub(schema.ColdSafetyEvaluation):
    """A subclass carrier that must never be flattened."""


class _UsageSub(advisory.ColdAdvisoryUsageReading):
    """A subclass usage reading that must never be flattened."""


class _Text(str):
    """A ``str`` subclass that must never pass as exact text."""


class _Context(dict[str, object]):
    """A ``dict`` subclass that must never pass as an exact context."""


def _with_extra_key(model: pydantic.BaseModel) -> pydantic.BaseModel:
    """Inject one undeclared ``__dict__`` key."""
    object.__getattribute__(model, "__dict__")["smuggled"] = 1
    return model


def _without_key(model: pydantic.BaseModel, name: str) -> pydantic.BaseModel:
    """Remove one declared ``__dict__`` key."""
    del object.__getattribute__(model, "__dict__")[name]
    return model


def _with_pydantic_extra(model: pydantic.BaseModel) -> pydantic.BaseModel:
    """Set a non-empty ``__pydantic_extra__`` slot."""
    object.__setattr__(model, "__pydantic_extra__", {"smuggled": 1})
    return model


def _forged_evaluations() -> dict[str, object]:
    """Return every forged evaluation carrier shape."""
    subclass = _EvaluationSub(**raw(evaluation()))
    return {
        "subclass": subclass,
        "extra-key": _with_extra_key(carrier()),
        "missing-key": _without_key(carrier(), "adjusted_fan"),
        "pydantic-extra": _with_pydantic_extra(carrier()),
        "non-member-verdict": carrier(verdict=object.__new__(Verdict)),
        "foreign-member-verdict": carrier(verdict=schema.ColdPhaseKind.RECORDING_OFF),
        "str-subclass-rule": carrier(rule=_Text("rule")),
        "str-input": carrier(input_heat="40"),
        "int-rule": carrier(rule=7),
        "bool-adjusted": carrier(adjusted_fan=True),
        "not-a-model": {"rule": "rule"},
    }


_EVALUATION_FORGERIES = list(_forged_evaluations())


@pytest.mark.parametrize("label", _EVALUATION_FORGERIES)
def test_forged_evaluation_carriers_refuse_before_flattening(tmp_path: Path, label: str) -> None:
    """V-T6/V-T21/AM25: a forged carrier refuses closed with no silent drop."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    forged = _forged_evaluations()[label]
    expect_evidence(NOT_VALIDATED, lambda: resolve(off, 0, evaluation=forged))


def test_carrier_admission_uses_the_closed_per_field_table() -> None:
    """V-T21/AM29: the helper itself refuses each mistyped field; exact carriers pass."""
    admit = advisory._admit_carrier  # pyright: ignore[reportPrivateUsage]
    table = advisory._EVALUATION_CARRIER  # pyright: ignore[reportPrivateUsage]
    usage_table = advisory._USAGE_CARRIER  # pyright: ignore[reportPrivateUsage]
    assert admit(evaluation(adjusted_heat=0), table) == raw(evaluation(adjusted_heat=0))
    assert admit(USAGE, usage_table) == raw(USAGE)
    assert set(table[1]) == set(schema.ColdSafetyEvaluation.model_fields)
    assert set(usage_table[1]) == set(advisory.ColdAdvisoryUsageReading.model_fields)
    for forged in (
        carrier(input_heat="40"),
        carrier(verdict=schema.ColdPhaseKind.RECORDING_OFF),
        carrier(verdict=object.__new__(Verdict)),
        carrier(adjusted_fan=True),
        carrier(rule=7),
        carrier(reason=None),
    ):
        expect_evidence(NOT_VALIDATED, lambda forged=forged: admit(forged, table))
    for value in (True, 1.0, Verdict.ALLOW, None):
        reading = usage_carrier(total_tokens=value)
        expect_evidence(NOT_VALIDATED, lambda reading=reading: admit(reading, usage_table))
    expect_evidence(NOT_VALIDATED, lambda: admit(USAGE, table))


# ------------------------------------------------------------------ V-T7 usage


def test_zero_usage_is_recorded_as_production_normalised(tmp_path: Path) -> None:
    """V-T7: all-zero usage is admitted with its basis, never as provider-reported zero."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    zeros = advisory.ColdAdvisoryUsageReading(
        input_tokens=0, output_tokens=0, total_tokens=0, reasoning_tokens=0
    )
    record = resolve(off, 0, usage=zeros)
    assert (record.usage_input_tokens, record.usage_reasoning_tokens) == (0, 0)
    assert record.usage_basis is advisory.ColdAdvisoryUsageBasis.PRODUCTION_NORMALISED
    assert resolve(off, 0, usage=USAGE).usage_reasoning_tokens is None
    assert set(advisory.ColdAdvisoryUsageReading.model_fields) == set(AdvisorUsage.model_fields)


@pytest.mark.parametrize("value", [True, -1, 1.0], ids=["bool", "negative", "float"])
def test_usage_reading_refuses_non_exact_counts(value: object) -> None:
    """V-T7: the usage reading itself refuses coerced or negative counts."""
    with pytest.raises(pydantic.ValidationError):
        advisory.ColdAdvisoryUsageReading.model_validate({**raw(USAGE), "input_tokens": value})


def usage_carrier(**overrides: typing.Any) -> advisory.ColdAdvisoryUsageReading:
    """Construct an unvalidated usage carrier with raw overrides."""
    values: dict[str, typing.Any] = {**raw(USAGE), **overrides}
    return advisory.ColdAdvisoryUsageReading.model_construct(**values)


def _forged_usages() -> dict[str, object]:
    """Return every forged usage carrier shape."""
    construct = usage_carrier
    return {
        "bool": construct(input_tokens=True),
        "negative": construct(output_tokens=-1),
        "float": construct(total_tokens=1.0),
        "verdict-member": construct(reasoning_tokens=Verdict.ALLOW),
        "subclass": _UsageSub(**raw(USAGE)),
        "extra-key": _with_extra_key(construct()),
        "missing-key": _without_key(construct(), "reasoning_tokens"),
        "pydantic-extra": _with_pydantic_extra(construct()),
        "advisor-usage": AdvisorUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    }


@pytest.mark.parametrize("label", list(_forged_usages()))
def test_forged_usage_carriers_refuse_before_flattening(tmp_path: Path, label: str) -> None:
    """V-T7/AM26: a forged usage carrier refuses closed with no silent drop."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    forged = _forged_usages()[label]
    expect_evidence(NOT_VALIDATED, lambda: resolve(off, 0, usage=forged))


# ------------------------------------------------------------------ V-T8 context


def test_a_real_advisor_context_round_trips(tmp_path: Path) -> None:
    """V-T8: a real context's ``mode="json"`` dump is retained and revalidates equal."""
    context = AdvisorContext(
        phase=RoastPhase.PREHEATING,
        roast_elapsed_seconds=0.0,
        development_elapsed_seconds=None,
        current_bean_temp_c=24.5,
        current_env_temp_c=25.0,
        bean_ror_c_per_min=None,
        env_ror_c_per_min=None,
        target_drop_temp_c=205.0,
        charge_guidance_min_c=180.0,
        charge_guidance_max_c=190.0,
        profile_name=PROFILE,
    )
    dumped = context.model_dump(mode="json")
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = intend(off, context=dumped, charge_guidance_min_c=180.0)
    assert AdvisorContext.model_validate(json.loads(record.context_canonical_json)) == context
    assert record.context_canonical_json == store.canonical_json(dumped)
    encoded = record.context_canonical_json.encode()
    assert record.context_byte_length == len(encoded)
    assert record.context_sha256 == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_name": "Another profile"},
        {"target_drop_temp_c": 206.0},
        {"charge_guidance_min_c": 180.0},
        {"charge_guidance_max_c": None},
        {"context": {key: value for key, value in CONTEXT.items() if key != "profile_name"}},
        {"context": {**CONTEXT, "target_drop_temp_c": 205}},
        {"context": {**CONTEXT, "charge_guidance_max_c": "190.0"}},
    ],
    ids=[
        "profile",
        "target",
        "null-against-float",
        "float-against-null",
        "missing-key",
        "int-against-float",
        "str-against-float",
    ],
)
def test_spec_and_context_must_agree_exactly(
    tmp_path: Path, overrides: dict[str, typing.Any]
) -> None:
    """V-T8/AM11: each spec value equals its context key by exact type and value."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    expect_evidence(NOT_VALIDATED, lambda: intend(off, **overrides))


def _with_text(record: Intent, text: str, **update: object) -> dict[str, object]:
    """Return a raw update replacing the context text with a self-consistent digest."""
    encoded = text.encode("utf-8", "surrogatepass")
    return {
        "context_canonical_json": text,
        "context_byte_length": len(encoded),
        "context_sha256": hashlib.sha256(encoded).hexdigest(),
        **update,
    }


def _padded(
    length: int, pad: str = "x", base: dict[str, object] | None = None
) -> dict[str, object]:
    """Return a context whose canonical text has exactly ``length`` characters."""
    document: dict[str, object] = {**(CONTEXT if base is None else base), "pad": ""}
    per = len(store.canonical_json(pad)) - 2
    missing = length - len(store.canonical_json(document))
    assert missing >= 0 and missing % per == 0
    document["pad"] = pad * (missing // per)
    assert len(store.canonical_json(document)) == length
    return document


_CANONICAL = store.canonical_json(CONTEXT)
_SHA = hashlib.sha256(_CANONICAL.encode()).hexdigest()
_TEXT_CASES: dict[str, typing.Callable[[Intent], dict[str, object]]] = {
    "not-canonical": lambda r: _with_text(r, json.dumps(CONTEXT)),
    "duplicate-key": lambda r: _with_text(
        r, _CANONICAL[:-1] + ',"profile_name":"' + PROFILE + '"}'
    ),
    "nan-literal": lambda r: _with_text(r, _CANONICAL.replace("24.5", "NaN")),
    "not-an-object": lambda r: _with_text(r, "[1]"),
    "not-json": lambda r: _with_text(r, "{"),
    "too-deep-for-the-parser": lambda r: _with_text(r, "[" * 5000 + "]" * 5000),
    "too-deep-for-the-walker": lambda r: _with_text(
        r, store.canonical_json({**CONTEXT, "nested": [[[[[[[[[[1]]]]]]]]]]})
    ),
    "surrogate": lambda r: _with_text(r, _CANONICAL.replace("preheating", "\ud800")),
    "chars-over-cap": lambda r: _with_text(r, store.canonical_json(_padded(65_537))),
    "bytes-over-cap": lambda r: _with_text(r, store.canonical_json(_padded(65_000, "€"))),
    "wrong-length": lambda r: {"context_byte_length": r.context_byte_length + 1},
    "wrong-digest": lambda r: {"context_sha256": "0" * 64},
    "uppercase-digest": lambda r: {"context_sha256": _SHA.upper()},
    "short-digest": lambda r: {"context_sha256": _SHA[:63]},
    "non-hex-digest": lambda r: {"context_sha256": "z" * 64},
    "tick-after-recording": lambda r: {"context_tick_monotonic": 10.5},
}


@pytest.mark.parametrize("label", list(_TEXT_CASES))
def test_context_grammar_refuses_at_both_layers(tmp_path: Path, label: str) -> None:
    """V-T8/V-T22: canonical text, length, digest, parse, and walk all bind the context."""
    record = intend(header_for(tmp_path, str(tmp_path.resolve()), OFF))
    update = _TEXT_CASES[label](record)
    with pytest.raises(pydantic.ValidationError):
        Intent.model_validate({**raw(record), **update}, strict=True)
    forged = record.model_copy(update=update)
    with pytest.raises(schema.ColdEvidenceError) as raised:
        advisory.validate_advisory_attempt_record(forged)
    expected = (
        schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED
        if label == "surrogate"
        else NOT_VALIDATED
    )
    assert raised.value.failure is expected


def test_context_size_bound_is_exact_at_the_builder(tmp_path: Path) -> None:
    """V-T8: 65,536 canonical characters are retained; 65,537 are refused."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    assert intend(off, context=_padded(65_536)).context_byte_length == 65_536
    expect_evidence(NOT_VALIDATED, lambda: intend(off, context=_padded(65_537)))


def test_canonical_text_is_the_store_canonical_json() -> None:
    """V-T8: the module-local canonicaliser equals the store's, byte for byte."""
    sample = {"b": [1, 2.5, None, True], "a": {"é": '€\n\x01"\\'}, "c": -0.0}
    text = advisory._canonical_text(sample)  # pyright: ignore[reportPrivateUsage]
    assert text.encode() == store.canonical_json(sample).encode()


def test_worst_case_valid_records_fit_the_real_line_limit(tmp_path: Path) -> None:
    """V-T8 size proof: maximal valid lines stay within ``MAX_RECORD_BYTES``."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    control = "\x01" * 2048
    context = _padded(65_536, '"', {**CONTEXT, "profile_name": control})
    intent = intend(
        off,
        context=context,
        profile_name=control,
        provider=control,
        model=control,
        prompt_version=control,
    )
    rule = carrier(rule=control, reason=control)
    resolution = resolve(off, 0, rationale=control, evaluation=rule)
    assert resolution.rationale_state is Rationale.RETAINED
    for record in (intent, resolution):
        size = len(store.canonical_json(record.model_dump(mode="json")).encode())
        assert size <= schema.MAX_RECORD_BYTES
    # Heaviness floor: the double-escaped quote context makes this a genuinely large line.
    assert len(store.canonical_json(intent.model_dump(mode="json"))) > 160_000


def test_lowered_line_limit_exercises_the_defensive_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-T8/AM13, test-only lowering of a resource limit (restored by monkeypatch).

    No valid record reaches the real limit; this is not a production-size claim.
    """
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = intend(off)
    monkeypatch.setattr(advisory, "_RECORD_LINE_LIMIT", 512)
    too_large = schema.ColdEvidenceFailure.RECORD_TOO_LARGE
    expect_evidence(too_large, lambda: intend(off))
    expect_evidence(too_large, lambda: advisory.validate_advisory_attempt_record(record))


# ------------------------------------------------------------------ V-T9 scalars and forgery


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt_index": True},
        {"monotonic_seconds": 10},
        {"monotonic_seconds": float("nan")},
        {"monotonic_seconds": float("inf")},
        {"monotonic_seconds": -1.0},
        {"context_tick_monotonic": 20.0},
        {"context_tick": -1},
        {"configured_call_bound_seconds": 0.0},
        {"configured_dwell_seconds": 5},
        {"configured_dwell_seconds": float("inf")},
        {"provider": "   "},
        {"provider": _Text("openrouter")},
        {"model": "m" * 2049},
        {"prompt_version": "\ud800"},
        {"recorded_at_utc": "2026-09-26T13:00:00+01:00"},
        {"recorded_at_utc": "yesterday"},
        {"context": [CONTEXT]},
        {"context": _Context(CONTEXT)},
    ],
    ids=lambda overrides: (
        next(iter(overrides)) + "-" + type(next(iter(overrides.values()))).__name__
    ),
)
def test_intent_scalars_are_exact(tmp_path: Path, overrides: dict[str, typing.Any]) -> None:
    """V-T9/V-T20: every non-exact or out-of-grammar intent argument refuses closed."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    expect_evidence(NOT_VALIDATED, lambda: intend(off, **overrides))


class _HeaderSub(schema.ColdRunHeader):
    """A header subclass that must never be bound."""


def test_builders_refuse_a_non_exact_header(tmp_path: Path) -> None:
    """V-T9: both builders admit only an exact, revalidated phase header."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    for header in (None, _HeaderSub.model_construct(**raw(off))):
        forged = typing.cast(schema.ColdRunHeader, header)
        expect_evidence(NOT_VALIDATED, lambda forged=forged: intend(forged))
        expect_evidence(NOT_VALIDATED, lambda forged=forged: resolve(forged))


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": 1.0000001},
        {"confidence": float("nan")},
        {"requested_heat": 101, "evaluation": evaluation(heat=101)},
        {"requested_fan": -1, "evaluation": evaluation(fan=-1)},
        {"should_drop": 1},
        {"rationale": ["x"] * 3000},
        {"evaluation": {"rule": "rule"}},
        {"usage": {"input_tokens": 1}},
        {"resolution": "returned_decision"},
        {"resolution": object.__new__(Kind)},
    ],
    ids=[
        "confidence-high",
        "confidence-nan",
        "heat-101",
        "fan-negative",
        "drop-int",
        "rationale-list",
        "evaluation-dict",
        "usage-dict",
        "resolution-str",
        "resolution-fake",
    ],
)
def test_resolution_scalars_are_exact(tmp_path: Path, overrides: dict[str, typing.Any]) -> None:
    """V-T9/V-T20/AM30: a non-``str`` rationale never fabricates a not-retained state."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    remaining = dict(overrides)
    kind = remaining.pop("resolution", Kind.RETURNED_DECISION)
    expect_evidence(NOT_VALIDATED, lambda: _resolve_raw(off, kind, remaining))


def _resolve_raw(
    header: schema.ColdRunHeader, kind: object, overrides: dict[str, typing.Any]
) -> object:
    """Call the resolution builder with an unchecked resolution argument."""
    payload = {**decision_payload(), **overrides}
    return advisory.build_advisory_resolution_record(
        header=header,
        attempt_index=0,
        recorded_at_utc=T0,
        monotonic_seconds=13.0,
        resolution=typing.cast(advisory.ColdAdvisoryResolution, kind),
        invocation_utc=T0,
        invocation_monotonic=11.0,
        resolved_utc=T0,
        resolved_monotonic=12.0,
        **payload,
    )


_FIELD_FORGERIES: dict[str, tuple[bool, dict[str, object]]] = {
    "str-subclass-stream": (True, {"stream": _Text("advisory_attempt")}),
    "non-str-digest-text": (True, {"context_sha256": 7}),
    "run-id-grammar": (True, {"run_id": "not-a-run-id"}),
    "digest-grammar": (True, {"identity_sha256": "x" * 64}),
    "int-temperature": (True, {"context_target_drop_temp_c": 205}),
    "long-rationale": (False, {"rationale": "x" * 2049}),
    "non-str-rationale": (False, {"rationale": 5}),
    "non-str-reason": (False, {"evaluation_reason": 5}),
    "str-evaluation-input": (False, {"evaluation_input_heat": "40"}),
    "int-drop": (False, {"should_drop": 1}),
}


@pytest.mark.parametrize("label", list(_FIELD_FORGERIES))
def test_field_admission_refuses_at_both_layers(tmp_path: Path, label: str) -> None:
    """V-T9: field rules the builders' pre-admission masks still refuse forged records."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    is_intent, update = _FIELD_FORGERIES[label]
    record: Record = intend(off) if is_intent else resolve(off, 0)
    with pytest.raises(pydantic.ValidationError):
        type(record).model_validate({**raw(record), **update}, strict=True)
    forged = record.model_copy(update=update)
    expect_evidence(NOT_VALIDATED, lambda: advisory.validate_advisory_attempt_record(forged))


def test_a_non_utc_invocation_instant_refuses(tmp_path: Path) -> None:
    """V-T9: the invocation instant uses the same UTC grammar."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    record = resolve(off, 0)
    expect_evidence(
        NOT_VALIDATED,
        lambda: advisory.validate_advisory_attempt_record(
            record.model_copy(update={"invocation_utc": "2026-09-26T13:00:00+01:00"})
        ),
    )


class _Foreign(enum.Enum):
    """A foreign enum whose member must never pass as a phase."""

    RECORDING_OFF = "recording_off"


class _IntentSub(Intent):
    """A record subclass that must never be admitted."""


def _forged_records(off: schema.ColdRunHeader) -> dict[str, Record]:
    """Return every forged record shape, each built from a valid intent or resolution."""
    intent = intend(off)
    return {
        "extra-key": typing.cast(Record, _with_extra_key(intend(off))),
        "missing-key": typing.cast(Record, _without_key(intend(off), "descriptor_model")),
        "pydantic-extra": typing.cast(Record, _with_pydantic_extra(intend(off))),
        "matrix-copy": resolve(off, 0).model_copy(update={"usage_state": None}),
        "foreign-enum": intent.model_copy(update={"phase": _Foreign.RECORDING_OFF}),
        "fake-member": intent.model_copy(update={"phase": object.__new__(schema.ColdPhaseKind)}),
        "subclass": _IntentSub.model_construct(**raw(intent)),
        "int-for-float": intent.model_copy(update={"configured_dwell_seconds": 5}),
        "bool-for-int": intent.model_copy(update={"attempt_index": True}),
        "non-str-key": typing.cast(Record, _with_key(intend(off), 1)),
    }


def _with_key(model: pydantic.BaseModel, key: object) -> pydantic.BaseModel:
    """Inject one non-``str`` ``__dict__`` key."""
    object.__getattribute__(model, "__dict__")[key] = 1
    return model


@pytest.mark.parametrize(
    "label",
    [
        "extra-key",
        "missing-key",
        "pydantic-extra",
        "matrix-copy",
        "foreign-enum",
        "fake-member",
        "subclass",
        "int-for-float",
        "bool-for-int",
        "non-str-key",
    ],
)
def test_forged_records_refuse_at_validator_and_writer(tmp_path: Path, label: str) -> None:
    """V-T9/AM14/AM15/AM22: forgeries refuse in both places; the writer stays usable."""
    writer, root, off = open_off(tmp_path)
    forged = _forged_records(off)[label]
    expect_evidence(NOT_VALIDATED, lambda: advisory.validate_advisory_attempt_record(forged))
    expect_evidence(NOT_VALIDATED, lambda: writer.append_advisory_attempt(forged))
    retained = write_read(writer, root, [intend(off)])
    assert retained.advisory_attempt_state is State.OPEN_TAIL


# ------------------------------------------------------------------ V-T10 writer order


def resolved_at(
    header: schema.ColdRunHeader,
    index: int,
    *,
    invoked: float | None,
    resolved: float,
    recorded: float,
    kind: advisory.ColdAdvisoryResolution = Kind.RETURNED_DECISION,
) -> Resolution:
    """Build one resolution at exact instants."""
    payload = decision_payload() if kind is Kind.RETURNED_DECISION else {}
    return advisory.build_advisory_resolution_record(
        header=header,
        attempt_index=index,
        recorded_at_utc=T0,
        monotonic_seconds=recorded,
        resolution=kind,
        invocation_utc=None if invoked is None else T0,
        invocation_monotonic=invoked,
        resolved_utc=T0,
        resolved_monotonic=resolved,
        **payload,
    )


def _order_state(writer: store.ColdEvidenceWriter) -> tuple[object, int, int]:
    """Return the writer's private order state (white-box)."""
    sequence = writer._advisory  # pyright: ignore[reportPrivateUsage]
    return sequence.open_attempt, sequence.next_index(OFF), sequence.next_index(ON)


def test_writer_order_refusals_leave_state_and_writer_usable(tmp_path: Path) -> None:
    """V-T10/AM16/AM18/AM22: each refusal has its exact member and changes nothing."""
    writer, root, off = open_off(tmp_path)
    append = writer.append_advisory_attempt
    expect_attempt(Order.ATTEMPT_INDEX_NOT_CONTIGUOUS, lambda: append(intend(off, 1)))
    expect_attempt(Order.RESOLUTION_WITHOUT_OPEN_ATTEMPT, lambda: append(resolve(off, 0)))
    assert _order_state(writer) == (None, 0, 0)
    append(intend(off, 0, at=10.0))
    expect_attempt(Order.ATTEMPT_ALREADY_OPEN, lambda: append(intend(off, 1, at=11.0)))
    expect_attempt(Order.RESOLUTION_NOT_MATCHING, lambda: append(resolve(off, 1)))
    expect_attempt(
        Order.OBSERVED_TIME_REGRESSED,
        lambda: append(resolved_at(off, 0, invoked=9.0, resolved=11.0, recorded=12.0)),
    )
    expect_attempt(
        Order.OBSERVED_TIME_REGRESSED,
        lambda: append(
            resolved_at(
                off, 0, invoked=None, resolved=9.5, recorded=12.0, kind=Kind.UNRESOLVED_AT_PHASE_END
            )
        ),
    )
    assert _order_state(writer) == ((OFF, 0), 1, 0)
    append(resolve(off, 0, start=10.0))
    expect_attempt(Order.ATTEMPT_INDEX_NOT_CONTIGUOUS, lambda: append(intend(off, 0, at=14.0)))
    expect_attempt(Order.OBSERVED_TIME_REGRESSED, lambda: append(intend(off, 1, at=12.5)))
    assert _order_state(writer) == (None, 1, 0)
    final = intend(off, 1, at=13.0)
    retained = write_read(writer, root, [final])
    assert [record.attempt_index for record in retained.advisory_attempts] == [0, 0, 1]
    assert retained.advisory_attempt_state is State.OPEN_TAIL


def test_writer_admits_equal_instants(tmp_path: Path) -> None:
    """V-T10/AM19: intent, invocation, resolution, record, and next intent may tie."""
    writer, root, off = open_off(tmp_path)
    records: list[Record] = [
        intend(off, 0, at=10.0),
        resolved_at(off, 0, invoked=10.0, resolved=10.0, recorded=10.0),
        intend(off, 1, at=10.0),
        resolved_at(
            off, 1, invoked=None, resolved=10.0, recorded=10.0, kind=Kind.UNRESOLVED_AT_PHASE_END
        ),
    ]
    assert list(write_read(writer, root, records).advisory_attempts) == records


def test_writer_outer_layers_refuse_before_the_sequence(tmp_path: Path) -> None:
    """V-T10/A6: missing header, then latest-phase, refuse before ordering; state holds."""
    writer, root, off = open_off(tmp_path)
    append = writer.append_advisory_attempt
    for record in attempt(off, 0, 10.0):
        append(record)
    on = on_header(tmp_path, root)
    expect(Failure.HEADER_MISSING, lambda: append(intend(on, 0, at=1300.0)))
    writer.append(on)
    expect_attempt(Order.PHASE_NOT_LATEST, lambda: append(intend(off, 1, at=1300.0)))
    assert _order_state(writer) == (None, 1, 0)
    other = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-other-run")
    expect(Failure.RUN_ID_MISMATCHED, lambda: append(intend(other, 1, at=1300.0)))
    records: list[Record] = [intend(on, 0, at=1300.0)]
    retained = write_read(writer, root, records)
    assert [record.phase for record in retained.advisory_attempts] == [OFF, OFF, ON]


def test_writer_refuses_an_open_attempt_crossing_into_a_bound_phase(tmp_path: Path) -> None:
    """V-T10/AM17/A6: with ON bound, an ON line while OFF is open crosses the phase."""
    writer, root, off = open_off(tmp_path)
    append = writer.append_advisory_attempt
    append(intend(off, 0, at=10.0))
    on = bind_on(tmp_path, writer, root)
    expect_attempt(Order.OPEN_ATTEMPT_CROSSES_PHASE, lambda: append(intend(on, 0, at=1300.0)))
    expect_attempt(Order.OPEN_ATTEMPT_CROSSES_PHASE, lambda: append(resolve(on, 3, start=1300.0)))
    expect_attempt(Order.PHASE_NOT_LATEST, lambda: append(resolve(off, 0)))
    assert _order_state(writer) == ((OFF, 0), 1, 0)
    writer.append(tick_for(on, 1))
    retained = read3(root, writer.seal().manifest_sha256)
    assert retained.advisory_attempt_state is State.OPEN_TAIL


# ------------------------------------------------------------------ V-T10a pure sequence priority


def _accept(sequence: advisory.ColdAdvisorySequence, record: Record) -> None:
    """Check then commit one record on the pure sequence."""
    sequence.check(record)
    sequence.commit(record)


def test_pure_sequence_refusal_priority_and_state(tmp_path: Path) -> None:
    """V-T10a/AM31/AM32/AM33: exact priority; a refusal changes nothing."""
    root = str(tmp_path.resolve())
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    regressed = advisory.ColdAdvisorySequence()
    _accept(regressed, intend(on, 0, at=1300.0))
    expect_attempt(Order.PHASE_REGRESSED, lambda: regressed.check(intend(off, 0, at=1301.0)))
    expect_attempt(Order.PHASE_REGRESSED, lambda: regressed.check(resolve(off, 0, start=1300.0)))
    assert (regressed.open_attempt, regressed.next_index(ON), regressed.next_index(OFF)) == (
        (ON, 0),
        1,
        0,
    )
    _accept(regressed, resolve(on, 0, start=1300.0))
    crossing = advisory.ColdAdvisorySequence()
    _accept(crossing, intend(off, 0, at=10.0))
    expect_attempt(
        Order.OPEN_ATTEMPT_CROSSES_PHASE, lambda: crossing.check(resolve(on, 5, start=1300.0))
    )
    expect_attempt(Order.RESOLUTION_NOT_MATCHING, lambda: crossing.check(resolve(off, 5)))
    assert (crossing.open_attempt, crossing.next_index(OFF)) == ((OFF, 0), 1)
    _accept(crossing, resolve(off, 0))
    expect_attempt(Order.OBSERVED_TIME_REGRESSED, lambda: crossing.check(intend(off, 1, at=12.0)))
    assert (crossing.open_attempt, crossing.next_index(OFF)) == (None, 1)
    _accept(crossing, intend(off, 1, at=13.0))
    assert crossing.open_attempt == (OFF, 1)


# ------------------------------------------------------------------ V-T11 reader order


def two_phase_root(tmp_path: Path) -> tuple[str, schema.ColdRunHeader, schema.ColdRunHeader]:
    """Seal a run with both headers and no advisory stream."""
    writer, root, off = open_off(tmp_path)
    on = bind_on(tmp_path, writer, root)
    writer.seal()
    return root, off, on


_READER_ORDER: dict[
    str,
    tuple[
        typing.Callable[
            [schema.ColdRunHeader, schema.ColdRunHeader], tuple[list[Record], list[Record]]
        ],
        advisory.ColdAdvisoryAttemptFailure,
    ],
] = {
    "gap": (lambda off, on: ([intend(off, 1)], []), Order.ATTEMPT_INDEX_NOT_CONTIGUOUS),
    "repeat": (
        lambda off, on: ([*attempt(off, 0, 10.0), intend(off, 0, at=20.0)], []),
        Order.ATTEMPT_INDEX_NOT_CONTIGUOUS,
    ),
    "two-trailing-intents": (
        lambda off, on: ([intend(off, 0), intend(off, 1, at=11.0)], []),
        Order.ATTEMPT_ALREADY_OPEN,
    ),
    "resolution-first": (
        lambda off, on: ([resolve(off, 0)], []),
        Order.RESOLUTION_WITHOUT_OPEN_ATTEMPT,
    ),
    "mismatched-index": (
        lambda off, on: ([intend(off, 0), resolve(off, 1)], []),
        Order.RESOLUTION_NOT_MATCHING,
    ),
    "invocation-before-intent": (
        lambda off, on: (
            [intend(off, 0), resolved_at(off, 0, invoked=9.0, resolved=11.0, recorded=12.0)],
            [],
        ),
        Order.OBSERVED_TIME_REGRESSED,
    ),
    "intent-before-previous-record": (
        lambda off, on: ([*attempt(off, 0, 10.0), intend(off, 1, at=12.0)], []),
        Order.OBSERVED_TIME_REGRESSED,
    ),
    "trailing-off-intent-then-on": (
        lambda off, on: ([intend(off, 0)], [intend(on, 0, at=1300.0)]),
        Order.OPEN_ATTEMPT_CROSSES_PHASE,
    ),
    "off-open-on-resolution-wrong-index": (
        lambda off, on: ([intend(off, 0)], [resolve(on, 3, start=1300.0)]),
        Order.OPEN_ATTEMPT_CROSSES_PHASE,
    ),
}


@pytest.mark.parametrize("label", list(_READER_ORDER))
def test_reader_refuses_crafted_order_violations(tmp_path: Path, label: str) -> None:
    """V-T11/V-T11a: crafted on-disk violations refuse with the same members as the writer."""
    root, off, on = two_phase_root(tmp_path)
    plan, failure = _READER_ORDER[label]
    off_records, on_records = plan(off, on)
    digest = rewrite(root, ADVISORY_OFF, lines(off_records))
    if on_records:
        digest = rewrite(root, ADVISORY_ON, lines(on_records))
    expect_attempt(failure, lambda: read3(root, digest))
    rewrite(root, ADVISORY_OFF, lines(attempt(off, 0, 10.0)))
    repaired = rewrite(root, ADVISORY_ON, lines(attempt(on, 0, 1300.0)))
    assert read3(root, repaired).advisory_attempt_state is State.COMPLETE


def test_reader_refuses_wrong_file_phase_before_ordering(tmp_path: Path) -> None:
    """V-T11/A6: an OFF line in the ON file is malformed, so no regression reaches ordering."""
    root, off, _on = two_phase_root(tmp_path)
    digest = rewrite(root, ADVISORY_ON, lines([resolve(off, 0)]))
    expect(Failure.LINE_MALFORMED, lambda: read3(root, digest))


def test_reader_binds_run_and_identity(tmp_path: Path) -> None:
    """V-T11: a foreign run id or identity digest refuses at binding."""
    root, _off, _on = two_phase_root(tmp_path)
    foreign_run = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-other-run")
    digest = rewrite(root, ADVISORY_OFF, lines([intend(foreign_run, 0)]))
    expect(Failure.RUN_ID_MISMATCHED, lambda: read3(root, digest))
    foreign_identity = header_for_device(tmp_path, root)
    digest = rewrite(root, ADVISORY_OFF, lines([intend(foreign_identity, 0)]))
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: read3(root, digest))


def header_for_device(tmp_path: Path, root: str) -> schema.ColdRunHeader:
    """Build a recording-off header with a different identity digest."""
    return builders.build_run_header(
        identity=make_identity(tmp_path, pi_root=root, audio_device="USB microphone two"),
        phase=OFF,
        recorded_at_utc=T0,
        monotonic_seconds=1.0,
    )


# ------------------------------------------------------------------ V-T12 reader hardening


def _resolution_document(off: schema.ColdRunHeader) -> dict[str, typing.Any]:
    """Return the default resolution's JSON document."""
    return resolve(off, 0).model_dump(mode="json")


_HARDENING: dict[
    str,
    tuple[
        typing.Callable[[dict[str, typing.Any], dict[str, typing.Any]], bytes],
        store.ColdEvidenceStoreFailure,
    ],
] = {
    "version-1": (
        lambda d, _l: line_of({**d, "schema_version": 1}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    "version-3": (
        lambda d, _l: line_of({**d, "schema_version": 3}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    "version-true": (
        lambda d, _l: line_of({**d, "schema_version": True}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    "version-text": (
        lambda d, _l: line_of({**d, "schema_version": "2"}),
        Failure.SCHEMA_VERSION_UNKNOWN,
    ),
    "lifecycle-line": (lambda _d, life: line_of(life), Failure.LINE_MALFORMED),
    "unknown-entry": (lambda d, _l: line_of({**d, "entry": "pending"}), Failure.LINE_MALFORMED),
    "capitalised-entry": (lambda d, _l: line_of({**d, "entry": "Intent"}), Failure.LINE_MALFORMED),
    "entry-not-text": (lambda d, _l: line_of({**d, "entry": 1}), Failure.LINE_MALFORMED),
    "intent-entry-on-resolution": (
        lambda d, _l: line_of({**d, "entry": "intent"}),
        Failure.LINE_MALFORMED,
    ),
    "wrong-stream": (lambda d, _l: line_of({**d, "stream": "advisory"}), Failure.LINE_MALFORMED),
    "stream-not-text": (lambda d, _l: line_of({**d, "stream": 2}), Failure.LINE_MALFORMED),
    "unknown-field": (lambda d, _l: line_of({**d, "zz_unknown": 1}), Failure.LINE_MALFORMED),
    "wrong-phase": (lambda d, _l: line_of({**d, "phase": ON.value}), Failure.LINE_MALFORMED),
    "int-for-float": (lambda d, _l: line_of({**d, "confidence": 1}), Failure.LINE_MALFORMED),
    "not-canonical": (
        lambda d, _l: line_of(d).replace(b'"confidence":0.5', b'"confidence":0.50'),
        Failure.LINE_NOT_CANONICAL,
    ),
    "not-an-object": (lambda _d, _l: b"[]\n", Failure.LINE_MALFORMED),
    "duplicate-key": (
        lambda d, _l: line_of(d)[:-2] + b',"phase":"recording_off"}\n',
        Failure.JSON_DUPLICATE_KEY,
    ),
    "oversize": (lambda _d, _l: b"x" * (reader.MAX_LINE_BYTES + 1) + b"\n", Failure.LINE_TOO_LARGE),
    "torn": (lambda d, _l: line_of(d)[:-1], Failure.LINE_MALFORMED),
    "blank-line": (lambda d, _l: line_of(d) + b"\n", Failure.LINE_MALFORMED),
}


@pytest.mark.parametrize("label", list(_HARDENING))
def test_reader_refuses_each_malformed_attempt_line(tmp_path: Path, label: str) -> None:
    """V-T12/AM2/AM3: each malformed line maps to its exact closed member."""
    root, off, _on = two_phase_root(tmp_path)
    make, failure = _HARDENING[label]
    document = _resolution_document(off)
    assert line_of(document).count(b'"confidence":0.5') == 1
    life = activated(at=5.0)(off, 0).model_dump(mode="json")
    digest = rewrite(root, ADVISORY_OFF, lines([intend(off, 0)]) + make(document, life))
    expect(failure, lambda: read3(root, digest))


def test_reader_walks_attempt_lines_before_dispatch(tmp_path: Path) -> None:
    """V-T12: a depth breach is refused by the shared walker."""
    root, off, _on = two_phase_root(tmp_path)
    nested: object = 1
    for _ in range(10):
        nested = [nested]
    digest = rewrite(root, ADVISORY_OFF, line_of({**_resolution_document(off), "zz": nested}))
    expect_evidence(schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED, lambda: read3(root, digest))


@pytest.mark.parametrize(
    "relative_path",
    [
        "records/recording_off/advisory_attempt.jsonl.bak",
        "records/recording_off/advisory_attempts.jsonl",
        "records/advisory_attempt.jsonl",
    ],
)
def test_v3_refuses_unknown_record_files(tmp_path: Path, relative_path: str) -> None:
    """V-T12/AM5: a near-miss name is refused, never ignored."""
    root, _off, _on = two_phase_root(tmp_path)
    digest = rewrite(root, relative_path, b"{}\n")
    expect(Failure.ENTRY_PATH_INVALID, lambda: read3(root, digest))


def test_an_attempt_file_without_its_header_refuses(tmp_path: Path) -> None:
    """V-T12: an advisory file in a phase with no bound header is ``HEADER_MISSING``."""
    writer, root, off = open_off(tmp_path)
    writer.seal()
    on = on_header(tmp_path, root)
    digest = rewrite(root, ADVISORY_ON, lines([intend(on, 0, at=1300.0)]))
    expect(Failure.HEADER_MISSING, lambda: read3(root, digest))
    assert off.phase is OFF


def test_tampered_attempts_are_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-T12/AM4: a flipped byte refuses at verification; nothing is decoded."""
    calls: list[bytes] = []
    real = store.load_strict_json

    def spy(data: bytes, *, malformed: store.ColdEvidenceStoreFailure) -> object:
        calls.append(data)
        return real(data, malformed=malformed)

    monkeypatch.setattr(reader, "load_strict_json", spy)
    writer, root, off = open_off(tmp_path)
    digest = write_read(writer, root, attempt(off, 0, 10.0)).run.manifest_sha256
    assert calls
    calls.clear()
    path = run_dir(root) / ADVISORY_OFF
    data = bytearray(path.read_bytes())
    data[10] ^= 0x01
    path.write_bytes(bytes(data))
    expect(Failure.FILE_DIGEST_MISMATCHED, lambda: read3(root, digest))
    assert calls == []


# ------------------------------------------------------------------ V-T13 write faults


def test_a_write_fault_poisons_before_the_sequence_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-T13/AM20/AM21: the fault poisons; nothing commits; no manifest is written."""
    writer, root, off = open_off(tmp_path)

    def fail(_descriptor: int, _view: memoryview) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_chunk", fail)
    expect(Failure.WRITE_FAILED, lambda: writer.append_advisory_attempt(intend(off)))
    monkeypatch.undo()
    assert _order_state(writer) == (None, 0, 0)
    expect(Failure.WRITER_POISONED, lambda: writer.append_advisory_attempt(intend(off)))
    expect(Failure.WRITER_POISONED, lambda: writer.append(tick_for(off, 1)))
    expect(Failure.WRITER_POISONED, writer.seal)
    assert not (run_dir(root) / "manifest.json").exists()


@pytest.mark.parametrize("site", ["write", "binding"])
def test_an_unexpected_exception_abandons_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """V-T13: any other exception propagates unchanged and poisons the writer."""
    writer, _root, off = open_off(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> typing.NoReturn:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        store, "_write_chunk" if site == "write" else "check_advisory_attempt_binding", boom
    )
    with pytest.raises(RuntimeError):
        writer.append_advisory_attempt(intend(off))
    monkeypatch.undo()
    expect(Failure.WRITER_POISONED, lambda: writer.append_advisory_attempt(intend(off)))


def test_an_append_after_seal_refuses(tmp_path: Path) -> None:
    """V-T13: a sealed writer refuses further attempts."""
    writer, _root, off = open_off(tmp_path)
    writer.seal()
    expect(Failure.WRITER_SEALED, lambda: writer.append_advisory_attempt(intend(off)))


# ------------------------------------------------------------------ V-T14 lower bound


def test_intent_instant_is_only_a_lower_bound(tmp_path: Path) -> None:
    """V-T14: an intent 5 s before invocation round-trips; nothing claims a request start."""
    writer, root, off = open_off(tmp_path)
    records: list[Record] = [
        intend(off, 0, at=10.0),
        resolved_at(off, 0, invoked=15.0, resolved=16.0, recorded=17.0),
    ]
    assert list(write_read(writer, root, records).advisory_attempts) == records
    names = set(Intent.model_fields) | set(Resolution.model_fields)
    assert not [
        name
        for name in names
        if any(token in name for token in ("request_start", "sent", "issued"))
    ]
    for documented in (Intent, advisory.build_advisory_intent_record):
        text = " ".join((documented.__doc__ or "").split())
        assert "lower bound" in text
        assert "not proof that a provider request was issued" in text


# ------------------------------------------------------------------ V-T15 OD neutrality


def test_retained_facts_support_every_pending_option_without_selecting_one(tmp_path: Path) -> None:
    """V-T15: windows, gaps, counts, and phase-end shapes are computable in test code only."""
    writer, root, off = open_off(tmp_path)
    records: list[Record] = [
        *attempt(off, 0, 10.0),
        *attempt(off, 1, 13.0, Kind.ABANDONED_AFTER_BOUND),
        intend(off, 2, at=20.0),
        resolved_at(
            off, 2, invoked=21.0, resolved=30.0, recorded=30.0, kind=Kind.UNRESOLVED_AT_PHASE_END
        ),
        intend(off, 3, at=30.0),
        resolved_at(
            off, 3, invoked=None, resolved=30.0, recorded=30.0, kind=Kind.UNRESOLVED_AT_PHASE_END
        ),
        intend(off, 4, at=30.0),
    ]
    retained = write_read(writer, root, records)
    resolutions = [item for item in retained.advisory_attempts if isinstance(item, Resolution)]
    invocations = [
        item.invocation_monotonic for item in resolutions if item.invocation_monotonic is not None
    ]
    assert len([instant for instant in invocations if 10.0 <= instant <= 20.0]) == 2
    intents = [item for item in retained.advisory_attempts if isinstance(item, Intent)]
    gaps = [
        nxt.monotonic_seconds - prev.resolved_monotonic
        for prev, nxt in zip(resolutions, intents[1:], strict=False)
    ]
    assert gaps[0] <= 1.0
    shapes = {(item.resolution, item.invocation_state) for item in resolutions}
    assert {
        (Kind.UNRESOLVED_AT_PHASE_END, Invocation.INVOKED),
        (Kind.UNRESOLVED_AT_PHASE_END, Invocation.NOT_INVOKED),
    } <= shapes
    assert retained.advisory_attempt_state is State.OPEN_TAIL


SOURCE = COLD_PACKAGE / "evidence_advisory.py"


def _tree() -> ast.Module:
    """Parse the advisory module."""
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def test_module_exposes_no_policy_or_clock() -> None:
    """V-T15: three public functions; no clock import; configured values never compared."""
    tree = _tree()
    public = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert public == {
        "validate_advisory_attempt_record",
        "build_advisory_intent_record",
        "build_advisory_resolution_record",
    }
    owned = {
        name: value
        for name, value in vars(advisory).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == advisory.__name__
    }
    assert {name for name, value in owned.items() if inspect.isfunction(value)} == public
    configured = {"configured_call_bound_seconds", "configured_dwell_seconds"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
            names |= {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)}
            assert not names & configured
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"sleep", "monotonic", "time", "now"}


# ------------------------------------------------------------------ V-T16 privacy


def test_refusals_never_carry_input_content(tmp_path: Path) -> None:
    """V-T16/AM23: a credential-shaped canary reaches no error channel; no chaining."""
    canary = "".join(["sk", "-live-", "Q7x", "Zr9", "Mv2", "Kp4"])
    writer, _root, off = open_off(tmp_path)
    record = intend(off)
    calls: list[typing.Callable[[], object]] = [
        lambda: resolve(off, 0, Kind.RETURNED_MALFORMED_OUTPUT, rationale=canary),
        lambda: intend(off, profile_name=canary),
        lambda: intend(off, provider=canary * 200),
        lambda: intend(off, context={**CONTEXT, "secret": canary, "profile_name": "x"}),
        lambda: resolve(off, 0, evaluation=carrier(rule=canary, input_heat=99)),
        lambda: advisory.validate_advisory_attempt_record(
            record.model_copy(update={"descriptor_model": canary * 400})
        ),
        lambda: writer.append_advisory_attempt(intend(off, 1, provider=canary)),
    ]
    for call in calls:
        with pytest.raises((schema.ColdEvidenceError, advisory.ColdAdvisoryAttemptError)) as raised:
            call()
        error = raised.value
        rendered = (
            str(error) + repr(error) + repr(error.args) + "".join(traceback.format_exception(error))
        )
        assert canary not in rendered
        assert error.__cause__ is None
        assert error.__context__ is None


# ------------------------------------------------------------------ V-T17 fences


def test_module_imports_exactly_the_ratified_set() -> None:
    """V-T17: the import set, names, and plain enums are fenced; no advisor or safety reach."""
    tree = _tree()
    plain = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert plain == {"enum", "hashlib", "json", "math", "typing", "pydantic"}
    found = {
        node.module: {alias.name for alias in node.names}
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    cold = "roastpilot_agent.cold_characterisation."
    assert found == {
        cold + "evidence_lifecycle": {"is_admissible_monotonic", "is_admissible_utc_instant"},
        cold + "evidence_schema": {
            "MAX_RECORD_BYTES",
            "MAX_TEXT_FIELD_BYTES",
            "ColdEvidenceError",
            "ColdEvidenceFailure",
            "ColdPhaseKind",
            "ColdRunHeader",
            "ColdSafetyEvaluation",
            "ColdSafetyVerdict",
            "validate_record",
            "walk_json_value",
        },
    }
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    identifiers |= {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    forbidden = {
        "call_tool",
        "finalise_session",
        "get_recommendation",
        "evaluate_command",
        "build_advisor",
        "set_heat",
        "set_fan",
        "drop_beans",
    }
    assert identifiers & forbidden == set()
    enums = [
        value
        for value in vars(advisory).values()
        if inspect.isclass(value)
        and issubclass(value, enum.Enum)
        and value.__module__ == advisory.__name__
    ]
    assert len(enums) == 10
    assert all(not issubclass(value, (str, int)) for value in enums)
    reach = _reachable_package_modules((cold + "evidence_advisory",))
    assert reach & {"roastpilot_agent.advisor", "roastpilot_agent.safety"} == set()


# ------------------------------------------------------------------ V-T18 parity and pins


def test_decision_descriptor_and_evaluation_names_match_production(tmp_path: Path) -> None:
    """V-T18: bounds and names mirror the production models (test-only imports)."""
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    for heat in (-1, 0, 100, 101):
        try:
            RoastDecision(
                target_heat=heat, target_fan=50, should_drop=False, confidence=0.5, rationale=""
            )
            production = True
        except pydantic.ValidationError:
            production = False
        try:
            resolve(off, 0, requested_heat=heat, evaluation=evaluation(heat=heat))
            ours = True
        except schema.ColdEvidenceError:
            ours = False
        assert ours is production, heat
    for confidence in (0.0, 1.0, -0.0001, 1.0001):
        try:
            RoastDecision(
                target_heat=1, target_fan=1, should_drop=False, confidence=confidence, rationale=""
            )
            production = True
        except pydantic.ValidationError:
            production = False
        try:
            resolve(off, 0, confidence=confidence)
            ours = True
        except schema.ColdEvidenceError:
            ours = False
        assert ours is production, confidence
    descriptor = {
        name.removeprefix("descriptor_")
        for name in Intent.model_fields
        if name.startswith("descriptor_")
    }
    assert descriptor == set(AdvisorDescriptor.model_fields)
    evaluated = {
        name.removeprefix("evaluation_")
        for name in Resolution.model_fields
        if name.startswith("evaluation_")
    } - {"state"}
    assert (
        evaluated
        == set(schema.ColdSafetyEvaluation.model_fields)
        == set(SafetyEvaluation.model_fields)
    )


def _function(name: str) -> ast.FunctionDef:
    """Return one function definition from the advisory module."""
    return next(
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _call_lines(scope: ast.AST, names: set[str]) -> list[int]:
    """Return the lines of calls whose callee name or attribute is in ``names``."""
    found: list[int] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Call):
            callee = node.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else ""
            )
            if name in names:
                found.append(node.lineno)
    return found


def test_context_length_check_precedes_encoding_structurally() -> None:
    """V-T18/AM12b, structural (not behavioural): ``len`` is compared before ``.encode``."""
    function = _function("_require_context")
    compares = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Compare) and _call_lines(node, {"len"})
    ]
    assert min(compares) < min(_call_lines(function, {"encode"}))


@pytest.mark.parametrize(
    "name", ["build_advisory_intent_record", "build_advisory_resolution_record"]
)
def test_raw_admission_precedes_any_derivation_structurally(name: str) -> None:
    """V-T20, structural pin: ``_admit_raw`` precedes every derivation call."""
    function = _function(name)
    derivations = {
        "len",
        "walk_json_value",
        "_canonical_text",
        "encode",
        "sha256",
        "_rationale_state",
        "_admit_carrier",
    }
    (admission,) = _call_lines(function, {"_admit_raw"})
    assert admission < min(_call_lines(function, derivations))


def test_carrier_admission_precedes_flattening_structurally() -> None:
    """V-T21, structural pin: carriers are admitted before any record dict is built."""
    function = _function("build_advisory_resolution_record")
    admitted = max(_call_lines(function, {"_admit_carrier"}))
    built = min(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "values"
    )
    assert admitted < built


# ------------------------------------------------------------------ V-T19 carrier state


def test_retained_run_v3_state_must_match_its_records(tmp_path: Path) -> None:
    """V-T19/AM24: each stream state must be exactly the one its records imply."""
    writer, root, off = open_off(tmp_path)
    records = attempt(off, 0, 10.0)
    retained = write_read(writer, root, records)
    life = activated(at=5.0)(off, 0)
    absent = lifecycle.ColdLifecycleEvidenceState.ABSENT
    present = lifecycle.ColdLifecycleEvidenceState.PRESENT
    bad: list[tuple[object, tuple[object, ...], object, tuple[Record, ...]]] = [
        (absent, (), State.OPEN_TAIL, tuple(records)),
        (absent, (), State.COMPLETE, (records[0],)),
        (absent, (), State.ABSENT, (records[0],)),
        (absent, (), State.COMPLETE, ()),
        (absent, (), State.OPEN_TAIL, ()),
        (present, (), State.COMPLETE, tuple(records)),
        (absent, (life,), State.COMPLETE, tuple(records)),
    ]
    for lifecycle_state, life_records, state, attempts in bad:
        with pytest.raises(pydantic.ValidationError):
            reader.ColdRetainedRunV3.model_validate(
                {
                    "run": retained.run,
                    "lifecycle_state": lifecycle_state,
                    "lifecycle": life_records,
                    "advisory_attempt_state": state,
                    "advisory_attempts": attempts,
                }
            )
    for state, attempts in ((State.OPEN_TAIL, (records[0],)), (State.ABSENT, ())):
        built = reader.ColdRetainedRunV3(
            run=retained.run,
            lifecycle_state=present,
            lifecycle=(life,),
            advisory_attempt_state=state,
            advisory_attempts=attempts,
        )
        assert built.advisory_attempt_state is state
