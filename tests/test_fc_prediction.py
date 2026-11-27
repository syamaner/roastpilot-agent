"""Tests for the D239 first-crack predictors and anchor tracker (#1118)."""

from __future__ import annotations

import math

import pytest

from roastpilot_agent.fc_prediction import (
    AnchorSettings,
    AnchorState,
    FcAnchorTracker,
    HeaterLagFilter,
    TwoNodeParameters,
    planned_finish_charge_seconds,
    slope_fc_eta_seconds,
    two_node_fc_eta_seconds,
)
from roastpilot_agent.roast_history import RoastCurveSample

#: The 30 s of indicated bean readings before the 7 Nov 2026 winter trim
#: (charge +421.989 s, BT 157 °C), from the run's SSE capture. Validation
#: excerpt: the study's predictor B gives 127.66 s here (124 s observed).
NOV07_TRIM_WINDOW = [
    (392.974, 152.0), (393.975, 153.0), (397.554, 153.0), (397.679, 153.0),
    (397.801, 153.0), (397.974, 153.0), (398.974, 153.0), (399.975, 153.0),
    (400.974, 153.0), (404.522, 154.0), (404.647, 154.0), (404.787, 154.0),
    (404.976, 154.0), (405.976, 154.0), (406.975, 154.0), (407.975, 155.0),
    (411.526, 155.0), (411.651, 155.0), (411.775, 155.0), (411.975, 155.0),
    (413.015, 155.0), (413.975, 155.0), (414.974, 156.0), (418.581, 156.0),
    (418.707, 156.0), (418.841, 156.0), (418.975, 156.0), (419.975, 156.0),
    (420.975, 157.0), (421.989, 157.0),
]  # fmt: skip


def _curve(points: list[tuple[float, float]]) -> list[RoastCurveSample]:
    return [
        RoastCurveSample(
            elapsed_since_charge_seconds=t,
            bean_temp_c=b,
            env_temp_c=200.0,
            heat_percent=65,
            fan_percent=30,
        )
        for t, b in points
    ]


def test_slope_eta_matches_the_study_on_the_winter_trim() -> None:
    """Golden: identical to the study's predictor B at the 7 Nov trim."""
    eta = slope_fc_eta_seconds(_curve(NOV07_TRIM_WINDOW), fc_target_bean_temp_c=176.0)
    assert eta == pytest.approx(127.66084553982904, abs=1e-6)


def test_slope_eta_exact_on_a_linear_ramp_and_window_excludes_old_samples() -> None:
    """A clean 0.2 °C/s ramp projects exactly; samples older than the window,
    even wildly different ones, are ignored."""
    ramp = [(float(t), 150.0 + 0.2 * t) for t in range(41)]  # 150 → 158 °C
    stale = [(-100.0, 20.0)]
    eta = slope_fc_eta_seconds(_curve(stale + ramp), fc_target_bean_temp_c=176.0)
    assert eta == pytest.approx((176.0 - 158.0) / 0.2)


@pytest.mark.parametrize(
    "points",
    [
        [],
        [(0.0, 150.0), (1.0, 151.0)],  # fewer than min_samples in the window
        [(0.0, 160.0), (1.0, 160.0), (2.0, 160.0)],  # flat
        [(0.0, 162.0), (1.0, 161.0), (2.0, 160.0)],  # cooling
        [(0.0, 175.0), (1.0, 176.0), (2.0, 177.0)],  # already past the target
        [(0.0, 150.0), (1.0, math.nan), (2.0, 152.0)],  # non-finite voids it
        [(5.0, 150.0), (5.0, 151.0), (5.0, 152.0)],  # times do not vary
    ],
)
def test_slope_eta_none_cases(points: list[tuple[float, float]]) -> None:
    assert slope_fc_eta_seconds(_curve(points), fc_target_bean_temp_c=176.0) is None


#: Study state at the 7 Nov trim (30 s smoothed BT/ET, replayed heater Q) and
#: its model ETA at 65 % heat in a 19.5 °C room: 146.5666 s (124 s observed).
NOV07_STATE = {"bean_temp_c": 156.6019619028337, "env_temp_c": 208.64129479867225}


