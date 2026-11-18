"""Pure conservative HUMAN recipe timing evaluator (D238 T1, #1095).

This module decides one evidence predicate: whether supplied timing and bean
temperature evidence conservatively proves that a recipe's minimum development
percentage and bean-temperature target are both met. It owns its own input
types and does not parse any wire, JSON or protocol schema.

Exactness boundary:
    Every bound is the exact binary value of the supplied Python scalar,
    converted with ``Fraction``; all comparisons and the development-percentage
    arithmetic are exact rational operations with no float division and no
    rounding. Any wire or decimal serialization error, and any acquisition-age
    allowance, must already be folded into the supplied ``lo``/``hi`` bounds by
    the caller. This module does not make the device clock exact; it only
    reasons exactly about the bounds it is given.

Conservative development bounds:
    For a charge interval ``C``, first-crack interval ``F`` and sample
    acquisition interval ``S`` in one clock domain, with ``Chi <= Flo``,
    ``Fhi <= Slo`` and ``Slo > Chi``, every point development percentage
    ``100*(s-f)/(s-c)`` lies within ``[L, U]`` where
    ``L = 100*(Slo-Fhi)/(Slo-Clo)`` and ``U = 100*(Shi-Flo)/(Shi-Chi)``.

Authority:
    ``MET`` is only an evidence predicate. It confers no dispatch, endpoint,
    phase, configuration, enablement or recovery authority. Freshness and
    ownership of the evidence are the caller's responsibility, and independent
    emergency and protection arbitration remain outside this module. There is
    no target maximum, so no upper-window completion can be expressed.

Totality:
    For normally constructed values (including dataclass instances holding
    wrong-typed fields), ``None`` and wrong-typed top-level arguments, the
    evaluator returns a typed result and does not raise. Malformed values become
    ``INVALID`` results; missing evidence becomes ``UNAVAILABLE`` and is never
    treated as zero.

All temperatures are Celsius; all times are session-elapsed seconds.
"""

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Final

# Representation-domain bound, not a physical limit: every integer in
# [-2**53, 2**53] is exactly representable as a binary64 value (the same
# convention as the human-mode exact-integer limit).
_EXACT_INT_LIMIT: Final = 2**53


@dataclass(frozen=True)
class TimingDomain:
    """Opaque identity of one producer clock domain.

    Attributes:
        session_id: Non-empty exact ``str`` session token.
        clock_epoch: Non-empty exact ``str`` clock-epoch token.
    """

    session_id: str
    clock_epoch: str


@dataclass(frozen=True)
class TimingInterval:
    """Inclusive session-elapsed time bounds, in seconds, in one clock domain.

    Attributes:
        domain: Clock domain the bounds belong to.
        lo: Earliest possible instant; must be ``>= 0``.
        hi: Latest possible instant; must be ``>= lo`` (equal means zero uncertainty).
    """

    domain: TimingDomain
    lo: int | float
    hi: int | float


@dataclass(frozen=True)
class BeanTempBounds:
    """Inclusive bean-temperature bounds in Celsius.

    Attributes:
        lo_c: Lowest possible bean temperature.
        hi_c: Highest possible bean temperature; must be ``>= lo_c``.
    """

    lo_c: int | float
    hi_c: int | float


@dataclass(frozen=True)
class BeanTempSample:
    """One atomic sample: BT bounds and the acquisition interval travel together.

    Attributes:
        acquisition: Acquisition interval of the BT reading, or ``None`` when the
            acquisition instant is not qualified.
        bean_temp: Bean-temperature bounds, or ``None`` when absent.
    """

    acquisition: TimingInterval | None
    bean_temp: BeanTempBounds | None


@dataclass(frozen=True)
class RecipeTimingTarget:
    """Joint recipe target: a minimum development percentage and a BT target.

    Attributes:
        min_development_percent: Minimum development percentage, ``0 < x < 100``.
        bean_temp_c: Inclusive bean-temperature target in Celsius, ``> 0``.
    """

    min_development_percent: int | float
    bean_temp_c: int | float


class TimingVerdict(Enum):
    """Closed outcome of a recipe timing evaluation."""

    MET = "met"
    NOT_MET = "not_met"
    INDETERMINATE = "indeterminate"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class TargetComparison(Enum):
    """Conservative comparison of a bounded quantity with a target."""

    AT_OR_ABOVE = "at_or_above"
    BELOW = "below"
    STRADDLES = "straddles"


