"""Behavioural tests for the pure D209 cold temperature screen (#997 T1); hardware-free.

Every projection is built through the real ``admit_cold_temperature_projection`` from
an S1-valid raw shape, and every expected reason tuple is written by hand.  Nothing
here touches an MCP child, a device, or a provider.
"""

import ast
import enum
import json
import typing
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import engine_policy
from roastpilot_agent.cold_characterisation import temperature_screen as screen
from roastpilot_agent.cold_characterisation.evidence_temperature_run import (
    ColdTemperatureScreenReason,
)
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTickTemperatureProjection,
    admit_cold_temperature_projection,
)

R = ColdTemperatureScreenReason
Projection = ColdTickTemperatureProjection
SOURCE = Path(screen.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NOT_ADMITTED = (R.SCREEN_INPUT_NOT_ADMITTED,)


class FloatSubclass(float):
    """A ``float`` subclass (never admitted as elapsed seconds)."""


class _Interrupt(BaseException):
    """A non-``Exception`` interruption."""


# ------------------------------------------------------------------ fixtures


def _admitted(raw: dict[str, object]) -> Projection:
    """Admit one raw projection through the real admission function."""
    projection = admit_cold_temperature_projection(raw)
    assert type(projection) is Projection, projection
    return projection


def _raw(outcome: str, **values: object) -> dict[str, object]:
    """Return one raw projection with every value field null unless named."""
    raw: dict[str, object] = {
        "projection_version": 1,
        "outcome": outcome,
        "configured_temperature_unit": None,
        "reported_temperature_unit": None,
        "last_packet_valid": None,
        "last_packet_bean_temp_c": None,
        "last_packet_env_temp_c": None,
        "retained_bean_temp_c": None,
        "retained_env_temp_c": None,
        "value_agreement": None,
        "status_packet_count": None,
        "ignored_temperature_packet_count": None,
        "status_read_error_count": None,
        "command_loop_error_count": None,
    }
    for name, value in values.items():
        assert name in raw, name
        raw[name] = value
    return raw


def obs(
    status: int,
    *,
    ignored: int = 0,
    rerr: int = 0,
    lerr: int = 0,
    bt: float = 20.0,
    et: float = 20.0,
    agree: bool = True,
) -> Projection:
    """An observed, Celsius, valid projection; ``agree=False`` retains bean 21.0."""
    return _admitted(
        _raw(
            "observed",
            configured_temperature_unit="celsius",
            reported_temperature_unit="celsius",
            last_packet_valid=True,
            last_packet_bean_temp_c=bt,
            last_packet_env_temp_c=et,
            retained_bean_temp_c=bt if agree else 21.0,
            retained_env_temp_c=et,
            value_agreement="agree" if agree else "disagree",
            status_packet_count=status,
            ignored_temperature_packet_count=ignored,
            status_read_error_count=rerr,
            command_loop_error_count=lerr,
        )
    )


def fahrenheit(status: int) -> Projection:
    """An observed, Fahrenheit-reported, valid projection with no Celsius last values."""
    return _admitted(
        _raw(
            "observed",
            configured_temperature_unit="fahrenheit",
            reported_temperature_unit="fahrenheit",
            last_packet_valid=True,
            retained_bean_temp_c=70.0,
            retained_env_temp_c=71.0,
            value_agreement="indeterminate",
            status_packet_count=status,
            ignored_temperature_packet_count=0,
            status_read_error_count=0,
            command_loop_error_count=0,
        )
    )


def unknown(status: int, *, ignored: int) -> Projection:
    """An observed projection whose latest packet's temperature was ignored."""
    retained = 20.0 if ignored < status else None
    return _admitted(
        _raw(
            "observed",
            configured_temperature_unit="auto",
            reported_temperature_unit="unknown",
            last_packet_valid=False,
            retained_bean_temp_c=retained,
            retained_env_temp_c=retained,
            value_agreement="indeterminate",
            status_packet_count=status,
            ignored_temperature_packet_count=ignored,
            status_read_error_count=0,
            command_loop_error_count=0,
        )
    )


def awaiting() -> Projection:
    """An eligible projection awaiting its first status packet."""
    return _admitted(
        _raw(
            "awaiting_first_packet",
            configured_temperature_unit="celsius",
            last_packet_valid=False,
            value_agreement="indeterminate",
            status_packet_count=0,
            ignored_temperature_packet_count=0,
            status_read_error_count=0,
            command_loop_error_count=0,
        )
    )


def empty(outcome: str) -> Projection:
    """A malformed, unsupported or not-eligible projection with every value null."""
    return _admitted(_raw(outcome))


def evaluate(current: object, previous: object, since: object) -> tuple[R, ...]:
    """Call the screen under test."""
    return screen.evaluate_temperature(current, previous=previous, since_activation_seconds=since)


# ------------------------------------------------------------ startup and prior


def test_sc1_startup_absence_before_the_boundary_has_no_reason() -> None:
    """SC1 (AC3): awaiting after awaiting at 59 s is ordinary startup absence."""
    assert evaluate(awaiting(), awaiting(), 59.0) == ()


def test_sc2_first_eligible_observation_without_a_prior_is_refused() -> None:
    """SC2 (AC2, AC3): no previous snapshot at exactly 60 s."""
    assert evaluate(obs(1), None, 60.0) == (R.PRIOR_MISSING,)


def test_sc2b_the_same_observation_just_before_the_boundary_has_no_reason() -> None:
    """SC2b: the boundary is inclusive at 60 s and not before."""
    assert evaluate(obs(1), None, 59.999) == ()


def test_sc13_an_unobservable_prior_is_a_missing_prior() -> None:
    """SC13: a previous snapshot without counters is a missing prior once eligible."""
    assert evaluate(obs(1), empty("unsupported"), 60.0) == (R.PRIOR_MISSING,)


def test_sc12_awaiting_after_the_boundary_is_not_observed_and_not_progressed() -> None:
    """SC12: outcome is branched on first; awaiting is not observed after startup."""
    assert evaluate(awaiting(), awaiting(), 60.0) == (
        R.NOT_OBSERVED_AFTER_STARTUP,
        R.PACKET_NOT_PROGRESSED,
    )


# ------------------------------------------------------------------- range


@pytest.mark.parametrize("value", [5.0, 40.0, 20.0])
def test_sc3_inclusive_range_bounds_have_no_reason(value: float) -> None:
    """SC3 (AC1): both temperatures at each inclusive bound and mid-range pass."""
    assert evaluate(obs(2, bt=value, et=value), obs(1), 100.0) == ()


@pytest.mark.parametrize(("bt", "et"), [(4.0, 20.0), (20.0, 4.0), (41.0, 20.0), (20.0, 41.0)])
def test_sc4_either_temperature_outside_the_range_is_refused(bt: float, et: float) -> None:
    """SC4 (AC1): bean and environment temperatures are each screened."""
    assert evaluate(obs(2, bt=bt, et=et), obs(1), 100.0) == (R.OUTSIDE_SCREEN,)


# ---------------------------------------------------- unit, validity, agreement


def test_sc5a_a_fahrenheit_report_is_not_valid_celsius() -> None:
    """SC5a (AC2): the reported unit must be Celsius."""
    assert evaluate(fahrenheit(2), obs(1), 100.0) == (R.LAST_PACKET_NOT_VALID_CELSIUS,)


def test_sc5b_an_ignored_latest_packet_is_not_valid_and_counts_a_fault() -> None:
    """SC5b (AC2): an unknown unit is not valid Celsius; its ignored packet is a fault."""
    assert evaluate(unknown(3, ignored=1), obs(1), 100.0) == (
        R.LAST_PACKET_NOT_VALID_CELSIUS,
        R.FAULT_COUNTED,
    )


def test_sc6_disagreeing_values_are_refused() -> None:
    """SC6 (AC2): raw/typed disagreement is refused (consistency, not corroboration)."""
    assert evaluate(obs(2, agree=False), obs(1), 100.0) == (R.VALUES_DISAGREE,)


# ------------------------------------------------------ progress and faults


def test_sc7a_no_new_accepted_packet_is_not_progressed() -> None:
    """SC7a (AC9): progress means arrival between observations."""
    assert evaluate(obs(1), obs(1), 100.0) == (R.PACKET_NOT_PROGRESSED,)


def test_sc7b_progress_counts_accepted_packets_not_status_packets() -> None:
    """SC7b: one more status packet that was ignored is not progress, and is a fault."""
    assert evaluate(obs(3, ignored=1), obs(2), 100.0) == (
        R.PACKET_NOT_PROGRESSED,
        R.FAULT_COUNTED,
    )


def _one_fault(status: int, counter: str) -> Projection:
    """An observation with exactly one newly counted fault of the named counter."""
    if counter == "rerr":
        return obs(status, rerr=1)
    if counter == "lerr":
        return obs(status, lerr=1)
    return obs(status, ignored=1)


@pytest.mark.parametrize("counter", ["rerr", "lerr"])
def test_sc8_newly_counted_errors_after_the_boundary_are_faults(counter: str) -> None:
    """SC8 (AC3): a newly counted read or command-loop error fails the run."""
    assert evaluate(_one_fault(2, counter), obs(1), 100.0) == (R.FAULT_COUNTED,)


@pytest.mark.parametrize("counter", ["rerr", "lerr", "ignored"])
def test_sc8b_newly_counted_faults_before_the_boundary_have_no_reason(counter: str) -> None:
    """SC8b: fault counting is gated on eligibility."""
    assert evaluate(_one_fault(3, counter), obs(1), 30.0) == ()


def test_sc9_the_first_eligible_observation_compares_against_a_pre_boundary_prior() -> None:
    """SC9 (AC2): a snapshot taken at 59 s is the prior of the 60 s observation."""
    assert evaluate(obs(2, rerr=1), obs(1), 60.0) == (R.FAULT_COUNTED,)


# ------------------------------------------------------------- regression

#: One individual counter decline per case: builds (previous, current).
DECLINES: typing.Final[dict[str, typing.Callable[[], tuple[Projection, Projection]]]] = {
    "status": lambda: (obs(5), obs(4)),
    "ignored": lambda: (obs(5, ignored=2), obs(6, ignored=1)),
    "rerr": lambda: (obs(5, rerr=2), obs(6, rerr=1)),
    "lerr": lambda: (obs(5, lerr=2), obs(6, lerr=1)),
}
#: The hand-derived reasons of each decline once eligible.
ELIGIBLE_DECLINES: typing.Final[dict[str, tuple[R, ...]]] = {
    # 4 accepted after 5: the derived count also declines and nothing progressed.
    "status": (R.COUNTER_REGRESSED, R.PACKET_NOT_PROGRESSED),
    # 5 accepted after 3: progressed; a decline is not a newly counted fault.
    "ignored": (R.COUNTER_REGRESSED,),
    "rerr": (R.COUNTER_REGRESSED,),
    "lerr": (R.COUNTER_REGRESSED,),
}


def _pair(name: str) -> tuple[Projection, Projection]:
    """Return one decline case as (previous, current) projections."""
    return DECLINES[name]()


@pytest.mark.parametrize("name", list(DECLINES))
def test_sc10_an_individual_counter_decline_is_refused_before_the_boundary(name: str) -> None:
    """SC10 (AC3, L2): a counter regression is not startup absence."""
    previous, current = _pair(name)
    assert evaluate(current, previous, 10.0) == (R.COUNTER_REGRESSED,)


@pytest.mark.parametrize("name", list(DECLINES))
def test_sc10b_an_individual_counter_decline_after_the_boundary(name: str) -> None:
    """SC10b: the same declines once eligible, with their hand-derived companions."""
    previous, current = _pair(name)
    assert evaluate(current, previous, 100.0) == ELIGIBLE_DECLINES[name]


def test_sc10c_a_derived_accepted_decline_is_refused_before_the_boundary() -> None:
    """SC10c (L2): status and ignored both rise, but accepted falls from 4 to 3."""
    assert evaluate(obs(6, ignored=3), obs(5, ignored=1), 10.0) == (R.COUNTER_REGRESSED,)


def test_sc10d_a_derived_accepted_decline_after_the_boundary() -> None:
    """SC10d: the same decline once eligible: not progressed, and an ignored-packet fault."""
    assert evaluate(obs(6, ignored=3), obs(5, ignored=1), 100.0) == (
        R.COUNTER_REGRESSED,
        R.PACKET_NOT_PROGRESSED,
        R.FAULT_COUNTED,
    )


# -------------------------------------------------------------- outcomes


def test_sc11_a_malformed_projection_is_refused_at_any_time() -> None:
    """SC11 (AC3, AC9): a closed malformed projection carries no detail."""
    assert evaluate(empty("malformed"), None, 10.0) == (R.PROJECTION_MALFORMED,)


@pytest.mark.parametrize("outcome", ["unsupported", "not_eligible"])
def test_sc11b_unknown_observability_is_refused_immediately(outcome: str) -> None:
    """SC11b (L1): unsupported and not-eligible are not ordinary startup absence."""
    assert evaluate(empty(outcome), None, 10.0) == (R.NOT_OBSERVABLE,)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("malformed", R.PROJECTION_MALFORMED),
        ("unsupported", R.NOT_OBSERVABLE),
        ("not_eligible", R.NOT_OBSERVABLE),
    ],
)
def test_sc11c_empty_outcomes_after_the_boundary(outcome: str, reason: R) -> None:
    """SC11c (AC7): outcome first once eligible too; no counters means no progress reason."""
    assert evaluate(empty(outcome), obs(1), 100.0) == (reason,)
    assert evaluate(empty(outcome), None, 100.0) == (reason, R.PRIOR_MISSING)


