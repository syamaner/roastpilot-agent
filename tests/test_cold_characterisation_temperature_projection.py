"""Behavioural tests for the closed version-1 cold temperature projection leaf.

Admission is exercised directly (T1-T8).  Every accepted shape is valid data,
never a verdict: no envelope, freshness, or readiness claim follows.
"""

import ast
import json
import math
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation import temperature_projection as leaf
from roastpilot_agent.cold_characterisation.temperature_projection import (
    FIELD_NAMES,
    ColdTemperatureAgreement,
    ColdTemperatureConfiguredUnit,
    ColdTemperatureOutcome,
    ColdTemperatureProjectionFailure,
    ColdTemperatureReportedUnit,
    ColdTickTemperatureProjection,
    admit_cold_temperature_projection,
)

Failure = ColdTemperatureProjectionFailure
_MAX_COUNTER = 2**53 - 1
_ENUM_FIELDS: dict[str, type[Enum]] = {
    "outcome": ColdTemperatureOutcome,
    "configured_temperature_unit": ColdTemperatureConfiguredUnit,
    "reported_temperature_unit": ColdTemperatureReportedUnit,
    "value_agreement": ColdTemperatureAgreement,
}
_VALUE_FIELDS = FIELD_NAMES[2:]
_NULL_VALUES: dict[str, object] = {name: None for name in _VALUE_FIELDS}
#: One exactly typed, individually admitted non-null value per value field.
_SOME_VALUE: dict[str, object] = {
    "configured_temperature_unit": "celsius",
    "reported_temperature_unit": "celsius",
    "last_packet_valid": False,
    "last_packet_bean_temp_c": 20.0,
    "last_packet_env_temp_c": 20.0,
    "retained_bean_temp_c": 20.0,
    "retained_env_temp_c": 20.0,
    "value_agreement": "indeterminate",
    "status_packet_count": 0,
    "ignored_temperature_packet_count": 0,
    "status_read_error_count": 0,
    "command_loop_error_count": 0,
}


def _shape(outcome: str, **values: object) -> dict[str, object]:
    """Return one raw projection in canonical order, nulls unless named."""
    raw: dict[str, object] = {"projection_version": 1, "outcome": outcome, **_NULL_VALUES}
    for name, value in values.items():
        assert name in raw, name
        raw[name] = value
    return raw


def celsius_agree(**changes: object) -> dict[str, object]:
    """Return an observed Celsius projection whose raw and typed values agree."""
    raw = _shape(
        "observed",
        configured_temperature_unit="celsius",
        reported_temperature_unit="celsius",
        last_packet_valid=True,
        last_packet_bean_temp_c=21.0,
        last_packet_env_temp_c=22.0,
        retained_bean_temp_c=21.0,
        retained_env_temp_c=22.0,
        value_agreement="agree",
        status_packet_count=5,
        ignored_temperature_packet_count=0,
        status_read_error_count=0,
        command_loop_error_count=0,
    )
    return _changed(raw, changes)


def _changed(raw: dict[str, object], changes: dict[str, object]) -> dict[str, object]:
    """Apply one-field-at-a-time changes to a known projection field set."""
    for name, value in changes.items():
        assert name in raw, name
        raw[name] = value
    return raw


def celsius_disagree(**changes: object) -> dict[str, object]:
    """Return an observed Celsius projection whose raw and typed values disagree."""
    raw = celsius_agree(retained_bean_temp_c=21.5, value_agreement="disagree")
    return _changed(raw, changes)


def fahrenheit(**changes: object) -> dict[str, object]:
    """Return an observed Fahrenheit projection (no Celsius last-packet values)."""
    raw = celsius_agree(
        configured_temperature_unit="fahrenheit",
        reported_temperature_unit="fahrenheit",
        last_packet_bean_temp_c=None,
        last_packet_env_temp_c=None,
        retained_bean_temp_c=70.0,
        retained_env_temp_c=71.0,
        value_agreement="indeterminate",
        status_packet_count=3,
        ignored_temperature_packet_count=1,
    )
    return _changed(raw, changes)


def unknown_all_ignored(**changes: object) -> dict[str, object]:
    """Return an observed projection whose every packet so far was ignored."""
    raw = celsius_agree(
        configured_temperature_unit="auto",
        reported_temperature_unit="unknown",
        last_packet_valid=False,
        last_packet_bean_temp_c=None,
        last_packet_env_temp_c=None,
        retained_bean_temp_c=None,
        retained_env_temp_c=None,
        value_agreement="indeterminate",
        status_packet_count=2,
        ignored_temperature_packet_count=2,
    )
    return _changed(raw, changes)


def unknown_partly_ignored(**changes: object) -> dict[str, object]:
    """Return an observed projection whose latest packet was ignored after a valid one."""
    raw = unknown_all_ignored(
        configured_temperature_unit="celsius",
        retained_bean_temp_c=20.0,
        retained_env_temp_c=21.0,
        status_packet_count=4,
        ignored_temperature_packet_count=1,
    )
    return _changed(raw, changes)


def awaiting(**changes: object) -> dict[str, object]:
    """Return an eligible projection awaiting its first status packet."""
    raw = _shape(
        "awaiting_first_packet",
        configured_temperature_unit="celsius",
        last_packet_valid=False,
        value_agreement="indeterminate",
        status_packet_count=0,
        ignored_temperature_packet_count=0,
        status_read_error_count=0,
        command_loop_error_count=0,
    )
    return _changed(raw, changes)


