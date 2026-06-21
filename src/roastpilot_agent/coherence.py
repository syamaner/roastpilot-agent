"""The post-FC coherence / deadband gate (D35 §4-A, D40.5, #276).

D35 puts the LLM back in the live control loop *after first crack* — it advises
heat, fan, and the drop, and the controller decides execute-or-not. The advisor
is allowed **bold, deliberate moves** there (a real heat cut, a real fan change):
post-FC is a fast, dynamic phase where a decisive lever step is correct. What it
must NOT do is the #218 failure — twiddle a lever back and forth
(``30 -> 40 -> 30 -> 40``) or self-contradict consult to consult, which physically
destabilises the rate-of-rise and reads as reacting to jitter rather than executing
a plan.

So this module damps **incoherent direction-reversal, not magnitude** (D35 §1).
The rule, applied **independently to each lever** (heat and fan):

* Compare the recommendation (already clamped into the per-phase safety box) to
  the lever value currently in effect — the last value the controller actually
  executed.
* A request with the **same** direction as the lever's last executed move, or the
  **first** move on a lever, or a reversal **at or above** the magnitude threshold
  is a *decisive* move and is **allowed** unchanged.
* A request that **reverses** the lever's last executed direction and is **below**
  the magnitude threshold is *incoherent thrash* and is **damped** — that lever is
  held at its current value (the reversal is suppressed).

This is the D40.5 reconciliation of the operator's ~2-3 s coherence dwell against
the ~5 s post-FC consult cadence: across consecutive consults, no small direction
reversal, but any decisive move passes. The magnitude threshold is a single named
configuration constant (``ControllerConfig.post_fc_deadband_threshold_percent``);
its exact value is tuned on the replay harness (#277).

The gate is **deterministic, pure, and typed** — it returns a typed
:class:`CoherenceDecision`, never a string verdict, and it holds no state of its
own (the caller passes the last-executed direction in and stores the returned one).
It runs **after** the safety box (:meth:`safety.SafetyPolicy.evaluate_command`) and
**before** execution: it can only ever turn an allowed move into a *hold*, never
into a larger or out-of-box move. It therefore cannot weaken a safety verdict — it
is strictly a damper layered on top of one.

All lever values are 0-100 percent duty (never temperatures).
"""

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CoherenceDecision",
    "LeverDirection",
    "LeverGateResult",
    "evaluate_lever_coherence",
]


class LeverDirection(Enum):
    """The direction of a lever's last executed move (D15: plain ``Enum``).

    A plain ``Enum``, deliberately not ``StrEnum``, so a string comparison against
    a direction is a pyright strict error — the same typed-verdict discipline the
    safety verdicts follow. ``NONE`` is the initial state for a lever that has not
    yet moved post-FC (no direction to reverse).
    """

    NONE = "none"
    UP = "up"
    DOWN = "down"


class CoherenceDecision(Enum):
    """Whether the coherence gate allowed or damped a lever request (D15: plain ``Enum``).

    Typed, never string-compared. ``ALLOW`` applies the requested lever value and
    records its direction; ``DAMP`` holds the lever at its current value (an
    incoherent sub-threshold reversal suppressed).
    """

    ALLOW = "allow"
    DAMP = "damp"


@dataclass(frozen=True)
class LeverGateResult:
    """The typed outcome of the coherence gate for one lever (#276).

    Attributes:
        decision: ``ALLOW`` (apply ``applied_value``) or ``DAMP`` (held).
        applied_value: The lever value to execute — the request when allowed, the
            unchanged current value when damped.
        direction: The lever's recorded direction *after* this decision: the new
            move's direction when allowed, the prior direction unchanged when
            damped (a suppressed move never rewrites the trajectory the next
            consult is judged against).
    """

    decision: CoherenceDecision
    applied_value: int
    direction: LeverDirection


def _direction_of(delta: int) -> LeverDirection:
    """The :class:`LeverDirection` of a signed lever delta (0 → ``NONE``)."""
    if delta > 0:
        return LeverDirection.UP
    if delta < 0:
        return LeverDirection.DOWN
    # Total-function completeness: the only caller guards ``delta != 0`` (a zero
    # delta is a hold, returned before this is reached), so a zero never arrives
    # here in practice.
    return LeverDirection.NONE  # pragma: no cover


def evaluate_lever_coherence(
    *,
    requested: int,
    current: int,
    last_direction: LeverDirection,
    threshold_percent: int,
) -> LeverGateResult:
    """Damp an incoherent sub-threshold direction reversal on one lever (#276).

    Pure and deterministic. Compares the (already safety-clamped) ``requested``
    value to the ``current`` in-effect value and the lever's ``last_direction``:

    * No change (``requested == current``): ``ALLOW`` a hold; the direction is
      preserved (a hold neither establishes nor reverses a trajectory).
    * Same direction as ``last_direction``, or ``last_direction`` is ``NONE``
      (the first post-FC move on this lever): ``ALLOW`` — a decisive or
      first move.
    * A reversal whose magnitude is at or above ``threshold_percent``: ``ALLOW``
      — a decisive move is correct post-FC even when it reverses (D35 §1).
    * A reversal whose magnitude is below ``threshold_percent``: ``DAMP`` — hold
      the lever; the suppressed move does not rewrite the recorded direction.

    Args:
        requested: The recommended lever value (0-100 percent), already clamped
            into the per-phase safety box by the caller.
        current: The lever value currently in effect (the last executed value).
        last_direction: The direction of this lever's last executed move
            (``NONE`` if it has not moved since first crack).
        threshold_percent: The reversal magnitude (percentage points) at or above
            which a reversal is decisive and allowed — the single named
            configuration constant, tuned on the replay harness (#277).

    Returns:
        The typed :class:`LeverGateResult` carrying the decision, the value to
        execute, and the lever's direction after the decision.
    """
    delta = requested - current
    if delta == 0:
        # A hold is always coherent; it neither sets nor reverses a direction.
        return LeverGateResult(CoherenceDecision.ALLOW, current, last_direction)
    move_direction = _direction_of(delta)
    reverses = last_direction is not LeverDirection.NONE and move_direction is not last_direction
    if reverses and abs(delta) < threshold_percent:
        # Incoherent sub-threshold reversal (the #218 30<->40<->30 thrash): hold
        # the lever and keep the prior direction so a suppressed move cannot seed
        # a fake trajectory for the next consult.
        return LeverGateResult(CoherenceDecision.DAMP, current, last_direction)
    # First move, same-direction move, or a decisive (>= threshold) reversal: a
    # deliberate step is correct post-FC — apply it and record its direction.
    return LeverGateResult(CoherenceDecision.ALLOW, requested, move_direction)
