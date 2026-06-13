"""Real-roast replay + scoring for the advisor bake-off (#172/#173).

The bake-off's quantitative layer: replay each known-good 7-Jun Hottop roast
tick-by-tick, reconstruct the :class:`~roastpilot_agent.advisor.AdvisorContext`
the agent would have seen at each decision tick, ask a candidate model for a
recommendation, and **score the recommendations against what the real roast
actually did**.

Honest framing — read before trusting a number
-----------------------------------------------
The ground truth is a **known-GOOD roast, not a provably optimal one.** Every
metric here measures *agreement with a known-good roast*, NOT absolute
correctness. A capable model may legitimately diverge from what the human did
and still produce an excellent roast; conversely, high agreement is not proof of
quality. These scores are a quantitative *aid* to the operator's judgement (the
advice samples + the latency gate), never a replacement for it, and an F1 of 1.0
is **not** "correct" — it is "matched this one good roast." Report wording must
preserve this.

What it scores, per (model, prompt, roast)
------------------------------------------
- **Drop decision** (``should_drop``) — the flavor-critical call. Treated as a
  binary classification over ticks against the real roast's drop time:
  precision / recall / **F1**, plus the **drop-timing error** of the model's
  first ``should_drop=True`` tick vs the real drop, in **seconds and °C** (drop
  before the ~196 °C bitter ceiling, near the DTR target, is the whole game).
- **Heat / fan** — mean absolute error vs the real setpoints **and directional
  agreement**: did the model move the lever the *same way* the human did at each
  tick (especially the anticipatory pre-FC heat cut)?
- **Latency** — per phase, against the hard gate (tightest at first crack).

The replay + scoring + report machinery is fully exercisable **without an API
key** via a :class:`~roastpilot_agent.advisor.FakeAdvisor` (or any callable that
returns canned recommendations). Only the real-candidate run needs
``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.models import RoastPhase

# How far back to estimate rate-of-rise from the raw temperature series.
_ROR_WINDOW_SECONDS = 60.0
# How many recent telemetry rows to hand the advisor as context per tick.
_RECENT_SAMPLES = 6
# A heat/fan move at or above this magnitude (percentage points) between the
# previous and current real setpoint counts as a deliberate "change" for the
# directional-agreement metric; smaller wobble is treated as "hold".
_DIRECTION_DEADBAND = 1.0


@dataclasses.dataclass(frozen=True)
class GroundTruth:
    """The known-good outcome a roast replay scores against.

    Attributes:
        t0_seconds: Charge (``beans_added``) time.
        first_crack_seconds: First-crack detection time.
        drop_seconds: Bean-drop time.
        drop_temp_c: Bean temperature at the real drop (the °C reference for
            the drop-timing error).
        development_time_ratio: ``(drop - first_crack) / (drop - t0)`` — the
            achieved DTR of the good roast.
    """

    t0_seconds: float
    first_crack_seconds: float
    drop_seconds: float
    drop_temp_c: float
    development_time_ratio: float


@dataclasses.dataclass(frozen=True)
class ReplayTick:
    """One decision tick of a replayed roast.

    Attributes:
        context: The reconstructed advisor context at this tick.
        real_heat_percent: The real roast's heat setpoint at this tick.
        real_fan_percent: The real roast's fan setpoint at this tick.
        prev_real_heat_percent: The real heat setpoint at the previous tick (for
            directional agreement); ``None`` at the first tick.
        prev_real_fan_percent: The real fan setpoint at the previous tick.
        real_should_drop: Whether the real roast had dropped by this tick (the
            binary ground-truth label for the drop classification).
        monotonic_seconds: The tick's roast timestamp.
    """

    context: AdvisorContext
    real_heat_percent: int
    real_fan_percent: int
    prev_real_heat_percent: int | None
    prev_real_fan_percent: int | None
    real_should_drop: bool
    monotonic_seconds: float


# A recommender: given a tick context, return a recommendation. The real
# advisor's ``get_recommendation`` matches this; a fake/canned callable matches
# it too, which is how the scoring is tested without a key.
Recommender = Callable[[AdvisorContext], Awaitable[RoastDecision]]


def load_roast(fixture: Path) -> tuple[list[dict[str, Any]], GroundTruth]:
    """Load a live-roast ``roast.jsonl`` into telemetry rows + its ground truth.

    Args:
        fixture: Path to the ``roast.jsonl`` export.

    Returns:
        ``(telemetry_rows, ground_truth)`` with rows in recorded order.

    Raises:
        ValueError: If the fixture lacks the charge / first-crack / drop events
            the scoring needs.
    """
    telemetry: list[dict[str, Any]] = []
    events: dict[str, float] = {}
    for line in fixture.read_text().splitlines():
        if not line.strip():
            continue
        row: dict[str, Any] = json.loads(line)
        if row.get("type") == "telemetry":
            telemetry.append(row)
        elif row.get("type") == "event":
            events[str(row["kind"])] = float(row["monotonic_seconds"])
    missing = {"beans_added", "first_crack_detected", "beans_dropped"} - events.keys()
    if missing:
        raise ValueError(f"fixture {fixture} lacks required events: {sorted(missing)}")
    t0 = events["beans_added"]
    fc = events["first_crack_detected"]
    drop = events["beans_dropped"]
    drop_row = min(telemetry, key=lambda r: abs(float(r["monotonic_seconds"]) - drop))
    ground = GroundTruth(
        t0_seconds=t0,
        first_crack_seconds=fc,
        drop_seconds=drop,
        drop_temp_c=float(drop_row["bean_temp_c"]),
        development_time_ratio=(drop - fc) / (drop - t0),
    )
    return telemetry, ground


def _ror(rows: list[dict[str, Any]], index: int, field: str) -> float | None:
    """Estimate °C/min for ``field`` at ``rows[index]`` over the prior ~60 s."""
    now_t = float(rows[index]["monotonic_seconds"])
    for past in reversed(rows[:index]):
        dt = now_t - float(past["monotonic_seconds"])
        if dt >= _ROR_WINDOW_SECONDS:
            return round((float(rows[index][field]) - float(past[field])) / dt * 60.0, 3)
    return None


def _recent_samples(rows: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """Return the recent telemetry window the advisor sees at a tick."""
    recent = rows[max(0, index - _RECENT_SAMPLES + 1) : index + 1]
    return [
        {
            "monotonic_seconds": float(r["monotonic_seconds"]),
            "bean_temp_c": float(r["bean_temp_c"]),
            "env_temp_c": float(r["env_temp_c"]),
            "heat_level_percent": int(r["heat_level_percent"]),
            "fan_level_percent": int(r["fan_level_percent"]),
        }
        for r in recent
    ]


def _phase_at(mono: float, ground: GroundTruth) -> RoastPhase:
    """Map a tick timestamp to the agent phase the controller would be in."""
    if mono < ground.t0_seconds:
        return RoastPhase.PREHEATING
    if mono < ground.first_crack_seconds:
        return RoastPhase.ROASTING_PRE_FIRST_CRACK
    return RoastPhase.DEVELOPMENT


def build_ticks(
    fixture: Path,
    *,
    cadence_seconds: float = 30.0,
    profile_name: str | None = None,
) -> tuple[list[ReplayTick], GroundTruth]:
    """Reconstruct the per-tick advisor contexts for a replayed roast.

    Walks the telemetry from the first row to the drop, emitting a
    :class:`ReplayTick` roughly every ``cadence_seconds`` of roast time. Each
    tick's context is grounded in the real telemetry (bean/env temp, RoR
    computed from the series, phase, development-elapsed from the FC event, FC
    flag + timestamp), and carries the real heat/fan setpoint (and the previous
    tick's setpoint) so the recommendation can be scored against what the human
    actually did.

    Args:
        fixture: The live-roast ``roast.jsonl`` to replay.
        cadence_seconds: Minimum roast-time spacing between emitted ticks. The
            controller ticks every 1 s, but a bake-off scoring every second
            would be 600+ calls per roast; 30 s samples the roast densely enough
            to score the curve while keeping a key-spending run affordable.
        profile_name: Optional profile name to stamp into the contexts; defaults
            to the fixture's ``parent/parent`` label.

    Returns:
        ``(ticks, ground_truth)``.
    """
    telemetry, ground = load_roast(fixture)
    name = profile_name or f"{fixture.parent.parent.name}/{fixture.parent.name}"
    drop_index = min(
        range(len(telemetry)),
        key=lambda i: abs(float(telemetry[i]["monotonic_seconds"]) - ground.drop_seconds),
    )
    # Indices to score: a cadence-spaced sample of the roast plus, always, the
    # drop row — the single tick whose ground-truth ``should_drop`` is True, so
    # the drop classification has a positive instance regardless of cadence.
    selected: list[int] = []
    last_emitted: float | None = None
    for index in range(drop_index + 1):
        mono = float(telemetry[index]["monotonic_seconds"])
        if index == drop_index or last_emitted is None or mono - last_emitted >= cadence_seconds:
            selected.append(index)
            last_emitted = mono
    if drop_index not in selected:  # pragma: no cover — drop_index is always added in-loop
        selected.append(drop_index)

    ticks: list[ReplayTick] = []
    prev_heat: int | None = None
    prev_fan: int | None = None
    for index in selected:
        row = telemetry[index]
        mono = float(row["monotonic_seconds"])
        heat = int(row["heat_level_percent"])
        fan = int(row["fan_level_percent"])
        phase = _phase_at(mono, ground)
        fc_detected = mono >= ground.first_crack_seconds
        context = AdvisorContext(
            phase=phase,
            roast_elapsed_seconds=round(mono - ground.t0_seconds, 3),
            development_elapsed_seconds=(
                round(mono - ground.first_crack_seconds, 3) if fc_detected else None
            ),
            current_bean_temp_c=float(row["bean_temp_c"]),
            current_env_temp_c=float(row["env_temp_c"]),
            bean_ror_c_per_min=_ror(telemetry, index, "bean_temp_c"),
            env_ror_c_per_min=_ror(telemetry, index, "env_temp_c"),
            target_drop_temp_c=ground.drop_temp_c,
            target_development_percent=round(ground.development_time_ratio * 100, 1),
            charge_guidance_min_c=180.0 if phase is RoastPhase.PREHEATING else None,
            charge_guidance_max_c=200.0 if phase is RoastPhase.PREHEATING else None,
            profile_name=name,
            recent_telemetry_samples=_recent_samples(telemetry, index),
            first_crack_detected=fc_detected,
            first_crack_timestamp_seconds=(
                round(ground.first_crack_seconds - ground.t0_seconds, 3) if fc_detected else None
            ),
        )
        ticks.append(
            ReplayTick(
                context=context,
                real_heat_percent=heat,
                real_fan_percent=fan,
                prev_real_heat_percent=prev_heat,
                prev_real_fan_percent=prev_fan,
                # The real roast dropped at (its nearest telemetry row to) the
                # drop event — that row carries the lone positive label, and
                # every earlier tick is correctly "do not drop yet".
                real_should_drop=index == drop_index,
                monotonic_seconds=mono,
            )
        )
        prev_heat, prev_fan = heat, fan
    return ticks, ground


@dataclasses.dataclass
class TickOutcome:
    """The recommendation + measurement for one replayed tick."""

    tick: ReplayTick
    decision: RoastDecision | None
    latency_seconds: float | None
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class DropMetrics:
    """Drop-decision agreement vs the known-good roast.

    Attributes:
        precision/recall/f1: Binary ``should_drop`` agreement over ticks.
        true_positives/false_positives/false_negatives: The confusion counts.
        first_drop_seconds: Roast time of the model's first ``should_drop=True``
            tick, or ``None`` if it never advised dropping.
        timing_error_seconds: ``first_drop - real_drop`` in seconds (negative =
            model dropped early), or ``None``.
        timing_error_c: Bean-temperature gap between the model's first-drop tick
            and the real drop, or ``None``.
    """

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    first_drop_seconds: float | None
    timing_error_seconds: float | None
    timing_error_c: float | None


@dataclasses.dataclass(frozen=True)
class LeverMetrics:
    """Heat-or-fan agreement vs the real setpoints.

    Attributes:
        mae: Mean absolute error (percentage points) vs the real setpoint.
        directional_agreement: Fraction of ticks (with a previous setpoint)
            where the model moved the lever the *same direction* the human did
            — up / down / hold, with a small dead-band. ``None`` if no tick had
            a previous setpoint to compare against.
        directional_samples: How many ticks the directional metric is over.
    """

    mae: float
    directional_agreement: float | None
    directional_samples: int


@dataclasses.dataclass(frozen=True)
class PhaseLatency:
    """Latency summary for one phase."""

    phase: str
    count: int
    median_seconds: float | None
    max_seconds: float | None


@dataclasses.dataclass(frozen=True)
class RoastScore:
    """The full scorecard for one (model, prompt, roast) replay."""

    roast_name: str
    tick_count: int
    ok_count: int
    drop: DropMetrics
    heat: LeverMetrics
    fan: LeverMetrics
    phase_latency: list[PhaseLatency]
    development_time_ratio_truth: float


def _direction(prev: int | None, now: int) -> int | None:
    """Return -1 / 0 / +1 for a setpoint move, or ``None`` with no baseline."""
    if prev is None:
        return None
    delta = now - prev
    if delta >= _DIRECTION_DEADBAND:
        return 1
    if delta <= -_DIRECTION_DEADBAND:
        return -1
    return 0


def _lever_metrics(
    outcomes: list[TickOutcome],
    *,
    real: Callable[[ReplayTick], int],
    rec: Callable[[RoastDecision], int],
    prev_real: Callable[[ReplayTick], int | None],
) -> LeverMetrics:
    """Compute MAE + directional agreement for one lever over the ok ticks."""
    abs_errors: list[float] = []
    dir_hits = 0
    dir_total = 0
    for o in outcomes:
        if o.decision is None:
            continue
        real_now = real(o.tick)
        model_now = rec(o.decision)
        abs_errors.append(abs(model_now - real_now))
        prev = prev_real(o.tick)
        real_dir = _direction(prev, real_now)
        if real_dir is None:
            continue
        model_dir = _direction(prev, model_now)
        dir_total += 1
        if model_dir == real_dir:
            dir_hits += 1
    return LeverMetrics(
        mae=round(statistics.mean(abs_errors), 2) if abs_errors else 0.0,
        directional_agreement=round(dir_hits / dir_total, 3) if dir_total else None,
        directional_samples=dir_total,
    )


def score_roast(outcomes: list[TickOutcome], ground: GroundTruth, roast_name: str) -> RoastScore:
    """Score a replayed roast's recommendations against the known-good roast.

    Computes the drop-decision F1 / precision / recall + drop-timing error,
    heat/fan MAE + directional agreement, and per-phase latency. Only ticks
    that produced a decision contribute to drop/heat/fan; latency is over the
    same ok ticks. See the module docstring for the honest-framing caveat — these
    are agreement metrics, not correctness.

    Args:
        outcomes: Per-tick recommendation + measurement results.
        ground: The roast's known-good ground truth.
        roast_name: A label for the scorecard.

    Returns:
        The :class:`RoastScore`.
    """
    ok = [o for o in outcomes if o.decision is not None]
    tp = fp = fn = 0
    first_drop: float | None = None
    for o in ok:
        assert o.decision is not None  # narrowed by the ok filter
        model_drop = o.decision.should_drop
        real_drop = o.tick.real_should_drop
        if model_drop and real_drop:
            tp += 1
        elif model_drop and not real_drop:
            fp += 1
        elif not model_drop and real_drop:
            fn += 1
        if model_drop and first_drop is None:
            first_drop = o.tick.monotonic_seconds
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    timing_err_s: float | None = None
    timing_err_c: float | None = None
    if first_drop is not None:
        timing_err_s = round(first_drop - ground.drop_seconds, 1)
        model_drop_tick = next(o for o in ok if o.tick.monotonic_seconds == first_drop)
        timing_err_c = round(
            model_drop_tick.tick.context.current_bean_temp_c - ground.drop_temp_c, 1
        )

    drop = DropMetrics(
        precision=round(precision, 3),
        recall=round(recall, 3),
        f1=round(f1, 3),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        first_drop_seconds=first_drop,
        timing_error_seconds=timing_err_s,
        timing_error_c=timing_err_c,
    )
    heat = _lever_metrics(
        ok,
        real=lambda t: t.real_heat_percent,
        rec=lambda d: d.target_heat,
        prev_real=lambda t: t.prev_real_heat_percent,
    )
    fan = _lever_metrics(
        ok,
        real=lambda t: t.real_fan_percent,
        rec=lambda d: d.target_fan,
        prev_real=lambda t: t.prev_real_fan_percent,
    )

    phase_latency: list[PhaseLatency] = []
    for phase in (
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    ):
        lats = [
            o.latency_seconds
            for o in ok
            if o.tick.context.phase is phase and o.latency_seconds is not None
        ]
        phase_latency.append(
            PhaseLatency(
                phase=phase.value,
                count=len(lats),
                median_seconds=round(statistics.median(lats), 2) if lats else None,
                max_seconds=round(max(lats), 2) if lats else None,
            )
        )

    return RoastScore(
        roast_name=roast_name,
        tick_count=len(outcomes),
        ok_count=len(ok),
        drop=drop,
        heat=heat,
        fan=fan,
        phase_latency=phase_latency,
        development_time_ratio_truth=round(ground.development_time_ratio * 100, 1),
    )


async def replay_roast(
    ticks: list[ReplayTick],
    recommender: Recommender,
    *,
    clock: Callable[[], float],
) -> list[TickOutcome]:
    """Run a recommender over every replay tick, capturing advice + latency.

    Args:
        ticks: The reconstructed ticks (from :func:`build_ticks`).
        recommender: The advisor's ``get_recommendation`` (real run) or any
            canned async callable (the key-free test path).
        clock: A monotonic clock returning seconds — ``time.perf_counter`` for a
            real run, or a deterministic fake in tests.

    Returns:
        One :class:`TickOutcome` per tick, in tick order.
    """
    outcomes: list[TickOutcome] = []
    for tick in ticks:
        started = clock()
        try:
            decision = await recommender(tick.context)
        except Exception as exc:  # noqa: BLE001 — capture, score the rest of the roast
            outcomes.append(
                TickOutcome(
                    tick=tick,
                    decision=None,
                    latency_seconds=round(clock() - started, 3),
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            )
            continue
        outcomes.append(
            TickOutcome(
                tick=tick,
                decision=decision,
                latency_seconds=round(clock() - started, 3),
            )
        )
    return outcomes


def score_to_json(score: RoastScore) -> dict[str, Any]:
    """Serialize a :class:`RoastScore` to a JSON-ready dict."""
    return {
        "roast_name": score.roast_name,
        "tick_count": score.tick_count,
        "ok_count": score.ok_count,
        "development_time_ratio_truth": score.development_time_ratio_truth,
        "drop": dataclasses.asdict(score.drop),
        "heat": dataclasses.asdict(score.heat),
        "fan": dataclasses.asdict(score.fan),
        "phase_latency": [dataclasses.asdict(p) for p in score.phase_latency],
    }
