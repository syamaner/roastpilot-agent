"""Predictive finish-tracking law for the anchored finish (D239 slice 3b, #1131).

Pure decision logic plus one bounded in-roast correction; no clock reads, no
I/O, and nothing here reaches an MCP write path. The controller (slice 3c)
evaluates every heat this module proposes through ``SafetyPolicy``.

Each control cadence the tracker:

1. aims bean temperature at ``target_bean_c`` at the planned finish
   T = F / (1 - p), or, once T is near or past, within ``late_horizon_seconds``
   (owner, 23 Nov: a late finish keeps raising heat);
2. simulates a small set of candidate heats, all within one ``step_percent``
   of the heat actually applied (``candidate_steps`` subdivisions of that
   step either way, D239: at most one step per cadence) and within floor and
   ceiling, forward with the two-node model through the thermal lag, and
   picks the one whose corrected prediction lands nearest the target,
   preferring the smaller change on a tie;
3. corrects the model from this roast: a prediction made one correction
   window ago, at a heat held unchanged since, is compared with the bean
   within 1.5 s of falling due, and a bounded rate bias (°C/min) is learned by
   exponential smoothing; predictions are also made in feedback mode, so a
   fallback can recover;
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
        candidate_steps: Subdivisions of one step either side of the applied
            heat that the predictive search considers.
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


#: Longest the correction may apply its learned rate bias over (seconds): a
#: bounded bias cannot shift a long projection by tens of degrees.
_BIAS_HORIZON_CAP_SECONDS = 300.0
#: A matured prediction compared more than this late is discarded, so the
#: correction never trains on a stale, systematically skewed sample.
_MAX_MATURITY_LATENESS_SECONDS = 1.5
#: Bound on stored predictions (a 60 s window at a 5 s cadence needs 13).
_MAX_PENDING = 64


@dataclass
class _Pending:
    due_charge_seconds: float
    predicted_bean_c: float
    heat_percent: int


@dataclass
class FinishTracker:
    """The predictive finish law with its in-roast correction (one per run).

    Never persisted or resumed: the controller creates a fresh tracker per run.
    """

    settings: FinishSettings = field(default_factory=FinishSettings)
    parameters: TwoNodeParameters = field(default_factory=TwoNodeParameters)
    rate_bias_c_per_min: float = 0.0
    last_error_c: float | None = None
    _pending: list[_Pending] = field(default_factory=lambda: list[_Pending]())
    _last_decide_at: float | None = None

    def observe(self, *, charge_seconds: float, bean_temp_c: float) -> None:
        """Compare matured predictions with the bean and update the bias.

        Only a prediction compared within ``_MAX_MATURITY_LATENESS_SECONDS`` of
        its due time trains the correction; a later one is discarded.

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
            if charge_seconds - pending.due_charge_seconds > _MAX_MATURITY_LATENESS_SECONDS:
                continue
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

        The proposal is always within one ``step_percent`` of the heat actually
        applied and, where that is possible within one step, inside
        [floor, ceiling]. Invalid input holds the applied heat.

        Args:
            inputs: This cadence's evidence.

        Returns:
            A bounded, step-limited proposal labelled with its mode.
        """
        s = self.settings
        applied = inputs.current_heat_percent
        now = inputs.charge_seconds
        checked = (
            now,
            inputs.bean_temp_c,
            inputs.env_temp_c,
            inputs.finish_charge_seconds,
            inputs.target_bean_c,
        )
        if not all(math.isfinite(x) for x in checked):
            return FinishDecision(applied, FinishMode.HOLD, now, None, "invalid_input")
        # Confounding guard: a stored prediction assumed its heat held for the
        # whole window, so any heat change since invalidates it.
        self._pending = [p for p in self._pending if p.heat_percent == applied][-_MAX_PENDING:]
        fresh = self._last_decide_at is None or now > self._last_decide_at
        self._last_decide_at = now if fresh else self._last_decide_at
        aim = max(inputs.finish_charge_seconds, now + s.late_horizon_seconds)
        horizon = aim - now
        reason = self._fallback_reason(inputs)
        if reason is None:
            decision = self._predictive(inputs, applied, aim, horizon)
        else:
            decision = self._feedback(inputs, applied, aim, horizon, reason)
        if fresh and decision.mode is not FinishMode.HOLD and self._model_ready(inputs):
            self._remember(inputs, decision.heat_percent)
        return decision

    def _model_ready(self, inputs: FinishInputs) -> bool:
        room, heater = inputs.ambient_temp_c, inputs.heater_state
        return (
            room is not None
            and math.isfinite(room)
            and heater is not None
            and math.isfinite(heater)
            and 0.0 <= heater <= 1.0
        )

    def _fallback_reason(self, inputs: FinishInputs) -> str | None:
        s = self.settings
        if inputs.ambient_temp_c is None or not math.isfinite(inputs.ambient_temp_c):
            return "no_room_reading"
        heater = inputs.heater_state
        if heater is None or not math.isfinite(heater) or not 0.0 <= heater <= 1.0:
            return "no_heater_state"
        if abs(self.rate_bias_c_per_min) >= s.max_rate_bias_c_per_min:
            return "bias_at_bound"
        if self.last_error_c is not None and abs(self.last_error_c) > s.max_prediction_error_c:
            return "prediction_error"
        return None

    def _reachable(self, applied: int) -> tuple[int, int]:
        """Heats reachable this cadence: one step of ``applied``, in range."""
        s = self.settings
        lo = max(s.heat_floor_percent, applied - s.step_percent)
        hi = min(s.heat_ceiling_percent, applied + s.step_percent)
        if lo > hi:  # applied lies more than one step outside the range
            toward = (
                applied + s.step_percent
                if applied < s.heat_floor_percent
                else applied - s.step_percent
            )
            return toward, toward
        return lo, hi

    def _candidates(self, applied: int) -> list[int]:
        s = self.settings
        lo, hi = self._reachable(applied)
        n = s.candidate_steps
        heats = {
            min(max(round(applied + s.step_percent * k / n), lo), hi) for k in range(-n, n + 1)
        }
        return sorted(heats, key=lambda h: (abs(h - applied), h))

    def _predictive(
        self, inputs: FinishInputs, applied: int, aim: float, horizon: float
    ) -> FinishDecision:
        assert inputs.ambient_temp_c is not None and inputs.heater_state is not None
        bias_shift = self.rate_bias_c_per_min * min(horizon, _BIAS_HORIZON_CAP_SECONDS) / 60.0
        best: tuple[float, int, float] | None = None
        for heat in self._candidates(applied):
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
            corrected = raw + bias_shift
            miss = abs(corrected - inputs.target_bean_c)
            if best is None or miss < best[0] - 1e-9:
                best = (miss, heat, corrected)
        if best is None:
            return self._feedback(inputs, applied, aim, horizon, "model_unavailable")
        _, heat, predicted = best
        return FinishDecision(heat, FinishMode.PREDICTIVE, aim, predicted, "predictive")

    def _remember(self, inputs: FinishInputs, heat: int) -> None:
        """Store this decision's short-horizon prediction for the correction.

        Also stored in feedback mode, so a fallback can recover once the model
        tracks again. One prediction per decision time; the list is bounded.
        """
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
        if ahead is not None:  # pragma: no branch - inputs validated by the caller
            self._pending.append(_Pending(inputs.charge_seconds + window, ahead, heat))
            self._pending = self._pending[-_MAX_PENDING:]

    def _feedback(
        self, inputs: FinishInputs, applied: int, aim: float, horizon: float, reason: str
    ) -> FinishDecision:
        s = self.settings
        ror = inputs.bean_ror_c_per_min
        if ror is None or not math.isfinite(ror):
            return FinishDecision(applied, FinishMode.HOLD, aim, None, f"{reason}+no_ror")
        projected = inputs.bean_temp_c + ror * s.lag_horizon_seconds / 60.0
        remaining = max(horizon - s.lag_horizon_seconds, s.lag_horizon_seconds)
        required = (inputs.target_bean_c - projected) * 60.0 / remaining
        lo, hi = self._reachable(applied)
        heat = round(min(max(applied + s.feedback_gain * (required - ror), lo), hi))
        return FinishDecision(heat, FinishMode.FEEDBACK, aim, None, reason)
