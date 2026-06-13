"""Tests for the real-roast replay + scoring machinery (#172/#173).

Network- and key-free (the M1 guardrail): the recommender is a canned async
callable, the clock is deterministic, and the replay runs against the committed
known-good 7-Jun roast fixtures. They assert that the replay reconstructs
phase-correct grounded ticks and that the scoring math (drop F1 / timing,
heat/fan MAE + directional agreement, per-phase latency) computes correctly —
including the honest-framing-relevant edge cases (a perfect dropper scores 1.0,
an early dropper is penalised on precision + timing).
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bakeoff_replay as replay  # noqa: E402

from roastpilot_agent.advisor import AdvisorContext, RoastDecision  # noqa: E402
from roastpilot_agent.models import RoastPhase  # noqa: E402

_S1 = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "live-roast-2026-06-07"
    / "session-1"
    / "roast.jsonl"
)


def _deterministic_clock(step: float = 0.5) -> Callable[[], float]:
    """A monotonic clock advancing ``step`` seconds per call."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += step
        return state["t"]

    return clock


def _const_recommender(
    decision: RoastDecision,
) -> Callable[[AdvisorContext], Awaitable[RoastDecision]]:
    """A recommender returning the same decision every tick."""

    async def recommend(context: AdvisorContext) -> RoastDecision:
        return decision

    return recommend


# --- load_roast / ground truth ----------------------------------------------


def test_load_roast_computes_known_good_ground_truth() -> None:
    """Session-1 ground truth matches the operator's recorded values."""
    _, ground = replay.load_roast(_S1)
    # FC 181 °C, drop 197 °C, DTR 15.0% (operator ground truth, 7 Jun).
    assert ground.drop_temp_c == 197.0
    assert round(ground.development_time_ratio * 100, 1) == 15.0
    assert ground.first_crack_seconds < ground.drop_seconds


def test_load_roast_rejects_fixture_missing_events(tmp_path: Path) -> None:
    """A fixture without the charge/FC/drop events raises a clear error."""
    fixture = tmp_path / "roast.jsonl"
    fixture.write_text(
        '{"type": "telemetry", "monotonic_seconds": 1.0, "bean_temp_c": 24.0, '
        '"env_temp_c": 24.0, "heat_level_percent": 0, "fan_level_percent": 0}'
    )
    with pytest.raises(ValueError, match="lacks required events"):
        replay.load_roast(fixture)


def test_load_roast_rejects_fixture_with_no_telemetry(tmp_path: Path) -> None:
    """A fixture with the events but zero telemetry rows raises a clear error."""
    fixture = tmp_path / "roast.jsonl"
    fixture.write_text(
        "\n".join(
            [
                '{"type": "event", "kind": "beans_added", "monotonic_seconds": 0.0}',
                '{"type": "event", "kind": "first_crack_detected", "monotonic_seconds": 500.0}',
                '{"type": "event", "kind": "beans_dropped", "monotonic_seconds": 600.0}',
            ]
        )
    )
    with pytest.raises(ValueError, match="no telemetry rows"):
        replay.load_roast(fixture)


# --- build_ticks -------------------------------------------------------------


