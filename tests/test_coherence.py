"""Unit tests for the post-FC coherence / deadband gate (#276).

The gate is pure and deterministic — these tests pin the rule directly:
direction-reversals below the magnitude threshold are damped to a hold, while
first moves, same-direction moves, and decisive (>= threshold) reversals pass.
The controller-wiring tests (deadband damps the #218 flip-flop, allows a decisive
move) live in ``test_controller.py``.
"""

import pytest

from roastpilot_agent.coherence import (
    CoherenceDecision,
    LeverDirection,
    LeverGateResult,
    evaluate_lever_coherence,
)

THRESHOLD = 15


def _gate(*, requested: int, current: int, last_direction: LeverDirection) -> LeverGateResult:
    return evaluate_lever_coherence(
        requested=requested,
        current=current,
        last_direction=last_direction,
        threshold_percent=THRESHOLD,
    )


def test_hold_is_always_allowed_and_preserves_direction() -> None:
    result = _gate(requested=40, current=40, last_direction=LeverDirection.UP)
    assert result.decision is CoherenceDecision.ALLOW
    assert result.applied_value == 40
    # A hold neither sets nor reverses a trajectory.
    assert result.direction is LeverDirection.UP


def test_first_move_is_allowed_and_records_direction() -> None:
    up = _gate(requested=50, current=40, last_direction=LeverDirection.NONE)
    assert up.decision is CoherenceDecision.ALLOW
    assert up.applied_value == 50
    assert up.direction is LeverDirection.UP
    down = _gate(requested=30, current=40, last_direction=LeverDirection.NONE)
    assert down.decision is CoherenceDecision.ALLOW
    assert down.applied_value == 30
    assert down.direction is LeverDirection.DOWN


def test_same_direction_move_is_allowed_regardless_of_size() -> None:
    # A small continuation in the same direction is not a reversal — allowed.
    result = _gate(requested=45, current=40, last_direction=LeverDirection.UP)
    assert result.decision is CoherenceDecision.ALLOW
    assert result.applied_value == 45
    assert result.direction is LeverDirection.UP


def test_sub_threshold_reversal_is_damped_to_a_hold() -> None:
    # Last move was UP; now a DOWN of 10 (< 15) — the incoherent #218 twiddle.
    result = _gate(requested=30, current=40, last_direction=LeverDirection.UP)
    assert result.decision is CoherenceDecision.DAMP
    assert result.applied_value == 40  # held at current
    # A suppressed move does not rewrite the recorded direction.
    assert result.direction is LeverDirection.UP


def test_decisive_reversal_at_threshold_is_allowed() -> None:
    # Last move UP; a DOWN of exactly the threshold (15) is decisive — allowed.
    result = _gate(requested=25, current=40, last_direction=LeverDirection.UP)
    assert result.decision is CoherenceDecision.ALLOW
    assert result.applied_value == 25
    assert result.direction is LeverDirection.DOWN


def test_decisive_reversal_above_threshold_is_allowed() -> None:
    # Last move DOWN; a large UP of 30 (>= 15) is a real move — allowed.
    result = _gate(requested=70, current=40, last_direction=LeverDirection.DOWN)
    assert result.decision is CoherenceDecision.ALLOW
    assert result.applied_value == 70
    assert result.direction is LeverDirection.UP


def test_flip_flop_sequence_is_damped_but_decisive_step_passes() -> None:
    """The #218 pattern: 30 -> 40 -> 30 -> 40 thrash is damped, a real cut passes.

    Threaded through the gate as the controller threads it (feeding each result's
    direction into the next call), with the levers held at the damped value.
    """
    current = 30
    direction = LeverDirection.NONE
    # 30 -> 40: first move, allowed, direction UP.
    step = _gate(requested=40, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    current, direction = step.applied_value, step.direction
    assert current == 40
    # 40 -> 30: sub-threshold reversal, damped — held at 40, direction still UP.
    step = _gate(requested=30, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    current, direction = step.applied_value, step.direction
    assert (current, direction) == (40, LeverDirection.UP)
    # 40 -> 40 (the next twiddle target, but we are still at 40): a hold.
    step = _gate(requested=50, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW  # same-direction continuation
    current, direction = step.applied_value, step.direction
    assert current == 50
    # A genuine decisive cut 50 -> 20 (reversal of 30 >= 15): allowed.
    step = _gate(requested=20, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    assert step.applied_value == 20
    assert step.direction is LeverDirection.DOWN


@pytest.mark.parametrize(
    ("requested", "current", "last_direction", "expected"),
    [
        # NONE baseline never reverses, so any move is allowed.
        (60, 50, LeverDirection.NONE, CoherenceDecision.ALLOW),
        (40, 50, LeverDirection.NONE, CoherenceDecision.ALLOW),
        # Sub-threshold reversal from each non-NONE baseline is damped.
        (44, 50, LeverDirection.UP, CoherenceDecision.DAMP),
        (56, 50, LeverDirection.DOWN, CoherenceDecision.DAMP),
    ],
)
def test_decision_matrix(
    requested: int,
    current: int,
    last_direction: LeverDirection,
    expected: CoherenceDecision,
) -> None:
    assert (
        _gate(requested=requested, current=current, last_direction=last_direction).decision
        is expected
    )