def members(raw: dict[str, object]) -> dict[str, object]:
    """Return the member form of a raw projection (test-side conversion only)."""
    values = dict(raw)
    for name, enum_type in _ENUM_FIELDS.items():
        token = values[name]
        if token is not None:
            values[name] = enum_type(token)
    return values


def valid_projection() -> ColdTickTemperatureProjection:
    """Return one admitted observed-Celsius projection in member form."""
    return ColdTickTemperatureProjection.model_validate(members(celsius_agree()))


ACCEPTED_SHAPES: list[tuple[str, dict[str, object]]] = [
    ("celsius-agree", celsius_agree()),
    ("celsius-disagree", celsius_disagree()),
    ("celsius-disagree-env", celsius_agree(retained_env_temp_c=22.5, value_agreement="disagree")),
    ("fahrenheit", fahrenheit()),
    ("unknown-all-ignored", unknown_all_ignored()),
    ("unknown-partly-ignored", unknown_partly_ignored()),
    ("awaiting", awaiting()),
    (
        "awaiting-auto-with-errors",
        awaiting(configured_temperature_unit="auto", status_read_error_count=3),
    ),
    ("not-eligible", _shape("not_eligible")),
    ("unsupported", _shape("unsupported")),
    ("malformed", _shape("malformed")),
    ("auto-celsius", celsius_agree(configured_temperature_unit="auto")),
    ("auto-fahrenheit", fahrenheit(configured_temperature_unit="auto")),
    ("auto-unknown", unknown_all_ignored(configured_temperature_unit="auto")),
    (
        "fahrenheit-configured-unknown",
        unknown_all_ignored(configured_temperature_unit="fahrenheit"),
    ),
    (
        "celsius-below-envelope",
        celsius_agree(
            last_packet_bean_temp_c=4.0,
            last_packet_env_temp_c=4.0,
            retained_bean_temp_c=4.0,
            retained_env_temp_c=4.0,
        ),
    ),
    (
        "celsius-above-envelope",
        celsius_agree(
            last_packet_bean_temp_c=41.0,
            last_packet_env_temp_c=41.0,
            retained_bean_temp_c=41.0,
            retained_env_temp_c=41.0,
        ),
    ),
    (
        "counter-max",
        celsius_agree(status_packet_count=_MAX_COUNTER, command_loop_error_count=_MAX_COUNTER),
    ),
    (
        "last-packet-bounds",
        celsius_agree(
            last_packet_bean_temp_c=0.0,
            last_packet_env_temp_c=65535.0,
            retained_bean_temp_c=0.0,
            retained_env_temp_c=65535.0,
        ),
    ),
    (
        "retained-unbounded",
        celsius_disagree(retained_bean_temp_c=-1e300, retained_env_temp_c=1e300),
    ),
]


@pytest.mark.parametrize(
    ("name", "raw"), ACCEPTED_SHAPES, ids=[case[0] for case in ACCEPTED_SHAPES]
)
def test_t1_accepted_shapes_admit_exact_members_and_values(
    name: str, raw: dict[str, object]
) -> None:
    """T1: every MCP-emittable shape is admitted as data with exact members and types."""
    del name
    before = json.dumps(raw)

    projection = admit_cold_temperature_projection(raw)

    assert type(projection) is ColdTickTemperatureProjection
    assert json.dumps(raw) == before
    assert list(type(projection).model_fields) == list(FIELD_NAMES)
    for field in FIELD_NAMES:
        value: object = getattr(projection, field)
        expected = raw[field]
        if field in _ENUM_FIELDS and expected is not None:
            assert type(value) is _ENUM_FIELDS[field], field
            assert isinstance(value, Enum)
            assert value.value == expected, field
        else:
            assert type(value) is type(expected), field
            assert value == expected, field


class _DictSubclass(dict[str, object]):
    """A non-exact mapping type."""


class _StrSubclass(str):
    """A non-exact key type."""

    __slots__ = ()


def _without(raw: dict[str, object], name: str) -> dict[str, object]:
    """Return a copy of ``raw`` without one key."""
    copy = dict(raw)
    del copy[name]
    return copy


