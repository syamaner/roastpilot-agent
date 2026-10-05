"""Synthetic D211 matrix, historical preservation and strict runtime admission."""

import copy
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import acceptance as a
from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import conformance as c
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import temperature_conformance as tc
from roastpilot_agent.cold_characterisation.evidence_advisory import ColdAdvisoryResolutionRecord
from roastpilot_agent.cold_characterisation.evidence_schema import ColdPhaseKind
from tests.test_cold_characterisation_acceptance import (
    Phase,
    default_ticks,
    result_for,
    write_run,
)
from tests.test_cold_characterisation_conformance import Plan
from tests.test_cold_characterisation_duration_policy import current_run
from tests.test_cold_characterisation_evidence_store import OFF, ON
from tests.test_cold_characterisation_temperature_conformance import written
from tests.test_cold_characterisation_temperature_screen import obs
from tests.test_cold_characterisation_two_phase import admit_checker, forged

P = "Audio first-crack detection is prepared for this session."
F = a.ColdCheckFailure


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize("position", [0, 1, 2, "pre"])
@pytest.mark.parametrize(
    ("changes", "benign"),
    [
        ({"reason": None}, True),
        ({"reason": P}, True),
        ({"reason": ""}, False),
        ({"reason": "microphone failed"}, False),
        ({"reason": "fatal inference error"}, False),
        ({"reason": "unknown"}, False),
        ({"reason": " " + P}, False),
        ({"reason": P + " "}, False),
        ({"reason": P.lower()}, False),
        ({"reason": P.replace(".", "．")}, False),
        ({"reason": P, "status": "faulted"}, False),
        ({"reason": P, "status": "unavailable"}, False),
        ({"reason": P, "status": "detected"}, False),
        ({"reason": P, "audio_running": False}, False),
        ({"reason": P, "mode": "manual"}, False),
        ({"reason": P, "detected_at_utc": "2026-10-05T12:00:00Z"}, False),
        ({"reason": P, "detected_monotonic_seconds": 1.0}, False),
    ],
)
def test_reason_matrix(
    tmp_path: Path,
    phase: ColdPhaseKind,
    position: int | str,
    changes: dict[str, object],
    benign: bool,
) -> None:
    """Exact-state recognition is symmetric and never normalises retained reasons."""
    ticks = list(default_ticks())
    result = result_for(phase)
    if position == "pre":
        result["pre_finalisation_first_crack_status"].update(changes)
    else:
        ticks[typing.cast(int, position)].update(changes)
    retained = write_run(tmp_path, {phase: Phase(ticks=tuple(ticks), results=(result,))})
    original = a.interpret_retained_run(retained).phases[0].results[1]
    revised = a.interpret_retained_run_revision1(retained).phases[0].results[1]
    assert (F.MICROPHONE_OR_FATAL_ERROR not in revised.failures) is benign
    assert (F.MICROPHONE_OR_FATAL_ERROR in original.failures) is (changes["reason"] is not None)
    rebound = a.interpret_retained_run_revision1(retained).rebound.phases[0]
    observed = (
        rebound.finalisation.pre_finalisation_first_crack_status
        if position == "pre" and rebound.finalisation is not None
        else rebound.ticks[typing.cast(int, position)].audio
    )
    assert observed is not None and observed.reason == changes["reason"]


def prepared(plan: Plan) -> None:
    """Set the independent literal on all synthetic live and pre-stop samples."""
    for phase in (OFF, ON):
        for tick in plan.ticks[phase]:
            tick.audio["reason"] = P
        plan.results[phase][0][0]["pre_finalisation_first_crack_status"]["reason"] = P


def test_original_policy4_and_public_interpreter_remain_reproducible(tmp_path: Path) -> None:
    """Prepared evidence fails legacy policy 4 and passes revised full composition."""
    retained = current_run(tmp_path, prepared)
    before = copy.deepcopy(retained)
    assert tc.check_current_conformance(retained).findings == (
        tc.ColdTemperatureConformanceFinding.ADVISORY_POLICY_NOT_CONFORMANT,
    )
    revised = tc.check_revised_conformance(retained)
    assert type(revised) is tc.ColdRevisedConformanceResult
    assert revised.policy_version == 4 and revised.interpretation_revision == 1
    assert revised.findings == ()
    assert all(
        F.MICROPHONE_OR_FATAL_ERROR in p.results[1].failures
        for p in a.interpret_retained_run(retained.run).phases
    )
    assert retained == before