def test_two_node_eta_matches_the_study_on_the_winter_trim() -> None:
    eta = two_node_fc_eta_seconds(
        **NOV07_STATE,
        heater_state=1.0,
        hold_heat_percent=65.0,
        ambient_temp_c=19.5,
        fc_target_bean_temp_c=176.0,
        parameters=TwoNodeParameters(),
    )
    assert eta == pytest.approx(146.5665720109136, abs=1e-6)


def test_two_node_eta_responds_in_the_physical_direction() -> None:
    """More heat or a warmer room reaches first crack sooner."""

    def eta(heat: float, room: float) -> float | None:
        return two_node_fc_eta_seconds(
            **NOV07_STATE,
            heater_state=0.8,
            hold_heat_percent=heat,
            ambient_temp_c=room,
            fc_target_bean_temp_c=176.0,
            parameters=TwoNodeParameters(),
        )

    low, high, cold, warm = eta(50.0, 20.0), eta(100.0, 20.0), eta(80.0, 18.0), eta(80.0, 28.0)
    assert low is not None and high is not None and cold is not None and warm is not None
    assert high < low
    assert warm < cold


@pytest.mark.parametrize(
    "override",
    [
        {"bean_temp_c": 176.0},  # already at the target
        {"ambient_temp_c": math.nan},  # missing room reading never guesses
        {"heater_state": math.inf},
        {"horizon_seconds": 10.0},  # target beyond the horizon
    ],
)
def test_two_node_eta_none_cases(override: dict[str, float]) -> None:
    kwargs: dict[str, float] = {
        **NOV07_STATE,
        "heater_state": 1.0,
        "hold_heat_percent": 65.0,
        "ambient_temp_c": 19.5,
        "fc_target_bean_temp_c": 176.0,
        "horizon_seconds": 900.0,
    }
    kwargs.update(override)
    assert two_node_fc_eta_seconds(parameters=TwoNodeParameters(), **kwargs) is None


def test_heater_lag_filter_follows_the_applied_heat_with_its_time_constant() -> None:
    params = TwoNodeParameters(tau_seconds=100.0)
    lag = HeaterLagFilter(params)
    assert lag.value is None
    assert lag.update(elapsed_seconds=0.0, heat_percent=100.0) == 1.0
    lag.update(elapsed_seconds=0.0, heat_percent=0.0)  # heat drops to 0 from t=0
    after = lag.update(elapsed_seconds=100.0, heat_percent=0.0)
    assert after == pytest.approx((1.0 - 1.0 / 100.0) ** 100)
    # A backwards clock and non-finite inputs leave the state alone.
    assert lag.update(elapsed_seconds=50.0, heat_percent=100.0) == after
    assert lag.update(elapsed_seconds=math.nan, heat_percent=100.0) == after
    assert lag.update(elapsed_seconds=200.0, heat_percent=math.nan) == after


def test_heater_lag_filter_clamps_heat_to_the_duty_range() -> None:
    lag = HeaterLagFilter(TwoNodeParameters())
    assert lag.update(elapsed_seconds=0.0, heat_percent=140.0) == 1.0


def test_anchor_lifecycle_provisional_then_frozen_by_eta() -> None:
    tracker = FcAnchorTracker()
    assert (
        tracker.update(charge_elapsed_seconds=400.0, bean_temp_c=150.0, eta_seconds=90.0).state
        is AnchorState.NONE
    )  # below the opening bean temperature
    assert (
        tracker.update(charge_elapsed_seconds=410.0, bean_temp_c=156.0, eta_seconds=150.0).state
        is AnchorState.NONE
    )  # ETA not yet trusted
    first = tracker.update(charge_elapsed_seconds=420.0, bean_temp_c=157.0, eta_seconds=110.0)
    assert (first.state, first.fc_charge_seconds) == (AnchorState.PROVISIONAL, 530.0)
    moved = tracker.update(charge_elapsed_seconds=440.0, bean_temp_c=160.0, eta_seconds=100.0)
    assert (moved.state, moved.fc_charge_seconds) == (AnchorState.PROVISIONAL, 540.0)
    kept = tracker.update(charge_elapsed_seconds=441.0, bean_temp_c=160.0, eta_seconds=None)
    assert kept == moved  # a missing ETA keeps the anchor
    frozen = tracker.update(charge_elapsed_seconds=500.0, bean_temp_c=168.0, eta_seconds=45.0)
    assert (frozen.state, frozen.fc_charge_seconds) == (AnchorState.FROZEN, 545.0)
    after = tracker.update(charge_elapsed_seconds=510.0, bean_temp_c=170.0, eta_seconds=10.0)
    assert after == frozen  # a frozen anchor never moves on an ETA
    assert tracker.anchor == frozen