LEAF_FAILURES: list[tuple[str, object, Failure]] = [
    ("null", None, Failure.PROJECTION_NULL),
    ("list", [], Failure.PROJECTION_NOT_OBJECT),
    ("string", "x", Failure.PROJECTION_NOT_OBJECT),
    ("integer", 1, Failure.PROJECTION_NOT_OBJECT),
    ("dict-subclass", _DictSubclass(celsius_agree()), Failure.PROJECTION_NOT_OBJECT),
    ("int-key", {**celsius_agree(), 1: 1}, Failure.FIELD_SET_MISMATCH),
    (
        "version-absent",
        _without(celsius_agree(), "projection_version"),
        Failure.VERSION_NOT_ADMITTED,
    ),
    ("version-true", celsius_agree(projection_version=True), Failure.VERSION_NOT_ADMITTED),
    ("version-float", celsius_agree(projection_version=1.0), Failure.VERSION_NOT_ADMITTED),
    ("version-string", celsius_agree(projection_version="1"), Failure.VERSION_NOT_ADMITTED),
    ("version-zero", celsius_agree(projection_version=0), Failure.VERSION_NOT_ADMITTED),
    ("version-two", celsius_agree(projection_version=2), Failure.VERSION_NOT_ADMITTED),
    ("version-null", celsius_agree(projection_version=None), Failure.VERSION_NOT_ADMITTED),
    ("extra-key", {**celsius_agree(), "extra": None}, Failure.FIELD_SET_MISMATCH),
    (
        "missing-value-key",
        _without(celsius_agree(), "status_read_error_count"),
        Failure.FIELD_SET_MISMATCH,
    ),
    ("missing-outcome", _without(celsius_agree(), "outcome"), Failure.FIELD_SET_MISMATCH),
    ("outcome-null", celsius_agree(outcome=None), Failure.OUTCOME_NOT_ADMITTED),
    ("outcome-int", celsius_agree(outcome=0), Failure.OUTCOME_NOT_ADMITTED),
    ("outcome-case", celsius_agree(outcome="Observed"), Failure.OUTCOME_NOT_ADMITTED),
    ("outcome-space", celsius_agree(outcome=" observed"), Failure.OUTCOME_NOT_ADMITTED),
    ("outcome-unknown", celsius_agree(outcome="stale"), Failure.OUTCOME_NOT_ADMITTED),
    ("outcome-roast-fan-token", celsius_agree(outcome="unreadable"), Failure.OUTCOME_NOT_ADMITTED),
    ("counter-bool", celsius_agree(status_packet_count=True), Failure.VALUE_TYPE_NOT_EXACT),
    (
        "counter-float",
        celsius_agree(ignored_temperature_packet_count=0.0),
        Failure.VALUE_TYPE_NOT_EXACT,
    ),
    ("counter-string", celsius_agree(command_loop_error_count="1"), Failure.VALUE_TYPE_NOT_EXACT),
    ("temperature-int", celsius_agree(last_packet_bean_temp_c=20), Failure.VALUE_TYPE_NOT_EXACT),
    ("retained-int", celsius_agree(retained_env_temp_c=22), Failure.VALUE_TYPE_NOT_EXACT),
    ("valid-int", celsius_agree(last_packet_valid=0), Failure.VALUE_TYPE_NOT_EXACT),
    ("token-number", celsius_agree(reported_temperature_unit=1), Failure.VALUE_TYPE_NOT_EXACT),
    ("token-list", celsius_agree(value_agreement=["agree"]), Failure.VALUE_TYPE_NOT_EXACT),
    ("reported-auto", celsius_agree(reported_temperature_unit="auto"), Failure.TOKEN_NOT_ADMITTED),
    (
        "configured-unknown",
        celsius_agree(configured_temperature_unit="unknown"),
        Failure.TOKEN_NOT_ADMITTED,
    ),
    (
        "configured-case",
        celsius_agree(configured_temperature_unit="Celsius"),
        Failure.TOKEN_NOT_ADMITTED,
    ),
    (
        "reported-space",
        celsius_agree(reported_temperature_unit="celsius "),
        Failure.TOKEN_NOT_ADMITTED,
    ),
    ("agreement-unknown", celsius_agree(value_agreement="agreed"), Failure.TOKEN_NOT_ADMITTED),
    ("counter-negative", celsius_agree(status_read_error_count=-1), Failure.VALUE_NOT_ADMITTED),
    ("counter-too-large", celsius_agree(status_packet_count=2**53), Failure.VALUE_NOT_ADMITTED),
    ("last-fractional", celsius_agree(last_packet_bean_temp_c=20.5), Failure.VALUE_NOT_ADMITTED),
    ("last-too-large", celsius_agree(last_packet_env_temp_c=65536.0), Failure.VALUE_NOT_ADMITTED),
    ("last-negative", celsius_agree(last_packet_bean_temp_c=-1.0), Failure.VALUE_NOT_ADMITTED),
    ("last-infinite", celsius_agree(last_packet_bean_temp_c=math.inf), Failure.VALUE_NOT_ADMITTED),
    ("retained-nan", celsius_agree(retained_bean_temp_c=math.nan), Failure.VALUE_NOT_ADMITTED),
    ("retained-infinite", celsius_agree(retained_env_temp_c=-math.inf), Failure.VALUE_NOT_ADMITTED),
    ("shape", celsius_agree(value_agreement="disagree"), Failure.SHAPE_INCONSISTENT),
]


@pytest.mark.parametrize(
    ("name", "raw", "expected"), LEAF_FAILURES, ids=[c[0] for c in LEAF_FAILURES]
)
def test_t2_each_leaf_failure_is_reached(name: str, raw: object, expected: Failure) -> None:
    """T2: each of the nine leaf failures is returned, never raised."""
    del name
    assert admit_cold_temperature_projection(raw) is expected


def test_t2_cases_cover_exactly_the_nine_leaf_failures() -> None:
    """T2: the parametrised corpus reaches every leaf failure and never the adapter's."""
    reached = {expected for _, _, expected in LEAF_FAILURES}
    assert reached == set(Failure) - {Failure.PROJECTION_KEY_MISSING}


