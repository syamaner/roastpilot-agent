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
    if not telemetry:
        raise ValueError(f"fixture {fixture} has no telemetry rows")
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
    target_drop_c_override: float | None = None,
    target_development_percent_override: float | None = None,
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
        target_drop_c_override: If set, the ``target_drop_temp_c`` stamped into
            every context, instead of the roast's actual drop temperature. Lets a
            roast be replayed *as if* a different profile drop target were set — a
            target-sensitivity / counterfactual analysis (e.g. feeding an
            over-dark roast the operator's intended ~195 °C target to isolate the
            advisor's behavior from the actual over-dark drop). Scoring is still
            against the real drop in ``ground``.
        target_development_percent_override: Likewise for
            ``target_development_percent``.

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
            target_drop_temp_c=(
                target_drop_c_override if target_drop_c_override is not None else ground.drop_temp_c
            ),
            target_development_percent=(
                target_development_percent_override
                if target_development_percent_override is not None
                else round(ground.development_time_ratio * 100, 1)
            ),
            charge_guidance_min_c=180.0 if phase is RoastPhase.PREHEATING else None,
            charge_guidance_max_c=200.0 if phase is RoastPhase.PREHEATING else None,
            profile_name=name,
            recent_telemetry_samples=_recent_samples(telemetry, index),
            first_crack_detected=fc_detected,
            first_crack_timestamp_seconds=(
                round(ground.first_crack_seconds - ground.t0_seconds, 3) if fc_detected else None
            ),
            # #497: the real roast's ACTUATED heat/fan at this row — the same
            # values ``ReplayTick.real_heat_percent``/``real_fan_percent`` below
            # score against — so a replayed context matches what the live
            # controller would have populated (never null, never the
            # recommendation being scored). ``post_fc_loop_active`` stays the
            # default False: every recorded fixture predates the deterministic
            # post-FC RoR-taper loop (#405/D88, still flag-off in production),
            # so no replayed tick was ever taper-actuated — every lever value
            # here is either advisor-driven or the deterministic pre-FC lever.
            current_heat_percent=heat,
            current_fan_percent=fan,
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
class DropConfusion:
    """The 2×2 drop-decision confusion matrix over the ok ticks.

    Predicted is the model's ``should_drop`` at a tick; actual is whether the
    tick is at/after the real drop tick (the same ground-truth label the
    F1/precision/recall in :class:`DropMetrics` derive from, so the counts are
    *consistent* with those — ``true_positives``/``false_positives``/
    ``false_negatives`` here equal the matching :class:`DropMetrics` fields, with
    ``true_negatives`` added).

    Honest framing: the per-tick drop classes are heavily imbalanced — almost
    every tick is "no drop yet", so ``true_negatives`` dominates and inflates any
    accuracy read. Read this matrix *with* the drop-timing-error metric, never
    alone.

    Attributes:
        true_positives: Model dropped and the tick was at/after the real drop.
        false_positives: Model dropped while the real roast had not yet dropped.
        true_negatives: Model held and the real roast had not yet dropped.
        false_negatives: Model held on a tick at/after the real drop.
    """

    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def total(self) -> int:
        """Total scored (ok) ticks the matrix reconciles against."""
        return (
            self.true_positives + self.false_positives + self.true_negatives + self.false_negatives
        )


# The ordered heat-direction classes for the 3×3 confusion matrix. Index in this
# tuple is the matrix row/column index; the labels are stable for the report and
# the JSON.
HEAT_DIRECTION_LABELS: tuple[str, str, str] = ("cut", "hold", "raise")


@dataclasses.dataclass(frozen=True)
class HeatDirectionConfusion:
    """The 3×3 heat-direction confusion matrix (cut / hold / raise).

    Per tick (with a previous setpoint), the model's recommended heat change vs
    the previous real setpoint is classified cut / hold / raise (a small
    dead-band around 0), as is the real roast's heat change at that tick; the
    pair indexes one cell. This visualises anticipatory-cut agreement — the more
    informative control-behaviour view than the imbalanced 2×2 drop matrix.

    Attributes:
        labels: The ordered class labels (rows = actual, columns = predicted).
        matrix: A 3×3 count grid; ``matrix[actual][predicted]`` is the number of
            ticks whose real move was ``labels[actual]`` and whose model move was
            ``labels[predicted]``.
        samples: Total ticks classified (ticks with a previous setpoint).
    """

    labels: tuple[str, str, str]
    matrix: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]
    samples: int

    @property
    def agreement(self) -> float | None:
        """Fraction on the diagonal (model move matched the real move)."""
        if self.samples == 0:
            return None
        diagonal = sum(self.matrix[i][i] for i in range(3))
        return round(diagonal / self.samples, 3)


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
    drop_confusion: DropConfusion
    heat_direction_confusion: HeatDirectionConfusion
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


