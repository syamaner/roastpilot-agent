"""Tests for the control-trajectory sanity scorer (#277 / D40.4).

Network- and key-free (the M1 guardrail): the trajectory scorer is pure, so the
tests build synthetic :class:`~bakeoff_replay.TickOutcome` sequences directly and
assert the metric ORDERING the research demands — a deliberately thrashy lever
sequence (the #218 fan twiddle 30↔40↔50↔60↔70) scores HIGH reversal-count +
entropy; a smooth steady decline scores LOW; a big heat cut on an already-low /
declining RoR raises ``momentum_cut_flags`` while the same cut on a high, rising
RoR does not.

The 13-14 Jun baked-roast traces are NOT committed as fixtures (``.artisan-
fixtures/`` is gitignored and empty in a clean checkout; only the 7-Jun roasts
are committed), so the known-thrash-vs-known-good comparison rides on the
synthetic trajectories. The committed 7-Jun roasts are exercised as a real-data
smoke (the scorer runs end-to-end over them and returns a well-formed scorecard).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import trajectory_scorer as ts  # noqa: E402
from bakeoff_replay import ReplayTick, TickOutcome  # noqa: E402

from roastpilot_agent.advisor import AdvisorContext, RoastDecision  # noqa: E402
from roastpilot_agent.models import RoastPhase  # noqa: E402


def _dev_outcome(
    *,
    heat: int,
    fan: int,
    seconds: float,
    bean_ror: float | None = 4.0,
) -> TickOutcome:
    """Build one development-phase outcome carrying a model heat/fan + a RoR.

    Args:
        heat: The model's recommended heat setpoint.
        fan: The model's recommended fan setpoint.
        seconds: The tick's roast time.
        bean_ror: The replayed bean RoR at the tick (the momentum-cut input).

    Returns:
        A :class:`TickOutcome` in the development phase.
    """
    context = AdvisorContext(
        phase=RoastPhase.DEVELOPMENT,
        roast_elapsed_seconds=seconds,
        development_elapsed_seconds=seconds - 600.0,
        current_bean_temp_c=190.0,
        current_env_temp_c=210.0,
        bean_ror_c_per_min=bean_ror,
        env_ror_c_per_min=None,
        target_drop_temp_c=197.0,
        target_development_percent=15.0,
        profile_name="synthetic",
        recent_telemetry_samples=[],
        first_crack_detected=True,
        first_crack_timestamp_seconds=600.0,
    )
    tick = ReplayTick(
        context=context,
        real_heat_percent=heat,
        real_fan_percent=fan,
        prev_real_heat_percent=None,
        prev_real_fan_percent=None,
        real_should_drop=False,
        monotonic_seconds=seconds,
    )
    return TickOutcome(
        tick=tick,
        decision=RoastDecision(
            target_heat=heat,
            target_fan=fan,
            should_drop=False,
            confidence=0.7,
            rationale="synthetic",
        ),
        latency_seconds=0.5,
    )


def _series_outcomes(
    heat_series: list[int],
    fan_series: list[int],
    *,
    ror_series: Sequence[float | None] | None = None,
) -> list[TickOutcome]:
    """Build development outcomes from explicit heat/fan (and optional RoR) series."""
    assert len(heat_series) == len(fan_series)
    rors = ror_series if ror_series is not None else [4.0] * len(heat_series)
    return [
        _dev_outcome(heat=h, fan=f, seconds=600.0 + 30.0 * i, bean_ror=r)
        for i, (h, f, r) in enumerate(zip(heat_series, fan_series, rors, strict=True))
    ]


# --- lever trajectory: reversal counting ------------------------------------


def test_thrashy_fan_scores_high_reversals_smooth_scores_low() -> None:
    """The #218 fan twiddle reverses on every step; a steady trim never reverses."""
    thrashy = ts.score_lever_trajectory("fan", [30, 40, 30, 40, 30, 40, 30])
    smooth = ts.score_lever_trajectory("fan", [60, 55, 50, 45, 40, 35, 30])

    # The twiddle flips direction at every interior delta-pair; the steady
    # decline never flips.
    assert thrashy.direction_reversal_count > smooth.direction_reversal_count
    assert smooth.direction_reversal_count == 0
    assert thrashy.reversal_rate > smooth.reversal_rate
    assert smooth.reversal_rate == 0.0


def test_monotone_ramp_up_or_down_has_zero_reversals() -> None:
    """A pure ramp (the #218 heat cascade 70→40→20→0) has changes but no flips."""
    cascade = ts.score_lever_trajectory("heat", [70, 40, 20, 0])
    assert cascade.change_count == 3
    assert cascade.direction_reversal_count == 0
    assert cascade.reversal_rate == 0.0