def test_t2_str_subclass_key_is_refused_by_the_key_type_guard() -> None:
    """T2/L1: a str-subclass key equal to a required name is refused before lookup.

    The mapping is otherwise complete and valid, so only the exact key-type
    guard can refuse it: the field-set comparison treats the subclass key as
    the required name.
    """
    raw = celsius_agree()
    value = raw.pop("outcome")
    raw[_StrSubclass("outcome")] = value
    assert set(raw) == set(FIELD_NAMES)

    assert admit_cold_temperature_projection(raw) is Failure.FIELD_SET_MISMATCH


def _shape_cases() -> list[tuple[str, dict[str, object]]]:
    """Return one raw case per broken shape rule, otherwise individually admitted."""
    cases: list[tuple[str, dict[str, object]]] = []
    for outcome in ("not_eligible", "unsupported", "malformed"):
        for field in _VALUE_FIELDS:
            cases.append((f"sh1-{outcome}-{field}", _shape(outcome, **{field: _SOME_VALUE[field]})))
    cases += [
        ("sh2-status", awaiting(status_packet_count=1)),
        ("sh2-ignored", awaiting(ignored_temperature_packet_count=1)),
        ("sh2-valid-true", awaiting(last_packet_valid=True)),
        ("sh2-valid-null", awaiting(last_packet_valid=None)),
        ("sh2-reported", awaiting(reported_temperature_unit="celsius")),
        ("sh2-agree", awaiting(value_agreement="agree")),
        ("sh2-agreement-null", awaiting(value_agreement=None)),
        ("sh2-configured-null", awaiting(configured_temperature_unit=None)),
        ("sh2-last-bean", awaiting(last_packet_bean_temp_c=20.0)),
        ("sh2-last-env", awaiting(last_packet_env_temp_c=20.0)),
        ("sh2-retained-bean", awaiting(retained_bean_temp_c=20.0)),
        ("sh2-retained-env", awaiting(retained_env_temp_c=20.0)),
        ("sh2-read-errors-null", awaiting(status_read_error_count=None)),
        ("sh2-loop-errors-null", awaiting(command_loop_error_count=None)),
        ("sh2-status-null", awaiting(status_packet_count=None)),
        ("sh2-ignored-null", awaiting(ignored_temperature_packet_count=None)),
        ("sh3-status-zero", celsius_agree(status_packet_count=0)),
        (
            "sh3-ignored-exceeds-status",
            unknown_all_ignored(status_packet_count=2, ignored_temperature_packet_count=3),
        ),
        ("sh3-retained-env-absent", celsius_agree(retained_env_temp_c=None)),
        ("sh3-retained-bean-absent", celsius_agree(retained_bean_temp_c=None)),
        # The next two break only the retained-pair rule: bean presence still
        # matches the ignored/status relation, and no unit or agreement rule
        # reads the retained environment temperature.
        ("sh3-retained-pair-fahrenheit-env-absent", fahrenheit(retained_env_temp_c=None)),
        (
            "sh3-retained-pair-unknown-bean-absent",
            unknown_all_ignored(retained_env_temp_c=21.0),
        ),
        (
            "sh3-retained-when-all-ignored",
            unknown_all_ignored(retained_bean_temp_c=20.0, retained_env_temp_c=21.0),
        ),
        (
            "sh3-retained-absent-when-some-valid",
            unknown_partly_ignored(retained_bean_temp_c=None, retained_env_temp_c=None),
        ),
    ]
    for field in (
        "configured_temperature_unit",
        "reported_temperature_unit",
        "last_packet_valid",
        "value_agreement",
        "status_packet_count",
        "ignored_temperature_packet_count",
        "status_read_error_count",
        "command_loop_error_count",
    ):
        cases.append((f"sh3-null-{field}", celsius_agree(**{field: None})))
    cases += [
        (
            "sh4-celsius-all-ignored",
            celsius_agree(
                retained_bean_temp_c=None,
                retained_env_temp_c=None,
                value_agreement="disagree",
                status_packet_count=2,
                ignored_temperature_packet_count=2,
            ),
        ),
        (
            "sh4-fahrenheit-all-ignored",
            fahrenheit(
                retained_bean_temp_c=None,
                retained_env_temp_c=None,
                status_packet_count=2,
                ignored_temperature_packet_count=2,
            ),
        ),
        (
            "sh4-celsius-some-valid-retained-absent",
            celsius_disagree(retained_bean_temp_c=None, retained_env_temp_c=None),
        ),
        ("sh4-celsius-invalid", celsius_agree(last_packet_valid=False)),
        ("sh4-fahrenheit-invalid", fahrenheit(last_packet_valid=False)),
        ("sh4c-last-bean-null", celsius_disagree(last_packet_bean_temp_c=None)),
        ("sh4c-last-env-null", celsius_disagree(last_packet_env_temp_c=None)),
        ("sh4c-indeterminate", celsius_agree(value_agreement="indeterminate")),
        ("sh4c-agree-unequal-bean", celsius_agree(retained_bean_temp_c=21.5)),
        ("sh4c-agree-unequal-env", celsius_agree(retained_env_temp_c=22.5)),
        ("sh4c-disagree-equal", celsius_agree(value_agreement="disagree")),
        ("sh4f-last-bean", fahrenheit(last_packet_bean_temp_c=70.0)),
        ("sh4f-last-env", fahrenheit(last_packet_env_temp_c=71.0)),
        ("sh4f-agree", fahrenheit(value_agreement="agree")),
        ("sh4f-disagree", fahrenheit(value_agreement="disagree")),
        ("sh5-valid-true", unknown_all_ignored(last_packet_valid=True)),
        (
            "sh5-nothing-ignored",
            unknown_partly_ignored(ignored_temperature_packet_count=0),
        ),
        ("sh5-last-bean", unknown_all_ignored(last_packet_bean_temp_c=20.0)),
        ("sh5-last-env", unknown_all_ignored(last_packet_env_temp_c=20.0)),
        ("sh5-agree", unknown_all_ignored(value_agreement="agree")),
        ("sh5-disagree", unknown_partly_ignored(value_agreement="disagree")),
        (
            "sh6-celsius-configured-fahrenheit-reported",
            fahrenheit(configured_temperature_unit="celsius"),
        ),
        (
            "sh6-fahrenheit-configured-celsius-reported",
            celsius_agree(configured_temperature_unit="fahrenheit"),
        ),
    ]
    return cases


