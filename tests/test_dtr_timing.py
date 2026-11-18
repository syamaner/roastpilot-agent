"""Tests for the pure conservative HUMAN recipe timing evaluator (D238 T1, #1095)."""

import ast
import dataclasses
import itertools
import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest

from roastpilot_agent.dtr_timing import (
    BeanTempBounds,
    BeanTempSample,
    RecipeTimingTarget,
    TargetComparison,
    TimingDomain,
    TimingEvaluation,
    TimingInterval,
    TimingReason,
    TimingVerdict,
    evaluate_recipe_timing,
)

_REPO = Path(__file__).resolve().parents[1]
_PACKAGE = _REPO / "src" / "roastpilot_agent"
_MODULE = _PACKAGE / "dtr_timing.py"

D = TimingDomain("session-1", "epoch-1")
ABOVE = TargetComparison.AT_OR_ABOVE
BELOW = TargetComparison.BELOW
STRADDLES = TargetComparison.STRADDLES
MET = TimingVerdict.MET
NOT_MET = TimingVerdict.NOT_MET
INDETERMINATE = TimingVerdict.INDETERMINATE
INVALID = TimingVerdict.INVALID
UNAVAILABLE = TimingVerdict.UNAVAILABLE
R = TimingReason


class _FloatSub(float):
    pass


class _StrSub(str):
    pass


class _SubDomain(TimingDomain):
    pass


class _SubInterval(TimingInterval):
    pass


class _SubBounds(BeanTempBounds):
    pass


class _SubSample(BeanTempSample):
    pass


class _SubTarget(RecipeTimingTarget):
    pass


# Baseline: C=[0,0], F=[86,86], S=[100,100] -> L=U=14 exactly; BT [205,210] vs 200.
_BASE: dict[str, Any] = {
    "c_lo": 0,
    "c_hi": 0,
    "f_lo": 86,
    "f_hi": 86,
    "s_lo": 100,
    "s_hi": 100,
    "bt_lo": 205,
    "bt_hi": 210,
    "min": 14,
    "bt_target": 200,
}


def _iv(lo: object, hi: object, domain: object = D) -> TimingInterval:
    return TimingInterval(cast(Any, domain), cast(Any, lo), cast(Any, hi))


def _bt(lo: object, hi: object) -> BeanTempBounds:
    return BeanTempBounds(cast(Any, lo), cast(Any, hi))


def _kwargs(**overrides: object) -> dict[str, object]:
    v: dict[str, Any] = {**_BASE, **overrides}
    return {
        "target": RecipeTimingTarget(v["min"], v["bt_target"]),
        "charge": _iv(v["c_lo"], v["c_hi"]),
        "first_crack": _iv(v["f_lo"], v["f_hi"]),
        "sample": BeanTempSample(_iv(v["s_lo"], v["s_hi"]), _bt(v["bt_lo"], v["bt_hi"])),
    }


def _call(**kwargs: object) -> TimingEvaluation:
    result = cast(Any, evaluate_recipe_timing)(**kwargs)
    assert type(result) is TimingEvaluation
    return result


def _run(**overrides: object) -> TimingEvaluation:
    return _call(**_kwargs(**overrides))


def _expect(
    result: TimingEvaluation,
    verdict: TimingVerdict,
    reason: TimingReason,
    lower: Fraction | None = None,
    upper: Fraction | None = None,
    development: TargetComparison | None = None,
    bean_temp: TargetComparison | None = None,
) -> None:
    assert result == TimingEvaluation(verdict, reason, lower, upper, development, bean_temp)
    for bound in (result.development_lower_percent, result.development_upper_percent):
        assert bound is None or type(bound) is Fraction


def _assert_invariants(result: TimingEvaluation) -> None:
    parts = (
        result.development_lower_percent,
        result.development_upper_percent,
        result.development,
        result.bean_temp,
    )
    classified = result.reason in {R.TARGET_BELOW, R.TARGET_STRADDLES, R.TARGETS_MET}
    if classified:
        assert all(part is not None for part in parts)
        assert type(result.development_lower_percent) is Fraction
        assert type(result.development_upper_percent) is Fraction
    else:
        assert all(part is None for part in parts)
    comparisons = (result.development, result.bean_temp)
    assert (result.verdict is MET) == (comparisons == (ABOVE, ABOVE))
    assert (result.verdict is MET) == (result.reason is R.TARGETS_MET)


# --- Behaviour: development comparison (BT kept definitely met) -------------------


def test_met_when_lower_bound_equals_minimum() -> None:
    _expect(_run(), MET, R.TARGETS_MET, Fraction(14), Fraction(14), ABOVE, ABOVE)


def test_lower_bound_equality_float_target() -> None:
    _expect(_run(min=14.0), MET, R.TARGETS_MET, Fraction(14), Fraction(14), ABOVE, ABOVE)


def test_not_met_below() -> None:
    _expect(_run(min=15), NOT_MET, R.TARGET_BELOW, Fraction(14), Fraction(14), BELOW, ABOVE)


def test_development_straddle_indeterminate() -> None:
    result = _run(s_hi=120, min=20)
    _expect(
        result, INDETERMINATE, R.TARGET_STRADDLES, Fraction(14), Fraction(85, 3), STRADDLES, ABOVE
    )


