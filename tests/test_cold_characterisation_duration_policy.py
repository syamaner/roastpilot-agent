"""D210 generation separation with independently authored retained V6 runs."""

import ast
import math
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import (
    advisory_conformance,
    conformance,
    duration_policy,
    engine_policy,
)
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation import temperature_conformance as tc
from roastpilot_agent.cold_characterisation.duration_policy import (
    ColdDurationGeneration as Generation,
)
from roastpilot_agent.cold_characterisation.duration_policy import admit_duration_generation
from tests.test_cold_characterisation_advisory_conformance import (
    Attempt,
    _write_run,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_cold_characterisation_conformance import (
    S_OFF,
    S_ON,
    Plan,
    Tick,
    audio,
    plan,
)
from tests.test_cold_characterisation_evidence_builders import RUN_ID
from tests.test_cold_characterisation_evidence_store import OFF, ON
from tests.test_cold_characterisation_evidence_temperature import temperature_for
from tests.test_cold_characterisation_evidence_temperature_run import candidate_for
from tests.test_cold_characterisation_temperature_conformance import written
from tests.test_cold_characterisation_temperature_screen import obs
from tests.test_cold_characterisation_two_phase import admit_checker, forged


def test_duration_policy_imports_only_pure_stdlib_dependencies() -> None:
    """The duration leaf depends only on enum, math and typing, never runtime or I/O."""
    tree = ast.parse(Path(duration_policy.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id != "__import__"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr != "import_module"
    assert imported == {"enum", "math", "typing"}


@pytest.mark.parametrize(
    ("activation", "end", "expected"),
    [
        (10.0, 1810.0, Generation.HISTORICAL),
        (10.0, 610.0, Generation.D210),
        (0.0, 600.0, Generation.D210),
        (0.0, 1800.0, Generation.HISTORICAL),
        (10.25, 610.25, Generation.D210),
        (10.0, math.nextafter(610.0, math.inf), None),
        (10.0, math.nextafter(610.0, 0.0), None),
        (10.0, math.nextafter(1810.0, math.inf), None),
        (10.0, math.nextafter(1810.0, 0.0), None),
        (10.0, 1210.0, None),
        (10.0, -1.0, None),
        (10.0, math.inf, None),
        (10, 610.0, None),
        (10.0, 610, None),
        (True, 600.0, None),
        ("10.0", 610.0, None),
        (10.0, "610.0", None),
        (-1.0, 599.0, None),
        (math.inf, math.inf, None),
        (math.nan, 610.0, None),
        (10.0, math.nan, None),
        (1e30, 1e30, None),
    ],
)
def test_exact_generation_relation(
    activation: object, end: object, expected: Generation | None
) -> None:
    """Unknown, coercible, rounded and numerically ambiguous relations fail closed."""
    assert admit_duration_generation(activation, end) is expected


class HostileFloat(float):
    """Numeric subclass whose hooks must never execute."""

    def __eq__(self, other: object) -> bool:
        raise AssertionError("untrusted equality")

    def __add__(self, other: float) -> float:
        raise AssertionError("untrusted addition")


def test_generation_admission_never_calls_numeric_subclass_hooks() -> None:
    """Type admission precedes arithmetic or comparison."""
    assert admit_duration_generation(HostileFloat(10.0), 610.0) is None
    assert admit_duration_generation(10.0, HostileFloat(610.0)) is None


def current_run(
    tmp_path: Path, edit: typing.Callable[[Plan], None] | None = None
) -> reader.ColdRetainedRunV6:
    """Write current anchors 10/610 and 630/1230, windows 250–550 and 870–1170."""
    run = plan(tmp_path, e_off=610.0, phase_seconds=600.0)
    for phase, activation, session in ((OFF, 10.0, S_OFF), (ON, 630.0, S_ON)):
        for offset in (59.0, 60.0, 61.0):
            index = len(run.ticks[phase])
            run.ticks[phase].append(
                Tick(activation + offset, index, session, offset, audio(index), None)
            )
            # Match the baseline device rather than inventing a different observation.
            run.ticks[phase][-1].device = run.ticks[phase][0].device
            run.hosts[phase].append(activation + offset)
    attempts = {
        phase: [
            Attempt(
                opening + 0.5 + 8.0 * index,
                opening + 3.0 + 8.0 * index,
                opening + 0.5 + 8.0 * index,
                opening + 3.0 + 8.0 * index,
                5.0,
                5.0,
                tick_at,
            )
            for index in range(38)
        ]
        for phase, opening, tick_at in ((OFF, 250.0, 11.0), (ON, 870.0, 631.0))
    }

    def candidate(writer: store.ColdEvidenceWriter, header: schema.ColdRunHeader) -> None:
        writer.append_mcp_candidate(candidate_for(header))

    def temperature(
        writer: store.ColdEvidenceWriter, header: schema.ColdRunHeader, tick: schema.ColdTickRecord
    ) -> None:
        del header
        writer.append_tick_temperature(temperature_for(tick, temperature=obs(tick.tick + 1)))

    if edit is not None:
        edit(run)
    root, digest = _write_run(
        tmp_path, run, attempts, after_header=candidate, after_tick=temperature, current=True
    )
    return reader.read_retained_run_v6(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def test_historical_current_rejection_matrix(tmp_path: Path) -> None:
    """Both generations conform only under their own strict public policy."""
    historical = written(tmp_path / "historical")
    current = current_run(tmp_path / "current")
    for run, historical_ok, current_ok in ((historical, True, False), (current, False, True)):
        archived_result = tc.check_temperature_conformance(run)
        current_result = tc.check_current_conformance(run)
        assert archived_result.policy_version == 3
        assert current_result.policy_version == 4
        assert (archived_result.findings == ()) is historical_ok
        assert (current_result.findings == ()) is current_ok
    assert tc.check_current_conformance(current).outcome is (
        tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT
    )


@pytest.mark.parametrize("generation", ["historical", "current"])
@pytest.mark.parametrize("phase", [OFF, ON])
@pytest.mark.parametrize(
    "case",
    ["mixed", "missing", "duplicate", "unknown", "substituted", "nan", "inf", "int", "bool", "str"],
)
def test_generation_lifecycle_substitution_refused_by_all_public_policies(
    tmp_path: Path, generation: str, phase: schema.ColdPhaseKind, case: str
) -> None:
    """Both phases fail closed in policies 1–4, including bound elapsed-end mutations."""
    run = written(tmp_path) if generation == "historical" else current_run(tmp_path)
    anchors = (
        [(10.0, 1810.0), (1830.0, 3630.0)]
        if generation == "historical"
        else [(10.0, 610.0), (630.0, 1230.0)]
    )
    assert [
        (r.event_monotonic_seconds, r.scheduled_end_monotonic)
        for r in run.lifecycle
        if r.event is lifecycle.ColdLifecycleEvent.PHASE_ACTIVATED
    ] == anchors
    records = list(run.lifecycle)
    index = next(
        i
        for i, record in enumerate(records)
        if record.event is lifecycle.ColdLifecycleEvent.PHASE_ACTIVATED and record.phase is phase
    )
    record = records[index]
    if case == "missing":
        records.pop(index)
    elif case == "duplicate":
        records.insert(index, record)
    else:
        # Literal opposite-generation and unknown ends are independent of runtime policy.
        mixed = {
            ("historical", OFF): 610.0,
            ("historical", ON): 2430.0,
            ("current", OFF): 1810.0,
            ("current", ON): 2430.0,
        }
        unknown = {
            ("historical", OFF): 1210.0,
            ("historical", ON): 3030.0,
            ("current", OFF): 1210.0,
            ("current", ON): 1830.0,
        }
        baseline_end = anchors[0 if phase is OFF else 1][1]
        end: object = {
            "mixed": mixed[generation, phase],
            "unknown": unknown[generation, phase],
            "substituted": math.nextafter(baseline_end, 0.0),
            "nan": math.nan,
            "inf": math.inf,
            "int": int(baseline_end),
            "bool": True,
            "str": str(baseline_end),
        }[case]
        records[index] = forged(record, scheduled_end_monotonic=end)
        if case in {"mixed", "unknown", "substituted"}:
            elapsed_index = next(
                i
                for i, item in enumerate(records)
                if item.event is lifecycle.ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED
                and item.phase is phase
            )
            records[elapsed_index] = forged(records[elapsed_index], scheduled_end_monotonic=end)
            assert records[elapsed_index].scheduled_end_monotonic == (
                records[index].scheduled_end_monotonic
            )
    tampered = forged(run, lifecycle=tuple(records))
    # Bypass constructors so the public checkers themselves must reject hostile records.
    v2 = reader.ColdRetainedRunV2.model_construct(
        run=tampered.run, lifecycle_state=tampered.lifecycle_state, lifecycle=tampered.lifecycle
    )
    v3 = reader.ColdRetainedRunV3.model_construct(
        run=tampered.run,
        lifecycle_state=tampered.lifecycle_state,
        lifecycle=tampered.lifecycle,
        advisory_attempt_state=tampered.advisory_attempt_state,
        advisory_attempts=tampered.advisory_attempts,
    )
    first = conformance.check_pre_advisory_conformance(v2)
    second = advisory_conformance.check_advisory_conformance(v3)
    historical = tc.check_temperature_conformance(tampered)
    current = tc.check_current_conformance(tampered)
    revised = tc.check_revised_conformance(tampered)
    assert [r.policy_version for r in (first, second, historical, current)] == [1, 2, 3, 4]
    assert revised.policy_version == 4 and revised.interpretation_revision == 1
    for result in (first, second, historical, current, revised):
        assert result.findings
    assert first.outcome is conformance.ColdConformanceOutcome.NOT_CONFORMANT
    assert historical.outcome is tc.ColdTemperatureConformanceOutcome.NOT_CONFORMANT
    assert current.outcome is tc.ColdTemperatureConformanceOutcome.NOT_CONFORMANT
    assert revised.outcome is tc.ColdTemperatureConformanceOutcome.NOT_CONFORMANT
    if generation == "historical" and phase is ON and case == "mixed":
        # All records remain individually valid and elapsed-end binding is unchanged.
        for item in records:
            lifecycle.validate_lifecycle_record(item)
        assert first.findings == (conformance.ColdConformanceFinding.SCHEDULED_END_MISMATCH,)


def test_runtime_refuses_historical_and_forged_policy3_results() -> None:
    """The exact current result class and exact version 4 are both necessary."""
    historic = tc.ColdTemperatureConformanceResult(
        policy_version=3,
        outcome=tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT,
        findings=(),
    )
    current = tc.ColdCurrentConformanceResult(
        policy_version=4, outcome=historic.outcome, findings=()
    )
    assert admit_checker(historic) is None
    assert admit_checker(forged(current, policy_version=3)) is None
    assert admit_checker(forged(historic, policy_version=4)) is None
    assert admit_checker(current) is None
    revised = tc.ColdRevisedConformanceResult(
        policy_version=4, interpretation_revision=1, outcome=historic.outcome, findings=()
    )
    assert admit_checker(revised) == revised
    for version in (True, 4.0, 3, "4"):
        with pytest.raises(pydantic.ValidationError):
            tc.ColdCurrentConformanceResult.model_validate(
                {"policy_version": version, "outcome": historic.outcome, "findings": ()}
            )


def test_current_builder_has_fixed_600_second_activation(tmp_path: Path) -> None:
    """The writer derives current scheduled ends without a caller duration."""
    run = current_run(tmp_path)
    activated = [
        r for r in run.lifecycle if r.event is lifecycle.ColdLifecycleEvent.PHASE_ACTIVATED
    ]
    assert [(r.event_monotonic_seconds, r.scheduled_end_monotonic) for r in activated] == [
        (10.0, 610.0),
        (630.0, 1230.0),
    ]
    assert engine_policy.COLD_PHASE_OBSERVATION_SECONDS == 600.0


@pytest.mark.parametrize("generation", ["historical", "current"])
def test_public_historical_policies_one_and_two_remain_pinned(
    tmp_path: Path, generation: str
) -> None:
    """Policy 1 and 2 never confer historical success on current activation relations."""
    retained = written(tmp_path) if generation == "historical" else current_run(tmp_path)
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
    first = conformance.check_pre_advisory_conformance(v2)
    second = advisory_conformance.check_advisory_conformance(v3)
    assert first.policy_version == 1 and second.policy_version == 2
    assert (first.findings == ()) is (generation == "historical")
    assert (second.findings == ()) is (generation == "historical")


@pytest.mark.parametrize("carrier", [None, object(), [], {}, "run"])
def test_current_checker_rejects_unadmitted_carriers(carrier: object) -> None:
    """Strict carrier admission is the first current-policy stage."""
    checked = tc.check_current_conformance(carrier)
    assert checked.policy_version == 4
    assert checked.findings == (tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,)


def test_current_checker_contains_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary checker errors produce a closed policy-4 failure."""

    def fail(*args: object) -> object:
        raise RuntimeError("injected internal failure")

    monkeypatch.setattr(tc, "_evaluate", fail)
    checked = tc.check_current_conformance(None)
    assert checked.findings == (tc.ColdTemperatureConformanceFinding.CHECKER_INTERNAL_FAILURE,)


def test_current_checker_propagates_base_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unchanged interruption boundary propagates non-Exception failures."""

    def fail(*args: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(tc, "_evaluate", fail)
    with pytest.raises(KeyboardInterrupt):
        tc.check_current_conformance(None)


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "temperature_screened_conformant"},
        {"findings": []},
        {"findings": ("carrier_not_admitted",)},
        {"findings": (tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,) * 2},
        {"findings": (tc.ColdTemperatureConformanceFinding.CARRIER_NOT_ADMITTED,)},
        {"outcome": tc.ColdTemperatureConformanceOutcome.NOT_CONFORMANT},
    ],
)
def test_current_result_invariants_are_closed(changes: dict[str, object]) -> None:
    """Current result validators preserve exact members, order and outcome agreement."""
    with pytest.raises(pydantic.ValidationError):
        tc.ColdCurrentConformanceResult.model_validate(
            {
                "policy_version": 4,
                "outcome": tc.ColdTemperatureConformanceOutcome.TEMPERATURE_SCREENED_CONFORMANT,
                "findings": (),
                **changes,
            }
        )
