"""Control-trajectory sanity scoring for the advisor bake-off (#277 / D40.4).

The piece that turns the bake-off from a *drop-decision* eval into a *control-loop*
eval. The existing :mod:`bakeoff_replay` layer scores each candidate against a
known-good roast (drop F1, heat/fan MAE, heat-direction agreement, latency); this
module adds an orthogonal, **agreement-free** view: *how coherent is the
candidate's own lever-command sequence over the development phase* — does it trim
heat steadily (roaster-like), or twiddle the fan and saw the heat up and down
(the #218 failure mode)?

Why the command signal, not the temperature curve
--------------------------------------------------
Per the verified research synthesis
(``docs/research/2026-06-14-roast-curve-features.md``, "The entropy question"):
**entropy of the temperature curve is dominated by sensor noise** — for a smooth,
slowly-varying signal, adding any i.i.d. noise drives permutation entropy to its
maximum (Bandt & Pompe). The roast curve is exactly that kind of signal, so its
"entropy" measures the thermocouple, not the roast. Instability / entropy earns
its place **on the control signal — the heat/fan command sequence** — as the
twiddle / oscillation measure, which is precisely the #218 failure mode (fan
30↔40↔50↔60↔70, heat 70→40→20→0). The research gives concrete cheap online
methods, ranked cheapest → richest: (1) lever **change-count + direction-reversal
count**, (2) waveform **area-ratio asymmetry**, (3) Hurst/DFA on the command
series. This module implements (1) as the load-bearing floor and a documented,
online-computable **reversal-rate entropy** proxy from family (3); it leaves the
area-ratio / full DFA as future enrichment if (1) proves too coarse.

The mill47 management rule the research confirms grounds the whole design:
crash / flick are avoided by a **steady decrease in heat for a gradually declining
RoR**, explicitly **NOT** throttling heat "back and forth to compensate". The
``momentum_cut_flags`` metric encodes the inverse of that rule — a large heat cut
issued while RoR is *already* low or declining is momentum-killing, exactly what a
steady trim avoids.

Honest framing — lower thrash is NOT higher quality
---------------------------------------------------
This scorer measures **coherence of the command sequence, not roast quality.**
Lower thrash is more roaster-like and is the behaviour the deterministic
steady-trim floor (#222) and the direction-flip dead-band (#223/#228) exist to
produce — but a perfectly smooth ramp to a *wrong* temperature is still a bad
roast. ``trajectory_sanity`` is **agreement-free in both directions**: it neither
rewards matching the known-good roast (that is the :mod:`bakeoff_replay` job) nor
proves correctness. Same calibration as the existing scorecard's
"agreement ≠ correctness": read these numbers WITH the drop / heat-direction
metrics and the advice samples, never alone.

All of this is pure (no I/O) and computes from the same per-tick
:class:`~bakeoff_replay.TickOutcome` list the replay produces, restricted to the
development phase (post-FC → drop), so it slots into the bake-off behind a flag
without touching the existing drop / heat metrics.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from bakeoff_replay import TickOutcome

from roastpilot_agent.models import RoastPhase

# --- Tunable thresholds (named constants, documented) -----------------------

# A lever move at or above this magnitude (percentage points) between consecutive
# development-phase recommendations counts as a deliberate "change". Mirrors the
# replay layer's ``_DIRECTION_DEADBAND`` (1.0) so a sub-1-point wobble is "hold", not
# a change — the same dead-band the direction-flip guard (#223/#228) uses, kept in
# sync deliberately. OPEN QUESTION for the operator: should the trajectory
# dead-band be WIDER than the agreement dead-band (e.g. 3-5 pts) so only roaster-
# meaningful moves count as thrash? Set conservatively to 1.0 for now.
CHANGE_DEADBAND_PCT = 1.0

# A heat cut at or beyond this magnitude (percentage points, single step) is a
# "large" cut for the momentum-cut check. 15 pts is roughly the n8n-proven
# deterministic step granularity (heat moves in ~10-20 pt blocks, not 1-pt trims);
# a cut bigger than this on an already-flat/declining RoR is the momentum-killing
# move the mill47 "don't throttle back and forth" rule warns against. OPEN
# QUESTION: confirm the magnitude against the operator's proven control steps.
LARGE_HEAT_CUT_PCT = 15.0

# RoR (°C/min) at or below this is "already low" — a regime where a large heat cut
# kills momentum rather than trimming it. The post-FC RoR on the operator's roasts
# runs low single digits into the drop; a big cut there flattens or crashes it.
# OPEN QUESTION: validate this floor against the 47-roast .alog development RoR
# distribution (memory: "Operator Hottop roast profile").
LOW_ROR_C_PER_MIN = 5.0

# Below this many development-phase recommendations the reversal_rate / entropy
# is LOW-CONFIDENCE: a reversal needs >= 2 directional moves (>= 3 setpoints), so a
# window of <= 2 ticks yields reversal_rate=0 by construction — "smooth" by
# accident, not coherence — while still eligible for momentum-cut penalties. Flag
# (do NOT auto-exclude) any window below this so the operator does not rank a
# short-window model "smooth" spuriously (safety-reviewer #2). Set to 4: the
# smallest window where at least two reversals can occur, so the rate is
# meaningfully populated rather than 0/near-0 by window length alone.
MIN_CONFIDENT_DEV_TICKS = 4


# --- Per-lever trajectory metrics -------------------------------------------


@dataclasses.dataclass(frozen=True)
class LeverTrajectory:
    """Command-sequence sanity for one lever (heat or fan) over development.

    All counts are over the *model's own recommended* setpoint sequence across
    the scored development-phase ticks — they do not reference the real roast at
    all (this metric is agreement-free by construction).

    Attributes:
        lever: ``"heat"`` or ``"fan"`` (which command series this summarises).
        sample_count: Number of development-phase recommendations in the series.
        change_count: Ticks where the lever moved at least
            :data:`CHANGE_DEADBAND_PCT` from the previous recommendation.
        direction_reversal_count: Sign flips between consecutive *signed* deltas
            — the core thrash signal (up-then-down or down-then-up). A steady
            trim has zero reversals; the 30↔40↔50↔60↔70 twiddle reverses on
            almost every step.
        reversal_rate: ``direction_reversal_count`` normalised by the number of
            adjacent *directional* (non-hold) move-pairs that could reverse
            (``max(directional_moves - 1, 0)``) — in ``[0, 1]``, and
            **hold-invariant**: interleaving holds between the same directional
            moves does not change the rate, so it is comparable across models
            regardless of how many sub-dead-band holds each emits.
        mean_abs_change: Mean magnitude (percentage points) of the moves that
            cleared the dead-band, or ``0.0`` if the lever never moved.
    """

    lever: str
    sample_count: int
    change_count: int
    direction_reversal_count: int
    reversal_rate: float
    mean_abs_change: float


def _signed_deltas(series: list[int]) -> list[int]:
    """Return the consecutive deltas of a setpoint series (``series[i+1]-series[i]``)."""
    return [series[i + 1] - series[i] for i in range(len(series) - 1)]


def _classify_delta(delta: int) -> int:
    """Classify a delta as -1 / 0 / +1 using the change dead-band.

    A move smaller than :data:`CHANGE_DEADBAND_PCT` in magnitude is "hold" (0)
    (so an exact 1 pp change counts as a move; only a sub-1-point sensor-grade
    wobble is held — this keeps noise from registering as thrash).

    Args:
        delta: The signed setpoint change in percentage points.

    Returns:
        ``-1`` for a cut, ``0`` for a hold, ``+1`` for a raise.
    """
    if delta >= CHANGE_DEADBAND_PCT:
        return 1
    if delta <= -CHANGE_DEADBAND_PCT:
        return -1
    return 0


def score_lever_trajectory(lever: str, series: list[int]) -> LeverTrajectory:
    """Score one lever's command sequence for change-count + reversal thrash.

    Implements the research's cheapest tier: lever change-count + direction-
    reversal count over the command series. A *reversal* is a sign flip between
    two consecutive non-hold deltas — i.e. the model raised then cut, or cut then
    raised. Holds (sub-dead-band moves) do not break a run and are not themselves
    reversals; only the ordered sequence of signed moves is examined, so
    ``70→40→20→0`` (three cuts, no flip) scores zero reversals while
    ``30→40→30→40`` (up, down, up) scores two.

    Args:
        lever: ``"heat"`` or ``"fan"`` — labels the result for the report.
        series: The model's recommended setpoints over the development ticks, in
            tick order.

    Returns:
        The :class:`LeverTrajectory` summary.
    """
    deltas = _signed_deltas(series)
    abs_changes = [abs(d) for d in deltas if abs(d) >= CHANGE_DEADBAND_PCT]
    change_count = len(abs_changes)

    # Reversals: walk the classified non-hold moves and count sign flips between
    # consecutive directional moves. Holds are skipped (they neither flip nor
    # reset the running direction — a trim, a pause, then a further trim is not a
    # reversal).
    directions = [c for c in (_classify_delta(d) for d in deltas) if c != 0]
    reversals = sum(1 for i in range(len(directions) - 1) if directions[i] != directions[i + 1])

    # Normalise by the number of *directional* (non-hold) move-pairs that could
    # reverse — NOT the raw sample-pair count — so the rate is HOLD-INVARIANT and
    # comparable across models. The numerator counts flips between non-hold moves,
    # so the denominator must too: with D directional moves there are max(D-1, 0)
    # adjacent directional pairs. Normalising by len(series)-2 instead would let a
    # model that interleaves holds between thrashy moves look falsely more coherent
    # than one thrashing back-to-back with the same reversals (safety-reviewer #1).
    directional_pairs = max(len(directions) - 1, 0)
    reversal_rate = round(reversals / directional_pairs, 3) if directional_pairs else 0.0

    return LeverTrajectory(
        lever=lever,
        sample_count=len(series),
        change_count=change_count,
        direction_reversal_count=reversals,
        reversal_rate=reversal_rate,
        mean_abs_change=round(sum(abs_changes) / change_count, 2) if change_count else 0.0,
    )


# --- Control-signal entropy (documented instability measure) ----------------


def control_signal_entropy(heat: LeverTrajectory, fan: LeverTrajectory) -> float:
    """Combine the two levers' reversal-rates into one instability scalar.

    A simple, cheap, online-computable instability measure over the command
    sequence, taken from the research's family-(3) "reversal-rate" floor rather
    than a full permutation-entropy / DFA computation (those are listed as the
    *richest* tier, to add only if the cheap floor proves too coarse — see the
    module docstring). The reversal-rate is itself an order/shape statistic in
    ``[0, 1]`` (the fraction of consecutive command moves that flipped
    direction), so a thrashy saw-tooth tends to its maximum and a monotone trim
    to zero — the same maxed-on-disorder / zero-on-order behaviour a permutation-
    entropy metric would give, at negligible compute.

    The combined scalar is the mean of the heat and fan reversal-rates: both
    levers thrash in the #218 failure modes (heat sawing AND fan twiddling), so
    weighting them equally surfaces either. The value stays in ``[0, 1]``; higher
    = more disordered command signal.

    Args:
        heat: The scored heat trajectory.
        fan: The scored fan trajectory.

    Returns:
        The combined reversal-rate entropy in ``[0, 1]`` (higher = more unstable).
    """
    return round((heat.reversal_rate + fan.reversal_rate) / 2.0, 3)


# --- Momentum-cut detection (the mill47 "don't throttle back" inverse) -------


@dataclasses.dataclass(frozen=True)
class MomentumCut:
    """One large heat cut issued on an already-low/declining RoR.

    Attributes:
        monotonic_seconds: Roast time of the tick whose recommendation cut heat.
        prev_heat_percent: The model's previous recommended heat setpoint.
        new_heat_percent: The model's recommended heat at this tick.
        bean_ror_c_per_min: The replayed bean RoR at this tick (``None`` if the
            context could not estimate it).
        ror_declining: Whether the RoR was already declining vs the prior tick.
    """

    monotonic_seconds: float
    prev_heat_percent: int
    new_heat_percent: int
    bean_ror_c_per_min: float | None
    ror_declining: bool


def find_momentum_cuts(outcomes: list[TickOutcome]) -> list[MomentumCut]:
    """Find large heat cuts issued while RoR was already low or declining.

    Encodes the inverse of the research-confirmed mill47 rule ("steady decrease
    in heat for a gradually declining RoR, NOT throttling back and forth"): a cut
    of at least :data:`LARGE_HEAT_CUT_PCT` is *momentum-killing* when the replayed
    bean RoR at that tick is **already low** (``<= LOW_ROR_C_PER_MIN``) **or
    already declining** vs the previous tick — there is no momentum left to trim,
    so a big cut flattens or crashes the roast instead of steering it.

    Uses the **replayed** RoR carried in each tick's context (the real roast's
    measured RoR), not anything the model reported — the question is whether the
    model cut hard into a regime that did not call for it.

    Args:
        outcomes: Per-tick outcomes over the development phase, in tick order.
            Only ticks that produced a decision are considered; a tick with no
            decision breaks the consecutive-pair chain (its successor has no valid
            previous recommendation to diff against).

    Returns:
        The list of detected :class:`MomentumCut`s, in tick order.
    """
    cuts: list[MomentumCut] = []
    prev_heat: int | None = None
    prev_ror: float | None = None
    for outcome in outcomes:
        if outcome.decision is None:
            # No recommendation: cannot form a delta from or to this tick.
            prev_heat = None
            continue
        new_heat = outcome.decision.target_heat
        ror = outcome.tick.context.bean_ror_c_per_min
        if prev_heat is not None:
            drop = prev_heat - new_heat
            declining = prev_ror is not None and ror is not None and ror < prev_ror
            low = ror is not None and ror <= LOW_ROR_C_PER_MIN
            if drop >= LARGE_HEAT_CUT_PCT and (low or declining):
                cuts.append(
                    MomentumCut(
                        monotonic_seconds=outcome.tick.monotonic_seconds,
                        prev_heat_percent=prev_heat,
                        new_heat_percent=new_heat,
                        bean_ror_c_per_min=ror,
                        ror_declining=declining,
                    )
                )
        prev_heat = new_heat
        prev_ror = ror
    return cuts


# --- Composite trajectory-sanity scorecard ----------------------------------


@dataclasses.dataclass(frozen=True)
class TrajectorySanity:
    """The control-trajectory sanity scorecard for one roast replay.

    Computed over the **development phase only** (post-FC → drop), from the
    candidate's own recommended lever sequence. Agreement-free: see the module
    docstring — lower thrash is more roaster-like, NOT proof of roast quality.

    Attributes:
        roast_name: The replay roast label.
        development_tick_count: Development-phase ticks scored (including any that
            failed to produce a decision).
        development_ok_count: Development-phase ticks that produced a decision.
        heat: The heat command-sequence trajectory.
        fan: The fan command-sequence trajectory.
        control_signal_entropy: The combined reversal-rate instability scalar in
            ``[0, 1]`` (higher = more disordered command signal).
        momentum_cut_flags: Count of large heat cuts issued on an already-low /
            declining RoR (the momentum-killing move).
        momentum_cuts: The detected cuts (for the report / diagnosis).
        trajectory_sanity: The composite thrash summary (lower = more roaster-
            like). See :func:`composite_sanity` for the exact combination.
        low_confidence: ``True`` when the development window had fewer than
            :data:`MIN_CONFIDENT_DEV_TICKS` recommendations — the reversal_rate /
            entropy is then near-zero by window length, not by coherence, so the
            "smooth" reading is not trustworthy. Surfaced in the report; the
            numbers are still computed (not nulled), just flagged.
    """

    roast_name: str
    development_tick_count: int
    development_ok_count: int
    heat: LeverTrajectory
    fan: LeverTrajectory
    control_signal_entropy: float
    momentum_cut_flags: int
    momentum_cuts: list[MomentumCut]
    trajectory_sanity: float
    low_confidence: bool


# Weight on each momentum-cut flag in the composite. A momentum-killing cut is a
# qualitatively worse failure than a reversal (it can crash the roast, not just
# look jittery), so each flag adds a fixed penalty on top of the entropy term.
# Documented + tunable. OPEN QUESTION: operator to weight momentum cuts vs raw
# thrash — is one bad cut worse than a fully sawing fan?
_MOMENTUM_CUT_WEIGHT = 0.5


def composite_sanity(entropy: float, momentum_cut_flags: int, ok_count: int) -> float:
    """Combine the entropy and momentum-cut terms into one thrash score.

    Lower is more roaster-like. The score is the command-signal entropy (in
    ``[0, 1]``) plus a per-flag penalty for each momentum-killing cut, the cut
    count normalised by the scored development ticks so a long roast is not
    penalised merely for having more ticks. With zero ticks the score is ``0.0``
    (nothing to thrash). The composite is **not** capped at 1.0 — a roast that
    both saws its levers and repeatedly cuts momentum should score visibly worse
    than one that only saws.

    Args:
        entropy: The combined reversal-rate entropy in ``[0, 1]``.
        momentum_cut_flags: Count of momentum-killing cuts.
        ok_count: Development ticks that produced a decision (the normaliser).

    Returns:
        The composite trajectory-sanity score (lower = better / less thrash).
    """
    if ok_count <= 0:
        return 0.0
    momentum_term = _MOMENTUM_CUT_WEIGHT * (momentum_cut_flags / ok_count)
    return round(entropy + momentum_term, 3)


def _development_outcomes(outcomes: list[TickOutcome]) -> list[TickOutcome]:
    """Return only the development-phase (post-FC → drop) ticks, in order."""
    return [o for o in outcomes if o.tick.context.phase is RoastPhase.DEVELOPMENT]


def score_trajectory(outcomes: list[TickOutcome], roast_name: str) -> TrajectorySanity:
    """Score a replay's development-phase command trajectory for sanity / thrash.

    Restricts to the development phase (the post-FC → drop window the control-loop
    eval cares about — the phase where the #218 thrash showed up and where the
    advisor's judgement is consulted unthrottled), extracts the candidate's own
    recommended heat and fan sequences, and computes the change / reversal counts,
    the control-signal entropy, the momentum-cut flags, and the composite.

    Args:
        outcomes: The full per-tick replay outcomes for one roast (any phase);
            non-development ticks are filtered out here.
        roast_name: A label for the scorecard.

    Returns:
        The :class:`TrajectorySanity` scorecard.
    """
    dev = _development_outcomes(outcomes)
    ok = [o for o in dev if o.decision is not None]
    heat_series = [o.decision.target_heat for o in ok if o.decision is not None]
    fan_series = [o.decision.target_fan for o in ok if o.decision is not None]

    heat = score_lever_trajectory("heat", heat_series)
    fan = score_lever_trajectory("fan", fan_series)
    entropy = control_signal_entropy(heat, fan)
    cuts = find_momentum_cuts(dev)
    sanity = composite_sanity(entropy, len(cuts), len(ok))

    return TrajectorySanity(
        roast_name=roast_name,
        development_tick_count=len(dev),
        development_ok_count=len(ok),
        heat=heat,
        fan=fan,
        control_signal_entropy=entropy,
        momentum_cut_flags=len(cuts),
        momentum_cuts=cuts,
        trajectory_sanity=sanity,
        low_confidence=len(ok) < MIN_CONFIDENT_DEV_TICKS,
    )


# --- Reporting --------------------------------------------------------------

_TRAJECTORY_FRAMING = (
    "> **Read first — what trajectory-sanity means.** These metrics score the "
    "candidate's OWN heat/fan command sequence over development (post-FC → drop) "
    "for coherence — change-count, direction reversals, and momentum-killing "
    "cuts (the #218 failure mode). Per the curve-features research, instability "
    "belongs on the COMMAND signal, not the temperature curve (whose entropy is "
    "sensor noise). This is **agreement-free**: lower thrash is more roaster-like "
    "and is what the steady-trim floor (#222) + direction-flip dead-band "
    "(#223/#228) aim for, but a smooth ramp to the WRONG temperature is still a "
    "bad roast. Lower = less thrash, NOT higher quality — read WITH the drop / "
    "heat-direction metrics, never alone."
)


def _render_lever_line(lever: LeverTrajectory) -> str:
    """Render one lever's trajectory metrics as a compact report line."""
    return (
        f"    {lever.lever}: changes={lever.change_count}/"
        f"{max(lever.sample_count - 1, 0)} "
        f"reversals={lever.direction_reversal_count} "
        f"reversal_rate={lever.reversal_rate} "
        f"mean_abs_change={lever.mean_abs_change}pp"
    )


def render_trajectory_md(sanity: TrajectorySanity) -> list[str]:
    """Render one roast's trajectory-sanity scorecard as markdown lines.

    Args:
        sanity: The scorecard to render.

    Returns:
        Markdown lines for the report (the caller indents under the cell).
    """
    confidence = (
        f" [LOW-CONFIDENCE: < {MIN_CONFIDENT_DEV_TICKS} dev ticks — reversal_rate/"
        "entropy near-zero by window length, not coherence; do NOT rank as 'smooth']"
        if sanity.low_confidence
        else ""
    )
    lines = [
        f"  - {sanity.roast_name} (dev ticks {sanity.development_ok_count}/"
        f"{sanity.development_tick_count}): "
        f"trajectory_sanity={sanity.trajectory_sanity} "
        f"(lower=less thrash) "
        f"entropy={sanity.control_signal_entropy} "
        f"momentum_cuts={sanity.momentum_cut_flags}{confidence}",
        _render_lever_line(sanity.heat),
        _render_lever_line(sanity.fan),
    ]
    for cut in sanity.momentum_cuts:
        ror = "?" if cut.bean_ror_c_per_min is None else f"{cut.bean_ror_c_per_min}"
        why = "declining" if cut.ror_declining else "low-RoR"
        lines.append(
            f"      momentum-cut @ {cut.monotonic_seconds:.0f}s: "
            f"heat {cut.prev_heat_percent}->{cut.new_heat_percent}% "
            f"on RoR={ror}°C/min ({why})"
        )
    return lines


def render_trajectory_report(scored: list[tuple[str, list[TrajectorySanity]]]) -> str:
    """Render the trajectory-sanity section for the bake-off markdown report.

    Args:
        scored: ``(candidate_label, [per-roast TrajectorySanity])`` pairs, in the
            order they should appear (one entry per (model, prompt) cell).

    Returns:
        The markdown section.
    """
    out: list[str] = [
        "# Control-trajectory sanity (#277 / D40.4) — command-signal coherence",
        "",
        _TRAJECTORY_FRAMING,
        "",
        (
            "Scored over the DEVELOPMENT phase (post-FC → drop) on each candidate's "
            "own heat/fan recommendations. change-count + direction-reversal count "
            "(the cheap research floor); control-signal entropy = combined "
            "reversal-rate (in [0,1]); momentum_cuts = large heat cuts (>= "
            f"{LARGE_HEAT_CUT_PCT:g}pp) on an already-low (<= {LOW_ROR_C_PER_MIN:g}"
            "°C/min) or declining RoR. trajectory_sanity composites them (lower = "
            "less thrash). NO auto-pick — agreement-free, read with the drop "
            "metrics."
        ),
    ]
    for label, per_roast in scored:
        out.append("")
        out.append(f"## {label}")
        for sanity in per_roast:
            out.extend(render_trajectory_md(sanity))
    return "\n".join(out)


def trajectory_to_json(sanity: TrajectorySanity) -> dict[str, Any]:
    """Serialize a :class:`TrajectorySanity` to a JSON-ready dict.

    Args:
        sanity: The scorecard to serialize.

    Returns:
        A JSON-serialisable dict (the ``momentum_cuts`` list is expanded so the
        per-cut detail survives into the artifact).
    """
    return {
        "roast_name": sanity.roast_name,
        "development_tick_count": sanity.development_tick_count,
        "development_ok_count": sanity.development_ok_count,
        "heat": dataclasses.asdict(sanity.heat),
        "fan": dataclasses.asdict(sanity.fan),
        "control_signal_entropy": sanity.control_signal_entropy,
        "momentum_cut_flags": sanity.momentum_cut_flags,
        "momentum_cuts": [dataclasses.asdict(c) for c in sanity.momentum_cuts],
        "trajectory_sanity": sanity.trajectory_sanity,
        "low_confidence": sanity.low_confidence,
    }