def test_upper_equals_minimum_is_straddle() -> None:
    result = _run(f_lo=80, f_hi=80, s_hi=200, min=60)
    _expect(result, INDETERMINATE, R.TARGET_STRADDLES, Fraction(20), Fraction(60), STRADDLES, ABOVE)


# --- Exact arithmetic regressions (fixed witnesses; preconditions asserted) -------


def _point_case(c: float | int, f: float | int, s: float | int, minimum: float) -> TimingEvaluation:
    return _run(c_lo=c, c_hi=c, f_lo=f, f_hi=f, s_lo=s, s_hi=s, min=minimum)


def test_exact_rational_not_float_rounded() -> None:
    c, f, s, minimum = 0, 2, 3, 33.333333333333336
    exact = 100 * (Fraction(s) - Fraction(f)) / (Fraction(s) - Fraction(c))
    assert 100 * (s - f) / (s - c) >= minimum
    assert exact < Fraction(minimum)
    _expect(_point_case(c, f, s, minimum), NOT_MET, R.TARGET_BELOW, exact, exact, BELOW, ABOVE)


def test_subtraction_not_float_rounded() -> None:
    c, f, s, minimum = 0.3, 0.7, 10.3, 96.00000000000001
    exact = 100 * (Fraction(s) - Fraction(f)) / (Fraction(s) - Fraction(c))
    assert exact < Fraction(minimum)
    assert 100 * Fraction(s - f) / Fraction(s - c) >= Fraction(minimum)
    _expect(_point_case(c, f, s, minimum), NOT_MET, R.TARGET_BELOW, exact, exact, BELOW, ABOVE)


def test_multiplication_not_float_rounded() -> None:
    c, f, s, minimum = 0.0, 0.3, 1.1, 72.72727272727273
    exact = 100 * (Fraction(s) - Fraction(f)) / (Fraction(s) - Fraction(c))
    assert exact < Fraction(minimum)
    assert (Fraction(100 * s) - Fraction(100 * f)) / (Fraction(s) - Fraction(c)) >= Fraction(
        minimum
    )
    _expect(_point_case(c, f, s, minimum), NOT_MET, R.TARGET_BELOW, exact, exact, BELOW, ABOVE)


# --- Behaviour: bean temperature (development kept definitely met) ----------------

_L14 = Fraction(14)


@pytest.mark.parametrize(("lo", "hi"), [(200, 200), (200, 250), (200.0, 200.0)])
def test_bt_inclusive_equality_met(lo: float, hi: float) -> None:
    _expect(_run(bt_lo=lo, bt_hi=hi), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)


@pytest.mark.parametrize(("lo", "hi"), [(199, 200), (199, 201), (199.999, 300)])
def test_bt_straddle_indeterminate(lo: float, hi: float) -> None:
    result = _run(bt_lo=lo, bt_hi=hi)
    _expect(result, INDETERMINATE, R.TARGET_STRADDLES, _L14, _L14, ABOVE, STRADDLES)


@pytest.mark.parametrize(("lo", "hi"), [(150, 199.999), (-10, -5)])
def test_bt_below_not_met(lo: float, hi: float) -> None:
    _expect(_run(bt_lo=lo, bt_hi=hi), NOT_MET, R.TARGET_BELOW, _L14, _L14, ABOVE, BELOW)


# --- Joint table -------------------------------------------------------------------

_DEV_MIN = {ABOVE: 14, STRADDLES: 20, BELOW: 30}
_BT_TARGET = {ABOVE: 205, STRADDLES: 207, BELOW: 211}


@pytest.mark.parametrize(
    ("development", "bean_temp", "verdict", "reason"),
    [
        (ABOVE, ABOVE, MET, R.TARGETS_MET),
        (ABOVE, STRADDLES, INDETERMINATE, R.TARGET_STRADDLES),
        (ABOVE, BELOW, NOT_MET, R.TARGET_BELOW),
        (STRADDLES, ABOVE, INDETERMINATE, R.TARGET_STRADDLES),
        (STRADDLES, STRADDLES, INDETERMINATE, R.TARGET_STRADDLES),
        (STRADDLES, BELOW, NOT_MET, R.TARGET_BELOW),
        (BELOW, ABOVE, NOT_MET, R.TARGET_BELOW),
        (BELOW, STRADDLES, NOT_MET, R.TARGET_BELOW),
        (BELOW, BELOW, NOT_MET, R.TARGET_BELOW),
    ],
)
def test_joint_grid(
    development: TargetComparison,
    bean_temp: TargetComparison,
    verdict: TimingVerdict,
    reason: TimingReason,
) -> None:
    result = _run(s_hi=120, min=_DEV_MIN[development], bt_target=_BT_TARGET[bean_temp])
    _expect(result, verdict, reason, _L14, Fraction(85, 3), development, bean_temp)


# --- Zero / reversed duration and chronology --------------------------------------