# ---------------------------------------------------------- admission (O5)


def _forged() -> Projection:
    """An exact-class projection whose content is not admitted."""
    return obs(2).model_copy(update={"projection_version": 2})


@pytest.mark.parametrize(
    ("current", "previous", "since"),
    [
        pytest.param(_forged(), obs(1), 100.0, id="forged-current"),
        pytest.param(obs(2), _forged(), 100.0, id="forged-previous"),
        pytest.param(None, obs(1), 100.0, id="none-current"),
        pytest.param({"projection_version": 1}, obs(1), 100.0, id="dict-current"),
        pytest.param(obs(2), {"projection_version": 1}, 100.0, id="dict-previous"),
        pytest.param(obs(2), obs(1), 60, id="int-since"),
        pytest.param(obs(2), obs(1), float("nan"), id="nan-since"),
        pytest.param(obs(2), obs(1), float("inf"), id="inf-since"),
        pytest.param(obs(2), obs(1), -1.0, id="negative-since"),
        pytest.param(obs(2), obs(1), FloatSubclass(100.0), id="float-subclass-since"),
        pytest.param(obs(2), obs(1), True, id="bool-since"),
    ],
)
def test_sc14_unadmitted_input_returns_the_single_admission_reason(
    current: object, previous: object, since: object
) -> None:
    """SC14 (O5): forged, non-model, and non-exact inputs are refused alone."""
    assert evaluate(current, previous, since) == NOT_ADMITTED