def test_build_ticks_are_phase_correct_and_grounded() -> None:
    """Ticks span all three phases, FC flips at first crack, drop tick labelled."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)
    assert ticks, "replay must produce ticks"

    phases = {t.context.phase for t in ticks}
    assert phases == {
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    }
    # FC flag is set exactly for ticks at/after first crack.
    for t in ticks:
        expected = t.monotonic_seconds >= ground.first_crack_seconds
        assert t.context.first_crack_detected is expected
        if expected:
            assert t.context.development_elapsed_seconds is not None

    # Exactly one tick carries the positive drop label (the drop row).
    assert sum(t.real_should_drop for t in ticks) == 1
    drop_tick = next(t for t in ticks if t.real_should_drop)
    assert drop_tick.context.phase is RoastPhase.DEVELOPMENT


def test_build_ticks_respects_cadence() -> None:
    """A coarser cadence yields fewer ticks; the drop tick is always present."""
    fine, _ = replay.build_ticks(_S1, cadence_seconds=15.0)
    coarse, _ = replay.build_ticks(_S1, cadence_seconds=120.0)
    assert len(coarse) < len(fine)
    assert sum(t.real_should_drop for t in coarse) == 1


# --- scoring: drop classification + timing ----------------------------------


@pytest.mark.asyncio
async def test_perfect_dropper_scores_f1_one_and_zero_timing() -> None:
    """A model that drops exactly at the real drop temp matches the good roast."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)

    async def recommend(context: AdvisorContext) -> RoastDecision:
        should_drop = (
            context.first_crack_detected and context.current_bean_temp_c >= ground.drop_temp_c
        )
        return RoastDecision(
            target_heat=40,
            target_fan=50,
            should_drop=should_drop,
            confidence=0.9,
            rationale="drop at target",
        )

    outcomes = await replay.replay_roast(ticks, recommend, clock=_deterministic_clock())
    score = replay.score_roast(outcomes, ground, "s1")

    assert score.drop.f1 == 1.0
    assert score.drop.precision == 1.0
    assert score.drop.recall == 1.0
    assert score.drop.false_positives == 0
    assert score.drop.timing_error_c == 0.0
    # Within one telemetry row of the real drop time.
    assert score.drop.timing_error_seconds is not None
    assert abs(score.drop.timing_error_seconds) < 5.0


@pytest.mark.asyncio
async def test_early_dropper_is_penalised_on_precision_and_timing() -> None:
    """Dropping well before the real drop costs precision and shows -°C/-s error."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)

    async def recommend(context: AdvisorContext) -> RoastDecision:
        should_drop = context.first_crack_detected and context.current_bean_temp_c >= 185.0
        return RoastDecision(
            target_heat=40,
            target_fan=50,
            should_drop=should_drop,
            confidence=0.9,
            rationale="drop early",
        )

    outcomes = await replay.replay_roast(ticks, recommend, clock=_deterministic_clock())
    score = replay.score_roast(outcomes, ground, "s1")

    assert score.drop.false_positives > 0
    assert score.drop.precision < 1.0
    assert score.drop.timing_error_c is not None and score.drop.timing_error_c < 0.0
    assert score.drop.timing_error_seconds is not None and score.drop.timing_error_seconds < 0.0


@pytest.mark.asyncio
async def test_never_dropper_reports_zero_recall_and_no_timing() -> None:
    """A model that never advises a drop has recall 0 and no timing error."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)
    never = _const_recommender(
        RoastDecision(
            target_heat=50, target_fan=50, should_drop=False, confidence=0.5, rationale="hold"
        )
    )

    outcomes = await replay.replay_roast(ticks, never, clock=_deterministic_clock())
    score = replay.score_roast(outcomes, ground, "s1")

    assert score.drop.recall == 0.0
    assert score.drop.false_negatives == 1
    assert score.drop.first_drop_seconds is None
    assert score.drop.timing_error_seconds is None
    assert score.drop.timing_error_c is None


# --- scoring: heat/fan MAE + directional agreement --------------------------


@pytest.mark.asyncio
async def test_lever_mae_and_directional_agreement() -> None:
    """A constant setpoint has a measurable MAE and ~zero directional agreement.

    Holding a constant heat means the model never moves the lever, so it only
    agrees with the human on ticks where the human also held — directional
    agreement is well below 1.0, and MAE is the mean gap to the real setpoints.
    """
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)
    const = _const_recommender(
        RoastDecision(
            target_heat=50, target_fan=50, should_drop=False, confidence=0.5, rationale="flat"
        )
    )

    outcomes = await replay.replay_roast(ticks, const, clock=_deterministic_clock())
    score = replay.score_roast(outcomes, ground, "s1")

    assert score.heat.mae > 0.0
    assert score.heat.directional_agreement is not None
    assert 0.0 <= score.heat.directional_agreement <= 1.0
    assert score.heat.directional_samples > 0