_SHAPE_CASES = _shape_cases()


@pytest.mark.parametrize(("name", "raw"), _SHAPE_CASES, ids=[case[0] for case in _SHAPE_CASES])
def test_t3_each_broken_shape_rule_is_inconsistent(name: str, raw: dict[str, object]) -> None:
    """T3: one broken shape rule in an otherwise admitted projection is refused."""
    del name
    assert admit_cold_temperature_projection(raw) is Failure.SHAPE_INCONSISTENT


@pytest.mark.parametrize(("name", "raw"), _SHAPE_CASES, ids=[case[0] for case in _SHAPE_CASES])
def test_t6_direct_member_form_construction_enforces_each_shape_rule(
    name: str, raw: dict[str, object]
) -> None:
    """T6: the same shape violation built from members is refused by the model."""
    del name
    with pytest.raises(ValidationError):
        ColdTickTemperatureProjection.model_validate(members(raw))


_PRECEDENCE: list[tuple[str, object, Failure]] = [
    (
        "key-type-before-version",
        {**_without(celsius_agree(), "projection_version"), 1: 1},
        Failure.FIELD_SET_MISMATCH,
    ),
    (
        "version-before-field-set",
        {**celsius_agree(projection_version=None), "extra": 1},
        Failure.VERSION_NOT_ADMITTED,
    ),
    (
        "field-set-before-outcome",
        {**celsius_agree(outcome="stale"), "extra": 1},
        Failure.FIELD_SET_MISMATCH,
    ),
    (
        "outcome-before-type",
        celsius_agree(outcome="stale", status_packet_count="1"),
        Failure.OUTCOME_NOT_ADMITTED,
    ),
    (
        "type-before-token",
        celsius_agree(reported_temperature_unit="auto", status_packet_count="1"),
        Failure.VALUE_TYPE_NOT_EXACT,
    ),
    (
        "token-before-value",
        celsius_agree(last_packet_bean_temp_c=20.5, value_agreement="agreed"),
        Failure.TOKEN_NOT_ADMITTED,
    ),
    (
        "value-before-shape",
        celsius_agree(status_packet_count=-1, value_agreement="disagree"),
        Failure.VALUE_NOT_ADMITTED,
    ),
]


@pytest.mark.parametrize(("name", "raw", "expected"), _PRECEDENCE, ids=[c[0] for c in _PRECEDENCE])
def test_t4_first_failure_in_precedence_order_wins(
    name: str, raw: object, expected: Failure
) -> None:
    """T4: with two failures present, the earlier step's failure is returned."""
    del name
    assert admit_cold_temperature_projection(raw) is expected


def _deep_list(depth: int) -> object:
    """Return a JSON-derived list nested ``depth`` levels deep."""
    return json.loads("[" * depth + "]" * depth)


def _json_corpus() -> list[object]:
    """Return JSON-derived scalars and containers, round-tripped through text."""
    values: list[object] = [
        None,
        True,
        False,
        0,
        1,
        -1,
        10**40,
        -(10**40),
        0.0,
        -0.0,
        1.5,
        1e308,
        -1e308,
        "",
        "observed",
        "été",
        "\U0001f525",
        [],
        {},
        [1, [2, [3]]],
        {"k": {"k": {"k": []}}},
        {"ünicode-key": 1},
    ]
    corpus = [json.loads(json.dumps(value, allow_nan=False)) for value in values]
    corpus.append(_deep_list(200))
    return corpus


_CORPUS = _json_corpus()


def test_t5_admission_is_total_over_json_derived_values() -> None:
    """T5: any JSON-derived input returns a projection or a leaf failure, never raises."""
    results: list[object] = []
    for value in _CORPUS:
        results.append(admit_cold_temperature_projection(value))
        results.append(admit_cold_temperature_projection({"projection_version": 1, "x": value}))
    for base in (celsius_agree(), fahrenheit(), awaiting(), _shape("malformed")):
        for field in FIELD_NAMES:
            for value in _CORPUS:
                mutated = dict(base)
                mutated[field] = value
                results.append(admit_cold_temperature_projection(mutated))
        for value in _CORPUS:
            results.append(admit_cold_temperature_projection({**base, "é": value}))
    for result in results:
        assert type(result) is ColdTickTemperatureProjection or type(result) is Failure
        assert result is not Failure.PROJECTION_KEY_MISSING


class _IntSubclass(int):
    """A non-exact counter type."""


class _FloatSubclass(float):
    """A non-exact temperature type."""


