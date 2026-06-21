"""Tune the post-FC coherence deadband threshold from the operator's roasts (#277).

Data-grounded, deterministic, NO API key, NO network. This script measures the
operator's OWN post-first-crack lever behaviour on the 17 known-good medium
Artisan fixtures and replays it through the production coherence gate
(:func:`roastpilot_agent.coherence.evaluate_lever_coherence`) at a sweep of
candidate thresholds, so the placeholder
``ControllerConfig.post_fc_deadband_threshold_percent`` (default 15) can be set
from real behaviour rather than a guess.

The #218 thesis the gate implements: damp incoherent flip-flop (the 30<->40<->50
staircase) but ALLOW the operator's intentional, decisive moves. So the right
threshold is the LARGEST value that still passes essentially all of the
operator's real direction-reversals (we never want to damp an intentional move)
while still catching sub-threshold jitter.

What it computes, per lever (heat, fan), across all 17 roasts:

1. The DEVELOPMENT-phase (post-FC -> drop) lever COMMAND sequence — the
   operator's actual setpoint moves, taken from the raw fixture telemetry between
   the ``first_crack_detected`` and ``beans_dropped`` events (the same phase
   boundaries the bake-off's ground truth uses, via
   :func:`bakeoff_replay.load_roast`).
2. The distribution of consecutive non-zero MOVE magnitudes, and specifically
   the direction-REVERSAL events (a move whose sign opposes the lever's prior
   non-zero direction) and their magnitudes + frequency.
3. A per-threshold (5, 8, 10, 12, 15, 20) replay through the real gate: how many
   of the operator's reversals are DAMPED (bad — those are intentional moves) and
   how many sub-threshold reversals would be caught as jitter.

Run::

    python scripts/deadband_tune.py            # human-readable report
    python scripts/deadband_tune.py --json      # machine aggregates (no raw data)

It reads the LOCAL-ONLY ``.artisan-fixtures`` (gitignored) and emits ONLY
aggregates — never the raw roast data.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advisor_bakeoff import FULL_MEDIUM_FIXTURE_NAMES, resolve_test_set  # noqa: E402
from bakeoff_replay import load_roast  # noqa: E402

from roastpilot_agent.coherence import (  # noqa: E402
    CoherenceDecision,
    LeverDirection,
    evaluate_lever_coherence,
)

# The candidate thresholds the brief sweeps (percentage points).
CANDIDATE_THRESHOLDS: tuple[int, ...] = (5, 8, 10, 12, 15, 20)

# The levers analysed, by their telemetry field name.
LEVERS: tuple[tuple[str, str], ...] = (
    ("heat", "heat_level_percent"),
    ("fan", "fan_level_percent"),
)


@dataclass(frozen=True)
class Move:
    """One consecutive change in a lever setpoint during development.

    Attributes:
        delta: Signed change (new - prior) in percentage points; never zero (a
            hold is not a move).
        reverses: ``True`` if this move's sign opposes the lever's prior
            non-zero move direction (a direction reversal); ``False`` for a
            sustained same-direction move or the lever's first move.
    """

    delta: int
    reverses: bool


def development_setpoints(fixture: Path, field: str) -> list[int]:
    """Return the operator's lever setpoints during development (post-FC -> drop).

    Walks the raw fixture telemetry and keeps every row whose timestamp is at or
    after the ``first_crack_detected`` event and at or before the
    ``beans_dropped`` event — the agent DEVELOPMENT phase, the as-built advisor
    scope (D35). The FC / drop boundaries come from
    :func:`bakeoff_replay.load_roast` so they match the eval's ground truth.

    Args:
        fixture: The live-roast ``roast.jsonl`` to read.
        field: The telemetry setpoint field (``heat_level_percent`` /
            ``fan_level_percent``).

    Returns:
        The lever setpoints in recorded (time) order across development.
    """
    telemetry, ground = load_roast(fixture)
    setpoints: list[int] = []
    for row in telemetry:
        mono = float(row["monotonic_seconds"])
        if ground.first_crack_seconds <= mono <= ground.drop_seconds:
            setpoints.append(int(row[field]))
    return setpoints


def moves_of(setpoints: list[int]) -> list[Move]:
    """Reduce a setpoint series to its non-zero moves, flagging reversals.

    A move is a consecutive change between distinct setpoints; equal-valued
    consecutive rows (a hold) produce no move. A move REVERSES when its sign
    opposes the lever's most recent non-zero move direction (the lever's first
    move never reverses — there is no prior direction).

    Args:
        setpoints: The lever setpoint series in time order.

    Returns:
        The ordered list of :class:`Move` events.
    """
    moves: list[Move] = []
    last_direction: LeverDirection = LeverDirection.NONE
    prev = setpoints[0] if setpoints else None
    for value in setpoints[1:]:
        delta = value - (prev if prev is not None else value)
        prev = value
        if delta == 0:
            continue
        direction = LeverDirection.UP if delta > 0 else LeverDirection.DOWN
        reverses = last_direction is not LeverDirection.NONE and direction is not last_direction
        moves.append(Move(delta=delta, reverses=reverses))
        last_direction = direction
    return moves


@dataclass(frozen=True)
class LeverDistribution:
    """Aggregate move / reversal statistics for one lever across all roasts."""

    lever: str
    total_moves: int
    move_magnitudes: list[int]
    reversal_magnitudes: list[int]
    roasts_with_any_reversal: int
    roasts_total: int


def summarise_lever(lever: str, per_roast_moves: list[list[Move]]) -> LeverDistribution:
    """Aggregate the per-roast moves for one lever into a distribution."""
    move_magnitudes: list[int] = []
    reversal_magnitudes: list[int] = []
    roasts_with_reversal = 0
    for moves in per_roast_moves:
        move_magnitudes.extend(abs(m.delta) for m in moves)
        roast_reversals = [abs(m.delta) for m in moves if m.reverses]
        reversal_magnitudes.extend(roast_reversals)
        if roast_reversals:
            roasts_with_reversal += 1
    return LeverDistribution(
        lever=lever,
        total_moves=len(move_magnitudes),
        move_magnitudes=sorted(move_magnitudes),
        reversal_magnitudes=sorted(reversal_magnitudes),
        roasts_with_any_reversal=roasts_with_reversal,
        roasts_total=len(per_roast_moves),
    )


@dataclass(frozen=True)
class ThresholdResult:
    """Per-threshold replay outcome for one lever.

    Attributes:
        threshold: The candidate threshold (percentage points).
        reversals_damped: How many of the operator's real reversals the gate
            DAMPS at this threshold (intentional moves we'd suppress — want 0).
        reversals_allowed: How many of the operator's real reversals the gate
            ALLOWS at this threshold.
        sub_threshold_reversals: How many reversals are below the threshold (the
            jitter the gate is designed to catch).
    """

    threshold: int
    reversals_damped: int
    reversals_allowed: int
    sub_threshold_reversals: int


def replay_threshold(per_roast_setpoints: list[list[int]], threshold: int) -> ThresholdResult:
    """Replay every operator development sequence through the gate at ``threshold``.

    Feeds each roast's setpoint series through the production
    :func:`evaluate_lever_coherence` exactly as the live loop would — carrying the
    gate's returned direction forward tick to tick — and counts how the
    operator's REAL reversals fare. A reversal that the gate ``DAMP``s is an
    intentional operator move we would have suppressed (the failure mode to
    avoid); the count of sub-threshold reversals is the jitter the threshold is
    meant to catch.

    Args:
        per_roast_setpoints: The development setpoint series for each roast.
        threshold: The candidate threshold to replay at.

    Returns:
        The aggregated :class:`ThresholdResult`.
    """
    damped = 0
    allowed = 0
    sub_threshold = 0
    for setpoints in per_roast_setpoints:
        current = setpoints[0] if setpoints else 0
        last_direction = LeverDirection.NONE
        prev_direction = LeverDirection.NONE
        for requested in setpoints[1:]:
            delta = requested - current
            if delta != 0:
                direction = LeverDirection.UP if delta > 0 else LeverDirection.DOWN
                is_reversal = (
                    prev_direction is not LeverDirection.NONE and direction is not prev_direction
                )
                if is_reversal:
                    if abs(delta) < threshold:
                        sub_threshold += 1
                    result = evaluate_lever_coherence(
                        requested=requested,
                        current=current,
                        last_direction=last_direction,
                        threshold_percent=threshold,
                    )
                    if result.decision is CoherenceDecision.DAMP:
                        damped += 1
                    else:
                        allowed += 1
                else:
                    result = evaluate_lever_coherence(
                        requested=requested,
                        current=current,
                        last_direction=last_direction,
                        threshold_percent=threshold,
                    )
                prev_direction = direction
                # The gate returns the recorded direction (advanced even on a
                # damp); thread it forward exactly as the live loop does.
                last_direction = result.direction
                current = result.applied_value
    return ThresholdResult(
        threshold=threshold,
        reversals_damped=damped,
        reversals_allowed=allowed,
        sub_threshold_reversals=sub_threshold,
    )


def _percentile(values: list[int], pct: float) -> float:
    """Return the ``pct`` percentile (0-100) of a sorted-or-unsorted int list."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _summary_stats(values: list[int]) -> dict[str, float | int]:
    """Compact descriptive stats for a magnitude list (empty-safe)."""
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "p90": round(_percentile(values, 90), 1),
        "max": max(values),
        "mean": round(statistics.fmean(values), 2),
    }


