"""First-crack time prediction and the anchored-finish anchor (D239, #1118).

Pure functions and one small state machine; no clock reads, no I/O, and
nothing here can reach an MCP write path. Temperatures are Celsius, times are
seconds on the charge-referenced clock, heat is 0-100 percent duty.

D239 anchors control on the predicted first crack. The owner decision after the
offline study (``docs/research/fc-predictor``) is:

- :func:`slope_fc_eta_seconds` (a least-squares line over the last 30 s) is the
  anchor's estimator;
- :func:`two_node_fc_eta_seconds` (a fitted bean/environment model with the
  room temperature as an input) runs in shadow only;
- :class:`FcAnchorTracker` turns per-tick estimates into a provisional anchor,
  freezes it, and re-anchors once to an observed first crack that lands far
  from it.

This module is observe-only in slice 2: it decides nothing about heat, fan or
drop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from roastpilot_agent.roast_history import RoastCurveSample

#: The anchor estimator's window (D239; leave-one-out median error 14 s at
#: 120 s before 176 °C over the 15 summer roasts, 125-131 s against 124 s
#: observed on the 7 Nov winter trace).
DEFAULT_SLOPE_WINDOW_SECONDS = 30.0

#: The shadow model's prediction horizon. Beyond it the model reports no ETA.
DEFAULT_MODEL_HORIZON_SECONDS = 900.0


def _least_squares_line(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float] | None:
    """Fit ``y = slope * (t - t_last) + value`` by least squares.

    Args:
        points: ``(t, y)`` pairs, most recent last.

    Returns:
        ``(slope, value_at_t_last)``, or ``None`` when the times do not vary.
    """
    t_last = points[-1][0]
    xs = [t - t_last for t, _ in points]
    ys = [y for _, y in points]
    n = float(len(points))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0.0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope, mean_y - slope * mean_x


def slope_fc_eta_seconds(
    curve: Sequence[RoastCurveSample],
    *,
    fc_target_bean_temp_c: float,
    window_seconds: float = DEFAULT_SLOPE_WINDOW_SECONDS,
    min_samples: int = 3,
) -> float | None:
    """Seconds until the bean reaches ``fc_target_bean_temp_c`` (30 s line).

    Fits a least-squares line to the bean samples in the last
    ``window_seconds`` and projects it from the line's value at the newest
    sample, exactly as the offline study's predictor B. A non-finite sample
    anywhere in the window voids the estimate rather than being skipped.

    Args:
        curve: The roast-so-far curve (most recent last).
        fc_target_bean_temp_c: The bean temperature to project to.
        window_seconds: The fit window, in seconds.
        min_samples: The minimum samples inside the window.

    Returns:
        The projected seconds, or ``None`` when there are too few samples, a
        sample is non-finite, the bean is not warming, or the line is already
        at or past the target.
    """
    if not curve:
        return None
    if not all(math.isfinite(s.elapsed_since_charge_seconds) for s in curve):
        return None
    t_last = curve[-1].elapsed_since_charge_seconds
    points = [
        (s.elapsed_since_charge_seconds, s.bean_temp_c)
        for s in curve
        if s.elapsed_since_charge_seconds >= t_last - window_seconds
    ]
    if len(points) < min_samples:
        return None
    if not all(math.isfinite(t) and math.isfinite(b) for t, b in points):
        return None
    line = _least_squares_line(points)
    if line is None:
        return None
    slope, value = line
    if slope <= 0.0:
        return None
    remaining = fc_target_bean_temp_c - value
    if remaining <= 0.0:
        return None
    return remaining / slope


def smoothed_bean_env_c(
    curve: Sequence[RoastCurveSample],
    *,
    window_seconds: float = DEFAULT_SLOPE_WINDOW_SECONDS,
    min_samples: int = 3,
) -> tuple[float, float] | None:
    """The 30 s least-squares values of bean and environment at the newest sample.

    The shadow model starts from this smoothed state, as the study's
    ``state_at`` does, rather than from one integer-quantised reading.

    Args:
        curve: The roast-so-far curve (most recent last).
        window_seconds: The fit window, in seconds.
        min_samples: The minimum samples inside the window.

    Returns:
        ``(bean_c, env_c)``, or ``None`` with too few samples, a non-finite
        sample, or times that do not vary.
    """
    if not curve:
        return None
    if not all(
        math.isfinite(s.elapsed_since_charge_seconds)
        and math.isfinite(s.bean_temp_c)
        and math.isfinite(s.env_temp_c)
        for s in curve
    ):
        return None
    t_last = curve[-1].elapsed_since_charge_seconds
    window = [s for s in curve if s.elapsed_since_charge_seconds >= t_last - window_seconds]
    if len(window) < min_samples:
        return None
    bean = _least_squares_line([(s.elapsed_since_charge_seconds, s.bean_temp_c) for s in window])
    env = _least_squares_line([(s.elapsed_since_charge_seconds, s.env_temp_c) for s in window])
    if bean is None or env is None:
        return None
    return bean[1], env[1]


@dataclass(frozen=True)
class TwoNodeParameters:
    """Fitted constants of the shadow bean/environment model (per second).

    ``dB/dt = kb (E - B) - cb``, ``dE/dt = g Q - ka (E - Ta) - ke (E - B)``,
    ``dQ/dt = (h - Q) / tau``, with B the bean, E the environment (drum air),
    Q the lagged heater output and h the applied heat fraction. Defaults are
    the all-15 fit in ``docs/research/fc-predictor/results/fitted_params.json``.
    ``cb`` came out negative (a non-physical bias term) and the constants trade
    off across folds, which is why this model is shadow-only.

    Attributes:
        kb: Environment-to-bean coupling.
        cb: Bean bias term.
        g: Heater gain on the environment node.
        ka: Environment loss to the room.
        ke: Environment-to-bean loss on the environment node.
        tau_seconds: Heater lag time constant.
    """

    kb: float = 0.002581627760325298
    cb: float = -0.014597977906020201
    g: float = 1.0114058860714497
    ka: float = 0.003323423087046201
    ke: float = 0.005567468791396583
    tau_seconds: float = 269.8706228643829

    def __post_init__(self) -> None:
        """Reject constants the integration cannot use.

        Raises:
            ValueError: If any constant is non-finite or ``tau_seconds`` is not
                positive.
        """
        values = (self.kb, self.cb, self.g, self.ka, self.ke, self.tau_seconds)
        if not all(math.isfinite(v) for v in values) or self.tau_seconds <= 0.0:
            raise ValueError("two-node parameters must be finite with tau_seconds > 0")


class HeaterLagFilter:
    """The model's heater state Q, replayed from the heat actually applied.

    Reproduces the study's ``heater_state``: Q starts at the first applied
    heat fraction and, on a 1 s grid from the first sample, follows
    ``Q += (h(g) - Q) / tau`` where ``h(g)`` is the heat in force at grid time
    ``g`` (zero-order hold on the samples). Feed it only applied
    (acknowledged) heat, never a requested value.
    """

    def __init__(self, parameters: TwoNodeParameters) -> None:
        """Create an unprimed filter.

        Args:
            parameters: The model constants (only tau_seconds is used).
        """
        self._tau = parameters.tau_seconds
        self._q: float | None = None
        self._grid: float = 0.0
        self._last_sample: float = 0.0
        self._heat: float = 0.0

    @property
    def value(self) -> float | None:
        """The current heater state as a 0-1 fraction, or None if unprimed."""
        return self._q

    def update(self, *, elapsed_seconds: float, heat_percent: float) -> float | None:
        """Advance to ``elapsed_seconds``; ``heat_percent`` applies from it on.

        Grid steps before ``elapsed_seconds`` use the heat of the previous
        sample. Non-finite inputs and a sample earlier than the last one leave
        the state unchanged.

        Args:
            elapsed_seconds: The charge-referenced time of this sample.
            heat_percent: The heat applied from this sample on, 0-100.

        Returns:
            The heater state after the update.
        """
        if not (math.isfinite(elapsed_seconds) and math.isfinite(heat_percent)):
            return self._q
        heat = min(max(heat_percent, 0.0), 100.0) / 100.0
        if self._q is None:
            self._q, self._grid, self._last_sample, self._heat = (
                heat,
                elapsed_seconds,
                elapsed_seconds,
                heat,
            )
            return self._q
        if elapsed_seconds < self._last_sample:
            return self._q
        q, grid = self._q, self._grid
        while grid < elapsed_seconds:
            q += (self._heat - q) / self._tau
            grid += 1.0
        self._q, self._grid, self._last_sample, self._heat = q, grid, elapsed_seconds, heat
        return self._q


def two_node_fc_eta_seconds(
    *,
    bean_temp_c: float,
    env_temp_c: float,
    heater_state: float,
    hold_heat_percent: float,
    ambient_temp_c: float,
    fc_target_bean_temp_c: float,
    parameters: TwoNodeParameters,
    horizon_seconds: float = DEFAULT_MODEL_HORIZON_SECONDS,
) -> float | None:
    """Shadow ETA: simulate the fitted model forward at the current heat.

    Integrates at 1 s steps from the given state, holding
    ``hold_heat_percent``, until the bean crosses the target (interpolated
    within the last step) or the horizon passes. Shadow only (D239).

    Args:
        bean_temp_c: The smoothed current bean temperature.
        env_temp_c: The smoothed current environment temperature.
        heater_state: The current heater state Q (0-1 fraction).
        hold_heat_percent: The heat held for the projection, 0-100.
        ambient_temp_c: The room temperature from the ambient probe.
        fc_target_bean_temp_c: The bean temperature to project to.
        parameters: The fitted model constants.
        horizon_seconds: The longest projection to report.

    Returns:
        Seconds until the target, or ``None`` when any input is non-finite,
        the bean is already at the target, or the horizon passes first.
    """
    inputs = (
        bean_temp_c,
        env_temp_c,
        heater_state,
        hold_heat_percent,
        ambient_temp_c,
        fc_target_bean_temp_c,
        horizon_seconds,
    )
    if not all(math.isfinite(x) for x in inputs):
        return None
    if bean_temp_c >= fc_target_bean_temp_c:
        return None
    p = parameters
    heat = min(max(hold_heat_percent, 0.0), 100.0) / 100.0
    b, e, q, t = bean_temp_c, env_temp_c, heater_state, 0.0
    while t < horizon_seconds:
        db = p.kb * (e - b) - p.cb
        de = p.g * q - p.ka * (e - ambient_temp_c) - p.ke * (e - b)
        q += (heat - q) / p.tau_seconds
        b += db
        e += de
        t += 1.0
        if b >= fc_target_bean_temp_c:
            return t - (b - fc_target_bean_temp_c) / max(db, 1e-9)
    return None


def two_node_bean_after_seconds(
    *,
    bean_temp_c: float,
    env_temp_c: float,
    heater_state: float,
    hold_heat_percent: float,
    ambient_temp_c: float,
    seconds: float,
    parameters: TwoNodeParameters,
) -> float | None:
    """Bean temperature after ``seconds`` at a held heat (D239 finish law).

    The same 1 s integration as :func:`two_node_fc_eta_seconds`, run for a
    fixed time instead of to a target, interpolating within the last step.

    Args:
        bean_temp_c: The smoothed current bean temperature.
        env_temp_c: The smoothed current environment temperature.
        heater_state: The current heater state Q (0-1 fraction).
        hold_heat_percent: The heat held for the projection, 0-100.
        ambient_temp_c: The room temperature.
        seconds: How far ahead to project; at most 1,800 s.
        parameters: The model constants.

    Returns:
        The projected bean temperature, or ``None`` for non-finite input or a
        horizon outside ``0 <= seconds <= 1800``.
    """
    inputs = (bean_temp_c, env_temp_c, heater_state, hold_heat_percent, ambient_temp_c, seconds)
    if not all(math.isfinite(x) for x in inputs) or not 0.0 <= seconds <= 1800.0:
        return None
    p = parameters
    heat = min(max(hold_heat_percent, 0.0), 100.0) / 100.0
    b, e, q, t = bean_temp_c, env_temp_c, heater_state, 0.0
    while t + 1.0 <= seconds:
        db = p.kb * (e - b) - p.cb
        de = p.g * q - p.ka * (e - ambient_temp_c) - p.ke * (e - b)
        q += (heat - q) / p.tau_seconds
        b += db
        e += de
        t += 1.0
    remainder = seconds - t
    return b + remainder * (p.kb * (e - b) - p.cb)


class AnchorState(Enum):
    """Where the first-crack anchor is in its lifecycle (D239)."""

    NONE = "none"
    PROVISIONAL = "provisional"
    FROZEN = "frozen"
    REANCHORED = "reanchored"


@dataclass(frozen=True)
class AnchorSettings:
    """D239's anchor window. Every value is a starting hypothesis.

    Attributes:
        open_min_bean_temp_c: Below this bean temperature no anchor is taken.
        open_max_eta_seconds: An ETA longer than this is not yet trusted.
        freeze_eta_seconds: The anchor freezes once the ETA is this short.
        freeze_bean_temp_c: ...or once the bean reaches this temperature.
        reanchor_threshold_seconds: An observed first crack further than this
            from the anchor moves it, once.
    """

    open_min_bean_temp_c: float = 155.0
    open_max_eta_seconds: float = 120.0
    freeze_eta_seconds: float = 45.0
    freeze_bean_temp_c: float = 171.0
    reanchor_threshold_seconds: float = 30.0


@dataclass(frozen=True)
class FcAnchor:
    """The anchor after one update.

    Attributes:
        state: The lifecycle state.
        fc_charge_seconds: The anchored first-crack time on the charge clock,
            or ``None`` while there is no anchor.
        reanchor_delta_seconds: Observed minus anchored time when an observed
            first crack was compared, else ``None``.
    """

    state: AnchorState
    fc_charge_seconds: float | None = None
    reanchor_delta_seconds: float | None = None


class FcAnchorTracker:
    """Provisional → frozen anchor, with one re-anchor to an observed FC.

    One tracker per run. :meth:`update` feeds the estimator's ETA each tick;
    :meth:`observe_first_crack` feeds the first observed first crack (operator
    mark or MCP detection), which is evidence, not a control switch.
    """

    def __init__(self, settings: AnchorSettings | None = None) -> None:
        """Create a tracker with no anchor.

        Args:
            settings: The anchor window; D239's defaults when omitted.
        """
        self._settings = settings or AnchorSettings()
        self._anchor = FcAnchor(AnchorState.NONE)
        self._observed = False

    @property
    def anchor(self) -> FcAnchor:
        """The current anchor."""
        return self._anchor

    def update(
        self,
        *,
        charge_elapsed_seconds: float,
        bean_temp_c: float,
        eta_seconds: float | None,
    ) -> FcAnchor:
        """Fold one tick's ETA into the anchor.

        A frozen or re-anchored anchor never moves here. A provisional anchor
        freezes in place once the bean reaches the freeze temperature, even
        with no usable ETA that tick. A negative ETA is ignored. A missing or
        non-finite ETA keeps the current anchor.

        Args:
            charge_elapsed_seconds: This tick's charge-referenced time.
            bean_temp_c: This tick's bean temperature.
            eta_seconds: The estimator's ETA this tick, or ``None``.

        Returns:
            The anchor after the update.
        """
        if self._anchor.state in (AnchorState.FROZEN, AnchorState.REANCHORED):
            return self._anchor
        s = self._settings
        if (
            self._anchor.state is AnchorState.PROVISIONAL
            and math.isfinite(bean_temp_c)
            and bean_temp_c >= s.freeze_bean_temp_c
        ):
            # Freeze on temperature even when this tick has no usable ETA (a
            # cold-room stall at or above the freeze temperature).
            self._anchor = FcAnchor(AnchorState.FROZEN, self._anchor.fc_charge_seconds)
            return self._anchor
        if eta_seconds is None or not all(
            math.isfinite(x) for x in (charge_elapsed_seconds, bean_temp_c, eta_seconds)
        ):
            return self._anchor
        if eta_seconds < 0.0:
            return self._anchor
        if bean_temp_c < s.open_min_bean_temp_c or eta_seconds > s.open_max_eta_seconds:
            return self._anchor
        fc = charge_elapsed_seconds + eta_seconds
        frozen = eta_seconds <= s.freeze_eta_seconds or bean_temp_c >= s.freeze_bean_temp_c
        self._anchor = FcAnchor(AnchorState.FROZEN if frozen else AnchorState.PROVISIONAL, fc)
        return self._anchor

    def observe_first_crack(self, *, charge_elapsed_seconds: float) -> FcAnchor:
        """Compare the first observed first crack with the anchor, once.

        With no anchor yet, the observation becomes the anchor. Otherwise it
        re-anchors only when it is further than the threshold from the anchor;
        a nearer one freezes the anchor where it is. Later observations, and a
        non-finite time, change nothing.

        Args:
            charge_elapsed_seconds: The observed first crack's charge time.

        Returns:
            The anchor after the comparison.
        """
        if self._observed or not math.isfinite(charge_elapsed_seconds):
            return self._anchor
        self._observed = True
        anchored = self._anchor.fc_charge_seconds
        if anchored is None:
            self._anchor = FcAnchor(AnchorState.REANCHORED, charge_elapsed_seconds)
            return self._anchor
        delta = charge_elapsed_seconds - anchored
        if abs(delta) > self._settings.reanchor_threshold_seconds:
            self._anchor = FcAnchor(AnchorState.REANCHORED, charge_elapsed_seconds, delta)
        else:
            self._anchor = FcAnchor(AnchorState.FROZEN, anchored, delta)
        return self._anchor


@dataclass(frozen=True)
class FcPredictionSnapshot:
    """One tick's observe-only first-crack evidence, persisted with telemetry.

    Attributes:
        slope_eta_seconds: The anchor estimator's ETA, or ``None``.
        model_eta_seconds: The shadow model's ETA, or ``None`` (including when
            the room reading is missing or stale).
        anchor_state: The anchor's lifecycle state.
        anchor_fc_charge_seconds: The anchored first-crack time, or ``None``.
        planned_finish_charge_seconds: ``F / (1 - p)`` for the anchor, or
            ``None``.
    """

    slope_eta_seconds: float | None
    model_eta_seconds: float | None
    anchor_state: AnchorState
    anchor_fc_charge_seconds: float | None
    planned_finish_charge_seconds: float | None


def planned_finish_charge_seconds(
    fc_charge_seconds: float, *, target_development_percent: float
) -> float | None:
    """The planned finish ``F / (1 - p)`` on the charge clock (D239).

    Args:
        fc_charge_seconds: The anchored first-crack time.
        target_development_percent: The profile's development target, 0-100.

    Returns:
        The planned finish, or ``None`` for a non-finite or non-positive
        anchor, or a target outside the open interval (0, 100).
    """
    if not (math.isfinite(fc_charge_seconds) and math.isfinite(target_development_percent)):
        return None
    if fc_charge_seconds <= 0.0 or not 0.0 < target_development_percent < 100.0:
        return None
    return fc_charge_seconds / (1.0 - target_development_percent / 100.0)