_DIRECT_REFUSALS: list[tuple[str, dict[str, object]]] = [
    ("version-true", {"projection_version": True}),
    ("version-two", {"projection_version": 2}),
    ("version-float", {"projection_version": 1.0}),
    ("version-int-subclass", {"projection_version": _IntSubclass(1)}),
    ("outcome-raw-string", {"outcome": "observed"}),
    ("outcome-null", {"outcome": None}),
    ("outcome-wrong-enum", {"outcome": ColdTemperatureAgreement.AGREE}),
    ("configured-raw-string", {"configured_temperature_unit": "celsius"}),
    ("configured-wrong-enum", {"configured_temperature_unit": ColdTemperatureReportedUnit.CELSIUS}),
    ("reported-raw-string", {"reported_temperature_unit": "celsius"}),
    ("reported-wrong-enum", {"reported_temperature_unit": ColdTemperatureConfiguredUnit.CELSIUS}),
    ("agreement-raw-string", {"value_agreement": "agree"}),
    ("agreement-wrong-enum", {"value_agreement": ColdTemperatureOutcome.OBSERVED}),
    ("counter-negative", {"status_packet_count": -1}),
    ("counter-too-large", {"status_read_error_count": 2**53}),
    ("counter-bool", {"command_loop_error_count": True}),
    ("counter-int-subclass", {"ignored_temperature_packet_count": _IntSubclass(0)}),
    ("last-int", {"last_packet_bean_temp_c": 21}),
    ("last-fractional", {"last_packet_bean_temp_c": 20.5}),
    ("last-infinite", {"last_packet_env_temp_c": math.inf}),
    ("last-too-large", {"last_packet_env_temp_c": 65536.0}),
    ("last-float-subclass", {"last_packet_bean_temp_c": _FloatSubclass(21.0)}),
    ("retained-nan", {"retained_bean_temp_c": math.nan}),
    ("retained-int", {"retained_env_temp_c": 22}),
    ("retained-float-subclass", {"retained_bean_temp_c": _FloatSubclass(21.0)}),
    ("valid-int", {"last_packet_valid": 0}),
    ("valid-int-one", {"last_packet_valid": 1}),
    ("extra-field", {"extra": None}),
]


@pytest.mark.parametrize(
    ("name", "changes"), _DIRECT_REFUSALS, ids=[case[0] for case in _DIRECT_REFUSALS]
)
def test_t6_direct_construction_enforces_the_whole_field_grammar(
    name: str, changes: dict[str, object]
) -> None:
    """T6: a value outside the grammar is refused on direct member-form construction."""
    del name
    values = members(celsius_agree())
    values.update(changes)
    with pytest.raises(ValidationError):
        ColdTickTemperatureProjection.model_validate(values)


@pytest.mark.parametrize(
    ("name", "raw"), ACCEPTED_SHAPES, ids=[case[0] for case in ACCEPTED_SHAPES]
)
def test_t6_valid_member_form_construction_succeeds(name: str, raw: dict[str, object]) -> None:
    """T6: every admitted shape is also constructible directly from members."""
    del name
    direct = ColdTickTemperatureProjection.model_validate(members(raw))
    assert direct == admit_cold_temperature_projection(raw)


def test_t6_model_is_closed_frozen_and_ordered() -> None:
    """T6: the model declares the closed configuration and canonical field order."""
    config = dict(ColdTickTemperatureProjection.model_config)
    assert config == {
        **config,
        "frozen": True,
        "extra": "forbid",
        "strict": True,
        "allow_inf_nan": False,
    }
    assert tuple(ColdTickTemperatureProjection.model_fields) == FIELD_NAMES
    assert all(info.is_required() for info in ColdTickTemperatureProjection.model_fields.values())
    projection = valid_projection()
    with pytest.raises(ValidationError):
        projection.status_packet_count = 6


def test_t7_vocabularies_are_closed_plain_enums() -> None:
    """T7: every vocabulary is a closed plain enum and the token tables equal it."""
    expected = {
        ColdTemperatureOutcome: {
            "OBSERVED",
            "AWAITING_FIRST_PACKET",
            "NOT_ELIGIBLE",
            "UNSUPPORTED",
            "MALFORMED",
        },
        ColdTemperatureConfiguredUnit: {"CELSIUS", "FAHRENHEIT", "AUTO"},
        ColdTemperatureReportedUnit: {"CELSIUS", "FAHRENHEIT", "UNKNOWN"},
        ColdTemperatureAgreement: {"AGREE", "DISAGREE", "INDETERMINATE"},
    }
    for enum_type, names in expected.items():
        assert {member.name for member in enum_type} == names
        assert enum_type.__mro__[1:] == (Enum, object)
        assert all(member.value == member.name.lower() for member in enum_type)
    assert [failure.name for failure in Failure] == [
        "PROJECTION_KEY_MISSING",
        "PROJECTION_NULL",
        "PROJECTION_NOT_OBJECT",
        "VERSION_NOT_ADMITTED",
        "FIELD_SET_MISMATCH",
        "OUTCOME_NOT_ADMITTED",
        "VALUE_TYPE_NOT_EXACT",
        "TOKEN_NOT_ADMITTED",
        "VALUE_NOT_ADMITTED",
        "SHAPE_INCONSISTENT",
    ]
    assert Failure.__mro__[1:] == (Enum, object)
    tables: dict[type[Enum], dict[str, Enum]] = {
        ColdTemperatureOutcome: dict(leaf._OUTCOME_TOKENS),  # pyright: ignore[reportPrivateUsage]
        ColdTemperatureConfiguredUnit: dict(leaf._CONFIGURED_UNIT_TOKENS),  # pyright: ignore[reportPrivateUsage]
        ColdTemperatureReportedUnit: dict(leaf._REPORTED_UNIT_TOKENS),  # pyright: ignore[reportPrivateUsage]
        ColdTemperatureAgreement: dict(leaf._AGREEMENT_TOKENS),  # pyright: ignore[reportPrivateUsage]
    }
    for enum_type, table in tables.items():
        assert table == {member.value: member for member in enum_type}