def test_anchor_freezes_on_bean_temperature_and_ignores_non_finite() -> None:
    tracker = FcAnchorTracker(AnchorSettings(freeze_bean_temp_c=171.0))
    assert (
        tracker.update(charge_elapsed_seconds=math.nan, bean_temp_c=171.0, eta_seconds=60.0).state
        is AnchorState.NONE
    )
    anchor = tracker.update(charge_elapsed_seconds=500.0, bean_temp_c=171.0, eta_seconds=60.0)
    assert (anchor.state, anchor.fc_charge_seconds) == (AnchorState.FROZEN, 560.0)


@pytest.mark.parametrize(
    ("observed", "state", "fc", "delta"),
    [
        (570.0, AnchorState.FROZEN, 545.0, 25.0),  # within 30 s: anchor stays
        (575.0, AnchorState.FROZEN, 545.0, 30.0),  # exactly the threshold stays
        (575.5, AnchorState.REANCHORED, 575.5, 30.5),  # beyond: moves to observed
        (500.0, AnchorState.REANCHORED, 500.0, -45.0),  # early crack moves it too
    ],
)
def test_observed_first_crack_reanchors_only_when_far(
    observed: float, state: AnchorState, fc: float, delta: float
) -> None:
    tracker = FcAnchorTracker()
    tracker.update(charge_elapsed_seconds=500.0, bean_temp_c=168.0, eta_seconds=45.0)
    anchor = tracker.observe_first_crack(charge_elapsed_seconds=observed)
    assert (anchor.state, anchor.fc_charge_seconds) == (state, fc)
    assert anchor.reanchor_delta_seconds == pytest.approx(delta)


def test_only_the_first_observation_counts_and_one_with_no_anchor_becomes_it() -> None:
    tracker = FcAnchorTracker()
    first = tracker.observe_first_crack(charge_elapsed_seconds=560.0)
    assert (first.state, first.fc_charge_seconds) == (AnchorState.REANCHORED, 560.0)
    assert tracker.observe_first_crack(charge_elapsed_seconds=600.0) == first
    assert tracker.update(charge_elapsed_seconds=561.0, bean_temp_c=176.0, eta_seconds=5.0) == first
    blank = FcAnchorTracker()
    assert blank.observe_first_crack(charge_elapsed_seconds=math.inf).state is AnchorState.NONE
    late = blank.observe_first_crack(charge_elapsed_seconds=580.0)
    assert late.state is AnchorState.REANCHORED  # a non-finite one was not "the first"


def test_observing_while_provisional_freezes_a_near_anchor() -> None:
    tracker = FcAnchorTracker()
    tracker.update(charge_elapsed_seconds=420.0, bean_temp_c=157.0, eta_seconds=110.0)
    anchor = tracker.observe_first_crack(charge_elapsed_seconds=540.0)
    assert (anchor.state, anchor.fc_charge_seconds) == (AnchorState.FROZEN, 530.0)


@pytest.mark.parametrize(
    ("fc", "percent", "expected"),
    [
        (570.0, 14.0, 570.0 / 0.86),
        (570.0, 0.0, None),
        (570.0, 100.0, None),
        (0.0, 14.0, None),
        (math.nan, 14.0, None),
        (570.0, math.inf, None),
    ],
)
def test_planned_finish(fc: float, percent: float, expected: float | None) -> None:
    got = planned_finish_charge_seconds(fc, target_development_percent=percent)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def _study_heater_state(times: list[float], heats: list[float], k: int, tau: float) -> float:
    """Transcription of the study's heater_state (zero-order-hold heat)."""

    def heat_at(x: float) -> float:
        i = max(j for j in range(len(times)) if times[j] <= x)
        return heats[i] / 100.0

    q = heats[0] / 100.0
    t = times[0]
    while t < times[k]:
        q += (heat_at(t) - q) / tau
        t += 1.0
    return q


