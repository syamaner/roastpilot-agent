"""Advisory conformance policy 2 over retained V3 cold runs (#954 slice 5b); hardware-free.

Behavioural cases write runs through the real writer, seal them, and read them back
with the strict V3 reader.  Unit cases (labelled) forge carriers with
``model_construct``/``object.__new__``/``object.__setattr__``, monkeypatch the module
namespace, or inspect its AST.  Every expected finding tuple is written by hand; the
window instants below are hand-derived from the base plan, never from the module.
"""

import ast
import dataclasses
import enum
import math
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import conformance
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from tests.test_cold_characterisation_conformance import (
    T0,
    Plan,
    _abort,  # pyright: ignore[reportPrivateUsage]
    _checker_consumers,  # pyright: ignore[reportPrivateUsage]
    advisory_for_at,
    finalisation,
    header_of,
    host_record,
    plan,
    tick_record,
)
from tests.test_cold_characterisation_evidence_builders import RUN_ID
from tests.test_cold_characterisation_evidence_store import OFF, ON, open_writer

F = ac.ColdAdvisoryConformanceFinding
Outcome = ac.ColdAdvisoryConformanceOutcome
Pre = conformance.ColdConformanceFinding
Kind = advisory.ColdAdvisoryResolution
State = advisory.ColdAdvisoryAttemptEvidenceState
Event = lifecycle.ColdLifecycleEvent
Phase = schema.ColdPhaseKind
Intent = advisory.ColdAdvisoryIntentRecord
Resolution = advisory.ColdAdvisoryResolutionRecord
Record = Intent | Resolution
V3 = reader.ColdRetainedRunV3
Findings = tuple[ac.ColdAdvisoryConformanceFinding, ...]
#: Private helpers, reached through ``Any`` (unit exceptions).
PRIVATE: typing.Any = ac
CANARY = "sk-live-Canary0123456789AbCdEfGh"

#: Hand-derived from ``plan()``: activation 10.0 and 1830.0, so S is 1810.0 and 3630.0.
OPEN: typing.Final = {OFF: 1450.0, ON: 3270.0}
CLOSE: typing.Final = {OFF: 1750.0, ON: 3570.0}
#: The base plan's tick 0 instant in each phase (activation plus one second).
TICK0: typing.Final = {OFF: 11.0, ON: 1831.0}
PROFILE = "Cold characterisation"
CONTEXT: typing.Final[dict[str, object]] = {
    "charge_guidance_max_c": 190.0,
    "charge_guidance_min_c": None,
    "current_bean_temp_c": 24.5,
    "padding": "p" * 2100,
    "phase": "preheating",
    "profile_name": PROFILE,
    "target_drop_temp_c": 205.0,
}
USAGE = advisory.ColdAdvisoryUsageReading(
    input_tokens=1200, output_tokens=80, total_tokens=1280, reasoning_tokens=None
)
RETURNED_FAILURES = (
    Kind.RETURNED_UNSAFE_OUTPUT,
    Kind.RETURNED_MALFORMED_OUTPUT,
    Kind.RETURNED_PROVIDER_ERROR,
    Kind.RAISED_UNCLASSIFIED,
)


# ------------------------------------------------------------------ fixture chain


@dataclasses.dataclass
class Attempt:
    """One attempt to write: invocation, resolution and the two recording instants."""

    inv: float | None
    res: float
    intent_at: float
    recorded: float
    dwell: float
    bound: float
    context_at: float
    kind: advisory.ColdAdvisoryResolution = Kind.RETURNED_DECISION
    context_tick: int = 0
    intent: dict[str, typing.Any] = dataclasses.field(default_factory=dict[str, typing.Any])
    payload: dict[str, typing.Any] | None = None
    paired: bool = True


Retime = dict[int, typing.Callable[[float], float]]
Chains = dict[schema.ColdPhaseKind, list[Attempt]]


def chain(
    phase: schema.ColdPhaseKind,
    *,
    f: float,
    g: float,
    d: float,
    w: float,
    b: float,
    n: int,
    inv_at: Retime | None = None,
    res_at: Retime | None = None,
) -> list[Attempt]:
    """``inv_0 = open + f``, ``inv_k = res_{k-1} + w + g``, ``res_k = inv_k + d``.

    ``inv_at[k]`` replaces ``inv_k`` as a function of its reference (``open`` for k=0,
    otherwise ``res_{k-1} + w``); ``res_at[k]`` replaces ``res_k`` as a function of
    ``inv_k``.  Later attempts are re-timed from the replaced instants.
    """
    inv_at, res_at = inv_at or {}, res_at or {}
    attempts: list[Attempt] = []
    for k in range(n):
        reference = OPEN[phase] if k == 0 else attempts[-1].res + w
        inv = inv_at[k](reference) if k in inv_at else reference + (f if k == 0 else g)
        res = res_at[k](inv) if k in res_at else inv + d
        attempts.append(Attempt(inv, res, inv, res, w, b, TICK0[phase]))
    return attempts


Timing = dict[str, typing.Any]


def chains(
    timing: Timing,
    *,
    fit: bool = True,
    inv_at: Retime | None = None,
    res_at: Retime | None = None,
) -> Chains:
    """Build one chain per phase; ``fit`` asserts last inv <= close < final due."""
    built = {phase: chain(phase, inv_at=inv_at, res_at=res_at, **timing) for phase in (OFF, ON)}
    if fit:
        for phase, attempts in built.items():
            last = attempts[-1]
            assert last.inv is not None and last.inv <= CLOSE[phase]
            assert last.res + last.dwell > CLOSE[phase]
    return built


BASE: typing.Final[Timing] = {"f": 0.5, "g": 0.5, "d": 2.5, "w": 5.0, "b": 5.0}


def base(*, inv_at: Retime | None = None, res_at: Retime | None = None) -> Chains:
    """The conforming base chain: 38 attempts per phase, last at +296.5, final due +304.0."""
    return chains({**BASE, "n": 38}, inv_at=inv_at, res_at=res_at)


def decision() -> dict[str, typing.Any]:
    """A returned decision with a retained rationale, a recorded evaluation and usage."""
    return {
        "requested_heat": 40,
        "requested_fan": 60,
        "should_drop": False,
        "confidence": 0.5,
        "rationale": "Hold heat while the probe settles.",
        "evaluation": schema.ColdSafetyEvaluation(
            rule="cold_observation_only",
            verdict=schema.ColdSafetyVerdict.REJECT,
            input_heat=40,
            input_fan=60,
            adjusted_heat=None,
            adjusted_fan=None,
            reason="Advisory output is never forwarded.",
        ),
        "usage": USAGE,
    }


def payload_for(kind: advisory.ColdAdvisoryResolution) -> dict[str, typing.Any]:
    """The payload 5a permits for one resolution kind."""
    if kind is Kind.RETURNED_DECISION:
        return decision()
    if kind is Kind.RETURNED_UNSAFE_OUTPUT:
        return {"usage": USAGE}
    return {}


def records_for(
    header: schema.ColdRunHeader, attempts: list[Attempt], document: dict[str, typing.Any]
) -> list[Record]:
    """Build each attempt's intent and (when paired) resolution through the 5a builders."""
    built: list[Record] = []
    for index, item in enumerate(attempts):
        arguments: dict[str, typing.Any] = {
            "header": header,
            "attempt_index": index,
            "recorded_at_utc": T0,
            "monotonic_seconds": item.intent_at,
            "context_tick": item.context_tick,
            "context_tick_monotonic": item.context_at,
            "context": CONTEXT,
            "profile_name": PROFILE,
            "target_drop_temp_c": 205.0,
            "charge_guidance_min_c": None,
            "charge_guidance_max_c": 190.0,
            "provider": document["advisor_provider"],
            "model": document["advisor_model"],
            "prompt_version": document["advisor_prompt_version"],
            "configured_call_bound_seconds": item.bound,
            "configured_dwell_seconds": item.dwell,
        }
        built.append(advisory.build_advisory_intent_record(**{**arguments, **item.intent}))
        if not item.paired:
            continue
        built.append(
            advisory.build_advisory_resolution_record(
                header=header,
                attempt_index=index,
                recorded_at_utc=T0,
                monotonic_seconds=item.recorded,
                resolution=item.kind,
                invocation_utc=None if item.inv is None else T0,
                invocation_monotonic=item.inv,
                resolved_utc=T0,
                resolved_monotonic=item.res,
                **(payload_for(item.kind) if item.payload is None else item.payload),
            )
        )
    return built