def analyse() -> dict[str, Any]:
    """Run the full analysis over the 17 known-good mediums; return aggregates."""
    fixtures = resolve_test_set(FULL_MEDIUM_FIXTURE_NAMES)
    distributions: dict[str, LeverDistribution] = {}
    per_lever_setpoints: dict[str, list[list[int]]] = {}
    for lever, field in LEVERS:
        per_roast_moves: list[list[Move]] = []
        per_roast_setpoints: list[list[int]] = []
        for fixture in fixtures:
            setpoints = development_setpoints(fixture, field)
            per_roast_setpoints.append(setpoints)
            per_roast_moves.append(moves_of(setpoints))
        distributions[lever] = summarise_lever(lever, per_roast_moves)
        per_lever_setpoints[lever] = per_roast_setpoints

    threshold_table: dict[str, list[ThresholdResult]] = {}
    for lever, _field in LEVERS:
        threshold_table[lever] = [
            replay_threshold(per_lever_setpoints[lever], t) for t in CANDIDATE_THRESHOLDS
        ]

    return {
        "fixtures": list(FULL_MEDIUM_FIXTURE_NAMES),
        "distributions": {
            lever: {
                "total_moves": d.total_moves,
                "roasts_with_any_reversal": d.roasts_with_any_reversal,
                "roasts_total": d.roasts_total,
                "move_magnitude_stats": _summary_stats(d.move_magnitudes),
                "reversal_magnitude_stats": _summary_stats(d.reversal_magnitudes),
                "reversal_magnitudes": d.reversal_magnitudes,
            }
            for lever, d in distributions.items()
        },
        "threshold_table": {
            lever: [
                {
                    "threshold": r.threshold,
                    "reversals_damped": r.reversals_damped,
                    "reversals_allowed": r.reversals_allowed,
                    "sub_threshold_reversals": r.sub_threshold_reversals,
                }
                for r in results
            ]
            for lever, results in threshold_table.items()
        },
    }


