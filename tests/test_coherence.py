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
    # #276 Fix 1: a damped reversal ADVANCES the recorded direction toward the
    # requested side (DOWN here) so a SUSTAINED push converges next consult; an
    # isolated reversal is still suppressed on this consult (value held).
    assert result.direction is LeverDirection.DOWN


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
    """The #218 pattern: an alternating 40 <-> 30 <-> 40 thrash stays damped, while
    a real decisive cut passes.

    Threaded through the gate as the controller threads it (feeding each result's
    direction into the next call), with the levers held at the damped value. Under
    #276 Fix 1 a damped reversal advances the recorded direction, so an ALTERNATING
    request re-reverses that advanced direction every consult and keeps being
    damped (it is never the sustained-cut case, so it does not break through).
    """
    current = 30
    direction = LeverDirection.NONE
    # 30 -> 40: first move, allowed, direction UP.
    step = _gate(requested=40, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    current, direction = step.applied_value, step.direction
    assert (current, direction) == (40, LeverDirection.UP)
    # 40 -> 30: sub-threshold reversal, damped — held at 40, direction advances DOWN.
    step = _gate(requested=30, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    current, direction = step.applied_value, step.direction
    assert (current, direction) == (40, LeverDirection.DOWN)
    # 40 -> 50: the OTHER side of the oscillation — now a sub-threshold reversal of
    # the advanced DOWN direction, so it is damped too (held at 40, dir advances UP).
    step = _gate(requested=50, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    current, direction = step.applied_value, step.direction
    assert (current, direction) == (40, LeverDirection.UP)
    # A genuine decisive cut 40 -> 20 (reversal of 20 >= 15): allowed.
    step = _gate(requested=20, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    assert step.applied_value == 20
    assert step.direction is LeverDirection.DOWN


def test_sustained_sub_threshold_cut_converges_within_two_consults() -> None:
    """#276 Fix 1: a SUSTAINED sub-threshold cut must NOT latch the lever high
    forever — it converges (executes) by the second consult.

    The failure mode this guards is over-roast: the advisor repeatedly asking
    heat 100 -> 90 (a -10 DOWN, below the 15 threshold) while the lever last moved
    UP. Before the fix that reversal was damped on every consult with the direction
    left UP, so it was a fresh reversal forever — heat pinned at 100. Now the first
    damp advances the recorded direction DOWN, so the second identical request is a
    same-direction move and executes.
    """
    current = 100
    direction = LeverDirection.UP
    # Consult 1: 100 -> 90, sub-threshold reversal — damped, held at 100, dir DOWN.
    step = _gate(requested=90, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    assert step.applied_value == 100
    current, direction = step.applied_value, step.direction
    assert direction is LeverDirection.DOWN
    # Consult 2: same 100 -> 90 request — now a same-direction (DOWN) move: ALLOW.
    step = _gate(requested=90, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    assert step.applied_value == 90  # converged: the cut actuates
    assert step.direction is LeverDirection.DOWN


def test_sustained_sub_threshold_raise_converges_within_two_consults() -> None:
    """#276 Fix 1 (symmetric): a SUSTAINED sub-threshold RAISE converges too — the
    rule is direction-agnostic. Repeated 50 -> 60 (a +10 UP, below 15) while the
    lever last moved DOWN executes by the second consult."""
    current = 50
    direction = LeverDirection.DOWN
    step = _gate(requested=60, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    assert step.applied_value == 50
    current, direction = step.applied_value, step.direction
    assert direction is LeverDirection.UP
    step = _gate(requested=60, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.ALLOW
    assert step.applied_value == 60  # converged
    assert step.direction is LeverDirection.UP


def test_isolated_single_sub_threshold_reversal_is_still_damped() -> None:
    """#276 Fix 1: an ISOLATED reversal (true jitter) is still suppressed on the
    consult it appears — convergence only happens when the intent PERSISTS.

    One DOWN-of-10 against a last-UP lever is damped (value held); the very next
    consult that returns to the original direction is a same-direction move, so the
    jitter never actuated and the lever stayed put for that single reversal.
    """
    current = 40
    direction = LeverDirection.UP
    # Isolated reversal: damped this consult (the value is held — jitter rejected).
    step = _gate(requested=30, current=current, last_direction=direction)
    assert step.decision is CoherenceDecision.DAMP
    assert step.applied_value == 40
    current, direction = step.applied_value, step.direction
    assert direction is LeverDirection.DOWN
    # The intent does NOT persist — the next request returns UP (same as the
    # original move). It is a same-direction move now, so nothing was thrashed: the
    # single reversal cost zero actuation.
    step = _gate(requested=45, current=current, last_direction=LeverDirection.UP)
    assert step.decision is CoherenceDecision.ALLOW
    assert step.applied_value == 45


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