def test_historical_policies_are_identical_after_revision_evaluation(tmp_path: Path) -> None:
    """Independent literal 30+30 evidence retains all public policy results."""
    retained = written(tmp_path)
    v2 = reader.ColdRetainedRunV2(
        run=retained.run, lifecycle_state=retained.lifecycle_state, lifecycle=retained.lifecycle
    )
    v3 = reader.ColdRetainedRunV3(
        run=retained.run,
        lifecycle_state=retained.lifecycle_state,
        lifecycle=retained.lifecycle,
        advisory_attempt_state=retained.advisory_attempt_state,
        advisory_attempts=retained.advisory_attempts,
    )
    before = (
        c.check_pre_advisory_conformance(v2),
        ac.check_advisory_conformance(v3),
        tc.check_temperature_conformance(retained),
    )
    assert all(result.findings == () for result in before)
    assert tc.check_revised_conformance(retained).findings != ()
    assert before == (
        c.check_pre_advisory_conformance(v2),
        ac.check_advisory_conformance(v3),
        tc.check_temperature_conformance(retained),
    )


@pytest.mark.parametrize("end", [1810.0, 1210.0, 610.0000000000001])
def test_revised_rejects_substituted_duration(tmp_path: Path, end: float) -> None:
    """Historical, mixed, unknown and rounded schedules cannot borrow D211 success."""
    retained = current_run(tmp_path, prepared)
    records = tuple(
        record.model_copy(update={"scheduled_end_monotonic": end})
        if record.phase is OFF and record.scheduled_end_monotonic is not None
        else record
        for record in retained.lifecycle
    )
    assert tc.check_revised_conformance(retained.model_copy(update={"lifecycle": records})).findings


@pytest.mark.parametrize("failure", ["advisory", "candidate", "temperature"])
def test_prepared_phrase_does_not_hide_later_stages(tmp_path: Path, failure: str) -> None:
    """Failures beyond the original early return still reject revised evidence."""
    retained = current_run(tmp_path, prepared)
    if failure == "advisory":
        retained = retained.model_copy(
            update={
                "advisory_attempts": tuple(
                    record.model_copy(
                        update={"resolved_monotonic": record.resolved_monotonic + 6.0}
                    )
                    if type(record) is ColdAdvisoryResolutionRecord
                    else record
                    for record in retained.advisory_attempts
                )
            }
        )
        expected = tc.ColdTemperatureConformanceFinding.ADVISORY_POLICY_NOT_CONFORMANT
    elif failure == "candidate":
        retained = retained.model_copy(
            update={
                "mcp_candidates": tuple(
                    record.model_copy(
                        update={
                            "candidate": record.candidate.model_copy(
                                update={"reported_version": "9.9.9"}
                            )
                        }
                    )
                    for record in retained.mcp_candidates
                )
            }
        )
        expected = tc.ColdTemperatureConformanceFinding.MCP_CANDIDATE_VERSION_MISMATCH
    else:
        retained = retained.model_copy(
            update={
                "tick_temperatures": tuple(
                    record.model_copy(update={"temperature": obs(record.tick + 1, bt=41.0)})
                    if record.phase is OFF and record.tick == 4
                    else record
                    for record in retained.tick_temperatures
                )
            }
        )
        expected = tc.ColdTemperatureConformanceFinding.TEMPERATURE_SCREEN_VIOLATED
    assert expected in tc.check_revised_conformance(retained).findings


@pytest.mark.parametrize("revision", [True, 1.0, 0, 2, "1", None])
def test_revision_is_exact_and_runtime_readmits_forged_values(revision: object) -> None:
    """Coercions and unknown revisions never reach current runtime success."""
    valid = tc.ColdRevisedConformanceResult(
        policy_version=4,
        interpretation_revision=1,
        outcome=tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT,
        findings=(),
    )
    assert admit_checker(valid) == valid
    assert admit_checker(forged(valid, interpretation_revision=revision)) is None
    with pytest.raises(pydantic.ValidationError):
        tc.ColdRevisedConformanceResult.model_validate(
            {
                "policy_version": 4,
                "interpretation_revision": revision,
                "outcome": valid.outcome,
                "findings": (),
            }
        )