@pytest.mark.parametrize(
    ("c", "f", "s"),
    [((5, 5), (5, 5), (5, 5)), ((0, 10), (10, 10), (10, 10)), ((0, 0), (0, 0), (0, 0))],
)
def test_zero_duration_invalid_no_raise(
    c: tuple[int, int], f: tuple[int, int], s: tuple[int, int]
) -> None:
    result = _run(c_lo=c[0], c_hi=c[1], f_lo=f[0], f_hi=f[1], s_lo=s[0], s_hi=s[1])
    _expect(result, INVALID, R.ZERO_DURATION)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"c_lo": 1, "c_hi": 0}, id="charge"),
        pytest.param({"f_lo": 90, "f_hi": 80}, id="first_crack"),
        pytest.param({"s_lo": 110, "s_hi": 100}, id="sample"),
        pytest.param({"bt_lo": 210, "bt_hi": 205}, id="bt"),
    ],
)
def test_reversed_interval_invalid(overrides: dict[str, object]) -> None:
    _expect(_run(**overrides), INVALID, R.INVALID_REVERSED_BOUNDS)


def test_zero_uncertainty_point_valid() -> None:
    _expect(_run(bt_lo=205, bt_hi=205), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)
    result = _run(c_lo=1.5, c_hi=1.5, f_lo=87.5, f_hi=87.5, s_lo=101.5, s_hi=101.5)
    _expect(result, MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)


def test_charge_after_fc_invalid() -> None:
    _expect(_run(c_lo=90, c_hi=90), INVALID, R.CHARGE_FIRST_CRACK_UNORDERED)


def test_charge_fc_overlap_invalid() -> None:
    _expect(_run(c_hi=87, f_lo=86, f_hi=88), INVALID, R.CHARGE_FIRST_CRACK_UNORDERED)


def test_charge_equals_fc_point_allowed() -> None:
    result = _run(c_lo=0, c_hi=50, f_lo=50, f_hi=50, min=50)
    _expect(result, MET, R.TARGETS_MET, Fraction(50), Fraction(100), ABOVE, ABOVE)


def test_fhi_equals_slo_allowed() -> None:
    result = _run(s_lo=86, s_hi=100)
    _expect(result, INDETERMINATE, R.TARGET_STRADDLES, Fraction(0), _L14, STRADDLES, ABOVE)


def test_sample_overlaps_fc_indeterminate() -> None:
    result = _run(f_lo=80, f_hi=90, s_lo=85, s_hi=100)
    _expect(result, INDETERMINATE, R.SAMPLE_NOT_AFTER_FIRST_CRACK)


def test_sample_precedes_fc_indeterminate() -> None:
    result = _run(s_lo=50, s_hi=60)
    _expect(result, INDETERMINATE, R.SAMPLE_NOT_AFTER_FIRST_CRACK)


# --- Invalid scalar inputs ---------------------------------------------------------

_BAD_SCALARS: list[Any] = [
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param("14", id="str"),
    pytest.param(None, id="none"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(-math.inf, id="-inf"),
    pytest.param(_FloatSub(14.0), id="float-subclass"),
    pytest.param(Decimal("14"), id="decimal"),
    pytest.param(Fraction(14), id="fraction"),
    pytest.param(2**53 + 1, id="2**53+1"),
    pytest.param(-(2**53 + 1), id="-(2**53+1)"),
]

_SCALAR_POSITIONS: list[Any] = [
    pytest.param(key, reason, id=key)
    for key, reason in [
        ("c_lo", R.INVALID_OPERAND),
        ("c_hi", R.INVALID_OPERAND),
        ("f_lo", R.INVALID_OPERAND),
        ("f_hi", R.INVALID_OPERAND),
        ("s_lo", R.INVALID_OPERAND),
        ("s_hi", R.INVALID_OPERAND),
        ("bt_lo", R.INVALID_OPERAND),
        ("bt_hi", R.INVALID_OPERAND),
        ("min", R.INVALID_TARGET),
        ("bt_target", R.INVALID_TARGET),
    ]
]


@pytest.mark.parametrize("value", _BAD_SCALARS)
@pytest.mark.parametrize(("key", "reason"), _SCALAR_POSITIONS)
def test_invalid_operand(key: str, reason: TimingReason, value: object) -> None:
    _expect(_run(**{key: value}), INVALID, reason)


def test_int_limit_inclusive() -> None:
    big = 2**53
    upper = Fraction(100 * (big - 86), big)
    _expect(_run(s_hi=big), MET, R.TARGETS_MET, _L14, upper, ABOVE, ABOVE)
    _expect(_run(bt_hi=big), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)
    result = _run(bt_lo=-big)
    _expect(result, INDETERMINATE, R.TARGET_STRADDLES, _L14, _L14, ABOVE, STRADDLES)
    _expect(_run(bt_target=big), NOT_MET, R.TARGET_BELOW, _L14, _L14, ABOVE, BELOW)


@pytest.mark.parametrize("key", ["c_lo", "f_lo", "s_lo"])
@pytest.mark.parametrize("value", [-1e-300, -0.5, -1])
def test_negative_time_invalid(key: str, value: float) -> None:
    _expect(_run(**{key: value}), INVALID, R.INVALID_NEGATIVE_TIME)


def test_negative_time_checked_before_reversal() -> None:
    _expect(_run(c_lo=-1, c_hi=-2), INVALID, R.INVALID_NEGATIVE_TIME)


@pytest.mark.parametrize(
    ("c_lo", "c_hi"),
    [(0, 0), (-0.0, -0.0), (-0.0, 0), (0.0, 0.0)],
    ids=["0", "-0.0", "mixed", "0.0"],
)
def test_zero_time_valid(c_lo: float, c_hi: float) -> None:
    _expect(_run(c_lo=c_lo, c_hi=c_hi), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)


def test_zero_first_crack_time_valid() -> None:
    result = _run(f_lo=0, f_hi=0, s_lo=100, s_hi=100)
    _expect(result, MET, R.TARGETS_MET, Fraction(100), Fraction(100), ABOVE, ABOVE)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"min": 0}, id="min-0"),
        pytest.param({"min": 0.0}, id="min-0.0"),
        pytest.param({"min": -0.0}, id="min--0.0"),
        pytest.param({"min": -1}, id="min--1"),
        pytest.param({"min": 100}, id="min-100"),
        pytest.param({"min": 100.0}, id="min-100.0"),
        pytest.param({"min": 150}, id="min-150"),
        pytest.param({"bt_target": 0}, id="bt-0"),
        pytest.param({"bt_target": 0.0}, id="bt-0.0"),
        pytest.param({"bt_target": -1}, id="bt--1"),
    ],
)
def test_invalid_target(overrides: dict[str, object]) -> None:
    _expect(_run(**overrides), INVALID, R.INVALID_TARGET)