_FORBIDDEN_IMPORTS = frozenset(
    {
        "logging",
        "os",
        "sys",
        "io",
        "subprocess",
        "socket",
        "pathlib",
        "json",
        "time",
        "asyncio",
        "mcp",
    }
)


def test_t8_leaf_imports_no_project_or_io_module() -> None:
    """T8: dependency direction is adapter to leaf; the leaf imports no project or I/O module."""
    source = Path(leaf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module)
    assert imported
    assert not any(name.startswith("roastpilot_agent") for name in imported)
    assert {name.split(".")[0] for name in imported} & _FORBIDDEN_IMPORTS == set()
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "isinstance" not in calls
    assert "print" not in calls
    assert "except Exception" not in source


def test_t13_hostile_mapping_shapes_return_failures_without_raising() -> None:
    """T13: a dict subclass and a non-str key are refused as values, not exceptions."""
    assert admit_cold_temperature_projection(_DictSubclass()) is Failure.PROJECTION_NOT_OBJECT
    assert admit_cold_temperature_projection({None: 1}) is Failure.FIELD_SET_MISMATCH
    assert admit_cold_temperature_projection({(1, 2): 1}) is Failure.FIELD_SET_MISMATCH


# ------------------------------------------------------- R1-R4 re-admission (S2a)


class _SubProjection(ColdTickTemperatureProjection):
    """A projection subclass with identical fields (never re-admitted)."""


class _EmptyDictSubclass(dict[str, object]):
    """A non-exact (empty) extra-state mapping."""


def _state_of(projection: ColdTickTemperatureProjection) -> dict[object, object]:
    """Return a copy of a projection's raw ``__dict__``."""
    return dict(object.__getattribute__(projection, "__dict__"))


def _with_state(data: object) -> ColdTickTemperatureProjection:
    """Return a copy of a valid projection whose raw ``__dict__`` is replaced."""
    copy = valid_projection().model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def _with_extra(extra: object) -> ColdTickTemperatureProjection:
    """Return a copy of a valid projection whose ``__pydantic_extra__`` is replaced."""
    copy = valid_projection().model_copy()
    object.__setattr__(copy, "__pydantic_extra__", extra)
    return copy


def _str_subclass_keyed() -> ColdTickTemperatureProjection:
    """Return a valid projection whose ``outcome`` key is a ``str`` subclass."""
    data = _state_of(valid_projection())
    data[_StrSubclass("outcome")] = data.pop("outcome")
    assert {str(key) for key in data} == set(FIELD_NAMES)
    return _with_state(data)


def _missing_one_field() -> ColdTickTemperatureProjection:
    """Return a ``model_construct`` projection that omits one declared field."""
    values: dict[str, Any] = members(celsius_agree())
    del values["status_read_error_count"]
    return ColdTickTemperatureProjection.model_construct(**values)


def _fabricated(enum_type: type[Enum]) -> object:
    """Return an exact-class enum object that is not one of the enum's members."""
    fabricated = object.__new__(enum_type)
    assert type(fabricated) is enum_type
    assert all(fabricated is not member for member in enum_type)
    return fabricated


def _all_null_observed() -> ColdTickTemperatureProjection:
    """Return a ``model_construct`` observed projection whose every value is null."""
    nulls: dict[str, Any] = dict(_NULL_VALUES)
    return ColdTickTemperatureProjection.model_construct(
        projection_version=1, outcome=ColdTemperatureOutcome.OBSERVED, **nulls
    )


def _copy(**update: object) -> ColdTickTemperatureProjection:
    """Return an unvalidated ``model_copy`` of the valid projection with updates."""
    return valid_projection().model_copy(update=update)