def test_sc14_positive_controls_for_the_admission_cases() -> None:
    """SC14 positive controls: the same calls with valid inputs carry no reason."""
    assert evaluate(obs(2), obs(1), 100.0) == ()
    assert evaluate(obs(2), obs(1), 0.0) == ()
    assert evaluate(obs(2), obs(1), -0.0) == ()


def _corpus() -> list[object]:
    """Every JSON-derived value kind plus a bare object."""
    values: list[object] = [
        None,
        True,
        0,
        -1,
        10**40,
        1.5,
        "",
        "observed",
        [],
        {},
        [1, [2]],
        {"projection_version": 1, "outcome": "observed"},
    ]
    corpus = [json.loads(json.dumps(value)) for value in values]
    corpus.append(object())
    return corpus


@pytest.mark.parametrize("value", _corpus())
def test_sc15_a_non_model_current_is_refused_and_never_raises(value: object) -> None:
    """SC15: the screen is total over non-model inputs."""
    assert evaluate(value, None, 10.0) == NOT_ADMITTED
    assert evaluate(obs(1), value, 10.0) == (() if value is None else NOT_ADMITTED)


def test_sc15_a_base_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``BaseException`` that is not an ``Exception`` is never caught."""

    def interrupt(value: object) -> object:
        raise _Interrupt

    monkeypatch.setattr(screen, "readmit_cold_temperature_projection", interrupt)
    with pytest.raises(_Interrupt):
        evaluate(obs(1), None, 10.0)


def test_sc15_the_input_instance_is_never_mutated() -> None:
    """The screen reads fresh copies; the caller's projections are unchanged."""
    current, previous = obs(2), obs(1)
    before = (current.model_dump(), previous.model_dump())
    evaluate(current, previous, 100.0)
    assert (current.model_dump(), previous.model_dump()) == before