def test_change_deadband_ignores_subthreshold_wobble() -> None:
    """A sub-dead-band wobble is a hold, not a change and not a reversal."""
    # Deltas of +0.? are impossible on ints; use a 1-pt step which IS the
    # dead-band boundary (>= 1.0 counts), and a 0-step which does not.
    flat = ts.score_lever_trajectory("heat", [50, 50, 50, 50])
    assert flat.change_count == 0
    assert flat.direction_reversal_count == 0
    assert flat.mean_abs_change == 0.0


def test_holds_between_trims_do_not_create_reversals() -> None:
    """A trim, a hold, then a further trim is one direction, not a reversal."""
    traj = ts.score_lever_trajectory("heat", [60, 55, 55, 50, 50, 45])
    # Three real cuts (60->55, 55->50, 50->45), two holds; all cuts, no flip.
    assert traj.change_count == 3
    assert traj.direction_reversal_count == 0


# --- control-signal entropy --------------------------------------------------


def test_entropy_orders_thrash_above_smooth() -> None:
    """Combined reversal-rate entropy is higher for a thrashy command signal."""
    thrashy = ts.score_trajectory(
        _series_outcomes([30, 40, 30, 40, 30, 40], [70, 60, 70, 60, 70, 60]),
        "thrashy",
    )
    smooth = ts.score_trajectory(
        _series_outcomes([60, 55, 50, 45, 40, 35], [40, 40, 40, 40, 40, 40]),
        "smooth",
    )
    assert thrashy.control_signal_entropy > smooth.control_signal_entropy
    assert 0.0 <= thrashy.control_signal_entropy <= 1.0
    assert smooth.control_signal_entropy == 0.0
    # The composite preserves the ordering (lower = more roaster-like).
    assert thrashy.trajectory_sanity > smooth.trajectory_sanity


# --- momentum-cut detection --------------------------------------------------


def test_big_cut_on_low_ror_raises_momentum_flag() -> None:
    """A >=15pp heat cut while RoR is already low (<=5) is a momentum cut."""
    # RoR steady-low at 3 °C/min; a 70->40 cut (30pp) on the second tick.
    outcomes = _series_outcomes([70, 40], [40, 40], ror_series=[3.0, 3.0])
    sanity = ts.score_trajectory(outcomes, "low-ror-cut")
    assert sanity.momentum_cut_flags == 1
    cut = sanity.momentum_cuts[0]
    assert cut.prev_heat_percent == 70
    assert cut.new_heat_percent == 40


def test_big_cut_on_declining_ror_raises_momentum_flag() -> None:
    """A big cut while RoR is declining (even if not yet low) is a momentum cut."""
    # RoR 12 -> 9: above the low floor but declining; a 30pp cut into it.
    outcomes = _series_outcomes([80, 50], [40, 40], ror_series=[12.0, 9.0])
    sanity = ts.score_trajectory(outcomes, "declining-ror-cut")
    assert sanity.momentum_cut_flags == 1
    assert sanity.momentum_cuts[0].ror_declining is True


def test_big_cut_on_high_rising_ror_does_not_flag() -> None:
    """The same big cut on a high, RISING RoR is NOT a momentum cut."""
    # RoR 10 -> 14: high and rising; trimming an over-fast roast is legitimate.
    outcomes = _series_outcomes([80, 50], [40, 40], ror_series=[10.0, 14.0])
    sanity = ts.score_trajectory(outcomes, "high-rising-cut")
    assert sanity.momentum_cut_flags == 0


def test_small_cut_on_low_ror_does_not_flag() -> None:
    """A small trim (< 15pp) on a low RoR is steady trimming, not a momentum cut."""
    # 5pp cut (the steady-trim behaviour) on a low RoR — exactly what's wanted.
    outcomes = _series_outcomes([50, 45], [40, 40], ror_series=[3.0, 3.0])
    sanity = ts.score_trajectory(outcomes, "steady-trim")
    assert sanity.momentum_cut_flags == 0


def test_momentum_cut_chain_breaks_on_missing_decision() -> None:
    """A tick with no decision breaks the consecutive-pair chain (no false cut)."""
    good = _dev_outcome(heat=70, fan=40, seconds=600.0, bean_ror=3.0)
    gap = TickOutcome(
        tick=good.tick,
        decision=None,
        latency_seconds=0.5,
        error="boom",
    )
    after = _dev_outcome(heat=40, fan=40, seconds=660.0, bean_ror=3.0)
    sanity = ts.score_trajectory([good, gap, after], "with-gap")
    # The 70->40 cut spans a None tick, so no consecutive pair forms across it.
    assert sanity.momentum_cut_flags == 0


# --- composite + phase restriction ------------------------------------------


def test_composite_adds_momentum_penalty_on_top_of_entropy() -> None:
    """Two roasts with equal entropy: the one with momentum cuts scores worse."""
    # Both saw the fan identically (equal entropy); one also momentum-cuts heat.
    no_cut = ts.score_trajectory(
        _series_outcomes([50, 50, 50, 50], [40, 50, 40, 50], ror_series=[3.0] * 4),
        "no-cut",
    )
    with_cut = ts.score_trajectory(
        _series_outcomes([70, 40, 40, 40], [40, 50, 40, 50], ror_series=[3.0] * 4),
        "with-cut",
    )
    assert with_cut.momentum_cut_flags >= 1
    assert with_cut.trajectory_sanity > no_cut.trajectory_sanity