def test_valid_target_extremes() -> None:
    _expect(_run(min=1e-300), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)
    _expect(_run(min=99.999), NOT_MET, R.TARGET_BELOW, _L14, _L14, BELOW, ABOVE)
    _expect(_run(bt_target=1e-300), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)


# --- Domains -----------------------------------------------------------------------

_BAD_TOKENS: list[Any] = [
    pytest.param("", id="empty"),
    pytest.param(1, id="int"),
    pytest.param(None, id="none"),
    pytest.param(True, id="bool"),
    pytest.param(b"session-1", id="bytes"),
    pytest.param(_StrSub("session-1"), id="str-subclass"),
]


def _with_domain(position: str, domain: object) -> dict[str, object]:
    kwargs = _kwargs()
    if position == "charge":
        kwargs["charge"] = _iv(0, 0, domain)
    elif position == "first_crack":
        kwargs["first_crack"] = _iv(86, 86, domain)
    else:
        kwargs["sample"] = BeanTempSample(_iv(100, 100, domain), _bt(205, 210))
    return kwargs


_POSITIONS = ["charge", "first_crack", "acquisition"]


@pytest.mark.parametrize("token", _BAD_TOKENS)
@pytest.mark.parametrize("field", ["session_id", "clock_epoch"])
@pytest.mark.parametrize("position", _POSITIONS)
def test_invalid_domain(position: str, field: str, token: object) -> None:
    domain = dataclasses.replace(D, **{field: cast(Any, token)})
    _expect(_call(**_with_domain(position, domain)), INVALID, R.INVALID_DOMAIN)


@pytest.mark.parametrize(
    "domain",
    [
        pytest.param(None, id="none"),
        pytest.param(("session-1", "epoch-1"), id="tuple"),
        pytest.param("session-1", id="str"),
        pytest.param(_SubDomain("session-1", "epoch-1"), id="subclass"),
    ],
)
@pytest.mark.parametrize("position", _POSITIONS)
def test_invalid_domain_object(position: str, domain: object) -> None:
    _expect(_call(**_with_domain(position, domain)), INVALID, R.INVALID_DOMAIN)


_OTHER = {
    "session_id": TimingDomain("session-2", "epoch-1"),
    "clock_epoch": TimingDomain("session-1", "epoch-2"),
}


@pytest.mark.parametrize("token", ["session_id", "clock_epoch"])
@pytest.mark.parametrize("odd", _POSITIONS)
def test_domain_mismatch_invalid(odd: str, token: str) -> None:
    _expect(_call(**_with_domain(odd, _OTHER[token])), INVALID, R.DOMAIN_MISMATCH)


@pytest.mark.parametrize("token", ["session_id", "clock_epoch"])
@pytest.mark.parametrize(
    ("absent", "odd"),
    [("sample", "first_crack"), ("first_crack", "acquisition"), ("charge", "acquisition")],
    ids=["C-F", "C-S", "F-S"],
)
def test_domain_mismatch_pairwise(absent: str, odd: str, token: str) -> None:
    kwargs = _with_domain(odd, _OTHER[token])
    kwargs[absent] = None
    _expect(_call(**kwargs), INVALID, R.DOMAIN_MISMATCH)


def test_equal_domain_by_value() -> None:
    kwargs = _with_domain("acquisition", TimingDomain("session-1", "epoch-1"))
    _expect(_call(**kwargs), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)


# --- Availability and precedence ---------------------------------------------------


def _absent(slot: str) -> dict[str, object]:
    kwargs = _kwargs()
    if slot == "acquisition":
        kwargs["sample"] = BeanTempSample(None, _bt(205, 210))
    elif slot == "bean_temp":
        kwargs["sample"] = BeanTempSample(_iv(100, 100), None)
    else:
        kwargs[slot] = None
    return kwargs


