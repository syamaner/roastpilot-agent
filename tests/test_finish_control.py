"""Tests for the D239 predictive finish law (#1131, slice 3b)."""

from __future__ import annotations

import dataclasses
import math

import pytest

from roastpilot_agent.config import (
    ControllerConfig,
    FcAnchorConfig,
    FinishControlConfig,
    PostFirstCrackControl,
)
from roastpilot_agent.fc_prediction import (
    TwoNodeParameters,
    two_node_bean_after_seconds,
    two_node_fc_eta_seconds,
)
from roastpilot_agent.finish_control import (
    FinishInputs,
    FinishMode,
    FinishSettings,
    FinishTracker,
    settings_from_config,
)

STATE = {"bean_temp_c": 157.0, "env_temp_c": 208.0, "heater_state": 1.0}


def _inputs(**override: object) -> FinishInputs:
    base: dict[str, object] = {
        "charge_seconds": 600.0,
        "bean_temp_c": 180.0,
        "env_temp_c": 215.0,
        "bean_ror_c_per_min": 8.0,
        "heater_state": 0.7,
        "ambient_temp_c": 19.5,
        "current_heat_percent": 70,
        "finish_charge_seconds": 700.0,
        "target_bean_c": 195.0,
    }
    base.update(override)
    return FinishInputs(**base)  # type: ignore[arg-type]


def _bean_after(heat: float, seconds: float, inputs: FinishInputs) -> float:
    assert inputs.heater_state is not None and inputs.ambient_temp_c is not None
    value = two_node_bean_after_seconds(
        bean_temp_c=inputs.bean_temp_c,
        env_temp_c=inputs.env_temp_c,
        heater_state=inputs.heater_state,
        hold_heat_percent=heat,
        ambient_temp_c=inputs.ambient_temp_c,
        seconds=seconds,
        parameters=TwoNodeParameters(),
    )
    assert value is not None
    return value


# --- two_node_bean_after_seconds -----------------------------------------------


def test_bean_after_seconds_agrees_with_the_eta_model() -> None:
    eta = two_node_fc_eta_seconds(
        **STATE,
        hold_heat_percent=65.0,
        ambient_temp_c=19.5,
        fc_target_bean_temp_c=176.0,
        parameters=TwoNodeParameters(),
    )
    assert eta is not None
    bean = two_node_bean_after_seconds(
        **STATE,
        hold_heat_percent=65.0,
        ambient_temp_c=19.5,
        seconds=eta,
        parameters=TwoNodeParameters(),
    )
    assert bean == pytest.approx(176.0, abs=0.05)


def test_bean_after_seconds_interpolates_and_rejects_bad_input() -> None:
    kwargs = {**STATE, "hold_heat_percent": 65.0, "ambient_temp_c": 19.5}
    p = TwoNodeParameters()
    zero = two_node_bean_after_seconds(**kwargs, seconds=0.0, parameters=p)
    one = two_node_bean_after_seconds(**kwargs, seconds=1.0, parameters=p)
    half = two_node_bean_after_seconds(**kwargs, seconds=0.5, parameters=p)
    assert zero == pytest.approx(157.0)
    assert one is not None and half is not None
    assert half == pytest.approx((157.0 + one) / 2.0)
    assert two_node_bean_after_seconds(**kwargs, seconds=-1.0, parameters=p) is None
    assert two_node_bean_after_seconds(**kwargs, seconds=1801.0, parameters=p) is None
    bad = {**kwargs, "ambient_temp_c": math.nan}
    assert two_node_bean_after_seconds(**bad, seconds=10.0, parameters=p) is None


# --- predictive choice -----------------------------------------------------------


def test_predictive_choice_lands_nearest_the_target_at_the_finish() -> None:
    inputs = _inputs()
    decision = FinishTracker().decide(inputs)
    assert decision.mode is FinishMode.PREDICTIVE
    assert decision.aim_charge_seconds == 700.0
    candidates = [60, 65, 70, 75, 80]
    misses = {h: abs(_bean_after(h, 100.0, inputs) - 195.0) for h in candidates}
    assert decision.heat_percent == min(candidates, key=lambda h: (misses[h], abs(h - 70)))
    assert decision.predicted_bean_c == pytest.approx(
        _bean_after(decision.heat_percent, 100.0, inputs)
    )


def test_running_hot_cuts_and_running_cold_raises_within_the_step_band() -> None:
    hot = FinishTracker().decide(_inputs(target_bean_c=185.0))
    cold = FinishTracker().decide(_inputs(target_bean_c=210.0))
    assert hot.heat_percent == 60  # two steps down, the band's edge
    assert cold.heat_percent == 80  # two steps up
    assert hot.mode is FinishMode.PREDICTIVE and cold.mode is FinishMode.PREDICTIVE


def test_candidates_respect_floor_and_ceiling() -> None:
    near_ceiling = FinishTracker().decide(_inputs(current_heat_percent=98, target_bean_c=230.0))
    near_floor = FinishTracker().decide(_inputs(current_heat_percent=27, target_bean_c=150.0))
    assert near_ceiling.heat_percent == 100
    assert near_floor.heat_percent == 25
    clamped = FinishTracker().decide(_inputs(current_heat_percent=10, target_bean_c=150.0))
    assert clamped.heat_percent == 25  # an out-of-range current heat is clamped first


def test_late_finish_aims_ahead_and_keeps_raising_heat() -> None:
    """Owner, 23 Nov: past the planned finish with the bean short, heat rises."""
    decision = FinishTracker().decide(
        _inputs(charge_seconds=720.0, finish_charge_seconds=700.0, target_bean_c=200.0)
    )
    assert decision.aim_charge_seconds == 780.0  # now + 60 s
    assert decision.heat_percent > 70