def _render(aggregates: dict[str, Any]) -> str:
    """Render the aggregates as a human-readable report."""
    lines: list[str] = []
    lines.append("# Post-FC deadband threshold tuning (operator's recorded roasts)")
    lines.append("")
    lines.append(f"Test set: {len(aggregates['fixtures'])} known-good medium Artisan roasts.")
    lines.append("")
    for lever in ("heat", "fan"):
        dist = aggregates["distributions"][lever]
        lines.append(f"## {lever.upper()} lever")
        lines.append("")
        lines.append(
            f"- moves: {dist['total_moves']} non-zero moves across {dist['roasts_total']} roasts"
        )
        ms = dist["move_magnitude_stats"]
        if ms.get("count"):
            lines.append(
                f"- move magnitude (pp): min {ms['min']}, median {ms['median']}, "
                f"p90 {ms['p90']}, max {ms['max']}, mean {ms['mean']}"
            )
        rs = dist["reversal_magnitude_stats"]
        lines.append(
            f"- direction reversals: {rs.get('count', 0)} total, in "
            f"{dist['roasts_with_any_reversal']}/{dist['roasts_total']} roasts"
        )
        if rs.get("count"):
            lines.append(
                f"- reversal magnitude (pp): min {rs['min']}, median {rs['median']}, "
                f"p90 {rs['p90']}, max {rs['max']}, mean {rs['mean']}"
            )
            lines.append(f"- reversal magnitudes: {dist['reversal_magnitudes']}")
        lines.append("")
        lines.append("| threshold | reversals damped | reversals allowed | sub-threshold |")
        lines.append("| --- | --- | --- | --- |")
        for row in aggregates["threshold_table"][lever]:
            lines.append(
                f"| {row['threshold']} | {row['reversals_damped']} | "
                f"{row['reversals_allowed']} | {row['sub_threshold_reversals']} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the report (or ``--json`` aggregates)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine aggregates")
    args = parser.parse_args(argv)
    aggregates = analyse()
    if args.json:
        print(json.dumps(aggregates, indent=2, default=str))
    else:
        print(_render(aggregates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
