"""Predictive finish-tracking law for the anchored finish (D239 slice 3b, #1131).

Pure decision logic plus one bounded in-roast correction; no clock reads, no
I/O, and nothing here reaches an MCP write path. The controller (slice 3c)
evaluates every heat this module proposes through ``SafetyPolicy``.

Each control cadence the tracker:

1. aims bean temperature at ``target_bean_c`` at the planned finish
   T = F / (1 - p), or, once T is near or past, within ``late_horizon_seconds``
   (owner, 23 Nov: a late finish keeps raising heat);
2. simulates a small set of candidate heats (current, and current plus or
   minus up to ``candidate_steps`` step limits, within floor and ceiling)
   forward with the two-node model through the thermal lag, and picks the one
   whose corrected prediction lands nearest the target, preferring the
   smaller change on a tie;
3. corrects the model from this roast: a prediction made one correction
   window ago is compared with what the bean actually did, and a bounded rate
   bias (°C/min) is learned by exponential smoothing;
4. falls back to rate-of-rise feedback when the room reading is missing, the
   model has no usable state, the bias reaches its bound, or the last
   prediction missed by more than ``max_prediction_error_c``.

Temperatures are Celsius; heat is 0-100 percent duty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from roastpilot_agent.config import FinishControlConfig
from roastpilot_agent.fc_prediction import TwoNodeParameters, two_node_bean_after_seconds


class FinishMode(Enum):
    """Which law produced a decision."""

    PREDICTIVE = "predictive"
    FEEDBACK = "feedback"
    HOLD = "hold"


@dataclass(frozen=True)
class FinishSettings:
    """Tuning for the finish law. Every value is a D239 starting hypothesis.

    Attributes:
        heat_floor_percent: Lowest heat the law commands.
        heat_ceiling_percent: Highest heat the law commands.
        step_percent: Largest heat change per decision.
        candidate_steps: Candidates span current ± this many steps.
        late_horizon_seconds: With T near or past, aim to reach the target
            this far ahead.
        correction_window_seconds: Age of the prediction compared with
            reality for the in-roast correction.
        correction_alpha: Smoothing weight of each new correction sample.
        max_rate_bias_c_per_min: Bound on the learned rate bias; reaching it
            forces the feedback fallback.
        max_prediction_error_c: A larger miss in the last comparison forces
            the feedback fallback.
        feedback_gain: Feedback heat change per °C/min of rate shortfall.
        lag_horizon_seconds: Feedback look-ahead for the thermal lag.
    """

    heat_floor_percent: int = 25
    heat_ceiling_percent: int = 100
    step_percent: int = 5
    candidate_steps: int = 2
    late_horizon_seconds: float = 60.0
    correction_window_seconds: float = 60.0
    correction_alpha: float = 0.3
    max_rate_bias_c_per_min: float = 3.0
    max_prediction_error_c: float = 5.0
    feedback_gain: float = 2.0
    lag_horizon_seconds: float = 30.0


def settings_from_config(config: FinishControlConfig) -> FinishSettings:
    """The law's settings from the controller config section.

    Args:
        config: ``ControllerConfig.finish_control``.

    Returns:
        The matching :class:`FinishSettings`.
    """
    return FinishSettings(
        heat_floor_percent=config.heat_floor_percent,
        heat_ceiling_percent=config.heat_ceiling_percent,
        step_percent=config.step_percent,
        candidate_steps=config.candidate_steps,
        late_horizon_seconds=config.late_horizon_seconds,
        correction_window_seconds=config.correction_window_seconds,
        correction_alpha=config.correction_alpha,
        max_rate_bias_c_per_min=config.max_rate_bias_c_per_min,
        max_prediction_error_c=config.max_prediction_error_c,
        feedback_gain=config.feedback_gain,
        lag_horizon_seconds=config.lag_horizon_seconds,
    )


@dataclass(frozen=True)
class FinishInputs:
    """One cadence's evidence for the law.

    Attributes:
        charge_seconds: Seconds since charge now.
        bean_temp_c: Smoothed current bean temperature.
        env_temp_c: Smoothed current environment temperature.
        bean_ror_c_per_min: Current bean rate of rise, or ``None``.
        heater_state: Replayed heater state Q (0-1), or ``None``.
        ambient_temp_c: Fresh room reading, or ``None``.
        current_heat_percent: The heat actually applied now.
        finish_charge_seconds: The planned finish T on the charge clock.
        target_bean_c: The drop target bean temperature.
    """

    charge_seconds: float
    bean_temp_c: float
    env_temp_c: float
    bean_ror_c_per_min: float | None
    heater_state: float | None
    ambient_temp_c: float | None
    current_heat_percent: int
    finish_charge_seconds: float
    target_bean_c: float


@dataclass(frozen=True)
class FinishDecision:
    """The law's proposal for this cadence.

    Attributes:
        heat_percent: The heat to request (bounded and step-limited).
        mode: Which law produced it.
        aim_charge_seconds: The time the law aimed the target at.
        predicted_bean_c: The corrected model prediction at the aim time for
            the chosen heat, or ``None`` outside predictive mode.
        reason: A short code for the mode choice.
    """

    heat_percent: int
    mode: FinishMode
    aim_charge_seconds: float
    predicted_bean_c: float | None
    reason: str


@dataclass
class _Pending:
    due_charge_seconds: float
    predicted_bean_c: float


@dataclass
class FinishTracker:
    """The predictive finish law with its in-roast correction (one per run)."""

    settings: FinishSettings = field(default_factory=FinishSettings)
    parameters: TwoNodeParameters = field(default_factory=TwoNodeParameters)
    rate_bias_c_per_min: float = 0.0
    last_error_c: float | None = None
    _pending: list[_Pending] = field(default_factory=lambda: list[_Pending]())

    def observe(self, *, charge_seconds: float, bean_temp_c: float) -> None:
        """Compare matured predictions with the bean and update the bias.

        Args:
            charge_seconds: Seconds since charge now.
            bean_temp_c: The bean temperature now.
        """
        if not (math.isfinite(charge_seconds) and math.isfinite(bean_temp_c)):
            return
        s = self.settings
        due = [p for p in self._pending if p.due_charge_seconds <= charge_seconds]
        self._pending = [p for p in self._pending if p.due_charge_seconds > charge_seconds]
        for pending in due:
            error = bean_temp_c - pending.predicted_bean_c
            self.last_error_c = error
            sample = error * 60.0 / s.correction_window_seconds
            blended = (1.0 - s.correction_alpha) * self.rate_bias_c_per_min + (
                s.correction_alpha * sample
            )
            bound = s.max_rate_bias_c_per_min
            self.rate_bias_c_per_min = min(max(blended, -bound), bound)

    def decide(self, inputs: FinishInputs) -> FinishDecision:
        """Propose this cadence's heat.

        Args:
            inputs: This cadence's evidence.

        Returns:
            A bounded, step-limited proposal labelled with its mode.
        """
        s = self.settings
        now = inputs.charge_seconds
        current = min(
            max(inputs.current_heat_percent, s.heat_floor_percent), s.heat_ceiling_percent
        )
        aim = max(inputs.finish_charge_seconds, now + s.late_horizon_seconds)
        horizon = aim - now
        reason = self._fallback_reason(inputs)
        if reason is None:
            return self._predictive(inputs, current, aim, horizon)
        return self._feedback(inputs, current, aim, horizon, reason)

    def _fallback_reason(self, inputs: FinishInputs) -> str | None:
        s = self.settings
        if inputs.ambient_temp_c is None or not math.isfinite(inputs.ambient_temp_c):
            return "no_room_reading"
        if inputs.heater_state is None or not math.isfinite(inputs.heater_state):
            return "no_heater_state"
        if abs(self.rate_bias_c_per_min) >= s.max_rate_bias_c_per_min:
            return "bias_at_bound"
        if self.last_error_c is not None and abs(self.last_error_c) > s.max_prediction_error_c:
            return "prediction_error"
        return None

    def _candidates(self, current: int) -> list[int]:
        s = self.settings
        heats = {
            min(max(current + k * s.step_percent, s.heat_floor_percent), s.heat_ceiling_percent)
            for k in range(-s.candidate_steps, s.candidate_steps + 1)
        }
        return sorted(heats, key=lambda h: (abs(h - current), h))

    def _predictive(
        self, inputs: FinishInputs, current: int, aim: float, horizon: float
    ) -> FinishDecision:
        assert inputs.ambient_temp_c is not None and inputs.heater_state is not None
        best: tuple[float, int, float] | None = None
        for heat in self._candidates(current):
            raw = two_node_bean_after_seconds(
                bean_temp_c=inputs.bean_temp_c,
                env_temp_c=inputs.env_temp_c,
                heater_state=inputs.heater_state,
                hold_heat_percent=float(heat),
                ambient_temp_c=inputs.ambient_temp_c,
                seconds=horizon,
                parameters=self.parameters,
            )
            if raw is None:
                continue
            corrected = raw + self.rate_bias_c_per_min * horizon / 60.0
            miss = abs(corrected - inputs.target_bean_c)
            if best is None or miss < best[0] - 1e-9:
                best = (miss, heat, corrected)
        if best is None:
            return self._feedback(inputs, current, aim, horizon, "model_unavailable")
        _, heat, predicted = best
        self._remember(inputs, heat)
        return FinishDecision(heat, FinishMode.PREDICTIVE, aim, predicted, "predictive")

    def _remember(self, inputs: FinishInputs, heat: int) -> None:
        """Store this decision's short-horizon prediction for the correction."""
        assert inputs.ambient_temp_c is not None and inputs.heater_state is not None
        window = self.settings.correction_window_seconds
        ahead = two_node_bean_after_seconds(
            bean_temp_c=inputs.bean_temp_c,
            env_temp_c=inputs.env_temp_c,
            heater_state=inputs.heater_state,
            hold_heat_percent=float(heat),
            ambient_temp_c=inputs.ambient_temp_c,
            seconds=window,
            parameters=self.parameters,
        )
        if ahead is not None:  # pragma: no branch - inputs already projected successfully
            self._pending.append(_Pending(inputs.charge_seconds + window, ahead))

    def _feedback(
        self, inputs: FinishInputs, current: int, aim: float, horizon: float, reason: str
    ) -> FinishDecision:
        s = self.settings
        ror = inputs.bean_ror_c_per_min
        if ror is None or not math.isfinite(ror):
            return FinishDecision(current, FinishMode.HOLD, aim, None, f"{reason}+no_ror")
        projected = inputs.bean_temp_c + ror * s.lag_horizon_seconds / 60.0
        remaining = max(horizon - s.lag_horizon_seconds, s.lag_horizon_seconds)
        required = (inputs.target_bean_c - projected) * 60.0 / remaining
        change = s.feedback_gain * (required - ror)
        step = min(max(change, -s.step_percent), s.step_percent)
        heat = round(min(max(current + step, s.heat_floor_percent), s.heat_ceiling_percent))
        return FinishDecision(heat, FinishMode.FEEDBACK, aim, None, reason)