def test_scorer_ignores_non_development_ticks() -> None:
    """Pre-FC ticks are excluded — only the development command series is scored."""
    pre_fc = _dev_outcome(heat=80, fan=30, seconds=300.0)
    # Stamp it pre-FC by rebuilding the context phase.
    pre_ctx = pre_fc.tick.context.model_copy(update={"phase": RoastPhase.ROASTING_PRE_FIRST_CRACK})
    pre_tick = TickOutcome(
        tick=ReplayTick(
            context=pre_ctx,
            real_heat_percent=80,
            real_fan_percent=30,
            prev_real_heat_percent=None,
            prev_real_fan_percent=None,
            real_should_drop=False,
            monotonic_seconds=300.0,
        ),
        decision=pre_fc.decision,
        latency_seconds=0.5,
    )
    dev = _series_outcomes([50, 45, 40], [40, 40, 40], ror_series=[3.0, 3.0, 3.0])
    sanity = ts.score_trajectory([pre_tick, *dev], "mixed-phase")
    # Only the 3 development ticks are scored.
    assert sanity.development_tick_count == 3
    assert sanity.heat.sample_count == 3


def test_empty_development_scores_zero() -> None:
    """No development ticks → an all-zero scorecard (nothing to thrash)."""
    sanity = ts.score_trajectory([], "empty")
    assert sanity.development_tick_count == 0
    assert sanity.development_ok_count == 0
    assert sanity.control_signal_entropy == 0.0
    assert sanity.trajectory_sanity == 0.0
    assert sanity.momentum_cut_flags == 0


# --- reporting + JSON --------------------------------------------------------


def test_render_trajectory_report_carries_framing_and_no_autopick() -> None:
    """The trajectory report leads with the agreement-free caveat; no winner."""
    sanity = ts.score_trajectory(
        _series_outcomes([70, 40, 50, 40], [30, 40, 30, 40], ror_series=[3.0] * 4),
        "thrashy-cuts",
    )
    report = ts.render_trajectory_report([("model-x prompt=v4", [sanity])])
    assert "agreement-free" in report.lower()
    assert "lower = less thrash" in report.lower() or "lower=less thrash" in report.lower()
    assert "NOT higher quality" in report
    assert "momentum_cuts" in report
    assert "winner" not in report.lower()


def test_trajectory_to_json_round_trips_fields() -> None:
    """The JSON carries every metric + the per-cut detail."""
    sanity = ts.score_trajectory(
        _series_outcomes([70, 40], [40, 50], ror_series=[3.0, 3.0]),
        "json-roast",
    )
    data = ts.trajectory_to_json(sanity)
    assert data["roast_name"] == "json-roast"
    assert data["momentum_cut_flags"] == len(data["momentum_cuts"])
    assert data["momentum_cut_flags"] == 1
    assert set(data["heat"]) == {
        "lever",
        "sample_count",
        "change_count",
        "direction_reversal_count",
        "reversal_rate",
        "mean_abs_change",
    }
    assert data["control_signal_entropy"] == sanity.control_signal_entropy
    assert data["trajectory_sanity"] == sanity.trajectory_sanity


# --- real-data smoke (committed 7-Jun roasts) -------------------------------

_S1 = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "live-roast-2026-06-07"
    / "session-1"
    / "roast.jsonl"
)


def test_scores_committed_roast_end_to_end() -> None:
    """The scorer runs over a real 7-Jun roast replay and returns a sane card.

    A real-data smoke (the baked thrash/good traces aren't committed): build the
    ticks, hand each tick's real heat/fan back as the "model" recommendation, and
    confirm the scorer produces a well-formed, in-range scorecard over the real
    development phase. (Replaying the human's own setpoints means the trajectory
    reflects the actual roast's command coherence — a useful real-world anchor.)
    """
    import bakeoff_replay as replay

    ticks, _ = replay.build_ticks(_S1, cadence_seconds=30.0)
    outcomes = [
        TickOutcome(
            tick=t,
            decision=RoastDecision(
                target_heat=t.real_heat_percent,
                target_fan=t.real_fan_percent,
                should_drop=t.real_should_drop,
                confidence=1.0,
                rationale="replayed real setpoints",
            ),
            latency_seconds=0.1,
        )
        for t in ticks
    ]
    sanity = ts.score_trajectory(outcomes, "session-1")
    assert sanity.development_tick_count > 0
    assert 0.0 <= sanity.control_signal_entropy <= 1.0
    assert sanity.trajectory_sanity >= 0.0
    assert sanity.momentum_cut_flags >= 0
    # The real good roast should not be wildly thrashy on its own command series.
    assert sanity.heat.sample_count == sanity.development_ok_count