def test_missing_revision_legacy_and_subclass_are_refused() -> None:
    """Neither old policy 4 nor subclasses can masquerade as revised results."""
    outcome = tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
    assert (
        admit_checker(
            tc.ColdCurrentConformanceResult(policy_version=4, outcome=outcome, findings=())
        )
        is None
    )

    class Sub(tc.ColdRevisedConformanceResult):
        """Synthetic inadmissible subclass."""

    assert (
        admit_checker(
            Sub(policy_version=4, interpretation_revision=1, outcome=outcome, findings=())
        )
        is None
    )
    assert (
        admit_checker(
            tc.ColdRevisedConformanceResult.model_construct(
                policy_version=4, outcome=outcome, findings=()
            )
        )
        is None
    )


@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(
    "failure", ["ticks", "pre", "windows", "duration", "counter", "recording", "stop"]
)
def test_other_acceptance_failures_survive_prepared_reason(
    tmp_path: Path,
    phase: ColdPhaseKind,
    failure: str,
) -> None:
    """The phrase never neutralises presence, counters, duration, recording or stop rules."""
    ticks = [dict(tick, reason=P) for tick in default_ticks()]
    result = result_for(phase)
    result["pre_finalisation_first_crack_status"]["reason"] = P
    if failure == "ticks":
        ticks = []
    elif failure == "pre":
        result["pre_finalisation_first_crack_status"] = None
    elif failure == "windows":
        result["pre_finalisation_first_crack_status"]["processed_window_count"] = 0
    elif failure == "duration":
        ticks[-1]["inference_overrun_count"] = 1
    elif failure == "counter":
        ticks[-1]["dropped_window_count"] = 1
    elif failure == "recording":
        result["recording"]["reason"] = "actual recording failure"
        if phase is OFF:
            result["recording"]["expected"] = True
    else:
        result["first_crack_runtime"]["final_status"]["audio_running"] = True
    retained = write_run(tmp_path, {phase: Phase(ticks=tuple(ticks), results=(result,))})
    checks = a.interpret_retained_run_revision1(retained).phases[0].results
    assert any(check.failures for check in checks)


@pytest.mark.parametrize("carrier", [None, object(), [], {}, "run"])
def test_revised_carrier_admission(carrier: object) -> None:
    """Revised evaluation preserves strict admission before interpreting."""
    assert tc.check_revised_conformance(carrier).findings == (
        tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,
    )


def test_revised_exception_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary failures close; process interruptions propagate as before."""

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(tc, "_evaluate", fail)
    assert tc.check_revised_conformance(None).findings == (
        tc.ColdTemperatureConformanceFinding.CHECKER_INTERNAL_FAILURE,
    )

    def interrupt(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(tc, "_evaluate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        tc.check_revised_conformance(None)


@pytest.mark.parametrize(
    "changes",
    [
        {"policy_version": True},
        {"policy_version": 4.0},
        {"policy_version": 3},
        {"outcome": "temperature_screened_conformant"},
        {"findings": []},
        {"findings": ("carrier_not_admitted",)},
        {"findings": (tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,) * 2},
        {"findings": (tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,)},
        {"outcome": tc.ColdTemperatureConformanceOutcome.NOT_CONFORMANT},
    ],
)
def test_revised_result_closed_agreement(changes: dict[str, object]) -> None:
    """The revised class retains exact member, ordering and outcome invariants."""
    with pytest.raises(pydantic.ValidationError):
        tc.ColdRevisedConformanceResult.model_validate(
            {
                "policy_version": 4,
                "interpretation_revision": 1,
                "outcome": tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT,
                "findings": (),
                **changes,
            }
        )


def test_revision_rebinds_and_refuses_malformed_carrier(tmp_path: Path) -> None:
    """The new entry point uses the same rebinding refusal boundary."""
    retained = current_run(tmp_path, prepared).run
    forged_run = retained.model_copy(update={"streams": list(retained.streams)})
    for interpreter in (a.interpret_retained_run, a.interpret_retained_run_revision1):
        with pytest.raises(a.ColdInterpretationError):
            interpreter(forged_run)


@pytest.mark.parametrize("phase", [OFF, ON])
def test_post_stop_reason_is_not_reinterpreted(tmp_path: Path, phase: ColdPhaseKind) -> None:
    """D211 leaves the historical post-stop rule group exactly unchanged."""
    payload = result_for(phase)
    payload["first_crack_runtime"]["final_status"]["reason"] = P
    retained = write_run(tmp_path, {phase: Phase(results=(payload,))})
    assert (
        a.interpret_retained_run(retained).phases
        == a.interpret_retained_run_revision1(retained).phases
    )