# --------------------------------------------------------------- structure


def _imports() -> tuple[set[str], dict[str, set[str]]]:
    """Return plain imports and ``from`` imports with their names."""
    plain: set[str] = set()
    named: dict[str, set[str]] = {}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            named.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return plain, named


def test_sc16_imports_are_the_allow_list_and_no_io() -> None:
    """SC16: the pure leaf imports only its allow-listed modules."""
    plain, named = _imports()
    cold = "roastpilot_agent.cold_characterisation."
    assert plain == {"math", "typing"}
    assert set(named) == {
        cold + "engine_policy",
        cold + "evidence_temperature_run",
        cold + "temperature_projection",
    }
    assert named[cold + "engine_policy"] == {"COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS"}
    forbidden = {"os", "sys", "io", "socket", "pathlib", "asyncio", "logging", "time"}
    assert not (plain | set(named)) & forbidden


def test_sc16_no_forbidden_syntax_or_text() -> None:
    """SC16: no ``isinstance``, broad ``except``, ``print``, ``StrEnum`` or fenced word."""
    source = SOURCE.read_text(encoding="utf-8")
    for text in ("isinstance(", "except Exception", "print(", "StrEnum", "subprocess"):
        assert text not in source, text
    handlers = [node for node in ast.walk(TREE) if isinstance(node, ast.ExceptHandler)]
    assert handlers == []