@pytest.mark.parametrize(
    ("slot", "reason"),
    [
        ("charge", R.CHARGE_ABSENT),
        ("first_crack", R.FIRST_CRACK_ABSENT),
        ("sample", R.SAMPLE_ABSENT),
        ("acquisition", R.SAMPLE_ACQUISITION_UNQUALIFIED),
        ("bean_temp", R.BEAN_TEMP_ABSENT),
    ],
)
def test_unavailable_never_met(slot: str, reason: TimingReason) -> None:
    _expect(_call(**_absent(slot)), UNAVAILABLE, reason)


# Only ``None`` means absent. Every other falsy value in an optional slot is a
# wrong-typed operand, so a truthiness check in place of ``is not None`` would
# misreport it as absent (UNAVAILABLE) instead of INVALID.
_EMPTY_LIST: list[object] = []
_EMPTY_TUPLE: tuple[object, ...] = ()
_EMPTY_DICT: dict[str, object] = {}
_FALSY_VALUES = [
    pytest.param(0, id="int-0"),
    pytest.param(0.0, id="float-0.0"),
    pytest.param(False, id="false"),
    pytest.param("", id="empty-str"),
    pytest.param(_EMPTY_LIST, id="empty-list"),
    pytest.param(_EMPTY_TUPLE, id="empty-tuple"),
    pytest.param(_EMPTY_DICT, id="empty-dict"),
]


def _falsy_in(slot: str, value: object) -> dict[str, object]:
    kwargs = _kwargs()
    if slot == "acquisition":
        kwargs["sample"] = BeanTempSample(cast(Any, value), _bt(205, 210))
    elif slot == "bean_temp":
        kwargs["sample"] = BeanTempSample(_iv(100, 100), cast(Any, value))
    else:
        kwargs[slot] = value
    return kwargs


def _check_falsy(slot: str, value: object) -> None:
    assert not value and value is not None
    _expect(_call(**_falsy_in(slot, value)), INVALID, R.INVALID_OPERAND)


@pytest.mark.parametrize("value", _FALSY_VALUES)
def test_falsy_charge_invalid_not_absent(value: object) -> None:
    _check_falsy("charge", value)


@pytest.mark.parametrize("value", _FALSY_VALUES)
def test_falsy_first_crack_invalid_not_absent(value: object) -> None:
    _check_falsy("first_crack", value)


@pytest.mark.parametrize("value", _FALSY_VALUES)
def test_falsy_sample_invalid_not_absent(value: object) -> None:
    _check_falsy("sample", value)


@pytest.mark.parametrize("value", _FALSY_VALUES)
def test_falsy_sample_acquisition_invalid_not_absent(value: object) -> None:
    _check_falsy("acquisition", value)


@pytest.mark.parametrize("value", _FALSY_VALUES)
def test_falsy_sample_bean_temp_invalid_not_absent(value: object) -> None:
    _check_falsy("bean_temp", value)


@pytest.mark.parametrize(
    "domain",
    [
        pytest.param(_SubDomain("session-1", "epoch-1"), id="subclassed-domain"),
        pytest.param(_iv(0, 0), id="interval-as-domain"),
        pytest.param(_bt(0, 0), id="bounds-as-domain"),
        pytest.param(RecipeTimingTarget(14, 200), id="target-as-domain"),
    ],
)
@pytest.mark.parametrize("position", _POSITIONS)
def test_wrong_or_subclassed_domain_is_invalid_domain(position: str, domain: object) -> None:
    # Contract step 2(b): the domain must be an exact TimingDomain; a wrong exact type
    # or a TimingDomain subclass (even with valid, matching tokens) is INVALID_DOMAIN.
    _expect(_call(**_with_domain(position, domain)), INVALID, R.INVALID_DOMAIN)


def test_absent_is_not_zero() -> None:
    # A charge at exactly 0 s qualifies as MET; an absent charge must not.
    _expect(_run(), MET, R.TARGETS_MET, _L14, _L14, ABOVE, ABOVE)
    _expect(_call(**_absent("charge")), UNAVAILABLE, R.CHARGE_ABSENT)
    # Absent BT is not read as 0 C, even against a target just above zero.
    kwargs = _absent("bean_temp")
    kwargs["target"] = RecipeTimingTarget(14, 1e-300)
    _expect(_call(**kwargs), UNAVAILABLE, R.BEAN_TEMP_ABSENT)


def test_availability_order() -> None:
    _expect(
        _call(target=_kwargs()["target"], charge=None, first_crack=None, sample=None),
        UNAVAILABLE,
        R.CHARGE_ABSENT,
    )
    kwargs = _absent("first_crack")
    kwargs["sample"] = None
    _expect(_call(**kwargs), UNAVAILABLE, R.FIRST_CRACK_ABSENT)
    kwargs = _kwargs()
    kwargs["sample"] = BeanTempSample(None, None)
    _expect(_call(**kwargs), UNAVAILABLE, R.SAMPLE_ACQUISITION_UNQUALIFIED)