def write_v3(tmp_path: Path, run: Plan, attempts: Chains) -> V3:
    """Mirror the conformance ``write``, appending each phase's attempts before its lifecycle."""
    writer, root = open_writer(tmp_path)
    sequence = 0
    for phase in run.phases:
        header = header_of(run.documents[phase], phase, run.headers[phase])
        writer.append(header)
        for tick in run.ticks[phase]:
            writer.append(tick_record(header, tick))
        for position, mono in enumerate(run.hosts[phase]):
            writer.append(
                host_record(header, mono, **run.host_overrides.get((phase, position), {}))
            )
        for payload, mono in run.results[phase]:
            writer.append(finalisation(header, payload, mono))
        for owner, make in run.extras:
            if owner is phase:
                writer.append(make(header))
        for record in records_for(header, attempts.get(phase, []), run.documents[phase]):
            writer.append_advisory_attempt(record)
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
    digest = writer.seal().manifest_sha256
    return reader.read_retained_run_v3(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def check(run: object) -> ac.ColdAdvisoryConformanceResult:
    """Run the checker under test."""
    return ac.check_advisory_conformance(run)


def expect(
    result: ac.ColdAdvisoryConformanceResult, findings: Findings, pre: tuple[Pre, ...] = ()
) -> None:
    """Assert the outcome, the exact finding tuple and the exact policy-1 tuple."""
    assert result.policy_version == 2
    assert result.outcome is (Outcome.NOT_CONFORMANT if findings else Outcome.ADVISORY_CONFORMANT)
    assert result.findings == findings
    assert result.pre_advisory_findings == pre


def run_case(
    tmp_path: Path, attempts: Chains, run: Plan | None = None
) -> ac.ColdAdvisoryConformanceResult:
    """Write one run with ``attempts`` and check it."""
    return check(write_v3(tmp_path, plan(tmp_path) if run is None else run, attempts))


# ------------------------------------------------------------ positive controls


def test_p1_base_run_is_advisory_conformant(tmp_path: Path) -> None:
    """P1: 38 decisions per phase at the base cadence conform; context exceeds 2,048 bytes."""
    retained = write_v3(tmp_path, plan(tmp_path), base())
    assert retained.advisory_attempt_state is State.COMPLETE
    assert len(retained.advisory_attempts) == 2 * 2 * 38
    intent = retained.advisory_attempts[0]
    assert type(intent) is Intent
    assert 2048 < intent.context_byte_length <= 65536
    assert (
        conformance.check_pre_advisory_conformance(
            reader.ColdRetainedRunV2(
                run=retained.run,
                lifecycle_state=retained.lifecycle_state,
                lifecycle=retained.lifecycle,
            )
        ).findings
        == ()
    )
    expect(check(retained), ())
    assert set(ac.ColdAdvisoryConformanceResult.model_fields) == {
        "policy_version",
        "outcome",
        "findings",
        "pre_advisory_findings",
    }


P_CASES: list[tuple[str, dict[str, typing.Any]]] = [
    (
        "p2a_first_at_open_plus_allowance",
        {"f": 1.0, "g": 0.5, "d": 2.5, "w": 5.0, "b": 5.0, "n": 38},
    ),
    ("p2b_next_at_due_plus_allowance", {"f": 0.5, "g": 1.0, "d": 2.5, "w": 5.0, "b": 5.0, "n": 36}),
    ("p2c_next_at_due", {"f": 0.5, "g": 0.0, "d": 2.5, "w": 5.0, "b": 5.0, "n": 40}),
    (
        "p2d_first_at_open_last_at_close",
        {"f": 0.0, "g": 0.5, "d": 2.0, "w": 5.0, "b": 5.0, "n": 41},
    ),
    ("p2e_duration_at_bound", {"f": 0.5, "g": 0.5, "d": 5.0, "w": 5.0, "b": 5.0, "n": 29}),
    ("p3_configured_dwell_and_bound", {"f": 0.5, "g": 0.75, "d": 2.5, "w": 7.0, "b": 3.0, "n": 30}),
]
#: Hand-derived offsets from ``open`` of each case's last invocation and final due.
P_LAST: typing.Final = {
    "p2a_first_at_open_plus_allowance": (297.0, 304.5),
    "p2b_next_at_due_plus_allowance": (298.0, 305.5),
    "p2c_next_at_due": (293.0, 300.5),
    "p2d_first_at_open_last_at_close": (300.0, 307.0),
    "p2e_duration_at_bound": (294.5, 304.5),
    "p3_configured_dwell_and_bound": (297.75, 307.25),
}


@pytest.mark.parametrize(("name", "timing"), P_CASES, ids=[c[0] for c in P_CASES])
def test_p2_p3_inclusive_boundaries_conform(
    tmp_path: Path, name: str, timing: dict[str, typing.Any]
) -> None:
    """P2a-P2e, P3: every inclusive boundary, and the retained dwell and bound, conform."""
    attempts = chains(timing)
    last_inv, last_due = P_LAST[name]
    for phase, items in attempts.items():
        assert items[-1].inv == OPEN[phase] + last_inv
        assert items[-1].res + items[-1].dwell == OPEN[phase] + last_due
    expect(run_case(tmp_path, attempts), ())


def test_q1_hand_built_v3_is_judged_like_the_read_run(tmp_path: Path) -> None:
    """Q1 (unit): a hand-built V3 from P1's fields conforms; the result is not provenance."""
    read = write_v3(tmp_path, plan(tmp_path), base())
    built = V3(
        run=read.run,
        lifecycle_state=read.lifecycle_state,
        lifecycle=read.lifecycle,
        advisory_attempt_state=read.advisory_attempt_state,
        advisory_attempts=read.advisory_attempts,
    )
    expect(check(built), ())


# ---------------------------------------------------------------- timing cases


def test_w1_first_invocation_just_after_the_allowance(tmp_path: Path) -> None:
    """W1: ``inv_0`` one ulp after ``open + 1.0``; the rest as P2a."""
    attempts = chains(
        {**BASE, "f": 1.0, "n": 38},
        inv_at={0: lambda ref: math.nextafter(ref + 1.0, math.inf)},
    )
    for phase, items in attempts.items():
        assert typing.cast(float, items[0].inv) - OPEN[phase] > 1.0
    expect(run_case(tmp_path, attempts), (F.FIRST_INVOCATION_NOT_TIMELY,))


def test_w1b_first_invocation_just_before_open(tmp_path: Path) -> None:
    """W1b: ``inv_0`` one ulp before ``open``: untimely and outside the window."""
    attempts = base(inv_at={0: lambda ref: math.nextafter(ref, -math.inf)})
    expect(
        run_case(tmp_path, attempts),
        (F.FIRST_INVOCATION_NOT_TIMELY, F.INVOCATION_OUTSIDE_WINDOW),
    )


def test_w2a_subsequent_invocation_just_after_the_allowance(tmp_path: Path) -> None:
    """W2a: ``inv_10`` one ulp after ``due_10 + 1.0``; later attempts re-timed."""
    attempts = base(inv_at={10: lambda due: math.nextafter(due + 1.0, math.inf)})
    for phase, items in attempts.items():
        assert math.isclose(typing.cast(float, items[-1].inv), OPEN[phase] + 297.0)
    expect(run_case(tmp_path, attempts), (F.SUBSEQUENT_INVOCATION_NOT_TIMELY,))


def test_w2b_subsequent_invocation_just_before_due(tmp_path: Path) -> None:
    """W2b: ``inv_10`` one ulp before ``due_10``; later attempts re-timed."""
    attempts = base(inv_at={10: lambda due: math.nextafter(due, -math.inf)})
    for phase, items in attempts.items():
        assert math.isclose(typing.cast(float, items[-1].inv), OPEN[phase] + 296.0)
    expect(run_case(tmp_path, attempts), (F.SUBSEQUENT_INVOCATION_NOT_TIMELY,))


def test_w3_invocation_just_after_close(tmp_path: Path) -> None:
    """W3: 50 attempts every six seconds, then one due at close invoked one ulp after it."""
    attempts = chains(
        {"f": 0.0, "g": 0.0, "d": 1.0, "w": 5.0, "b": 5.0, "n": 51},
        fit=False,
        inv_at={50: lambda due: math.nextafter(due, math.inf)},
    )
    for phase, items in attempts.items():
        assert items[49].inv == OPEN[phase] + 294.0 and items[49].res == OPEN[phase] + 295.0
        assert items[49].res + 5.0 == CLOSE[phase]
        assert typing.cast(float, items[50].inv) > CLOSE[phase]
    expect(run_case(tmp_path, attempts), (F.INVOCATION_OUTSIDE_WINDOW,))


def test_w4_a_call_due_inside_the_window_is_missing(tmp_path: Path) -> None:
    """W4: the base chain without attempt 37; the final due (+296.0) is inside the window."""
    attempts = chains({**BASE, "n": 37}, fit=False)
    for phase, items in attempts.items():
        assert items[-1].res + items[-1].dwell == OPEN[phase] + 296.0 <= CLOSE[phase]
    expect(run_case(tmp_path, attempts), (F.WINDOW_CALL_MISSING,))


def test_w5_dwell_below_the_floor(tmp_path: Path) -> None:
    """W5: a constant retained dwell of 4.999 seconds on every intent."""
    attempts = chains({**BASE, "w": 4.999, "n": 38})
    expect(run_case(tmp_path, attempts), (F.DWELL_BELOW_MINIMUM,))


def test_d1_call_duration_just_over_the_bound(tmp_path: Path) -> None:
    """D1: ``res_10`` one ulp after ``inv_10 + 5.0``; later attempts re-timed."""
    attempts = base(res_at={10: lambda inv: math.nextafter(inv + 5.0, math.inf)})
    for phase, items in attempts.items():
        assert items[10].res - typing.cast(float, items[10].inv) > 5.0
        assert math.isclose(typing.cast(float, items[-1].inv), OPEN[phase] + 299.0)
    expect(run_case(tmp_path, attempts), (F.CALL_DURATION_EXCEEDED,))


# ------------------------------------------------------- resolution-kind cases


def _each(attempts: Chains, index: int, **changes: typing.Any) -> Chains:
    """Apply the same attempt edit in both phases."""
    for items in attempts.values():
        for name, value in changes.items():
            setattr(items[index], name, value)
    return attempts


@pytest.mark.parametrize("kind", RETURNED_FAILURES, ids=[k.name for k in RETURNED_FAILURES])
def test_k1_returned_failure_kinds_never_conform(
    tmp_path: Path, kind: advisory.ColdAdvisoryResolution
) -> None:
    """K1: attempt 37 is each of the four returned-failure kinds."""
    expect(run_case(tmp_path, _each(base(), 37, kind=kind)), (F.ATTEMPT_RETURNED_FAILURE,))


def test_k2_abandoned_attempt_never_conforms(tmp_path: Path) -> None:
    """K2: classifier evidence only; abandonment semantics belong to the producer."""
    expect(
        run_case(tmp_path, _each(base(), 37, kind=Kind.ABANDONED_AFTER_BOUND)),
        (F.ATTEMPT_ABANDONED,),
    )


def test_k2b_abandoned_attempt_over_its_bound(tmp_path: Path) -> None:
    """K2b: an abandoned attempt lasting 6.0 seconds also exceeds the 5.0-second bound."""
    attempts = base(res_at={37: lambda inv: inv + 6.0})
    expect(
        run_case(tmp_path, _each(attempts, 37, kind=Kind.ABANDONED_AFTER_BOUND)),
        (F.ATTEMPT_ABANDONED, F.CALL_DURATION_EXCEEDED),
    )


def test_k3_invoked_unresolved_attempt_never_conforms(tmp_path: Path) -> None:
    """K3: classifier evidence only, not producer proof."""
    expect(
        run_case(tmp_path, _each(base(), 37, kind=Kind.UNRESOLVED_AT_PHASE_END)),
        (F.ATTEMPT_UNRESOLVED_AT_PHASE_END,),
    )


def test_k4_not_invoked_unresolved_attempt(tmp_path: Path) -> None:
    """K4: not invoked; resolved and recorded at the intent instant (``open + 296.5``)."""
    attempts = base()
    for phase, items in attempts.items():
        at = OPEN[phase] + 296.5
        assert items[37].intent_at == at
        _each({phase: items}, 37, kind=Kind.UNRESOLVED_AT_PHASE_END, inv=None, res=at, recorded=at)
    expect(
        run_case(tmp_path, attempts),
        (F.ATTEMPT_UNRESOLVED_AT_PHASE_END, F.ATTEMPT_NOT_INVOKED),
    )


def test_k5_every_attempt_a_provider_error(tmp_path: Path) -> None:
    """K5: every attempt failed; the stream is still structurally ``COMPLETE``."""
    attempts = base()
    for index in range(38):
        _each(attempts, index, kind=Kind.RETURNED_PROVIDER_ERROR)
    retained = write_v3(tmp_path, plan(tmp_path), attempts)
    assert retained.advisory_attempt_state is State.COMPLETE
    expect(check(retained), (F.ATTEMPT_RETURNED_FAILURE,))


def _tail(at: float) -> Attempt:
    """An unpaired recording-on intent (index 38) recorded at ``at``."""
    return Attempt(None, at, at, at, 5.0, 5.0, TICK0[ON], paired=False)


def test_k6a_open_tail_inside_the_window(tmp_path: Path) -> None:
    """K6a: a final recording-on intent at ``open_on + 299.0`` with no resolution."""
    attempts = base()
    attempts[ON].append(_tail(OPEN[ON] + 299.0))
    retained = write_v3(tmp_path, plan(tmp_path), attempts)
    assert retained.advisory_attempt_state is State.OPEN_TAIL
    expect(check(retained), (F.ATTEMPTS_OPEN_TAIL,))


def test_k6b_open_tail_after_termination(tmp_path: Path) -> None:
    """K6b: the unpaired intent at 3634.0 is past the elapsed and terminal instants."""
    attempts = base()
    attempts[ON].append(_tail(3634.0))
    expect(
        run_case(tmp_path, attempts),
        (F.ATTEMPTS_OPEN_TAIL, F.ATTEMPT_CAUSAL_ORDER_VIOLATED, F.ATTEMPT_AFTER_TERMINATION),
    )


def test_k7a_recording_on_attempts_absent(tmp_path: Path) -> None:
    """K7a: no recording-on attempts."""
    attempts = base()
    del attempts[ON]
    expect(run_case(tmp_path, attempts), (F.ATTEMPTS_ABSENT,))


def test_k7b_no_attempts_anywhere(tmp_path: Path) -> None:
    """K7b: no attempts at all (state ``ABSENT``)."""
    retained = write_v3(tmp_path, plan(tmp_path), {})
    assert retained.advisory_attempt_state is State.ABSENT
    expect(check(retained), (F.ATTEMPTS_ABSENT,))


def _without(name: str) -> dict[str, typing.Any]:
    payload = decision()
    del payload[name]
    return payload


@pytest.mark.parametrize(
    ("payload", "finding"),
    [
        ({**decision(), "rationale": "x" * 2049}, F.RATIONALE_NOT_RETAINED),
        (_without("evaluation"), F.EVALUATION_NOT_RECORDED),
        (_without("usage"), F.USAGE_NOT_RECORDED),
    ],
    ids=["l1_rationale", "l2_evaluation", "l3_usage"],
)
def test_l_decision_payload_must_be_retained(
    tmp_path: Path, payload: dict[str, typing.Any], finding: ac.ColdAdvisoryConformanceFinding
) -> None:
    """L1-L3: attempt 5's rationale, evaluation or usage is not retained or recorded."""
    expect(run_case(tmp_path, _each(base(), 5, payload=payload)), (finding,))


# -------------------------------------------------------- configuration cases


def test_c1_configuration_differs_on_one_intent(tmp_path: Path) -> None:
    """C1: one recording-off intent names another profile (its context agrees)."""
    attempts = base()
    attempts[OFF][7].intent = {
        "profile_name": "Another profile",
        "context": {**CONTEXT, "profile_name": "Another profile"},
    }
    expect(run_case(tmp_path, attempts), (F.CONFIGURATION_NOT_CONSTANT,))


def test_c2_descriptor_differs_from_the_identity(tmp_path: Path) -> None:
    """C2: every intent carries descriptor ``("p", "m", "v")``, constant but unbound."""
    attempts = base()
    for items in attempts.values():
        for item in items:
            item.intent = {"provider": "p", "model": "m", "prompt_version": "v"}
    expect(run_case(tmp_path, attempts), (F.DESCRIPTOR_NOT_BOUND,))


@pytest.mark.parametrize(
    ("changes"), [{"context_tick": 99}, {"context_at": 11.5}], ids=["c3a_index", "c3b_instant"]
)
def test_c3_context_tick_not_retained(tmp_path: Path, changes: dict[str, typing.Any]) -> None:
    """C3a/C3b: recording-off intent 3 names tick 99, or tick 0 at 11.5."""
    attempts = base()
    for name, value in changes.items():
        setattr(attempts[OFF][3], name, value)
    expect(run_case(tmp_path, attempts), (F.CONTEXT_TICK_NOT_BOUND,))


# ---------------------------------------------------------- causal and terminal


def test_t1a_intent_before_activation(tmp_path: Path) -> None:
    """T1a: intent 0 recorded at 9.0 naming tick 0 at 9.0; no retained tick is there."""
    attempts = base()
    attempts[OFF][0].intent_at = 9.0
    attempts[OFF][0].context_at = 9.0
    expect(
        run_case(tmp_path, attempts),
        (F.CONTEXT_TICK_NOT_BOUND, F.ATTEMPT_CAUSAL_ORDER_VIOLATED),
    )


def test_t1b_resolution_recorded_after_the_elapsed_instant(tmp_path: Path) -> None:
    """T1b: recording-off resolution 37 recorded at 1810.5 (resolved instant unchanged)."""
    attempts = base()
    attempts[OFF][37].recorded = 1810.5
    expect(run_case(tmp_path, attempts), (F.ATTEMPT_CAUSAL_ORDER_VIOLATED,))


def test_t2_resolution_recorded_after_termination(tmp_path: Path) -> None:
    """T2: recording-on resolution 37 recorded at 3634.0."""
    attempts = base()
    attempts[ON][37].recorded = 3634.0
    expect(
        run_case(tmp_path, attempts),
        (F.ATTEMPT_CAUSAL_ORDER_VIOLATED, F.ATTEMPT_AFTER_TERMINATION),
    )


# ---------------------------------------------------- policy 1 by composition


def test_g1_v1_advisory_record_fails_policy_1(tmp_path: Path) -> None:
    """G1: a v1 advisory record in recording-off fails policy 1."""
    run = plan(tmp_path)
    run.extras.append((OFF, lambda h: advisory_for_at(h, 12.5)))
    expect(
        run_case(tmp_path, base(), run),
        (F.PRE_ADVISORY_NOT_CONFORMANT,),
        (Pre.ADVISORY_EVIDENCE_PRESENT,),
    )


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize("domain", list(schema.ColdAbortDomain))
def test_g2a_any_abort_fails_policy_1(
    tmp_path: Path, phase: schema.ColdPhaseKind, domain: schema.ColdAbortDomain
) -> None:
    """G2a: the N1 abort in each of the seven domains, in either phase."""
    run = plan(tmp_path)
    run.extras.append((phase, _abort(domain, run.ticks[phase][0].mono + 0.5)))
    expect(
        run_case(tmp_path, base(), run),
        (F.PRE_ADVISORY_NOT_CONFORMANT,),
        (Pre.ABORT_RECORDED,),
    )


@pytest.mark.parametrize("phase", [OFF, ON])
def test_g2b_empty_tick_stream_fails_policy_1(tmp_path: Path, phase: schema.ColdPhaseKind) -> None:
    """G2b: N3, a phase with no ticks and no hosts."""
    run = plan(tmp_path)
    run.ticks[phase], run.hosts[phase] = [], []
    expect(
        run_case(tmp_path, base(), run),
        (F.PRE_ADVISORY_NOT_CONFORMANT,),
        (Pre.TICKS_ABSENT, Pre.ACCEPTANCE_CHECK_FAILED, Pre.D191_METRICS_UNAVAILABLE),
    )


# ======================================================= unit cases (forged)

CALLS: list[str] = []


class SpyDict(dict[object, object]):
    """A ``dict`` subclass counting length, iteration and ``get``."""

    def __len__(self) -> int:
        CALLS.append("len")
        return super().__len__()

    def __iter__(self) -> typing.Iterator[object]:
        CALLS.append("iter")
        return super().__iter__()

    def get(self, key: object, default: object = None) -> object:
        CALLS.append("get")
        return super().get(key, default)


class SpyKey:
    """A non-``str`` key whose hash collides with one named field and which counts use."""

    def __init__(self, name: str) -> None:
        self.collides = hash(name)

    def __hash__(self) -> int:
        CALLS.append("hash")
        return self.collides

    def __eq__(self, other: object) -> bool:
        CALLS.append("eq")
        return False


class SpyMeta(type):
    """A metaclass derived from ``type`` counting equality and hashing."""

    def __eq__(cls, other: object) -> bool:
        CALLS.append("meta_eq")
        return type.__eq__(cls, other)

    def __hash__(cls) -> int:
        CALLS.append("meta_hash")
        return type.__hash__(cls)


class SpyValue(metaclass=SpyMeta):
    """A value whose class has the spy metaclass."""


_ModelMeta: typing.Any = type(pydantic.BaseModel)


class SpyModelMeta(_ModelMeta):
    """A metaclass derived from pydantic's model metaclass counting equality and hashing."""

    def __eq__(self, other: object) -> bool:
        CALLS.append("model_meta_eq")
        return type.__eq__(self, other)

    def __hash__(self) -> int:
        CALLS.append("model_meta_hash")
        return type.__hash__(self)


class SpyModel(pydantic.BaseModel, metaclass=SpyModelMeta):
    """A pydantic model class whose metaclass is the spy."""


def probe(call: typing.Callable[[], object]) -> object:
    """Return a helper's value, or ``"raised"`` so an unexpected exception is an assertion."""
    try:
        return call()
    except Exception:
        return "raised"


def raw(model: object) -> dict[object, object]:
    """A copy of a model's raw ``__dict__``."""
    return dict(object.__getattribute__(model, "__dict__"))


def with_dict(model: pydantic.BaseModel, data: dict[object, object]) -> pydantic.BaseModel:
    """A copy of a model whose ``__dict__`` is replaced."""
    copy = model.model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def with_extra(model: pydantic.BaseModel) -> pydantic.BaseModel:
    """A copy of a model carrying non-empty ``__pydantic_extra__``."""
    copy = model.model_copy()
    object.__setattr__(copy, "__pydantic_extra__", {"extra": 1})
    return copy


def colliding(model: pydantic.BaseModel, name: str) -> pydantic.BaseModel:
    """A copy whose ``name`` key is replaced by a colliding ``SpyKey``."""
    data = raw(model)
    data[SpyKey(name)] = data.pop(name)
    return with_dict(model, data)


def renamed(model: pydantic.BaseModel, name: str, other: str) -> pydantic.BaseModel:
    """A copy whose ``name`` key is replaced by ``other`` (same length)."""
    data = raw(model)
    data[other] = data.pop(name)
    return with_dict(model, data)


def forged_member(kind: type[enum.Enum]) -> object:
    """An instance of an enum class that is not one of its members."""
    return object.__new__(kind)


V3_FIELDS = ("run", "lifecycle_state", "lifecycle", "advisory_attempt_state", "advisory_attempts")


def v3_with(source: V3, /, **changes: typing.Any) -> V3:
    """A ``model_construct`` V3 from a genuine one's slots with ``changes``."""
    values: dict[str, typing.Any] = {name: getattr(source, name) for name in V3_FIELDS}
    return V3.model_construct(**{**values, **changes})


def with_attempt(run: V3, index: int, record: object) -> V3:
    """A V3 whose attempt at ``index`` is replaced."""
    attempts = list(run.advisory_attempts)
    attempts[index] = typing.cast(Record, record)
    return v3_with(run, advisory_attempts=tuple(attempts))


@pytest.fixture(scope="module")
def genuine(tmp_path_factory: pytest.TempPathFactory) -> V3:
    """P1's conforming V3, read once for the unit cases."""
    tmp_path = tmp_path_factory.mktemp("advisory-genuine")
    return write_v3(tmp_path, plan(tmp_path), base())


def first_intent(run: V3) -> Intent:
    return typing.cast(Intent, run.advisory_attempts[0])


def first_resolution(run: V3) -> Resolution:
    return typing.cast(Resolution, run.advisory_attempts[1])


class SubV3(V3):
    """A V3 subclass with the same fields."""


H1: list[tuple[str, typing.Callable[[V3], object], bool]] = [
    (
        "h1a_subclass",
        lambda r: SubV3.model_construct(**{n: getattr(r, n) for n in V3_FIELDS}),
        False,
    ),
    ("h1b_spy_dict", lambda r: with_dict(r, SpyDict(raw(r))), True),
    ("h1c_sixth_key", lambda r: with_dict(r, {**raw(r), "sixth": 1}), False),
    ("h1d_colliding_key", lambda r: colliding(r, "lifecycle"), True),
    ("h1e_misspelt_key", lambda r: renamed(r, "lifecycle", "lifecycle "), False),
    ("h1f_extras", with_extra, False),
    ("h1g_run_dict", lambda r: v3_with(r, run=raw(r.run)), False),
    (
        "h1h_lifecycle_state_forged",
        lambda r: v3_with(r, lifecycle_state=forged_member(lifecycle.ColdLifecycleEvidenceState)),
        False,
    ),
    ("h1i_lifecycle_list", lambda r: v3_with(r, lifecycle=list(r.lifecycle)), False),
    (
        "h1j_attempt_state_forged",
        lambda r: v3_with(r, advisory_attempt_state=forged_member(State)),
        False,
    ),
    ("h1k_attempts_list", lambda r: v3_with(r, advisory_attempts=list(r.advisory_attempts)), False),
]


@pytest.mark.parametrize(("name", "forge", "spied"), H1, ids=[c[0] for c in H1])
def test_h1_forged_v3_roots_are_not_admitted(
    genuine: V3, name: str, forge: typing.Callable[[V3], object], spied: bool
) -> None:
    """H1: ``_admit_v3`` refuses each forgery with zero spy calls; end to end not admitted."""
    del name, spied
    forged = forge(genuine)
    CALLS.clear()
    admitted = probe(lambda: PRIVATE._admit_v3(forged))
    assert CALLS == []
    assert admitted is None
    result = check(forged)
    assert CALLS == []
    expect(result, (F.CARRIER_NOT_ADMITTED,))


def _spy_model_instance(run: V3) -> object:
    forged = object.__new__(SpyModel)
    object.__setattr__(forged, "__dict__", raw(first_intent(run)))
    object.__setattr__(forged, "__pydantic_extra__", None)
    return forged


def _intent_with(run: V3, **update: object) -> object:
    return first_intent(run).model_copy(update=update)


def _resolution_with(run: V3, **update: object) -> object:
    return first_resolution(run).model_copy(update=update)


H2: list[tuple[str, int, typing.Callable[[V3], object]]] = [
    ("h2a_spy_model_metaclass", 0, _spy_model_instance),
    ("h2b_spy_dict", 0, lambda r: with_dict(first_intent(r), SpyDict(raw(first_intent(r))))),
    (
        "h2c_many_extra_keys",
        0,
        lambda r: with_dict(
            first_intent(r), {**raw(first_intent(r)), **{f"k{i}": i for i in range(100_000)}}
        ),
    ),
    ("h2d_colliding_key", 0, lambda r: colliding(first_intent(r), "descriptor_model")),
    (
        "h2e_misspelt_key",
        0,
        lambda r: renamed(first_intent(r), "descriptor_model", "descriptor_mode1"),
    ),
    ("h2f_extras", 0, lambda r: with_extra(first_intent(r))),
    ("h2g_spy_metaclass_value", 0, lambda r: _intent_with(r, descriptor_model=SpyValue())),
    ("h2h_int_bound", 0, lambda r: _intent_with(r, attempt_index=10**schema.MAX_INT_DIGITS)),
    ("h2i_nan", 1, lambda r: _resolution_with(r, confidence=math.nan)),
    (
        "h2j_characters",
        0,
        lambda r: _intent_with(r, descriptor_model="a" * (schema.MAX_TEXT_FIELD_BYTES + 1)),
    ),
    ("h2j2_bytes", 0, lambda r: _intent_with(r, descriptor_model="€" * 683)),
    (
        "h2k_context_characters",
        0,
        lambda r: _intent_with(
            r, context_canonical_json="x" * (advisory.MAX_ADVISORY_CONTEXT_BYTES + 1)
        ),
    ),
    ("h2l_surrogate", 0, lambda r: _intent_with(r, descriptor_model="\ud800")),
    ("h2m_forged_member", 1, lambda r: _resolution_with(r, resolution=forged_member(Kind))),
]


@pytest.mark.parametrize(("name", "index", "forge"), H2, ids=[c[0] for c in H2])
def test_h2_forged_attempts_are_not_admitted(
    genuine: V3, name: str, index: int, forge: typing.Callable[[V3], object]
) -> None:
    """H2: ``_admit_attempt`` refuses each forgery with zero spy calls; end to end refused."""
    del name
    forged = forge(genuine)
    run = with_attempt(genuine, index, forged)
    CALLS.clear()
    admitted = probe(lambda: PRIVATE._admit_attempt(forged))
    assert CALLS == []
    assert admitted is False
    result = check(run)
    assert CALLS == []
    expect(result, (F.ATTEMPT_NOT_ADMITTED,))


def test_h2j2_byte_bound_is_independent_of_the_character_bound() -> None:
    """H2j2 fixture: 683 characters within the character bound, 2,049 bytes over it."""
    text = "€" * 683
    assert len(text) <= schema.MAX_TEXT_FIELD_BYTES < len(text.encode("utf-8")) == 2049


@pytest.mark.parametrize(
    ("value", "admitted"),
    [
        ("a" * 8, True),
        ("€" * 2, True),
        (b"abc", False),
        ("a" * 9, False),
        ("€" * 3, False),
        ("\ud800", False),
    ],
    ids=["fits", "multibyte_fits", "bytes", "characters", "multibyte_bytes", "surrogate"],
)
def test_admitted_text_probes(value: object, admitted: bool) -> None:
    """``_admitted_text`` at limit 8: exact ``str``, characters, encodability and bytes."""
    assert probe(lambda: PRIVATE._admitted_text(value, 8)) is admitted


def test_h2n_admitted_values_still_meet_the_5a_grammar(genuine: V3) -> None:
    """H2n: every value exact, but the evaluation input differs from the request."""
    forged = _resolution_with(genuine, evaluation_input_heat=41)
    assert probe(lambda: PRIVATE._admit_attempt(forged)) is True
    expect(check(with_attempt(genuine, 1, forged)), (F.ATTEMPT_NOT_ADMITTED,))


def test_h2o_admission_precedes_the_5a_validator(
    genuine: V3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2o: the H2g carrier never reaches ``validate_advisory_attempt_record``."""
    recorded: list[object] = []
    real = advisory.validate_advisory_attempt_record

    def recorder(record: Record) -> Record:
        recorded.append(type(record))
        return real(record)

    monkeypatch.setattr(ac, "validate_advisory_attempt_record", recorder)
    forged = _intent_with(genuine, descriptor_model=SpyValue())
    run = with_attempt(genuine, 0, forged)
    CALLS.clear()
    result = check(run)
    assert recorded == []
    assert CALLS == []
    expect(result, (F.ATTEMPT_NOT_ADMITTED,))


def _replace_first_tick(run: V3, record: schema.ColdTickRecord) -> V3:
    streams = list(run.run.streams)
    position = next(i for i, s in enumerate(streams) if s.stream is schema.ColdEvidenceStream.TICK)
    stream = streams[position]
    streams[position] = stream.model_copy(update={"records": (record, *stream.records[1:])})
    return v3_with(run, run=run.run.model_copy(update={"streams": tuple(streams)}))


def test_h3_hostile_run_value_is_left_to_policy_1(genuine: V3) -> None:
    """H3: a spy value inside a tick's ``raw_audio_extra``; nothing reads it before policy 1."""
    stream = next(s for s in genuine.run.streams if s.stream is schema.ColdEvidenceStream.TICK)
    tick = typing.cast(schema.ColdTickRecord, stream.records[0])
    forged = _replace_first_tick(
        genuine, tick.model_copy(update={"raw_audio_extra": {"k": SpyValue()}})
    )
    CALLS.clear()
    assert probe(lambda: PRIVATE._admit_v3(forged)) is not None
    result = check(forged)
    assert CALLS == []
    expect(result, (F.PRE_ADVISORY_NOT_CONFORMANT,), (Pre.CARRIER_NOT_ADMITTED,))


# ------------------------------------------------------- structural refusals


def test_s1_attempt_bound_to_another_identity(genuine: V3) -> None:
    """S1: one attempt's identity digest differs from its phase header's."""
    forged = _intent_with(genuine, identity_sha256="0" * 64)
    expect(check(with_attempt(genuine, 0, forged)), (F.ATTEMPT_BINDING_REFUSED,))


def test_s2_gap_in_the_attempt_index_sequence(genuine: V3) -> None:
    """S2: recording-off attempt 5 (intent and resolution) removed."""
    attempts = genuine.advisory_attempts
    expect(
        check(v3_with(genuine, advisory_attempts=(*attempts[:10], *attempts[12:]))),
        (F.ATTEMPT_SEQUENCE_REFUSED,),
    )


def test_s3_carried_state_disagrees_with_the_records(genuine: V3) -> None:
    """S3: ``OPEN_TAIL`` carried, but the last attempt is a resolution."""
    expect(
        check(v3_with(genuine, advisory_attempt_state=State.OPEN_TAIL)),
        (F.ATTEMPT_STATE_MISMATCH,),
    )


# ---------------------------------------------------- policy-1 re-admission


class SubResult(conformance.ColdConformanceResult):
    """A policy-1 result subclass with the same fields."""


PreOutcome = conformance.ColdConformanceOutcome
NOT_PRE = PreOutcome.NOT_CONFORMANT
PRESENT = (Pre.ADVISORY_EVIDENCE_PRESENT,)


def pre_result(
    version: object = 1, outcome: object = NOT_PRE, findings: object = PRESENT
) -> conformance.ColdConformanceResult:
    """A ``model_construct`` policy-1 result; native validation is bypassed."""
    return conformance.ColdConformanceResult.model_construct(
        policy_version=version, outcome=outcome, findings=findings
    )


RA: list[tuple[str, typing.Callable[[], object]]] = [
    (
        "ra_type",
        lambda: SubResult.model_construct(policy_version=1, outcome=NOT_PRE, findings=PRESENT),
    ),
    ("ra_dict", lambda: with_dict(pre_result(), SpyDict(raw(pre_result())))),
    ("ra_count", lambda: with_dict(pre_result(), {**raw(pre_result()), "extra": 1})),
    ("ra_key", lambda: colliding(pre_result(), "findings")),
    ("ra_pres", lambda: renamed(pre_result(), "findings", "finding5")),
    ("ra_extra", lambda: with_extra(pre_result())),
    ("ra_ver1", lambda: pre_result(version=True)),
    ("ra_ver2", lambda: pre_result(version=2)),
    ("ra_out1", lambda: pre_result(outcome="pre_advisory_conformant")),
    ("ra_out2", lambda: pre_result(outcome=forged_member(PreOutcome))),
    ("ra_tuple", lambda: pre_result(findings=list(PRESENT))),
    ("ra_mem", lambda: pre_result(findings=("advisory_evidence_present",))),
    ("ra_dup", lambda: pre_result(findings=(Pre.ABORT_RECORDED, Pre.ABORT_RECORDED))),
    ("ra_ord", lambda: pre_result(findings=(Pre.ABORT_RECORDED, Pre.TICKS_ABSENT))),
    ("ra_agree1", lambda: pre_result(outcome=PreOutcome.PRE_ADVISORY_CONFORMANT)),
    ("ra_agree2", lambda: pre_result(findings=())),
]


@pytest.mark.parametrize(("name", "forge"), RA, ids=[c[0] for c in RA])
def test_ra_forged_policy_1_results_are_not_readmitted(
    name: str, forge: typing.Callable[[], object]
) -> None:
    """RA: each forged policy-1 result is refused, with zero spy calls."""
    del name
    forged = forge()
    CALLS.clear()
    admitted = probe(lambda: PRIVATE._admit_pre_advisory_result(forged))
    assert CALLS == []
    assert admitted is None


def test_ra_genuine_policy_1_results_are_readmitted() -> None:
    """RA positive control: genuine conformant and not-conformant results are admitted."""
    for result in (
        conformance.ColdConformanceResult(
            policy_version=1, outcome=PreOutcome.PRE_ADVISORY_CONFORMANT, findings=()
        ),
        conformance.ColdConformanceResult(policy_version=1, outcome=NOT_PRE, findings=PRESENT),
    ):
        assert PRIVATE._admit_pre_advisory_result(result) is result


def test_ra_agree1_end_to_end_is_an_internal_failure(
    genuine: V3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RA-agree1: a conformant outcome carrying a finding is never treated as conformant."""
    forged = pre_result(outcome=PreOutcome.PRE_ADVISORY_CONFORMANT)

    def forged_check(run: object) -> object:
        del run
        return forged

    monkeypatch.setattr(ac, "check_pre_advisory_conformance", forged_check)
    expect(check(genuine), (F.CHECKER_INTERNAL_FAILURE,))


# -------------------------------------------------------------- public result


class Foreign(enum.Enum):
    """A foreign enum whose member is never an advisory finding."""

    CARRIER_NOT_ADMITTED = "carrier_not_admitted"


def test_pr_helper_probes_refuse() -> None:
    """PR-v/m/t/s: the pure admission helpers refuse every non-exact input."""
    members = PRIVATE._FINDING_MEMBERS
    assert PRIVATE._is_exact_version(2, 2) is True
    assert PRIVATE._is_exact_version(True, 2) is False
    assert PRIVATE._is_exact_version(2.0, 2) is False
    assert PRIVATE._is_member(Outcome.ADVISORY_CONFORMANT, PRIVATE._OUTCOME_MEMBERS) is True
    assert PRIVATE._is_member("advisory_conformant", PRIVATE._OUTCOME_MEMBERS) is False
    assert PRIVATE._is_member(forged_member(Outcome), PRIVATE._OUTCOME_MEMBERS) is False
    assert PRIVATE._is_member_tuple((F.ATTEMPTS_ABSENT,), members) is True
    assert PRIVATE._is_member_tuple([F.ATTEMPTS_ABSENT], members) is False
    assert PRIVATE._is_member_tuple(("attempts_absent",), members) is False
    assert PRIVATE._is_member_tuple((Foreign.CARRIER_NOT_ADMITTED,), members) is False
    assert PRIVATE._strictly_declared((F.CARRIER_NOT_ADMITTED, F.ATTEMPTS_ABSENT), members) is True
    assert PRIVATE._strictly_declared((F.ATTEMPTS_ABSENT, F.ATTEMPTS_ABSENT), members) is False
    assert PRIVATE._strictly_declared((F.ATTEMPTS_ABSENT, F.CARRIER_NOT_ADMITTED), members) is False
    assert PRIVATE._strictly_declared(("attempts_absent",), members) is False


NOT = Outcome.NOT_CONFORMANT
YES = Outcome.ADVISORY_CONFORMANT
PR_REFUSED: list[tuple[str, dict[str, object]]] = [
    ("version_true", {"policy_version": True}),
    ("version_float", {"policy_version": 2.0}),
    ("outcome_string", {"outcome": "not_conformant"}),
    ("findings_list", {"findings": [F.ATTEMPTS_ABSENT]}),
    ("pre_string", {"pre_advisory_findings": ("ticks_absent",)}),
    ("order", {"findings": (F.ATTEMPTS_ABSENT, F.CARRIER_NOT_ADMITTED)}),
    (
        "pre_order",
        {
            "findings": (F.PRE_ADVISORY_NOT_CONFORMANT,),
            "pre_advisory_findings": (Pre.ABORT_RECORDED, Pre.TICKS_ABSENT),
        },
    ),
    ("agree_conformant_with_findings", {"outcome": YES}),
    ("agree_not_conformant_without", {"findings": ()}),
    ("pre_without_flag", {"pre_advisory_findings": (Pre.TICKS_ABSENT,)}),
    ("flag_without_pre", {"findings": (F.PRE_ADVISORY_NOT_CONFORMANT,)}),
]


@pytest.mark.parametrize(("name", "changes"), PR_REFUSED, ids=[c[0] for c in PR_REFUSED])
def test_pr_result_model_is_closed(name: str, changes: dict[str, object]) -> None:
    """PR-agree/PR-pre and the before-validators refuse an inconsistent public result."""
    del name
    values: dict[str, object] = {
        "policy_version": 2,
        "outcome": NOT,
        "findings": (F.ATTEMPTS_ABSENT,),
        "pre_advisory_findings": (),
    }
    with pytest.raises(pydantic.ValidationError):
        ac.ColdAdvisoryConformanceResult.model_validate({**values, **changes})


def test_pr_valid_results_construct() -> None:
    """Positive control: the three valid result shapes construct."""
    ac.ColdAdvisoryConformanceResult(
        policy_version=2, outcome=YES, findings=(), pre_advisory_findings=()
    )
    ac.ColdAdvisoryConformanceResult(
        policy_version=2, outcome=NOT, findings=(F.ATTEMPTS_ABSENT,), pre_advisory_findings=()
    )
    ac.ColdAdvisoryConformanceResult(
        policy_version=2,
        outcome=NOT,
        findings=(F.PRE_ADVISORY_NOT_CONFORMANT,),
        pre_advisory_findings=(Pre.TICKS_ABSENT,),
    )


def test_closed_vocabularies() -> None:
    """The version, the outcome and the 27 findings in declaration order, plain ``Enum``."""
    assert ac.ADVISORY_CONFORMANCE_POLICY_VERSION == 2
    assert conformance.CONFORMANCE_POLICY_VERSION == 1
    assert [m.name for m in Outcome] == ["ADVISORY_CONFORMANT", "NOT_CONFORMANT"]
    assert [m.name for m in F] == [
        "CARRIER_NOT_ADMITTED",
        "PRE_ADVISORY_NOT_CONFORMANT",
        "ATTEMPT_NOT_ADMITTED",
        "ATTEMPT_BINDING_REFUSED",
        "ATTEMPT_SEQUENCE_REFUSED",
        "ATTEMPT_STATE_MISMATCH",
        "ATTEMPTS_ABSENT",
        "ATTEMPTS_OPEN_TAIL",
        "ATTEMPT_RETURNED_FAILURE",
        "ATTEMPT_ABANDONED",
        "ATTEMPT_UNRESOLVED_AT_PHASE_END",
        "ATTEMPT_NOT_INVOKED",
        "RATIONALE_NOT_RETAINED",
        "EVALUATION_NOT_RECORDED",
        "USAGE_NOT_RECORDED",
        "CONFIGURATION_NOT_CONSTANT",
        "DESCRIPTOR_NOT_BOUND",
        "DWELL_BELOW_MINIMUM",
        "CONTEXT_TICK_NOT_BOUND",
        "FIRST_INVOCATION_NOT_TIMELY",
        "SUBSEQUENT_INVOCATION_NOT_TIMELY",
        "INVOCATION_OUTSIDE_WINDOW",
        "WINDOW_CALL_MISSING",
        "CALL_DURATION_EXCEEDED",
        "ATTEMPT_CAUSAL_ORDER_VIOLATED",
        "ATTEMPT_AFTER_TERMINATION",
        "CHECKER_INTERNAL_FAILURE",
    ]
    for kind in (Outcome, F):
        assert type(kind) is enum.EnumMeta and not issubclass(kind, str)
        assert all(member.value == member.name.lower() for member in kind)


# ---------------------------------------------------------- internal failures


def test_r2_rule_exception_is_an_internal_failure(
    genuine: V3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: a rule helper raising is ``CHECKER_INTERNAL_FAILURE``; no text is retained."""

    def boom(*args: object) -> None:
        raise ValueError(CANARY)

    monkeypatch.setattr(ac, "_check_timing", boom)
    result = check(genuine)
    expect(result, (F.CHECKER_INTERNAL_FAILURE,))
    assert CANARY not in repr(result) and CANARY not in result.model_dump_json()


def test_r2_view_exception_is_an_internal_failure(
    genuine: V3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: building the views raising skips every rule and is an internal failure."""

    def boom(*args: object) -> None:
        raise ValueError(CANARY)

    monkeypatch.setattr(ac, "_views", boom)
    expect(check(genuine), (F.CHECKER_INTERNAL_FAILURE,))


def test_r2_admission_exception_is_an_internal_failure(genuine: V3) -> None:
    """R2: an attempt whose extras slot is unset raises inside admission; contained."""
    forged = object.__new__(Intent)
    object.__setattr__(forged, "__dict__", raw(first_intent(genuine)))
    expect(check(with_attempt(genuine, 0, forged)), (F.CHECKER_INTERNAL_FAILURE,))


class Interrupt(KeyboardInterrupt):
    """A ``BaseException`` that must propagate."""


def test_r2b_base_exceptions_propagate(genuine: V3, monkeypatch: pytest.MonkeyPatch) -> None:
    """R2b: a ``KeyboardInterrupt`` subclass is never swallowed."""

    def interrupt(*args: object) -> None:
        raise Interrupt

    monkeypatch.setattr(ac, "_check_bounds", interrupt)
    with pytest.raises(Interrupt):
        check(genuine)


# ------------------------------------------------------ AST fences and pins

SOURCE = Path(ac.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
#: Sentinel for a pinned node that is absent: no real line is ever at or past it.
ABSENT_LINE = 10**9
_COLD = "roastpilot_agent.cold_characterisation."
ALLOWED: dict[str, frozenset[str]] = {
    _COLD + "advisory_window": frozenset(
        {
            "ADVISORY_INVOCATION_ALLOWANCE_SECONDS",
            "MIN_POST_COMPLETION_DWELL_SECONDS",
            "advisory_window_bounds",
        }
    ),
    _COLD + "conformance": frozenset(
        {
            "CONFORMANCE_POLICY_VERSION",
            "ColdConformanceFinding",
            "ColdConformanceOutcome",
            "ColdConformanceResult",
            "check_pre_advisory_conformance",
        }
    ),
    _COLD + "evidence_advisory": frozenset(
        {
            "ADMITTED_ENUM_TYPES",
            "MAX_ADVISORY_CONTEXT_BYTES",
            "ColdAdvisoryAttemptError",
            "ColdAdvisoryAttemptEvidenceState",
            "ColdAdvisoryEvaluationState",
            "ColdAdvisoryIntentRecord",
            "ColdAdvisoryInvocationState",
            "ColdAdvisoryRationaleState",
            "ColdAdvisoryResolution",
            "ColdAdvisoryResolutionRecord",
            "ColdAdvisorySequence",
            "ColdAdvisoryUsageState",
            "validate_advisory_attempt_record",
        }
    ),
    _COLD + "evidence_reader": frozenset(
        {"ColdRetainedRun", "ColdRetainedRunV2", "ColdRetainedRunV3"}
    ),
    _COLD + "evidence_lifecycle": frozenset(
        {"ColdLifecycleEvidenceState", "ColdLifecycleRecord", "validate_lifecycle_record"}
    ),
    _COLD + "evidence_schema": frozenset(
        {
            "MAX_INT_DIGITS",
            "MAX_TEXT_FIELD_BYTES",
            "ColdEvidenceError",
            "ColdEvidenceStream",
            "ColdPhaseKind",
            "validate_record",
            "walk_json_value",
        }
    ),
    _COLD + "evidence_store": frozenset(
        {
            "ColdBindingState",
            "ColdEvidenceStoreFailure",
            "check_advisory_attempt_binding",
            "check_record_binding",
            "load_strict_json",
        }
    ),
}
FORBIDDEN_MODULES = {
    "time",
    "asyncio",
    "os",
    "subprocess",
    "socket",
    "logging",
    "io",
    "pathlib",
    "advisor",
    "safety",
    "controller",
    "api",
    "store",
    "cli",
    "live",
    "mcp",
    "mcp_client",
    "engine",
    "engine_policy",
    "two_phase",
    "host",
    "identity",
}
FORBIDDEN_ATTRIBUTES = {
    "get_recommendation",
    "evaluate_command",
    "build_advisor",
    "call_tool",
    "finalise_session",
    "set_heat",
    "set_fan",
    "drop_beans",
    "start_cooling",
    "stop_cooling",
    "emergency_stop",
    "active",
    "elapsed_monotonic_seconds",
    "device_state",
    "first_crack_status",
}


def test_f_imp_imports_attributes_and_public_surface() -> None:
    """F-IMP: allowlisted imports, no forbidden module or attribute, exact ``__all__``."""
    plain: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in ALLOWED, node.module
            modules.add(node.module)
            names = {alias.name for alias in node.names}
            assert names <= ALLOWED[node.module], names - ALLOWED[node.module]
    assert plain == {"enum", "math", "typing", "pydantic"}
    leaves = {name.rsplit(".", 1)[-1] for name in plain | modules}
    assert leaves.isdisjoint(FORBIDDEN_MODULES)
    attributes = {node.attr for node in ast.walk(TREE) if isinstance(node, ast.Attribute)}
    assert attributes.isdisjoint(FORBIDDEN_ATTRIBUTES)
    assert ac.__all__ == (
        "ADVISORY_CONFORMANCE_POLICY_VERSION",
        "ColdAdvisoryConformanceFinding",
        "ColdAdvisoryConformanceOutcome",
        "ColdAdvisoryConformanceResult",
        "check_advisory_conformance",
    )
    operands = {
        operand.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Compare)
        for operand in (node.left, *node.comparators)
        if isinstance(operand, ast.Constant) and type(operand.value) in (int, float)
    }
    assert not any(value in (300, 360, 60, 5.0, 1.0) for value in operands)


def test_f_imp_only_the_advisory_checker_imports_the_window() -> None:
    """Only ``advisory_conformance`` imports ``advisory_window``; nothing imports it."""
    package = SOURCE.parents[1]
    assert _checker_consumers(package, "roastpilot_agent", module="advisory_window") == [
        "cold_characterisation/advisory_conformance.py"
    ]
    assert _checker_consumers(package, "roastpilot_agent", module="advisory_conformance") == []


def _function(name: str) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(TREE) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_len_of(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and _is_name(node.func, "len")
        and len(node.args) == 1
        and _is_name(node.args[0], name)
    )


def _is_method_call(node: ast.AST, owner: str, method: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and _is_name(node.func.value, owner)
        and node.func.attr == method
    )


def _first_line(tree: ast.AST, predicate: typing.Callable[[ast.AST], bool]) -> int:
    """The first matching line, or a sentinel past every line when nothing matches."""
    return min(
        (
            typing.cast(int, getattr(node, "lineno", None))
            for node in ast.walk(tree)
            if predicate(node)
        ),
        default=ABSENT_LINE,
    )


def _iterates(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.For):
        return _is_name(node.iter, name)
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
        return any(_is_name(item.iter, name) for item in node.generators)
    return False


def _is_str_key_scan(node: ast.AST) -> bool:
    if not (isinstance(node, ast.GeneratorExp) and _iterates(node, "data")):
        return False
    test = node.elt
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.left, ast.Call)
        and _is_name(test.left.func, "type")
        and _is_name(test.comparators[0], "str")
    )


def test_s1_shape_checks_the_count_before_reading_keys() -> None:
    """S-1 (structural): ``len(data)`` is compared before any key or value read."""
    shape = _function("_shape")
    count = _first_line(shape, lambda n: isinstance(n, ast.Compare) and _is_len_of(n.left, "data"))
    reads = _first_line(shape, lambda n: _iterates(n, "data") or _is_method_call(n, "data", "get"))
    assert count < reads


def test_s1b_shape_checks_key_types_before_any_lookup() -> None:
    """S-1b (structural): the exact-``str`` key scan precedes the first ``data.get``."""
    shape = _function("_shape")
    scan = _first_line(shape, _is_str_key_scan)
    lookup = _first_line(shape, lambda n: _is_method_call(n, "data", "get"))
    assert scan < lookup


def test_s2_text_checks_length_before_encoding() -> None:
    """S-2 (structural): ``len(value)`` is compared before ``value.encode``."""
    text = _function("_admitted_text")
    length = _first_line(text, lambda n: isinstance(n, ast.Compare) and _is_len_of(n.left, "value"))
    encode = _first_line(text, lambda n: _is_method_call(n, "value", "encode"))
    assert length < encode


@pytest.mark.parametrize("name", ["_admit_v3", "_admit_attempt", "_admit_pre_advisory_result"])
def test_s3_every_admission_uses_the_shared_shape(name: str) -> None:
    """S-3 (structural): each admission helper calls ``_shape``."""
    assert any(
        isinstance(node, ast.Call) and _is_name(node.func, "_shape")
        for node in ast.walk(_function(name))
    )


def test_ra_member_and_order_guards_are_structurally_present() -> None:
    """RA-member/RA-order (structural): re-admission scans every finding by identity and
    checks declaration order; later agreement checks would otherwise mask their removal."""
    calls = [
        node.func.id
        for node in ast.walk(_function("_admit_pre_advisory_result"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("_is_member") == 2
    assert calls.count("_strictly_declared") == 1
    assert calls.count("_is_exact_version") == 1


def test_shape_presence_guard_refuses_a_misspelt_name(genuine: V3) -> None:
    """Directive 3: ``_shape`` itself refuses an equal-count dict with one misspelt name."""
    forged = renamed(genuine, "lifecycle", "lifecycle ")
    assert probe(lambda: PRIVATE._shape(forged, V3_FIELDS)) is None
    assert probe(lambda: PRIVATE._shape(genuine, V3_FIELDS)) == tuple(
        getattr(genuine, name) for name in V3_FIELDS
    )