def test_sc16_constants_are_the_ratified_values() -> None:
    """SC16: the inclusive Celsius screen and the shared 60-second engine constant."""
    assert (screen.COLD_TEMPERATURE_SCREEN_MIN_C, screen.COLD_TEMPERATURE_SCREEN_MAX_C) == (
        5.0,
        40.0,
    )
    assert type(screen.COLD_TEMPERATURE_SCREEN_MIN_C) is float
    assert type(screen.COLD_TEMPERATURE_SCREEN_MAX_C) is float
    imported = vars(screen)["COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS"]
    assert imported is engine_policy.COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS
    assert engine_policy.COLD_STARTUP_TELEMETRY_DEADLINE_SECONDS == 60.0


def test_sc16_reasons_are_a_closed_plain_enum_in_report_order() -> None:
    """SC16: the reason vocabulary is a plain ``Enum`` with the pinned tokens."""
    assert R.__bases__ == (enum.Enum,)
    assert not issubclass(R, (str, int))
    assert [(member.name, member.value) for member in R] == [
        ("SCREEN_INPUT_NOT_ADMITTED", "temperature_screen_input_not_admitted"),
        ("PROJECTION_MALFORMED", "temperature_projection_malformed"),
        ("NOT_OBSERVABLE", "temperature_not_observable"),
        ("COUNTER_REGRESSED", "temperature_counter_regressed"),
        ("PRIOR_MISSING", "temperature_prior_missing"),
        ("NOT_OBSERVED_AFTER_STARTUP", "temperature_not_observed_after_startup"),
        ("LAST_PACKET_NOT_VALID_CELSIUS", "temperature_last_packet_not_valid_celsius"),
        ("VALUES_DISAGREE", "temperature_values_disagree"),
        ("OUTSIDE_SCREEN", "temperature_outside_screen"),
        ("PACKET_NOT_PROGRESSED", "temperature_packet_not_progressed"),
        ("FAULT_COUNTED", "temperature_fault_counted"),
    ]
