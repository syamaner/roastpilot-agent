"""Tests for the RP-D joint-objective bake-off metric core (#711, plan D124).

Covers the pure :func:`joint_window_score` arithmetic (HIT boundaries, the
ratified scalar shape, the termination penalty, clamping, non-finite-input
rejection) and :func:`joint_score_to_json` serialization. No LLM, no store — the
metric is a pure function of a roast's achieved outcome vs its targets, applying
the fixed ratified tolerances/weights. The store/trace adapter that sources
authoritative targets and DTR is PR-D2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bakeoff_replay as replay  # noqa: E402


def _score(
    drop: float,
    target_drop: float,
    dtr: float,
    target_dtr: float,
    *,
    terminated_abnormally: bool = False,
) -> replay.JointWindowScore:
    """Score achieved (drop temp, DTR %) against (target drop, target DTR %)."""
    return replay.joint_window_score(
        drop_temp_c=drop,
        target_drop_temp_c=target_drop,
        dtr_percent=dtr,
        target_dtr_percent=target_dtr,
        terminated_abnormally=terminated_abnormally,
    )


# --- joint_window_score: HIT rule -------------------------------------------


def test_perfect_roast_is_a_hit_and_scores_one() -> None:
    s = _score(195.0, 195.0, 16.0, 16.0)
    assert s.hit is True
    assert s.scalar == pytest.approx(1.0)
    assert s.drop_temp_error_c == pytest.approx(0.0)
    assert s.dtr_error_pp == pytest.approx(0.0)


def test_exact_window_edge_is_a_hit_and_scores_one_half() -> None:
    # Ratified tolerances are inclusive (<=): exactly +3 C and +2 pp is a HIT,
    # and the literal 50/50-plus-half scalar puts the window edge at 0.5.
    s = _score(198.0, 195.0, 18.0, 16.0)
    assert s.hit is True
    assert s.scalar == pytest.approx(0.5)


def test_negative_window_edge_is_also_a_hit_and_scores_one_half() -> None:
    # The ratified window is symmetric (±3 C / ±2 pp): exactly -3 C and -2 pp
    # is equally a HIT at scalar 0.5, with signed errors on the short/under side.
    s = _score(192.0, 195.0, 14.0, 16.0)
    assert s.hit is True
    assert s.scalar == pytest.approx(0.5)
    assert s.drop_temp_error_c == pytest.approx(-3.0)
    assert s.dtr_error_pp == pytest.approx(-2.0)


def test_just_over_drop_temp_tolerance_is_a_miss() -> None:
    s = _score(198.01, 195.0, 16.0, 16.0)
    assert s.hit is False


def test_just_over_dtr_tolerance_is_a_miss() -> None:
    s = _score(195.0, 195.0, 18.01, 16.0)
    assert s.hit is False


def test_hit_requires_both_axes_in_tolerance() -> None:
    # Drop temp perfect, DTR far out -> not a hit.
    assert _score(195.0, 195.0, 24.0, 16.0).hit is False
    # DTR perfect, drop temp far out -> not a hit.
    assert _score(188.0, 195.0, 16.0, 16.0).hit is False


# --- joint_window_score: the ratified Conebosque A/B (#559) ------------------


def test_conebosque_baseline_misses_and_floors_at_zero() -> None:
    # baseline 55f6a034: dropped 188 C (7 short) at 21% DTR (5 pp over) vs 195/16.
    s = _score(188.0, 195.0, 21.0, 16.0)
    assert s.hit is False
    assert s.scalar == pytest.approx(0.0)
    assert s.drop_temp_error_c == pytest.approx(-7.0)  # short -> negative
    assert s.dtr_error_pp == pytest.approx(5.0)  # over -> positive


def test_conebosque_treatment_misses_and_floors_at_zero() -> None:
    # treatment 3ca102f8: dropped 190 C (5 short) at 24% DTR (8 pp over) vs 195/16.
    s = _score(190.0, 195.0, 24.0, 16.0)
    assert s.hit is False
    assert s.scalar == pytest.approx(0.0)
    assert s.drop_temp_error_c == pytest.approx(-5.0)
    assert s.dtr_error_pp == pytest.approx(8.0)


# --- joint_window_score: scalar shape ---------------------------------------


def test_scalar_ranks_a_nearer_miss_above_a_farther_miss() -> None:
    near = _score(196.0, 195.0, 17.0, 16.0)  # 1 C, 1 pp
    far = _score(197.0, 195.0, 18.0, 16.0)  # 2 C, 2 pp
    assert near.scalar > far.scalar
    # Hand-computed literals (not a re-derivation of the source formula), so a
    # structural change to the scalar's shape is caught, not just a weight typo.
    assert near.scalar == pytest.approx(0.7916667)
    assert far.scalar == pytest.approx(0.5833333)


def test_large_miss_clamps_scalar_at_zero_not_negative() -> None:
    s = _score(150.0, 195.0, 40.0, 16.0)
    assert s.scalar == 0.0


# --- joint_window_score: termination penalty --------------------------------


def test_abnormal_termination_zeroes_scalar_and_blocks_hit() -> None:
    # Numbers land perfectly, but a guard/emergency/fault drop is never a HIT.
    s = _score(195.0, 195.0, 16.0, 16.0, terminated_abnormally=True)
    assert s.hit is False
    assert s.scalar == pytest.approx(0.0)
    assert s.terminated_abnormally is True


# --- joint_window_score: input validation -----------------------------------


@pytest.mark.parametrize(
    ("drop", "target_drop", "dtr", "target_dtr"),
    [
        (float("inf"), 195.0, 16.0, 16.0),  # corrupt achieved drop temp
        (195.0, float("nan"), 16.0, 16.0),  # corrupt target drop temp
        (195.0, 195.0, float("inf"), 16.0),  # corrupt achieved DTR
        (195.0, 195.0, 16.0, float("nan")),  # corrupt target DTR
        (1e308, -1e308, 16.0, 16.0),  # finite inputs, drop-temp error overflows
        (195.0, 195.0, 1e308, -1e308),  # finite inputs, DTR error overflows
    ],
)
def test_non_finite_or_overflowing_input_raises(
    drop: float, target_drop: float, dtr: float, target_dtr: float
) -> None:
    # A non-finite outcome/target (e.g. an inf MCP temperature in a corrupt
    # trace) OR a finite-but-overflowing pair must fail closed, not score as an
    # ordinary worst roast and emit invalid Infinity/NaN JSON that biases the
    # aggregate stats. Validating the computed errors catches both.
    with pytest.raises(ValueError, match="finite"):
        _score(drop, target_drop, dtr, target_dtr)


# --- joint_score_to_json ----------------------------------------------------


def test_joint_score_to_json_shape() -> None:
    s = _score(188.0, 195.0, 21.0, 16.0)
    d = replay.joint_score_to_json(s)
    assert d == {
        "hit": False,
        "scalar": 0.0,
        "drop_temp_c": 188.0,
        "target_drop_temp_c": 195.0,
        "drop_temp_error_c": -7.0,
        "dtr_percent": 21.0,
        "target_dtr_percent": 16.0,
        "dtr_error_pp": 5.0,
        "terminated_abnormally": False,
    }


def test_joint_score_to_json_rounds_at_the_documented_precision() -> None:
    # Genuinely fractional inputs so the rounding precision is exercised: scalar
    # to 4 dp, the two errors to 2 dp. A 2-dp scalar rounding (0.94) would fail.
    s = _score(195.256, 195.0, 16.339, 16.0)
    d = replay.joint_score_to_json(s)
    assert d["scalar"] == 0.9363
    assert d["drop_temp_error_c"] == 0.26
    assert d["dtr_error_pp"] == 0.34