def _readmission_refusals() -> list[tuple[str, object, Failure]]:
    """Return one isolated re-admission refusal per guard (every other part is valid)."""
    return [
        ("null", None, Failure.PROJECTION_NULL),
        ("raw-dict", celsius_agree(), Failure.PROJECTION_NOT_OBJECT),
        (
            "subclass",
            _SubProjection.model_validate(members(celsius_agree())),
            Failure.PROJECTION_NOT_OBJECT,
        ),
        (
            "uninitialised",
            ColdTickTemperatureProjection.__new__(ColdTickTemperatureProjection),
            Failure.FIELD_SET_MISMATCH,
        ),
        ("missing-field", _missing_one_field(), Failure.FIELD_SET_MISMATCH),
        (
            "extra-state-key",
            _with_state({**_state_of(valid_projection()), "extra": None}),
            Failure.FIELD_SET_MISMATCH,
        ),
        ("dict-subclass-state", _with_state(_DictSubclass()), Failure.FIELD_SET_MISMATCH),
        (
            "non-str-state-key",
            _with_state({**_state_of(valid_projection()), 1: 1}),
            Failure.FIELD_SET_MISMATCH,
        ),
        ("str-subclass-key", _str_subclass_keyed(), Failure.FIELD_SET_MISMATCH),
        ("extra-non-empty", _with_extra({"extra": None}), Failure.FIELD_SET_MISMATCH),
        ("extra-list", _with_extra([]), Failure.FIELD_SET_MISMATCH),
        (
            "extra-empty-dict-subclass",
            _with_extra(_EmptyDictSubclass()),
            Failure.FIELD_SET_MISMATCH,
        ),
        ("outcome-raw-string", _copy(outcome="observed"), Failure.VALUE_TYPE_NOT_EXACT),
        (
            "configured-holds-reported-member",
            _copy(configured_temperature_unit=ColdTemperatureReportedUnit.CELSIUS),
            Failure.VALUE_TYPE_NOT_EXACT,
        ),
        (
            "outcome-fabricated",
            _copy(outcome=_fabricated(ColdTemperatureOutcome)),
            Failure.VALUE_TYPE_NOT_EXACT,
        ),
        (
            "agreement-fabricated",
            _copy(value_agreement=_fabricated(ColdTemperatureAgreement)),
            Failure.VALUE_TYPE_NOT_EXACT,
        ),
        ("all-null-observed", _all_null_observed(), Failure.SHAPE_INCONSISTENT),
        ("version-two", _copy(projection_version=2), Failure.VERSION_NOT_ADMITTED),
        ("counter-negative", _copy(status_packet_count=-1), Failure.VALUE_NOT_ADMITTED),
        ("counter-bool", _copy(status_packet_count=True), Failure.VALUE_TYPE_NOT_EXACT),
        (
            "last-float-subclass",
            _copy(last_packet_bean_temp_c=_FloatSubclass(21.0)),
            Failure.VALUE_TYPE_NOT_EXACT,
        ),
    ]


_READMISSION_REFUSALS = _readmission_refusals()


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    _READMISSION_REFUSALS,
    ids=[case[0] for case in _READMISSION_REFUSALS],
)
def test_r2_each_readmission_guard_refuses_in_isolation(
    name: str, value: object, expected: Failure
) -> None:
    """R2: each guard returns its closed failure as a value; nothing is raised."""
    del name
    assert leaf.readmit_cold_temperature_projection(value) is expected


@pytest.mark.parametrize(
    ("name", "raw"), ACCEPTED_SHAPES, ids=[case[0] for case in ACCEPTED_SHAPES]
)
def test_r1_every_accepted_shape_is_readmitted_as_a_fresh_equal_snapshot(
    name: str, raw: dict[str, object]
) -> None:
    """R1: re-admission returns an equal, fresh model carrying the identical members."""
    del name
    admitted = admit_cold_temperature_projection(raw)
    assert type(admitted) is ColdTickTemperatureProjection

    readmitted = leaf.readmit_cold_temperature_projection(admitted)

    assert type(readmitted) is ColdTickTemperatureProjection
    assert readmitted == admitted
    assert readmitted is not admitted
    for field in FIELD_NAMES:
        before: object = getattr(admitted, field)
        after: object = getattr(readmitted, field)
        if field in _ENUM_FIELDS:
            assert after is before, field
        else:
            assert type(after) is type(before), field
            assert after == before, field


def test_r1_valid_content_with_an_exact_empty_extra_mapping_is_readmitted() -> None:
    """R1: an exact empty extra-state dict carries no undeclared state and is admitted."""
    projection = _with_extra({})
    readmitted = leaf.readmit_cold_temperature_projection(projection)
    assert readmitted == valid_projection()
    assert readmitted is not projection


def test_r1_honest_limit_a_constructed_instance_with_valid_content_is_readmitted() -> None:
    """R1: content, not provenance, is re-admitted; a valid ``model_construct`` is accepted."""
    values: dict[str, Any] = members(celsius_agree())
    constructed = ColdTickTemperatureProjection.model_construct(**values)
    readmitted = leaf.readmit_cold_temperature_projection(constructed)
    assert type(readmitted) is ColdTickTemperatureProjection
    assert readmitted == valid_projection()
    assert readmitted is not constructed


def test_r3_model_validate_on_an_instance_is_not_readmission() -> None:
    """R3: ``model_validate`` returns a forged instance unchanged; re-admission refuses it.

    The model's after-validator still runs on an instance, so the forgery keeps a
    consistent shape and breaks only a field rule the field validators would apply.
    """
    forged = _copy(projection_version=2)
    assert ColdTickTemperatureProjection.model_validate(forged) is forged
    assert leaf.readmit_cold_temperature_projection(forged) is Failure.VERSION_NOT_ADMITTED
    with pytest.raises(ValidationError):
        ColdTickTemperatureProjection.model_validate(_copy(status_packet_count=-1))


def test_r4_readmission_adds_no_failure_member_and_keeps_the_leaf_fences() -> None:
    """R4: the failure vocabulary is unchanged and the new function is a leaf function."""
    assert len(Failure) == 10
    source = Path(leaf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "readmit_cold_temperature_projection" in functions
    test_t7_vocabularies_are_closed_plain_enums()
    test_t8_leaf_imports_no_project_or_io_module()
