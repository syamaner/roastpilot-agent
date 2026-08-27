"""Tests for the deterministic joint-window drop planner (#710 RP-C slice 1,
D176/D177).

``plan_joint_window`` is a pure, stateless, total function: no clock read, no
I/O, no randomness, no mutable state, never raises. These tests exercise the
runway closed form, the projection clamp, the ordered/disjoint classification
chain (``TEMP_SHORT`` > ``CLOSING`` > ``AHEAD`` > ``ON_TRACK``), the atomic
``None`` absent block, the ``JointWindowPlanner`` config (D177's cross-field
invariant lives in ``tests/test_config.py``, alongside the other
``ControllerConfig`` validators), and the replay pin (``tests/test_replay.py``).

No hardware. No model, advisor, or controller wiring — that lands in slice 3.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Any

import pytest

from roastpilot_agent.models import JointWindowStatus
from roastpilot_agent.post_fc_control import (
    JointWindowInputs,
    JointWindowPlan,
    plan_joint_window,
)

# --- shared fixtures -------------------------------------------------------

#: A comfortably valid baseline: DEVELOPMENT window 17-23 %, charge 600 s,
#: development 60 s (DTR 10 %), RoR 6 °C/min, target/ceiling both 196 °C.
_BASE_INPUTS = JointWindowInputs(
    bean_temp_c=180.0,
    bean_ror_c_per_min=6.0,
    charge_elapsed_seconds=600.0,
    development_elapsed_seconds=60.0,
    target_drop_temp_c=196.0,
    ceiling_temp_c=196.0,
    development_percent_min=17.0,
    development_percent_max=23.0,
)
_TEMP_MARGIN_C = 3.0
_CLOSING_HORIZON_SECONDS = 30.0

_NON_FINITE = [math.nan, math.inf, -math.inf]


def _call(
    inputs: JointWindowInputs = _BASE_INPUTS,
    *,
    temp_margin_c: float = _TEMP_MARGIN_C,
    closing_horizon_seconds: float = _CLOSING_HORIZON_SECONDS,
    **overrides: Any,
) -> JointWindowPlan | None:
    """Call :func:`plan_joint_window` against ``inputs`` with field overrides."""
    if overrides:
        inputs = replace(inputs, **overrides)
    return plan_joint_window(
        inputs, temp_margin_c=temp_margin_c, closing_horizon_seconds=closing_horizon_seconds
    )


def _values_equal(a: object, b: object) -> bool:
    """Equality through an ``object``-typed boundary.

    Keeps the *runtime* string-comparison assertion in T21 below without
    tripping pyright strict's ``reportUnnecessaryComparison`` on a
    statically-known-disjoint literal comparison — the exact static error
    the AGENTS.md typed-vocabulary invariant requires the enum to trigger
    when compared directly. This helper does not defeat that invariant: the
    production code never contains an unguarded literal comparison like the
    one this test evaluates only at runtime.
    """
    return a == b


# --- T1: runway closed form -------------------------------------------------


def test_t1_runway_closed_form_matches_formula_and_dtr_cross_check() -> None:
    charge = 500.0
    development = 40.0
    f_open = 0.15
    f_close = 0.25
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=charge,
        development_elapsed_seconds=development,
        development_percent_min=f_open * 100.0,
        development_percent_max=f_close * 100.0,
    )
    plan = _call(inputs)
    assert plan is not None

    expected_open = (f_open * charge - development) / (1.0 - f_open)
    expected_close = (f_close * charge - development) / (1.0 - f_close)
    assert math.isclose(plan.window_open_runway_seconds, expected_open, rel_tol=1e-12)
    assert math.isclose(plan.window_close_runway_seconds, expected_close, rel_tol=1e-12)

    # Cross-check: advancing both clocks by the returned runway reproduces
    # exactly the target DTR fraction.
    new_charge_open = charge + plan.window_open_runway_seconds
    new_development_open = development + plan.window_open_runway_seconds
    assert math.isclose(new_development_open / new_charge_open, f_open, rel_tol=1e-12)

    new_charge_close = charge + plan.window_close_runway_seconds
    new_development_close = development + plan.window_close_runway_seconds
    assert math.isclose(new_development_close / new_charge_close, f_close, rel_tol=1e-12)


# --- T2: projection ----------------------------------------------------------


def test_t2_projected_close_temp_matches_bean_plus_ror_times_clamped_runway() -> None:
    plan = _call()
    assert plan is not None
    assert plan.window_close_runway_seconds > 0.0  # unclamped == clamped here
    ror = _BASE_INPUTS.bean_ror_c_per_min
    assert ror is not None
    expected = _BASE_INPUTS.bean_temp_c + ror * plan.window_close_runway_seconds / 60.0
    assert math.isclose(plan.projected_temp_at_close_c, expected, rel_tol=1e-12)


# --- T3/T4: TEMP_SHORT boundary ---------------------------------------------


def _temp_short_boundary_inputs(*, close_runway_seconds: float = 120.0) -> JointWindowInputs:
    """Build inputs with a KNOWN, exact close runway (120 s here: f_close=0.25,
    charge=600, development=60 solves the closed form to exactly 120)."""
    return replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=60.0,
        development_percent_min=15.0,
        development_percent_max=25.0,
        target_drop_temp_c=196.0,
        ceiling_temp_c=196.0,
    )


def test_t3_one_milli_degree_below_threshold_is_temp_short() -> None:
    inputs = _temp_short_boundary_inputs()
    plan_probe = _call(inputs, bean_ror_c_per_min=0.0)
    assert plan_probe is not None
    close_runway = plan_probe.window_close_runway_seconds
    assert math.isclose(close_runway, 120.0, rel_tol=1e-9)

    threshold = 196.0 - _TEMP_MARGIN_C
    desired_projected_close = threshold - 0.001
    ror = (desired_projected_close - inputs.bean_temp_c) / (close_runway / 60.0)

    plan = _call(inputs, bean_ror_c_per_min=ror)
    assert plan is not None
    assert math.isclose(plan.projected_temp_at_close_c, desired_projected_close, abs_tol=1e-9)
    assert plan.status is JointWindowStatus.TEMP_SHORT


def test_t4_exact_threshold_boundary_is_not_temp_short() -> None:
    inputs = _temp_short_boundary_inputs()
    plan_probe = _call(inputs, bean_ror_c_per_min=0.0)
    assert plan_probe is not None
    close_runway = plan_probe.window_close_runway_seconds

    threshold = 196.0 - _TEMP_MARGIN_C
    ror = (threshold - inputs.bean_temp_c) / (close_runway / 60.0)

    plan = _call(inputs, bean_ror_c_per_min=ror)
    assert plan is not None
    assert math.isclose(plan.projected_temp_at_close_c, threshold, abs_tol=1e-9)
    assert plan.status is not JointWindowStatus.TEMP_SHORT
    # Fully pin the expected residual classification too (not CLOSING: the
    # 120 s close runway is far outside the 30 s default horizon; not AHEAD:
    # the open-window projection stays below target with this RoR).
    assert plan.status is JointWindowStatus.ON_TRACK


# --- T5/T6: CLOSING boundary -------------------------------------------------


def test_t5_close_runway_exactly_at_horizon_is_closing() -> None:
    # charge=600, f_close=0.25 -> development=127.5 gives close_runway == 30.0
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=127.5,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_temp_c=190.0,
        bean_ror_c_per_min=8.0,
    )
    plan = _call(inputs)
    assert plan is not None
    assert math.isclose(plan.window_close_runway_seconds, 30.0, rel_tol=1e-12)
    # Comfortably not temperature-short: 190 + 8*(30/60) = 194 >= 196-3=193.
    assert plan.status is JointWindowStatus.CLOSING


def test_t6_negative_close_runway_projects_to_now_not_backwards() -> None:
    # charge=600, f_close=0.25 -> development=187.5 gives close_runway == -50.0
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=187.5,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_temp_c=194.0,
        bean_ror_c_per_min=5.0,
    )
    plan = _call(inputs)
    assert plan is not None
    assert math.isclose(plan.window_close_runway_seconds, -50.0, rel_tol=1e-12)
    # The RAW runway stays negative (reported honestly)...
    assert plan.window_close_runway_seconds < 0.0
    # ...but the projection clamps to now: bean temp unchanged by RoR.
    assert plan.projected_temp_at_close_c == inputs.bean_temp_c
    assert plan.status is JointWindowStatus.CLOSING


def test_t6_negative_open_runway_projects_to_now_and_can_be_ahead() -> None:
    """An already-open window never projects RoR backwards from now."""
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=100.0,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_temp_c=196.0,
        bean_ror_c_per_min=6.0,
    )
    plan = _call(inputs)
    assert plan is not None
    assert plan.window_open_runway_seconds < 0.0
    assert plan.window_close_runway_seconds > _CLOSING_HORIZON_SECONDS
    assert plan.projected_temp_at_open_c == inputs.bean_temp_c
    assert plan.status is JointWindowStatus.AHEAD


# --- T7: precedence -----------------------------------------------------


def test_t7_temp_short_takes_precedence_over_closing() -> None:
    # close_runway == 10 s (<= 30 s horizon) AND projected close well below
    # threshold (193.0): both TEMP_SHORT and CLOSING conditions hold.
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=142.5,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_temp_c=180.0,
        bean_ror_c_per_min=6.0,
    )
    plan = _call(inputs)
    assert plan is not None
    assert math.isclose(plan.window_close_runway_seconds, 10.0, rel_tol=1e-9)
    assert plan.window_close_runway_seconds <= _CLOSING_HORIZON_SECONDS
    assert plan.projected_temp_at_close_c < 196.0 - _TEMP_MARGIN_C
    assert plan.status is JointWindowStatus.TEMP_SHORT


# --- T8/T9: AHEAD boundary / ON_TRACK residual ------------------------------


def _open_boundary_inputs(*, bean_ror_c_per_min: float) -> JointWindowInputs:
    # f_open=0.15, charge=600, development=39 -> open_runway == 60.0 exactly;
    # f_close=0.25 -> close_runway == 148.0 (far outside the 30 s horizon).
    return replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=39.0,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_temp_c=190.0,
        bean_ror_c_per_min=bean_ror_c_per_min,
        target_drop_temp_c=196.0,
        ceiling_temp_c=196.0,
    )


def test_t8_projected_open_temp_exactly_equal_to_target_is_ahead() -> None:
    inputs = _open_boundary_inputs(bean_ror_c_per_min=6.0)
    plan = _call(inputs)
    assert plan is not None
    assert math.isclose(plan.window_open_runway_seconds, 60.0, rel_tol=1e-12)
    assert plan.window_close_runway_seconds > _CLOSING_HORIZON_SECONDS
    assert plan.projected_temp_at_open_c == 196.0
    assert plan.status is JointWindowStatus.AHEAD


def test_t9_on_track_when_not_short_not_closing_and_not_yet_ahead() -> None:
    inputs = _open_boundary_inputs(bean_ror_c_per_min=4.0)
    plan = _call(inputs)
    assert plan is not None
    assert plan.projected_temp_at_open_c < 196.0
    assert plan.window_close_runway_seconds > _CLOSING_HORIZON_SECONDS
    assert plan.projected_temp_at_close_c >= 196.0 - _TEMP_MARGIN_C
    assert plan.status is JointWindowStatus.ON_TRACK


# --- T10: exhaustive disjointness / coverage grid ---------------------------


def _reference_status(
    inputs: JointWindowInputs, *, temp_margin_c: float, horizon: float
) -> JointWindowStatus:
    """An independently-written re-statement of the documented classification
    formula (spec §2.3.4-§2.3.5), used ONLY to sweep a grid and confirm
    ``plan_joint_window`` agrees at every point — a second reading of the
    same ratified arithmetic, not a black-box oracle."""
    f_open = inputs.development_percent_min / 100.0
    f_close = inputs.development_percent_max / 100.0
    charge = inputs.charge_elapsed_seconds
    development = inputs.development_elapsed_seconds
    assert development is not None
    ror = inputs.bean_ror_c_per_min
    assert ror is not None
    r_open = (f_open * charge - development) / (1.0 - f_open)
    r_close = (f_close * charge - development) / (1.0 - f_close)
    effective_target = min(inputs.target_drop_temp_c, inputs.ceiling_temp_c)
    proj_open = inputs.bean_temp_c + ror * max(0.0, r_open) / 60.0
    proj_close = inputs.bean_temp_c + ror * max(0.0, r_close) / 60.0
    if proj_close < effective_target - temp_margin_c:
        return JointWindowStatus.TEMP_SHORT
    if r_close <= horizon:
        return JointWindowStatus.CLOSING
    if proj_open >= effective_target:
        return JointWindowStatus.AHEAD
    return JointWindowStatus.ON_TRACK


def test_t10_grid_is_disjoint_and_covers_all_four_statuses() -> None:
    charge = 600.0
    development_values = [30.0, 60.0, 90.0, 120.0, 127.5, 150.0, 187.5, 250.0, 400.0]
    ror_values = [-3.0, 0.0, 2.0, 6.0, 12.0]
    bean_temp_values = [150.0, 180.0, 194.0, 200.0]

    seen_statuses: set[JointWindowStatus] = set()
    checked = 0
    for development in development_values:
        for ror in ror_values:
            for bean_temp in bean_temp_values:
                inputs = replace(
                    _BASE_INPUTS,
                    charge_elapsed_seconds=charge,
                    development_elapsed_seconds=development,
                    development_percent_min=15.0,
                    development_percent_max=25.0,
                    bean_temp_c=bean_temp,
                    bean_ror_c_per_min=ror,
                    target_drop_temp_c=196.0,
                    ceiling_temp_c=196.0,
                )
                plan = _call(inputs)
                assert plan is not None  # every combination above is valid
                expected = _reference_status(
                    inputs, temp_margin_c=_TEMP_MARGIN_C, horizon=_CLOSING_HORIZON_SECONDS
                )
                assert plan.status is expected
                seen_statuses.add(plan.status)
                checked += 1

    assert checked == len(development_values) * len(ror_values) * len(bean_temp_values)
    assert seen_statuses == set(JointWindowStatus)


# --- T11/T12: effective-target cap ------------------------------------------


def test_t11_effective_target_is_capped_at_the_ceiling() -> None:
    inputs = _open_boundary_inputs(bean_ror_c_per_min=6.0)
    inputs = replace(inputs, target_drop_temp_c=200.0, ceiling_temp_c=196.0)
    plan = _call(inputs)
    assert plan is not None
    assert plan.effective_target_temp_c == 196.0
    # Classification still uses the capped 196.0 (matches the uncapped case
    # exactly since the cap is what makes the effective target 196.0 here).
    assert plan.status is JointWindowStatus.AHEAD


def test_t12_effective_target_cap_is_a_min_not_a_replace() -> None:
    inputs = replace(_BASE_INPUTS, target_drop_temp_c=195.0, ceiling_temp_c=196.0)
    plan = _call(inputs)
    assert plan is not None
    assert plan.effective_target_temp_c == 195.0


# --- T13/T14: zero / negative RoR are valid ---------------------------------


def test_t13_zero_ror_is_valid_and_typically_temp_short() -> None:
    plan = _call(bean_temp_c=100.0, bean_ror_c_per_min=0.0)
    assert plan is not None
    assert plan.status is JointWindowStatus.TEMP_SHORT


def test_t14_negative_ror_is_valid_and_projects_below_current() -> None:
    inputs = replace(
        _BASE_INPUTS,
        charge_elapsed_seconds=600.0,
        development_elapsed_seconds=39.0,
        development_percent_min=15.0,
        development_percent_max=25.0,
        bean_ror_c_per_min=-5.0,
    )
    plan = _call(inputs)
    assert plan is not None
    assert plan.window_open_runway_seconds > 0.0
    assert plan.window_close_runway_seconds > 0.0
    assert plan.projected_temp_at_open_c < inputs.bean_temp_c
    assert plan.projected_temp_at_close_c < inputs.bean_temp_c


# --- T15: absent block is atomic --------------------------------------------


def _plan_with_input_field_override(field: str, value: float) -> JointWindowPlan | None:
    """Build ``_BASE_INPUTS`` with exactly one field overridden by name.

    Routed through ``dataclasses.replace`` (whose ``obj`` parameter is
    positional-only) rather than ``_call(**{field: value})``: a dynamically
    keyed ``**`` unpack against ``_call``'s own named ``inputs`` parameter
    cannot be ruled out by pyright strict as targeting ``inputs`` itself
    (``field`` is a plain ``str``, not a literal), which spuriously flags
    ``reportArgumentType``. This helper has no such collision.
    """
    field_override: dict[str, Any] = {field: value}
    inputs = replace(_BASE_INPUTS, **field_override)
    return plan_joint_window(
        inputs, temp_margin_c=_TEMP_MARGIN_C, closing_horizon_seconds=_CLOSING_HORIZON_SECONDS
    )


def _plan_with_planner_param_override(field: str, value: float) -> JointWindowPlan | None:
    """Build the ``temp_margin_c``/``closing_horizon_seconds`` pair with one
    overridden by name, for the same pyright-collision reason above —
    ``inputs`` is passed positionally, so the ``dict[str, float]`` unpack
    below can only ever target the two keyword-only planner parameters."""
    planner_kwargs: dict[str, float] = {
        "temp_margin_c": _TEMP_MARGIN_C,
        "closing_horizon_seconds": _CLOSING_HORIZON_SECONDS,
    }
    planner_kwargs[field] = value
    return plan_joint_window(_BASE_INPUTS, **planner_kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "bean_temp_c",
        "bean_ror_c_per_min",
        "charge_elapsed_seconds",
        "development_elapsed_seconds",
        "target_drop_temp_c",
        "ceiling_temp_c",
        "development_percent_min",
        "development_percent_max",
    ],
)
@pytest.mark.parametrize("value", _NON_FINITE)
def test_t15_non_finite_input_field_returns_none(field: str, value: float) -> None:
    assert _plan_with_input_field_override(field, value) is None


@pytest.mark.parametrize("field", ["temp_margin_c", "closing_horizon_seconds"])
@pytest.mark.parametrize("value", _NON_FINITE)
def test_t15_non_finite_planner_param_returns_none(field: str, value: float) -> None:
    assert _plan_with_planner_param_override(field, value) is None


def test_t15_missing_ror_returns_none_not_an_exception() -> None:
    assert _call(bean_ror_c_per_min=None) is None


def test_t15_missing_development_elapsed_returns_none_not_an_exception() -> None:
    assert _call(development_elapsed_seconds=None) is None


def test_t15_finite_input_overflow_returns_no_partial_plan() -> None:
    """Finite input multiplication that overflows makes the plan atomically absent."""
    assert _call(bean_ror_c_per_min=1e308) is None


def test_t15_large_but_finite_outputs_remain_valid() -> None:
    """Large finite arithmetic remains usable rather than being over-rejected."""
    plan = _call(bean_temp_c=1e300, bean_ror_c_per_min=1e300)
    assert plan is not None
    assert all(
        math.isfinite(value)
        for value in (
            plan.effective_target_temp_c,
            plan.window_open_runway_seconds,
            plan.window_close_runway_seconds,
            plan.projected_temp_at_open_c,
            plan.projected_temp_at_close_c,
        )
    )


# --- T16: clock validity -----------------------------------------------------


@pytest.mark.parametrize("charge", [0.0, -1.0, -100.0])
def test_t16_non_positive_charge_returns_none(charge: float) -> None:
    assert _call(charge_elapsed_seconds=charge) is None


def test_t16_negative_development_returns_none() -> None:
    assert _call(development_elapsed_seconds=-0.1) is None


def test_t16_development_greater_than_charge_returns_none() -> None:
    assert _call(development_elapsed_seconds=_BASE_INPUTS.charge_elapsed_seconds + 1.0) is None


# --- T17/T18: window ordering -------------------------------------------------


def test_t17_min_greater_than_max_returns_none() -> None:
    assert _call(development_percent_min=24.0, development_percent_max=23.0) is None


def test_t18_min_equal_max_collapsed_point_window_returns_a_plan() -> None:
    plan = _call(development_percent_min=20.0, development_percent_max=20.0)
    assert plan is not None
    assert plan.window_open_runway_seconds == plan.window_close_runway_seconds


# --- T19: fraction open-unit-interval guard ----------------------------------


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_t19_fraction_min_not_strictly_positive_returns_none(value: float) -> None:
    assert _call(development_percent_min=value) is None


@pytest.mark.parametrize("value", [100.0, 105.0])
def test_t19_fraction_max_at_or_above_100_returns_none(value: float) -> None:
    assert _call(development_percent_max=value) is None


# --- T20: statelessness / purity ---------------------------------------------


def test_t20_repeated_shuffled_calls_match_isolated_calls() -> None:
    scenarios = [
        replace(
            _BASE_INPUTS,
            bean_temp_c=150.0 + i,
            bean_ror_c_per_min=float((i % 7) - 3),
            charge_elapsed_seconds=500.0 + i * 3,
            development_elapsed_seconds=30.0 + i,
            development_percent_min=15.0,
            development_percent_max=25.0,
        )
        for i in range(40)
    ]
    isolated_results = [_call(scenario) for scenario in scenarios]

    call_order = list(range(len(scenarios))) * 25
    random.Random(0).shuffle(call_order)
    assert len(call_order) == 1000

    for index in call_order:
        assert _call(scenarios[index]) == isolated_results[index]

    # No module-level or caller-visible state exists for this function: it
    # reads only its ``inputs``/keyword arguments and returns a fresh,
    # independent ``JointWindowPlan`` (or ``None``) every call — the
    # identical-output re-check above is the operative purity proof, since
    # there is no mutable object to inspect before/after.


# --- T21: plain Enum, never string-compared ---------------------------------


def test_t21_joint_window_status_is_plain_enum_not_str_subclass() -> None:
    assert not issubclass(JointWindowStatus, str)
    assert _values_equal(JointWindowStatus.TEMP_SHORT, "temp_short") is False
    for member in JointWindowStatus:
        assert not isinstance(member, str)