class TimingReason(Enum):
    """Closed reason accompanying a timing verdict."""

    INVALID_TARGET = "invalid_target"
    INVALID_DOMAIN = "invalid_domain"
    INVALID_OPERAND = "invalid_operand"
    INVALID_NEGATIVE_TIME = "invalid_negative_time"
    INVALID_REVERSED_BOUNDS = "invalid_reversed_bounds"
    DOMAIN_MISMATCH = "domain_mismatch"
    CHARGE_FIRST_CRACK_UNORDERED = "charge_first_crack_unordered"
    CHARGE_ABSENT = "charge_absent"
    FIRST_CRACK_ABSENT = "first_crack_absent"
    SAMPLE_ABSENT = "sample_absent"
    SAMPLE_ACQUISITION_UNQUALIFIED = "sample_acquisition_unqualified"
    BEAN_TEMP_ABSENT = "bean_temp_absent"
    SAMPLE_NOT_AFTER_FIRST_CRACK = "sample_not_after_first_crack"
    ZERO_DURATION = "zero_duration"
    TARGET_BELOW = "target_below"
    TARGET_STRADDLES = "target_straddles"
    TARGETS_MET = "targets_met"


@dataclass(frozen=True)
class TimingEvaluation:
    """Typed result of :func:`evaluate_recipe_timing`.

    The bounds and both comparisons are all present exactly when the evaluation
    reached target classification, and all ``None`` otherwise.

    Attributes:
        verdict: Closed outcome.
        reason: Closed reason for the outcome.
        development_lower_percent: Exact conservative lower bound ``L``.
        development_upper_percent: Exact conservative upper bound ``U``.
        development: Comparison of ``[L, U]`` with the minimum development target.
        bean_temp: Comparison of the BT bounds with the BT target.
    """

    verdict: TimingVerdict
    reason: TimingReason
    development_lower_percent: Fraction | None
    development_upper_percent: Fraction | None
    development: TargetComparison | None
    bean_temp: TargetComparison | None


@dataclass(frozen=True)
class _Span:
    domain: TimingDomain
    lo: Fraction
    hi: Fraction


def _exact(value: object) -> Fraction | None:
    """Return the exact value of an exact scalar, else ``None``."""
    if type(value) is int:
        if -_EXACT_INT_LIMIT <= value <= _EXACT_INT_LIMIT:
            return Fraction(value)
        return None
    if type(value) is float and math.isfinite(value):
        return Fraction(value)
    return None


def _valid_domain(domain: object) -> bool:
    if type(domain) is not TimingDomain:
        return False
    for token in (domain.session_id, domain.clock_epoch):
        if type(token) is not str or token == "":
            return False
    return True


def _same_domain(first: TimingDomain, second: TimingDomain) -> bool:
    return first.session_id == second.session_id and first.clock_epoch == second.clock_epoch


def _interval(value: object) -> _Span | TimingReason:
    if type(value) is not TimingInterval:
        return TimingReason.INVALID_OPERAND
    if not _valid_domain(value.domain):
        return TimingReason.INVALID_DOMAIN
    lo = _exact(value.lo)
    hi = _exact(value.hi)
    if lo is None or hi is None:
        return TimingReason.INVALID_OPERAND
    if lo < 0:
        return TimingReason.INVALID_NEGATIVE_TIME
    if lo > hi:
        return TimingReason.INVALID_REVERSED_BOUNDS
    return _Span(value.domain, lo, hi)


def _bean_temp_bounds(value: object) -> tuple[Fraction, Fraction] | TimingReason:
    if type(value) is not BeanTempBounds:
        return TimingReason.INVALID_OPERAND
    lo = _exact(value.lo_c)
    hi = _exact(value.hi_c)
    if lo is None or hi is None:
        return TimingReason.INVALID_OPERAND
    if lo > hi:
        return TimingReason.INVALID_REVERSED_BOUNDS
    return lo, hi


def _target(value: object) -> tuple[Fraction, Fraction] | None:
    if type(value) is not RecipeTimingTarget:
        return None
    minimum = _exact(value.min_development_percent)
    bean_temp = _exact(value.bean_temp_c)
    if minimum is None or bean_temp is None:
        return None
    if minimum <= 0:
        return None
    if minimum >= 100:
        return None
    if bean_temp <= 0:
        return None
    return minimum, bean_temp


def _classify_development(lower: Fraction, upper: Fraction, minimum: Fraction) -> TargetComparison:
    if lower >= minimum:
        return TargetComparison.AT_OR_ABOVE
    if upper < minimum:
        return TargetComparison.BELOW
    return TargetComparison.STRADDLES


def _classify_bean_temp(lo: Fraction, hi: Fraction, target: Fraction) -> TargetComparison:
    if lo >= target:
        return TargetComparison.AT_OR_ABOVE
    if hi < target:
        return TargetComparison.BELOW
    return TargetComparison.STRADDLES


def _unresolved(verdict: TimingVerdict, reason: TimingReason) -> TimingEvaluation:
    return TimingEvaluation(verdict, reason, None, None, None, None)