def test_precedence_invalid_over_unavailable() -> None:
    kwargs = _absent("first_crack")
    kwargs["charge"] = _iv(1, 0)
    _expect(_call(**kwargs), INVALID, R.INVALID_REVERSED_BOUNDS)

    kwargs = _absent("charge")
    kwargs["sample"] = BeanTempSample(_iv(100, 100), _bt(210, 205))
    _expect(_call(**kwargs), INVALID, R.INVALID_REVERSED_BOUNDS)

    kwargs = _absent("bean_temp")
    kwargs["charge"] = _iv(0, 0, TimingDomain("session-2", "epoch-1"))
    _expect(_call(**kwargs), INVALID, R.DOMAIN_MISMATCH)

    _expect(
        _call(target=RecipeTimingTarget(0, 200), charge=None, first_crack=None, sample=None),
        INVALID,
        R.INVALID_TARGET,
    )
    kwargs = _absent("acquisition")
    kwargs["charge"] = _iv(-1, 0)
    _expect(_call(**kwargs), INVALID, R.INVALID_NEGATIVE_TIME)


def test_cf_unordered_beats_absent_sample() -> None:
    kwargs = _absent("sample")
    kwargs["charge"] = _iv(90, 90)
    _expect(_call(**kwargs), INVALID, R.CHARGE_FIRST_CRACK_UNORDERED)
    kwargs = _absent("acquisition")
    kwargs["charge"] = _iv(0, 87)
    _expect(_call(**kwargs), INVALID, R.CHARGE_FIRST_CRACK_UNORDERED)


# Explicit pairwise precedence witnesses: each fixture fails two adjacent checks, so
# swapping those two checks changes the full result.
_OTHER_SESSION = TimingDomain("session-2", "epoch-1")


def test_sample_not_after_fc_precedes_zero_duration() -> None:
    # Fhi=10 > Slo=0 (step 6) and Slo=0 == Chi=0 (step 7): step 6 wins.
    result = _run(c_lo=0, c_hi=0, f_lo=0, f_hi=10, s_lo=0, s_hi=20)
    _expect(result, INDETERMINATE, R.SAMPLE_NOT_AFTER_FIRST_CRACK)


def test_charge_absent_precedes_sample_not_after_fc() -> None:
    # Absent charge (step 5) and S=[50,60] before F=[86,86] (step 6): step 5 wins.
    kwargs = _kwargs(s_lo=50, s_hi=60)
    kwargs["charge"] = None
    _expect(_call(**kwargs), UNAVAILABLE, R.CHARGE_ABSENT)


def test_cf_unordered_precedes_sample_not_after_fc() -> None:
    # C=[90,90] after F=[86,86] (step 4) and S=[50,60] before F (step 6): step 4 wins.
    result = _run(c_lo=90, c_hi=90, s_lo=50, s_hi=60)
    _expect(result, INVALID, R.CHARGE_FIRST_CRACK_UNORDERED)


def test_bt_validity_precedes_domain_mismatch() -> None:
    # Reversed BT bounds (step 2) and a charge in another session (step 3): step 2 wins.
    kwargs = _kwargs(bt_lo=210, bt_hi=205)
    kwargs["charge"] = _iv(0, 0, _OTHER_SESSION)
    _expect(_call(**kwargs), INVALID, R.INVALID_REVERSED_BOUNDS)


def test_domain_mismatch_precedes_cf_unordered() -> None:
    # Charge C=[90,90] in another session (step 3) and after F=[86,86] (step 4): step 3 wins.
    kwargs = _kwargs()
    kwargs["charge"] = _iv(90, 90, _OTHER_SESSION)
    _expect(_call(**kwargs), INVALID, R.DOMAIN_MISMATCH)


def test_operand_check_order() -> None:
    # Charge is checked before first crack, first crack before the sample.
    kwargs = _kwargs()
    kwargs["charge"] = _iv(-1, 0)
    kwargs["first_crack"] = _iv(90, 80)
    kwargs["sample"] = "not-a-sample"
    _expect(_call(**kwargs), INVALID, R.INVALID_NEGATIVE_TIME)
    kwargs["charge"] = _iv(0, 0)
    _expect(_call(**kwargs), INVALID, R.INVALID_REVERSED_BOUNDS)
    kwargs["first_crack"] = _iv(86, 86)
    _expect(_call(**kwargs), INVALID, R.INVALID_OPERAND)
    # Within the sample: acquisition before BT bounds.
    kwargs["sample"] = BeanTempSample(_iv(-1, 100), _bt("x", 1))
    _expect(_call(**kwargs), INVALID, R.INVALID_NEGATIVE_TIME)
    # Within an interval: domain, then scalars, then sign, then ordering.
    kwargs["sample"] = BeanTempSample(_iv("x", -1, TimingDomain("", "e")), None)
    _expect(_call(**kwargs), INVALID, R.INVALID_DOMAIN)
    kwargs["sample"] = BeanTempSample(_iv(-1, "x"), None)
    _expect(_call(**kwargs), INVALID, R.INVALID_OPERAND)


# --- Exact type / subclass rejection -----------------------------------------------