@pytest.mark.parametrize(
    ("times", "heats"),
    [
        ([0.0, 1.0, 1.86, 3.62], [100.0, 0.0, 100.0, 0.0]),  # review case
        (
            [392.974, 393.975, 397.554, 397.679, 397.801, 397.974, 398.974, 404.522],
            [100.0, 100.0, 65.0, 65.0, 80.0, 65.0, 65.0, 40.0],
        ),  # SSE-style bursts inside one grid second
    ],
)
def test_heater_lag_filter_reproduces_the_study_on_irregular_samples(
    times: list[float], heats: list[float]
) -> None:
    lag = HeaterLagFilter(TwoNodeParameters(tau_seconds=10.0))
    for k, (t, h) in enumerate(zip(times, heats, strict=True)):
        got = lag.update(elapsed_seconds=t, heat_percent=h)
        assert got == pytest.approx(_study_heater_state(times, heats, k, 10.0))


def test_heater_lag_filter_clamps_negative_heat_to_zero() -> None:
    lag = HeaterLagFilter(TwoNodeParameters())
    assert lag.update(elapsed_seconds=0.0, heat_percent=-20.0) == 0.0


@pytest.mark.parametrize(
    "override",
    [{"tau_seconds": 0.0}, {"tau_seconds": -1.0}, {"kb": math.nan}, {"g": math.inf}],
)
def test_two_node_parameters_reject_unusable_constants(override: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="finite"):
        TwoNodeParameters(**override)


def test_slope_eta_nan_timestamp_voids_the_estimate() -> None:
    points = [(0.0, 150.0), (math.nan, 151.0), (2.0, 152.0), (3.0, 153.0)]
    assert slope_fc_eta_seconds(_curve(points), fc_target_bean_temp_c=176.0) is None


def test_slope_window_includes_the_sample_exactly_at_its_edge() -> None:
    """The window is inclusive: a sample exactly 30 s old still counts."""
    points = [(0.0, 140.0), (30.0, 160.0), (30.0, 160.0)]
    eta = slope_fc_eta_seconds(_curve(points), fc_target_bean_temp_c=176.0)
    assert eta is not None  # without the edge sample the times would not vary


def test_anchor_freezes_on_temperature_without_an_eta() -> None:
    """A cold-room stall at/above the 175 °C default freeze (no usable ETA) still freezes."""
    tracker = FcAnchorTracker()
    tracker.update(charge_elapsed_seconds=420.0, bean_temp_c=157.0, eta_seconds=110.0)
    stalled = tracker.update(charge_elapsed_seconds=600.0, bean_temp_c=176.0, eta_seconds=None)
    assert (stalled.state, stalled.fc_charge_seconds) == (AnchorState.FROZEN, 530.0)


def test_anchor_ignores_a_negative_eta_and_opens_at_its_exact_bounds() -> None:
    tracker = FcAnchorTracker()
    assert (
        tracker.update(charge_elapsed_seconds=420.0, bean_temp_c=160.0, eta_seconds=-5.0).state
        is AnchorState.NONE
    )
    opened = tracker.update(charge_elapsed_seconds=420.0, bean_temp_c=155.0, eta_seconds=120.0)
    assert (opened.state, opened.fc_charge_seconds) == (AnchorState.PROVISIONAL, 540.0)


def test_smoothed_bean_env_uses_the_line_value_at_the_newest_sample() -> None:
    from roastpilot_agent.fc_prediction import smoothed_bean_env_c

    curve = [
        RoastCurveSample(
            elapsed_since_charge_seconds=float(t),
            bean_temp_c=150.0 + t,
            env_temp_c=200.0 + 2 * t,
            heat_percent=65,
            fan_percent=30,
        )
        for t in range(5)
    ]
    smoothed = smoothed_bean_env_c(curve)
    assert smoothed is not None
    assert smoothed == (pytest.approx(154.0), pytest.approx(208.0))


@pytest.mark.parametrize(
    "points",
    [
        [],
        [(0.0, 150.0), (1.0, 151.0)],  # too few samples
        [(0.0, 150.0), (1.0, math.nan), (2.0, 152.0)],  # non-finite
        [(5.0, 150.0), (5.0, 151.0), (5.0, 152.0)],  # times do not vary
    ],
)
def test_smoothed_bean_env_none_cases(points: list[tuple[float, float]]) -> None:
    from roastpilot_agent.fc_prediction import smoothed_bean_env_c

    assert smoothed_bean_env_c(_curve(points)) is None