def _invalid(reason: TimingReason) -> TimingEvaluation:
    return _unresolved(TimingVerdict.INVALID, reason)


def _unavailable(reason: TimingReason) -> TimingEvaluation:
    return _unresolved(TimingVerdict.UNAVAILABLE, reason)


def evaluate_recipe_timing(
    *,
    target: RecipeTimingTarget,
    charge: TimingInterval | None,
    first_crack: TimingInterval | None,
    sample: BeanTempSample | None,
) -> TimingEvaluation:
    """Conservatively evaluate the joint development and bean-temperature target.

    Checks run in a fixed order and the first failure wins: target validity;
    validity of each present operand (charge, first crack, then the sample, its
    acquisition interval and its BT bounds); a single clock domain; charge
    ordered before first crack; availability; sample after first crack; non-zero
    duration; and finally exact target classification. Every failure yields a
    non-``MET`` verdict.

    Args:
        target: Joint recipe target.
        charge: Charge interval, or ``None`` when absent.
        first_crack: First-crack interval, or ``None`` when absent.
        sample: Atomic BT sample, or ``None`` when absent.

    Returns:
        A typed evaluation. ``MET`` requires both the conservative lower
        development bound and the lower BT bound to be at or above their targets.
    """
    resolved_target = _target(target)
    if resolved_target is None:
        return _invalid(TimingReason.INVALID_TARGET)
    minimum, bean_temp_target = resolved_target

    charge_span: _Span | None = None
    if charge is not None:
        checked = _interval(charge)
        if isinstance(checked, TimingReason):
            return _invalid(checked)
        charge_span = checked

    first_crack_span: _Span | None = None
    if first_crack is not None:
        checked = _interval(first_crack)
        if isinstance(checked, TimingReason):
            return _invalid(checked)
        first_crack_span = checked

    sample_present = sample is not None
    sample_span: _Span | None = None
    bean_temp: tuple[Fraction, Fraction] | None = None
    if sample is not None:
        if type(sample) is not BeanTempSample:
            return _invalid(TimingReason.INVALID_OPERAND)
        if sample.acquisition is not None:
            checked = _interval(sample.acquisition)
            if isinstance(checked, TimingReason):
                return _invalid(checked)
            sample_span = checked
        if sample.bean_temp is not None:
            bounds = _bean_temp_bounds(sample.bean_temp)
            if isinstance(bounds, TimingReason):
                return _invalid(bounds)
            bean_temp = bounds

    present = [span for span in (charge_span, first_crack_span, sample_span) if span is not None]
    for span in present[1:]:
        if not _same_domain(present[0].domain, span.domain):
            return _invalid(TimingReason.DOMAIN_MISMATCH)

    if (
        charge_span is not None
        and first_crack_span is not None
        and not charge_span.hi <= first_crack_span.lo
    ):
        return _invalid(TimingReason.CHARGE_FIRST_CRACK_UNORDERED)

    if charge_span is None:
        return _unavailable(TimingReason.CHARGE_ABSENT)
    if first_crack_span is None:
        return _unavailable(TimingReason.FIRST_CRACK_ABSENT)
    if not sample_present:
        return _unavailable(TimingReason.SAMPLE_ABSENT)
    if sample_span is None:
        return _unavailable(TimingReason.SAMPLE_ACQUISITION_UNQUALIFIED)
    if bean_temp is None:
        return _unavailable(TimingReason.BEAN_TEMP_ABSENT)

    if not first_crack_span.hi <= sample_span.lo:
        return _unresolved(TimingVerdict.INDETERMINATE, TimingReason.SAMPLE_NOT_AFTER_FIRST_CRACK)

    if not sample_span.lo > charge_span.hi:
        return _invalid(TimingReason.ZERO_DURATION)

    lower = 100 * (sample_span.lo - first_crack_span.hi) / (sample_span.lo - charge_span.lo)
    upper = 100 * (sample_span.hi - first_crack_span.lo) / (sample_span.hi - charge_span.hi)

    development = _classify_development(lower, upper, minimum)
    bean_temp_comparison = _classify_bean_temp(bean_temp[0], bean_temp[1], bean_temp_target)
    comparisons = (development, bean_temp_comparison)
    if TargetComparison.BELOW in comparisons:
        verdict, reason = TimingVerdict.NOT_MET, TimingReason.TARGET_BELOW
    elif TargetComparison.STRADDLES in comparisons:
        verdict, reason = TimingVerdict.INDETERMINATE, TimingReason.TARGET_STRADDLES
    else:
        verdict, reason = TimingVerdict.MET, TimingReason.TARGETS_MET
    return TimingEvaluation(verdict, reason, lower, upper, development, bean_temp_comparison)