@pytest.mark.asyncio
async def test_per_phase_latency_uses_injected_clock() -> None:
    """Per-phase latency is computed from the injected clock, deterministically."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)
    rec = _const_recommender(
        RoastDecision(
            target_heat=50, target_fan=50, should_drop=False, confidence=0.5, rationale="x"
        )
    )

    # Each call advances the clock by exactly 2.0 s (start→end), so every tick's
    # measured latency is 2.0 s.
    outcomes = await replay.replay_roast(ticks, rec, clock=_deterministic_clock(step=1.0))
    score = replay.score_roast(outcomes, ground, "s1")

    measured = {pl.phase: pl for pl in score.phase_latency}
    dev = measured[RoastPhase.DEVELOPMENT.value]
    assert dev.count > 0
    assert dev.median_seconds == 1.0


@pytest.mark.asyncio
async def test_recommender_failure_is_captured_not_fatal() -> None:
    """A recommender raising on a tick is captured; the rest of the roast scores."""
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=60.0)
    calls = {"n": 0}

    async def flaky(context: AdvisorContext) -> RoastDecision:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider hiccup")
        return RoastDecision(
            target_heat=50, target_fan=50, should_drop=False, confidence=0.5, rationale="ok"
        )

    outcomes = await replay.replay_roast(ticks, flaky, clock=_deterministic_clock())
    assert outcomes[0].decision is None
    assert outcomes[0].error is not None and "provider hiccup" in outcomes[0].error
    score = replay.score_roast(outcomes, ground, "s1")
    assert score.ok_count == len(ticks) - 1
    assert score.tick_count == len(ticks)


# --- confusion matrices (derived from per-tick data; NO new calls) ----------


def _synthetic_tick(
    *,
    real_should_drop: bool,
    real_heat: int = 50,
    prev_real_heat: int | None = None,
) -> replay.ReplayTick:
    """A minimal :class:`ReplayTick` for confusion-matrix tests (canned data).

    Only the fields the confusion matrices read are meaningful here; the context
    is a plausible grounded development tick so the dataclass is well-formed.
    """
    context = AdvisorContext(
        phase=RoastPhase.DEVELOPMENT,
        roast_elapsed_seconds=600.0,
        development_elapsed_seconds=60.0,
        current_bean_temp_c=195.0,
        current_env_temp_c=210.0,
        bean_ror_c_per_min=5.0,
        env_ror_c_per_min=4.0,
        target_drop_temp_c=197.0,
        target_development_percent=15.0,
        profile_name="synthetic",
        recent_telemetry_samples=[],
        first_crack_detected=True,
        first_crack_timestamp_seconds=540.0,
    )
    return replay.ReplayTick(
        context=context,
        real_heat_percent=real_heat,
        real_fan_percent=50,
        prev_real_heat_percent=prev_real_heat,
        prev_real_fan_percent=None,
        real_should_drop=real_should_drop,
        monotonic_seconds=600.0,
    )


def _outcome(
    tick: replay.ReplayTick,
    *,
    model_should_drop: bool,
    model_heat: int = 50,
) -> replay.TickOutcome:
    """Wrap a synthetic tick with a canned model decision (no recommender)."""
    return replay.TickOutcome(
        tick=tick,
        decision=RoastDecision(
            target_heat=model_heat,
            target_fan=50,
            should_drop=model_should_drop,
            confidence=0.8,
            rationale="synthetic",
        ),
        latency_seconds=1.0,
    )


def test_drop_confusion_counts_tp_fp_tn_fn_from_known_sequence() -> None:
    """A canned drop sequence produces the expected TP/FP/TN/FN."""
    no_drop = _synthetic_tick(real_should_drop=False)
    real_drop = _synthetic_tick(real_should_drop=True)
    outcomes = [
        # TN: model holds, real not yet dropped.
        _outcome(no_drop, model_should_drop=False),
        _outcome(no_drop, model_should_drop=False),
        # FP: model drops early, real not yet dropped.
        _outcome(no_drop, model_should_drop=True),
        # TP: model drops at the real drop tick.
        _outcome(real_drop, model_should_drop=True),
        # FN: a (second) real-drop tick the model held on.
        _outcome(real_drop, model_should_drop=False),
    ]
    confusion = replay.drop_confusion(outcomes)
    assert confusion.true_positives == 1
    assert confusion.false_positives == 1
    assert confusion.true_negatives == 2
    assert confusion.false_negatives == 1
    # Totals reconcile with the tick count.
    assert confusion.total == len(outcomes)


def test_drop_confusion_ignores_no_decision_ticks() -> None:
    """A tick with no decision contributes to neither the matrix nor the total."""
    no_drop = _synthetic_tick(real_should_drop=False)
    outcomes = [
        _outcome(no_drop, model_should_drop=False),
        replay.TickOutcome(tick=no_drop, decision=None, latency_seconds=None, error="boom"),
    ]
    confusion = replay.drop_confusion(outcomes)
    assert confusion.total == 1
    assert confusion.true_negatives == 1


@pytest.mark.asyncio
async def test_drop_confusion_is_consistent_with_precision_recall_f1() -> None:
    """Over a real replay, the 2×2 TP/FP/FN match the DropMetrics fields.

    The matrix must be derived from the same ground-truth label the F1 uses, so
    the shared cells reconcile exactly and only ``true_negatives`` is additional.
    """
    ticks, ground = replay.build_ticks(_S1, cadence_seconds=30.0)

    async def recommend(context: AdvisorContext) -> RoastDecision:
        # Early dropper: produces both TPs/FNs and FPs, so all cells exercise.
        should_drop = context.first_crack_detected and context.current_bean_temp_c >= 190.0
        return RoastDecision(
            target_heat=40, target_fan=50, should_drop=should_drop, confidence=0.9, rationale="x"
        )

    outcomes = await replay.replay_roast(ticks, recommend, clock=_deterministic_clock())
    score = replay.score_roast(outcomes, ground, "s1")

    assert score.drop_confusion.true_positives == score.drop.true_positives
    assert score.drop_confusion.false_positives == score.drop.false_positives
    assert score.drop_confusion.false_negatives == score.drop.false_negatives
    # The matrix reconciles with the ok-tick count.
    assert score.drop_confusion.total == score.ok_count


def test_heat_direction_confusion_3x3_from_known_sequence() -> None:
    """A canned heat-direction sequence produces the expected 3×3 counts.

    Each tick carries a previous real heat setpoint; the real move (prev→real)
    and the model move (prev→model) each classify cut / hold / raise, indexing
    one cell with rows = real move, cols = model move.
    """
    outcomes = [
        # real raise (50→60), model raise (50→70) → (raise, raise) diagonal.
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=60, prev_real_heat=50),
            model_should_drop=False,
            model_heat=70,
        ),
        # real raise (50→60), model cut (50→40) → (raise, cut) off-diagonal.
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=60, prev_real_heat=50),
            model_should_drop=False,
            model_heat=40,
        ),
        # real cut (50→40), model cut (50→30) → (cut, cut) diagonal.
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=40, prev_real_heat=50),
            model_should_drop=False,
            model_heat=30,
        ),
        # real hold (50→50), model hold (50→50) → (hold, hold) diagonal.
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=50, prev_real_heat=50),
            model_should_drop=False,
            model_heat=50,
        ),
        # No previous setpoint → excluded from the matrix entirely.
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=60, prev_real_heat=None),
            model_should_drop=False,
            model_heat=70,
        ),
    ]
    confusion = replay.heat_direction_confusion(outcomes)
    assert confusion.labels == ("cut", "hold", "raise")
    # rows = real (cut=0, hold=1, raise=2); cols = model (cut=0, hold=1, raise=2).
    assert confusion.matrix == (
        (1, 0, 0),  # real cut: 1 model-cut
        (0, 1, 0),  # real hold: 1 model-hold
        (1, 0, 1),  # real raise: 1 model-cut, 1 model-raise
    )
    # Four classified ticks (the no-previous tick is excluded).
    assert confusion.samples == 4
    # Totals reconcile: the grid sums to the sample count.
    assert sum(sum(row) for row in confusion.matrix) == confusion.samples
    # Diagonal agreement = 3/4 = 0.75 (cut/cut, hold/hold, raise/raise).
    assert confusion.agreement == 0.75


def test_heat_direction_confusion_empty_when_no_previous_setpoints() -> None:
    """With no tick carrying a previous setpoint the matrix is empty, agreement None."""
    outcomes = [
        _outcome(
            _synthetic_tick(real_should_drop=False, real_heat=60, prev_real_heat=None),
            model_should_drop=False,
            model_heat=40,
        )
    ]
    confusion = replay.heat_direction_confusion(outcomes)
    assert confusion.samples == 0
    assert confusion.agreement is None
    assert sum(sum(row) for row in confusion.matrix) == 0


def test_confusion_matrices_render_to_markdown() -> None:
    """Both matrices render compact markdown lines for the report."""
    drop_md = replay.render_drop_confusion_md(
        replay.DropConfusion(
            true_positives=1, false_positives=2, true_negatives=20, false_negatives=0
        )
    )
    joined = "\n".join(drop_md)
    assert "TP=1" in joined and "FP=2" in joined and "TN=20" in joined and "FN=0" in joined
    assert "drop-timing error" in joined  # the honest-framing reminder

    heat_md = replay.render_heat_direction_confusion_md(
        replay.HeatDirectionConfusion(
            labels=replay.HEAT_DIRECTION_LABELS,
            matrix=((1, 0, 0), (0, 1, 0), (1, 0, 1)),
            samples=4,
        )
    )
    heat_joined = "\n".join(heat_md)
    assert "cut" in heat_joined and "hold" in heat_joined and "raise" in heat_joined
    assert "agreement=0.75" in heat_joined


def test_score_to_json_round_trips() -> None:
    """The scorecard serialises to a JSON-ready dict with the metric fields."""
    drop = replay.DropMetrics(
        precision=1.0,
        recall=1.0,
        f1=1.0,
        true_positives=1,
        false_positives=0,
        false_negatives=0,
        first_drop_seconds=100.0,
        timing_error_seconds=0.0,
        timing_error_c=0.0,
    )
    lever = replay.LeverMetrics(mae=5.0, directional_agreement=0.5, directional_samples=4)
    score = replay.RoastScore(
        roast_name="s1",
        tick_count=10,
        ok_count=10,
        drop=drop,
        drop_confusion=replay.DropConfusion(
            true_positives=1, false_positives=0, true_negatives=9, false_negatives=0
        ),
        heat_direction_confusion=replay.HeatDirectionConfusion(
            labels=replay.HEAT_DIRECTION_LABELS,
            matrix=((1, 0, 0), (0, 2, 0), (0, 0, 1)),
            samples=4,
        ),
        heat=lever,
        fan=lever,
        phase_latency=[replay.PhaseLatency("development", 3, 1.2, 1.5)],
        development_time_ratio_truth=15.0,
    )
    out = replay.score_to_json(score)
    assert out["drop"]["f1"] == 1.0
    assert out["heat"]["directional_agreement"] == 0.5
    assert out["phase_latency"][0]["phase"] == "development"
    # Confusion matrices are exposed in the JSON.
    assert out["drop_confusion"]["true_positives"] == 1
    assert out["drop_confusion"]["true_negatives"] == 9
    assert out["drop_confusion"]["total"] == 10
    assert out["heat_direction_confusion"]["labels"] == ["cut", "hold", "raise"]
    assert out["heat_direction_confusion"]["matrix"] == [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
    assert out["heat_direction_confusion"]["samples"] == 4
    # All four samples are on the diagonal → perfect agreement.
    assert out["heat_direction_confusion"]["agreement"] == 1.0