@pytest.mark.parametrize(
    ("slot", "value", "reason"),
    [
        pytest.param("charge", _SubInterval(D, 0, 0), R.INVALID_OPERAND, id="charge-sub"),
        pytest.param("first_crack", _SubInterval(D, 86, 86), R.INVALID_OPERAND, id="fc-sub"),
        pytest.param(
            "sample",
            BeanTempSample(_SubInterval(D, 100, 100), _bt(205, 210)),
            R.INVALID_OPERAND,
            id="acquisition-sub",
        ),
        pytest.param(
            "sample", _SubSample(_iv(100, 100), _bt(205, 210)), R.INVALID_OPERAND, id="sample-sub"
        ),
        pytest.param(
            "sample",
            BeanTempSample(_iv(100, 100), _SubBounds(205, 210)),
            R.INVALID_OPERAND,
            id="bounds-sub",
        ),
        pytest.param("target", _SubTarget(14, 200), R.INVALID_TARGET, id="target-sub"),
        pytest.param("charge", "0", R.INVALID_OPERAND, id="charge-str"),
        pytest.param("first_crack", _bt(86, 86), R.INVALID_OPERAND, id="fc-bounds"),
        pytest.param("sample", _iv(100, 100), R.INVALID_OPERAND, id="sample-interval"),
        pytest.param(
            "sample",
            BeanTempSample(cast(Any, _bt(100, 100)), _bt(205, 210)),
            R.INVALID_OPERAND,
            id="acquisition-bounds",
        ),
        pytest.param(
            "sample",
            BeanTempSample(_iv(100, 100), cast(Any, _iv(205, 210))),
            R.INVALID_OPERAND,
            id="bounds-interval",
        ),
        pytest.param("target", None, R.INVALID_TARGET, id="target-none"),
        pytest.param("target", (14, 200), R.INVALID_TARGET, id="target-tuple"),
    ],
)
def test_subclass_operand_invalid(slot: str, value: object, reason: TimingReason) -> None:
    kwargs = _kwargs()
    kwargs[slot] = value
    _expect(_call(**kwargs), INVALID, reason)


# --- Translation invariance, delayed response, conservativeness --------------------

_TRANSLATION_CASES: list[dict[str, object]] = [
    {},
    {"s_hi": 120, "min": 20},
    {"c_lo": 0.5, "c_hi": 1.5, "f_lo": 86.25, "f_hi": 87, "s_lo": 100, "s_hi": 101.75},
    {"f_lo": 80, "f_hi": 90, "s_lo": 85, "s_hi": 100},
    {"min": 15},
]


@pytest.mark.parametrize("case", _TRANSLATION_CASES)
@pytest.mark.parametrize("shift", [0, 1, 1000, 2**20])
def test_translation_invariance(case: dict[str, Any], shift: int) -> None:
    base = _run(**case)
    times = {key: case.get(key, _BASE[key]) for key in ("c_lo", "c_hi", "f_lo", "f_hi")}
    times |= {key: case.get(key, _BASE[key]) for key in ("s_lo", "s_hi")}
    shifted = {key: value + shift for key, value in times.items()}
    for key, value in times.items():
        assert Fraction(shifted[key]) - shift == Fraction(value)  # exactly representable
    assert _run(**{**case, **shifted}) == base


def test_delayed_response_counterexample() -> None:
    true_sample = {"c_lo": 0, "c_hi": 0, "f_lo": 600, "f_hi": 600, "min": 15}
    exact = Fraction(100, 7)
    result = _run(**true_sample, s_lo=700, s_hi=700)
    _expect(result, NOT_MET, R.TARGET_BELOW, exact, exact, BELOW, ABOVE)
    # A naive point at delayed receipt R=720 would claim the target.
    assert Fraction(100 * (720 - 600), 720) >= 15
    receipt_bound = _run(**true_sample, s_lo=700, s_hi=720)
    _expect(
        receipt_bound, INDETERMINATE, R.TARGET_STRADDLES, exact, Fraction(50, 3), STRADDLES, ABOVE
    )
    assert receipt_bound.development_lower_percent == result.development_lower_percent


def test_bounds_contain_every_point_dtr() -> None:
    spans = [(lo, hi) for lo in range(5) for hi in range(lo, 5)]
    classified = 0
    for c, f, s in itertools.product(spans, repeat=3):
        result = _run(c_lo=c[0], c_hi=c[1], f_lo=f[0], f_hi=f[1], s_lo=s[0], s_hi=s[1], min=50)
        _assert_invariants(result)
        reachable = c[1] <= f[0] and f[1] <= s[0] and s[0] > c[1]
        assert (result.development_lower_percent is not None) == reachable
        if not reachable:
            assert result.verdict is not MET
            continue
        classified += 1
        lower = cast(Fraction, result.development_lower_percent)
        upper = cast(Fraction, result.development_upper_percent)
        assert 0 <= lower <= upper <= 100
        for cp in range(c[0], c[1] + 1):
            for fp in range(f[0], f[1] + 1):
                for sp in range(s[0], s[1] + 1):
                    assert lower <= Fraction(100 * (sp - fp), sp - cp) <= upper
    assert classified > 50


# --- Named operand-swap witnesses for L and U ---------------------------------------
# Fixture A: C=[0,0] F=[80,80] S=[100,200] -> L=20, U=60, min 50 straddles.
# Fixture B: C=[0,0] F=[70,90] S=[100,100] -> L=10, U=30, min 25 straddles.
# Fixture C: C=[0,20] F=[90,90] S=[100,100] -> L=10, U=25/2, min 12 straddles.
_A = {"f_lo": 80, "f_hi": 80, "s_hi": 200, "min": 50}
_B = {"f_lo": 70, "f_hi": 90, "min": 25}
_C = {"c_hi": 20, "f_lo": 90, "f_hi": 90, "min": 12}