def test_tie_prefers_the_smaller_change() -> None:
    tracker = FinishTracker(parameters=dataclasses.replace(TwoNodeParameters(), g=0.0))
    decision = tracker.decide(_inputs())  # heat has no effect: every candidate ties
    assert decision.heat_percent == 70


# --- in-roast correction -----------------------------------------------------------


def test_correction_learns_a_bounded_rate_bias_from_matured_predictions() -> None:
    tracker = FinishTracker()
    tracker.decide(_inputs(charge_seconds=600.0))
    predicted = tracker._pending[0].predicted_bean_c  # pyright: ignore[reportPrivateUsage]
    tracker.observe(charge_seconds=630.0, bean_temp_c=predicted + 10.0)
    assert tracker.rate_bias_c_per_min == 0.0  # not yet due (60 s window)
    tracker.observe(charge_seconds=660.0, bean_temp_c=predicted + 2.0)
    assert tracker.last_error_c == pytest.approx(2.0)
    assert tracker.rate_bias_c_per_min == pytest.approx(0.3 * 2.0)  # 2 °C over 60 s
    for t in range(10):
        tracker.decide(_inputs(charge_seconds=700.0 + t))
    tracker.observe(charge_seconds=800.0, bean_temp_c=300.0)  # every prediction far too low
    assert tracker.rate_bias_c_per_min == pytest.approx(3.0)  # bounded


def test_bias_shifts_the_predictive_choice() -> None:
    """With the target at the 70 % prediction the plain law holds 70 %; a
    learned positive bias (the bean runs faster than the model) cuts heat."""
    target = _bean_after(70.0, 100.0, _inputs())
    plain = FinishTracker().decide(_inputs(target_bean_c=target))
    assert plain.heat_percent == 70
    biased_tracker = FinishTracker()
    biased_tracker.rate_bias_c_per_min = 2.0
    biased = biased_tracker.decide(_inputs(target_bean_c=target))
    assert biased.heat_percent < 70


def test_observe_ignores_non_finite_input() -> None:
    tracker = FinishTracker()
    tracker.decide(_inputs())
    tracker.observe(charge_seconds=math.nan, bean_temp_c=190.0)
    tracker.observe(charge_seconds=700.0, bean_temp_c=math.inf)
    assert tracker.last_error_c is None and tracker.rate_bias_c_per_min == 0.0


# --- fallback ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "override", "reason"),
    [
        (None, {"ambient_temp_c": None}, "no_room_reading"),
        (None, {"ambient_temp_c": math.nan}, "no_room_reading"),
        (None, {"heater_state": None}, "no_heater_state"),
        ("bias", {}, "bias_at_bound"),
        ("error", {}, "prediction_error"),
        ("model", {}, "model_unavailable"),
    ],
)
def test_falls_back_to_feedback_with_a_logged_reason(
    setup: str | None, override: dict[str, object], reason: str
) -> None:
    tracker = FinishTracker()
    if setup == "bias":
        tracker.rate_bias_c_per_min = 3.0
    elif setup == "error":
        tracker.last_error_c = -6.0
    elif setup == "model":
        override = {"finish_charge_seconds": 5000.0}  # beyond the model's horizon
    decision = tracker.decide(_inputs(**override))
    assert decision.mode is FinishMode.FEEDBACK
    assert decision.reason == reason
    assert decision.predicted_bean_c is None


def test_feedback_steps_toward_the_required_rate_within_one_step() -> None:
    """r* = (195 - (180 + 8*0.5)) * 60 / (100 - 30) = 9.43 °C/min > 8 → raise."""
    tracker = FinishTracker()
    raise_ = tracker.decide(_inputs(ambient_temp_c=None))
    assert raise_.heat_percent == 70 + 3  # 2.0 * (9.43 - 8) = 2.86, rounded
    cut = tracker.decide(_inputs(ambient_temp_c=None, bean_ror_c_per_min=20.0))
    assert cut.heat_percent == 65  # a large excess is limited to one step
    assert raise_.aim_charge_seconds == 700.0


def test_feedback_without_ror_holds_the_current_heat() -> None:
    decision = FinishTracker().decide(_inputs(ambient_temp_c=None, bean_ror_c_per_min=None))
    assert (decision.mode, decision.heat_percent) == (FinishMode.HOLD, 70)
    assert decision.reason == "no_room_reading+no_ror"


# --- configuration ----------------------------------------------------------------------


def test_settings_follow_the_config() -> None:
    cfg = FinishControlConfig(step_percent=7, candidate_steps=3, heat_floor_percent=30)
    settings = settings_from_config(cfg)
    assert (settings.step_percent, settings.candidate_steps, settings.heat_floor_percent) == (
        7,
        3,
        30,
    )
    assert settings_from_config(FinishControlConfig()) == FinishSettings()


def test_config_refuses_an_empty_heat_range_and_missing_prerequisites() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        FinishControlConfig(heat_floor_percent=80, heat_ceiling_percent=60)
    on = FinishControlConfig(enabled=True)
    assert ControllerConfig(finish_control=on).finish_control.enabled
    with pytest.raises(ValueError, match="requires fc_anchor.enabled"):
        ControllerConfig(finish_control=on, fc_anchor=FcAnchorConfig(enabled=False))
    with pytest.raises(ValueError, match="ceiling_guard_drop_enabled"):
        ControllerConfig(
            finish_control=on,
            post_first_crack_control=PostFirstCrackControl(ceiling_guard_drop_enabled=False),
        )