def drop_confusion(outcomes: list[TickOutcome]) -> DropConfusion:
    """Build the 2×2 drop-decision confusion matrix from the ok ticks.

    Predicted = the model's ``should_drop``; actual = the tick's
    ``real_should_drop`` (at/after the real drop tick) — the *same* label
    :func:`score_roast` uses for precision/recall/F1, so the TP/FP/FN counts here
    match the :class:`DropMetrics` fields and only ``true_negatives`` is new.

    Args:
        outcomes: Per-tick outcomes; only ticks with a decision are counted.

    Returns:
        The :class:`DropConfusion` matrix.
    """
    tp = fp = tn = fn = 0
    for o in outcomes:
        if o.decision is None:
            continue
        predicted = o.decision.should_drop
        actual = o.tick.real_should_drop
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
    return DropConfusion(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )


def heat_direction_confusion(outcomes: list[TickOutcome]) -> HeatDirectionConfusion:
    """Build the 3×3 heat-direction (cut / hold / raise) confusion matrix.

    Per tick with a previous real setpoint, classify the model's recommended
    heat (vs that previous setpoint) and the real roast's heat at the tick (vs
    the same previous setpoint) into cut / hold / raise with the shared
    dead-band, and tally the (actual, predicted) cell. Rows are the actual move,
    columns the model's move; the order follows :data:`HEAT_DIRECTION_LABELS`.

    Args:
        outcomes: Per-tick outcomes; only ok ticks with a previous setpoint are
            classified.

    Returns:
        The :class:`HeatDirectionConfusion` matrix.
    """
    # _direction returns -1 / 0 / +1 → map to the row/column index 0 / 1 / 2.
    index_of = {-1: 0, 0: 1, 1: 2}
    grid = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    samples = 0
    for o in outcomes:
        if o.decision is None:
            continue
        prev = o.tick.prev_real_heat_percent
        real_dir = _direction(prev, o.tick.real_heat_percent)
        if real_dir is None:
            continue
        model_dir = _direction(prev, o.decision.target_heat)
        # model_dir is non-None whenever real_dir is (same non-None prev).
        assert model_dir is not None
        grid[index_of[real_dir]][index_of[model_dir]] += 1
        samples += 1
    matrix = (
        (grid[0][0], grid[0][1], grid[0][2]),
        (grid[1][0], grid[1][1], grid[1][2]),
        (grid[2][0], grid[2][1], grid[2][2]),
    )
    return HeatDirectionConfusion(
        labels=HEAT_DIRECTION_LABELS,
        matrix=matrix,
        samples=samples,
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
        drop_confusion=drop_confusion(ok),
        heat_direction_confusion=heat_direction_confusion(ok),
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


def render_drop_confusion_md(confusion: DropConfusion) -> list[str]:
    """Render the 2×2 drop matrix as compact markdown lines.

    The honest-framing note is left to the report header; this just lays the
    counts out as a readable 2×2 with totals so the operator can reconcile it
    against the F1/precision/recall already shown.

    Args:
        confusion: The drop confusion matrix.

    Returns:
        Markdown lines (a small table) for the report.
    """
    c = confusion
    return [
        "    drop confusion (rows = actual drop?, cols = model said drop):",
        "    |            | model: drop | model: hold |",
        "    |------------|-------------|-------------|",
        f"    | real: drop |  TP={c.true_positives:<5}  |  FN={c.false_negatives:<5}  |",
        f"    | real: hold |  FP={c.false_positives:<5}  |  TN={c.true_negatives:<5}  |",
        f"    (total ticks={c.total}; TN dominates — read WITH drop-timing error)",
    ]


def render_heat_direction_confusion_md(confusion: HeatDirectionConfusion) -> list[str]:
    """Render the 3×3 heat-direction matrix as compact markdown lines.

    Rows are the real roast's heat move (cut / hold / raise); columns are the
    model's recommended move. The diagonal is agreement.

    Args:
        confusion: The heat-direction confusion matrix.

    Returns:
        Markdown lines (a small table) for the report.
    """
    labels = confusion.labels
    agreement = confusion.agreement
    agreement_str = "—" if agreement is None else f"{agreement}"
    lines = [
        "    heat-direction confusion (rows = real move, cols = model move):",
        f"    |  real \\ model | {labels[0]:>5} | {labels[1]:>5} | {labels[2]:>5} |",
        "    |--------------|-------|-------|-------|",
    ]
    for i, label in enumerate(labels):
        row = confusion.matrix[i]
        lines.append(f"    | {label:>12} | {row[0]:>5} | {row[1]:>5} | {row[2]:>5} |")
    lines.append(
        f"    (n={confusion.samples}; diagonal agreement={agreement_str} — "
        "the more informative control-behaviour view)"
    )
    return lines


def score_to_json(score: RoastScore) -> dict[str, Any]:
    """Serialize a :class:`RoastScore` to a JSON-ready dict."""
    return {
        "roast_name": score.roast_name,
        "tick_count": score.tick_count,
        "ok_count": score.ok_count,
        "development_time_ratio_truth": score.development_time_ratio_truth,
        "drop": dataclasses.asdict(score.drop),
        "drop_confusion": {
            **dataclasses.asdict(score.drop_confusion),
            "total": score.drop_confusion.total,
        },
        "heat_direction_confusion": {
            "labels": list(score.heat_direction_confusion.labels),
            # rows = actual move, columns = model move (HEAT_DIRECTION_LABELS order).
            "matrix": [list(row) for row in score.heat_direction_confusion.matrix],
            "samples": score.heat_direction_confusion.samples,
            "agreement": score.heat_direction_confusion.agreement,
        },
        "heat": dataclasses.asdict(score.heat),
        "fan": dataclasses.asdict(score.fan),
        "phase_latency": [dataclasses.asdict(p) for p in score.phase_latency],
    }


# --- RP-D joint-objective metric (#711, plan D124) ---------------------------
#
# The operator-ratified "did the roast land BOTH targets" metric over a
# completed roast trace. Unlike the agreement metrics above (which need a
# model decision per tick), this is pure arithmetic over the roast's OUTCOME
# — the achieved drop temperature and DTR vs the profile's targets — so it
# scores a real, replayed, or historical roast with NO LLM call and NO store
# or safety surface. It is the fixed yardstick the #707 joint-drop-objective
# tree is measured against, and it feeds #396 (prompt/model A/B) and #705
# (math-reliability) through the same report path.
#
# Tolerances and weights are the 6 Aug operator ratification (#711): HIT within
# ±3 °C drop temp AND ±2 pp DTR; scalar weighted 50/50. The scalar's literal
# ratified form carries both the 0.5 weights AND a final /2, so a roast sitting
# exactly on the joint-window edge scores 0.5 and a perfect roast 1.0
# (operator-confirmed, 7 Aug).
#
# This is the pure metric CORE (PR-D1): callers pass the authoritative numbers
# and it applies the ratified rule. The DTR passed in MUST be the deterministic
# #219/#220 ``development_percent`` value, never recomputed from raw seconds (the
# #705/c10 lesson), and the targets MUST be the frozen ``profile_json`` targets,
# never the achieved values (``build_ticks`` stamps the achieved outcome into the
# reconstructed context by default, so a fixture context is NOT a safe target
# source). Sourcing those authoritative inputs from the store (profile targets,
# ``development_percent``, drop-event bean temp, and outcome → abnormal
# termination for faulted/aborted/guard-drop roasts) and wiring the per-roast +
# corpus report is PR-D2; this slice ships only the arithmetic.
JOINT_DROP_TEMP_TOL_C = 3.0
JOINT_DTR_TOL_PP = 2.0
JOINT_WEIGHT_DROP_TEMP = 0.5
JOINT_WEIGHT_DTR = 0.5


@dataclasses.dataclass(frozen=True)
class JointWindowScore:
    """The RP-D joint-objective scorecard for one completed roast (#711).

    Attributes:
        hit: The binary primary result — ``True`` only when the roast dropped
            within the drop-temp tolerance AND within the DTR band AND
            terminated normally. An abnormal termination (guard-drop,
            emergency stop, fault) is never a hit, even if the numbers
            coincidentally fall in range: a hard-stop-forced drop did not
            *land* the joint window under control (fail-closed).
        scalar: The secondary [0, 1] score that ranks non-hits (1.0 perfect,
            0.5 at the exact window edge), clamped at 0 and zeroed by an
            abnormal termination. Do not read it without ``hit`` — a prompt
            tuned to the scalar alone could learn to drop early-and-cool (the
            #711 Goodhart risk); the binary HIT and the D42 operator rating
            stay primary.
        drop_temp_error_c: Signed achieved − target drop temperature (negative
            = dropped short of target).
        dtr_error_pp: Signed achieved − target DTR in percentage points
            (positive = over-developed).
        terminated_abnormally: Whether the roast ended in a guard-drop,
            emergency stop, or fault (the termination penalty was applied).
        drop_temp_c: The achieved drop bean temperature (°C), echoed for the
            report.
        target_drop_temp_c: The profile's target drop temperature (°C).
        dtr_percent: The achieved DTR as a percentage.
        target_dtr_percent: The profile's target DTR as a percentage.
    """

    hit: bool
    scalar: float
    drop_temp_error_c: float
    dtr_error_pp: float
    terminated_abnormally: bool
    drop_temp_c: float
    target_drop_temp_c: float
    dtr_percent: float
    target_dtr_percent: float


def joint_window_score(
    *,
    drop_temp_c: float,
    target_drop_temp_c: float,
    dtr_percent: float,
    target_dtr_percent: float,
    terminated_abnormally: bool = False,
    tol_temp_c: float = JOINT_DROP_TEMP_TOL_C,
    tol_dtr_pp: float = JOINT_DTR_TOL_PP,
    weight_temp: float = JOINT_WEIGHT_DROP_TEMP,
    weight_dtr: float = JOINT_WEIGHT_DTR,
) -> JointWindowScore:
    """Score a roast's achieved outcome against its joint drop targets (#711).

    Pure arithmetic — no LLM, no store, no safety surface. The caller passes the
    achieved drop temperature and DTR (both read from the deterministic trace,
    never recomputed here) and the profile's targets; this applies the
    operator-ratified HIT rule and scalar.

    Args:
        drop_temp_c: The achieved drop bean temperature in Celsius.
        target_drop_temp_c: The profile's target drop temperature in Celsius.
        dtr_percent: The achieved development-time ratio as a percentage (e.g.
            ``21.0``), read from the deterministic #219/#220 clock — not
            recomputed from raw seconds (the #705/c10 discipline).
        target_dtr_percent: The profile's target development percentage.
        terminated_abnormally: ``True`` when the roast ended in a guard-drop,
            emergency stop, or fault; zeroes the scalar and blocks a HIT.
        tol_temp_c: The drop-temp HIT tolerance (default 3 °C, ratified).
        tol_dtr_pp: The DTR HIT tolerance in percentage points (default 2 pp,
            ratified).
        weight_temp: The drop-temp scalar weight (default 0.5, ratified).
        weight_dtr: The DTR scalar weight (default 0.5, ratified).

    Returns:
        The :class:`JointWindowScore`.

    Raises:
        ValueError: If a tolerance is not strictly positive (a zero tolerance
            would divide by zero in the scalar).
    """
    if tol_temp_c <= 0.0 or tol_dtr_pp <= 0.0:
        raise ValueError("joint-window tolerances must be strictly positive")
    drop_err = drop_temp_c - target_drop_temp_c
    dtr_err = dtr_percent - target_dtr_percent
    within_temp = abs(drop_err) <= tol_temp_c
    within_dtr = abs(dtr_err) <= tol_dtr_pp
    hit = within_temp and within_dtr and not terminated_abnormally
    # Literal ratified form: weights AND a final /2 (window edge → 0.5). Clamp at
    # 0 so a large miss floors rather than going negative; an abnormal
    # termination multiplies the whole thing to 0.
    raw = (
        1.0
        - (weight_temp * abs(drop_err) / tol_temp_c + weight_dtr * abs(dtr_err) / tol_dtr_pp) / 2.0
    )
    penalty = 0.0 if terminated_abnormally else 1.0
    scalar = max(0.0, raw) * penalty
    return JointWindowScore(
        hit=hit,
        scalar=scalar,
        drop_temp_error_c=drop_err,
        dtr_error_pp=dtr_err,
        terminated_abnormally=terminated_abnormally,
        drop_temp_c=drop_temp_c,
        target_drop_temp_c=target_drop_temp_c,
        dtr_percent=dtr_percent,
        target_dtr_percent=target_dtr_percent,
    )


def joint_score_to_json(score: JointWindowScore) -> dict[str, Any]:
    """Serialize a :class:`JointWindowScore` to a JSON-ready dict.

    Parallels :func:`score_to_json` for the agreement scorecard, so the RP-D
    metric flows through the same per-roast report path (and into
    ``advisor_significance.py``'s paired stats over any per-roast scalar).

    Args:
        score: The joint-window scorecard to serialize.

    Returns:
        A JSON-ready dict of the scorecard.
    """
    return {
        "hit": score.hit,
        "scalar": round(score.scalar, 4),
        "drop_temp_c": score.drop_temp_c,
        "target_drop_temp_c": score.target_drop_temp_c,
        "drop_temp_error_c": round(score.drop_temp_error_c, 2),
        "dtr_percent": round(score.dtr_percent, 2),
        "target_dtr_percent": score.target_dtr_percent,
        "dtr_error_pp": round(score.dtr_error_pp, 2),
        "terminated_abnormally": score.terminated_abnormally,
    }