def test_l_uses_slo() -> None:
    _expect(
        _run(**_A), INDETERMINATE, R.TARGET_STRADDLES, Fraction(20), Fraction(60), STRADDLES, ABOVE
    )


def test_l_uses_fhi() -> None:
    _expect(
        _run(**_B), INDETERMINATE, R.TARGET_STRADDLES, Fraction(10), Fraction(30), STRADDLES, ABOVE
    )


def test_l_uses_clo() -> None:
    result = _run(**_C)
    _expect(
        result, INDETERMINATE, R.TARGET_STRADDLES, Fraction(10), Fraction(25, 2), STRADDLES, ABOVE
    )


def test_u_uses_shi() -> None:
    _expect(
        _run(**_A), INDETERMINATE, R.TARGET_STRADDLES, Fraction(20), Fraction(60), STRADDLES, ABOVE
    )


def test_u_uses_flo() -> None:
    _expect(
        _run(**_B), INDETERMINATE, R.TARGET_STRADDLES, Fraction(10), Fraction(30), STRADDLES, ABOVE
    )


def test_u_uses_chi() -> None:
    result = _run(**_C)
    _expect(
        result, INDETERMINATE, R.TARGET_STRADDLES, Fraction(10), Fraction(25, 2), STRADDLES, ABOVE
    )


# --- Totality, invariants, immutability, closed grammar -----------------------------

_JUNK: list[object] = [
    None,
    0,
    -1,
    1.5,
    True,
    "x",
    "",
    b"x",
    object(),
    [],
    {},
    (),
    Decimal(1),
    Fraction(1),
    math.nan,
    math.inf,
    2**64,
    D,
    _iv(0, 0),
    _bt(205, 210),
    BeanTempSample(None, None),
    RecipeTimingTarget(14, 200),
]


def _mutated_kwargs() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    base = _kwargs()
    for junk in _JUNK:
        for slot in ("target", "charge", "first_crack", "sample"):
            cases.append({**base, slot: junk})
        for slot, fields in (
            ("target", ("min_development_percent", "bean_temp_c")),
            ("charge", ("domain", "lo", "hi")),
            ("first_crack", ("domain", "lo", "hi")),
            ("sample", ("acquisition", "bean_temp")),
        ):
            for field in fields:
                replaced = dataclasses.replace(cast(Any, base[slot]), **{field: junk})
                cases.append({**base, slot: replaced})
        sample = cast(BeanTempSample, base["sample"])
        for field in ("domain", "lo", "hi"):
            acquisition = dataclasses.replace(cast(Any, sample.acquisition), **{field: junk})
            cases.append({**base, "sample": BeanTempSample(acquisition, sample.bean_temp)})
        for field in ("lo_c", "hi_c"):
            bounds = dataclasses.replace(cast(Any, sample.bean_temp), **{field: junk})
            cases.append({**base, "sample": BeanTempSample(sample.acquisition, bounds)})
        for field in ("session_id", "clock_epoch"):
            domain = dataclasses.replace(D, **{field: cast(Any, junk)})
            cases.append({**base, "charge": _iv(0, 0, domain)})
    return cases


def test_evaluator_total() -> None:
    cases = _mutated_kwargs()
    assert len(cases) > 400
    for kwargs in cases:
        result = _call(**kwargs)
        assert type(result.verdict) is TimingVerdict
        assert type(result.reason) is TimingReason
        _assert_invariants(result)


def test_result_invariants() -> None:
    results = [_call(**kwargs) for kwargs in _mutated_kwargs()]
    results += [_run(**case) for case in _TRANSLATION_CASES]
    results += [_call(**_absent(slot)) for slot in ("charge", "sample", "bean_temp")]
    seen = {result.verdict for result in results}
    assert seen == set(TimingVerdict)
    for result in results:
        _assert_invariants(result)


def test_types_are_frozen() -> None:
    instances: list[object] = [
        D,
        _iv(0, 0),
        _bt(1, 2),
        BeanTempSample(None, None),
        RecipeTimingTarget(14, 200),
        _run(),
    ]
    for instance in instances:
        field = dataclasses.fields(cast(Any, instance))[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field, None)


def test_closed_plain_enums() -> None:
    assert [m.name for m in TimingVerdict] == [
        "MET",
        "NOT_MET",
        "INDETERMINATE",
        "INVALID",
        "UNAVAILABLE",
    ]
    assert [m.name for m in TargetComparison] == ["AT_OR_ABOVE", "BELOW", "STRADDLES"]
    assert len(TimingReason) == 17
    for enum_type in (TimingVerdict, TargetComparison, TimingReason):
        assert not issubclass(enum_type, str)
        assert not issubclass(enum_type, int)
    assert {f.name for f in dataclasses.fields(RecipeTimingTarget)} == {
        "min_development_percent",
        "bean_temp_c",
    }


# --- Import confinement --------------------------------------------------------------

_ALLOWED_IMPORTS = {"fractions", "math", "dataclasses", "enum", "typing"}


def test_module_import_allowlist() -> None:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "eval", "exec", "open"}
    assert imported
    assert imported <= _ALLOWED_IMPORTS


def test_no_production_importer() -> None:
    offenders: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path == _MODULE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            if any("dtr_timing" in name for name in names):
                offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []
