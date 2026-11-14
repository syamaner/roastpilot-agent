"""Behavioural contract for D230 accepted-reading evidence and drop eligibility."""

from dataclasses import dataclass, replace
from enum import Enum

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.human_protection import (
    HUMAN_PROTECTION_BT_C,
    AcceptedReadingTracker,
    CommandDisposition,
    ContainmentAttempt,
    ContainmentOutcome,
    ContainmentReason,
    ControlAuthority,
    CounterClass,
    CounterEvidence,
    EndpointDisposition,
    EndpointOwner,
    HumanReadEvidence,
    RawTemperature,
    ReadingFreshness,
    ToolDiscovery,
    WriteClass,
    classify_raw_temperature,
    extract_accepted_counters,
    protection_eligible,
)
from roastpilot_agent.models import DropReason, RoastPhase, RoastTelemetry


def _eligible(
    *,
    authority: ControlAuthority = ControlAuthority.HUMAN,
    charged: bool = True,
    phase: RoastPhase = RoastPhase.ROASTING_PRE_FIRST_CRACK,
    freshness: ReadingFreshness = ReadingFreshness.FRESH,
    bean_temp_c: object = 196.0,
    endpoint: EndpointDisposition = EndpointDisposition.NONE,
    command: CommandDisposition = CommandDisposition.SETTLED,
) -> bool:
    return protection_eligible(
        authority=authority,
        charged=charged,
        phase=phase,
        freshness=freshness,
        bean_temp_c=bean_temp_c,
        endpoint=endpoint,
        command=command,
    )


@pytest.mark.parametrize(("bt", "eligible"), [(195.99, False), (196.0, True), (196.01, True)])
def test_threshold_inclusive_below_equal_above(bt: float, eligible: bool) -> None:
    assert _eligible(bean_temp_c=bt) is eligible


def test_uncharged_never_eligible() -> None:
    assert not _eligible(charged=False)


@pytest.mark.parametrize("phase", RoastPhase)
def test_phase_set_pre_fc_and_development_only(phase: RoastPhase) -> None:
    assert _eligible(phase=phase) is (
        phase in {RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT}
    )


@pytest.mark.parametrize(
    "freshness", [ReadingFreshness.STALE, ReadingFreshness.UNPROVEN, ReadingFreshness.INVALID]
)
def test_non_fresh_never_eligible(freshness: ReadingFreshness) -> None:
    assert not _eligible(freshness=freshness)


@pytest.mark.parametrize(
    "endpoint",
    [endpoint for endpoint in EndpointDisposition if endpoint is not EndpointDisposition.NONE],
)
def test_latched_endpoint_never_eligible(endpoint: EndpointDisposition) -> None:
    assert not _eligible(endpoint=endpoint)


def test_unknown_command_never_eligible() -> None:
    assert not _eligible(command=CommandDisposition.UNKNOWN)


def test_automatic_never_eligible() -> None:
    assert not _eligible(authority=ControlAuthority.AUTOMATIC)


@pytest.mark.parametrize("bt", [float("nan"), float("inf"), float("-inf"), True, "196"])
def test_invalid_temperature_never_eligible(bt: object) -> None:
    assert not _eligible(bean_temp_c=bt)


def test_constant_equals_bitter_ceiling_default() -> None:
    assert HUMAN_PROTECTION_BT_C == SafetyLimits().bitter_ceiling_temp_c == 196.0


@pytest.mark.parametrize(
    "enum",
    [
        ControlAuthority,
        WriteClass,
        ReadingFreshness,
        CounterClass,
        EndpointDisposition,
        EndpointOwner,
        CommandDisposition,
        ContainmentOutcome,
        ContainmentReason,
        ContainmentAttempt,
        DropReason,
        RawTemperature,
        ToolDiscovery,
    ],
)
def test_new_enums_plain_enum(enum: type[Enum]) -> None:
    assert enum.__bases__ == (Enum,)


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (196, RawTemperature.VALID),
        (196.0, RawTemperature.VALID),
        (2**53, RawTemperature.VALID),
        (-(2**53), RawTemperature.VALID),
        (2**53 + 1, RawTemperature.INVALID),
        (-(2**53) - 1, RawTemperature.INVALID),
        (10**400, RawTemperature.INVALID),
        (_IntSubclass(196), RawTemperature.INVALID),
        (_FloatSubclass(196), RawTemperature.INVALID),
        ("196", RawTemperature.INVALID),
        (True, RawTemperature.INVALID),
        (False, RawTemperature.INVALID),
        ([], RawTemperature.INVALID),
        ({}, RawTemperature.INVALID),
        (float("nan"), RawTemperature.INVALID),
        (float("inf"), RawTemperature.INVALID),
        (float("-inf"), RawTemperature.INVALID),
        (None, RawTemperature.ABSENT),
    ],
)
def test_classify_raw_temperature(value: object, expected: RawTemperature) -> None:
    assert classify_raw_temperature(value) is expected


def _human_evidence() -> HumanReadEvidence:
    return HumanReadEvidence(
        telemetry=RoastTelemetry(bean_temp_c=196.0, env_temp_c=214.0),
        device_object=True,
        bean=RawTemperature.VALID,
        env=RawTemperature.VALID,
        bean_temp_c=196,
        env_temp_c=214.0,
        counters=CounterEvidence(23, 2, CounterClass.PRESENT),
        request_admitted_at=10.0,
        bound_child_epoch=4,
    )


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        pytest.param({"device_object": 1}, "types", id="I1-device"),
        pytest.param({"bean": "valid"}, "types", id="I1-bean"),
        pytest.param({"env": "valid"}, "types", id="I1-env"),
        pytest.param({"counters": {}}, "types", id="I1-counters"),
        pytest.param({"bean_temp_c": None}, "exact raw value", id="I2-missing-valid"),
        pytest.param({"env_temp_c": True}, "exact raw value", id="I2-malformed-valid"),
        pytest.param(
            {"env": RawTemperature.INVALID, "telemetry": None},
            "cannot retain",
            id="I2-invalid-with-value",
        ),
        pytest.param(
            {
                "device_object": False,
                "env": RawTemperature.ABSENT,
                "env_temp_c": None,
                "telemetry": None,
                "counters": CounterEvidence(None, None, CounterClass.ABSENT),
            },
            "absent device",
            id="I3-bean",
        ),
        pytest.param(
            {
                "device_object": False,
                "bean": RawTemperature.ABSENT,
                "bean_temp_c": None,
                "telemetry": None,
                "counters": CounterEvidence(None, None, CounterClass.ABSENT),
            },
            "absent device",
            id="I3-env",
        ),
        pytest.param(
            {
                "device_object": False,
                "bean": RawTemperature.ABSENT,
                "env": RawTemperature.ABSENT,
                "bean_temp_c": None,
                "env_temp_c": None,
                "telemetry": None,
            },
            "absent device",
            id="I3-counters",
        ),
        pytest.param({"telemetry": None}, "valid temperature pair", id="I4-no-telemetry"),
        pytest.param(
            {"bean": RawTemperature.INVALID, "bean_temp_c": None},
            "valid temperature pair",
            id="I4-unusable-pair",
        ),
        pytest.param({"bean_temp_c": 195}, "must match", id="I4-mismatched-bean"),
        pytest.param({"env_temp_c": 213.0}, "must match", id="I4-mismatched-env"),
        pytest.param({"bound_child_epoch": True}, "exact integer", id="I5-bool"),
        pytest.param({"bound_child_epoch": 4.0}, "exact integer", id="I5-float"),
        pytest.param({"request_admitted_at": False}, "exact number", id="I6-bool"),
        pytest.param({"request_admitted_at": "10"}, "exact number", id="I6-string"),
    ],
)
def test_human_evidence_enforces_invariants(changes: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        replace(_human_evidence(), **changes)


def test_human_evidence_rejects_raw_bool_even_when_projection_matches() -> None:
    with pytest.raises(ValueError, match="exact raw value"):
        replace(
            _human_evidence(),
            env_temp_c=True,
            telemetry=RoastTelemetry(bean_temp_c=196.0, env_temp_c=1.0),
        )


@pytest.mark.parametrize("admission", [10, 10.0, float("inf")])
def test_human_evidence_leaves_clock_finiteness_to_tracker(admission: float) -> None:
    evidence = replace(_human_evidence(), request_admitted_at=admission)
    assert evidence.request_admitted_at is admission


def test_only_unknown_containment_outcomes_are_unknown() -> None:
    assert {outcome for outcome in ContainmentOutcome if outcome.is_unknown} == {
        ContainmentOutcome.UNKNOWN_DRIVER_RESULT,
        ContainmentOutcome.UNKNOWN_COMPLETION,
        ContainmentOutcome.UNKNOWN_TRANSPORT,
        ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE,
    }


@pytest.mark.parametrize(
    "raw",
    [{}, None, {"status_packet_count": 1}, {"ignored_temperature_packet_count": 0}],
)
def test_missing_raw_vendor_keys_or_device_state_none_absent(raw: dict[str, object] | None) -> None:
    assert extract_accepted_counters(raw) == CounterEvidence(None, None, CounterClass.ABSENT)


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_counter_bool_float_str_none_invalid(value: object) -> None:
    assert _extract(value, 0) == CounterEvidence(None, None, CounterClass.INVALID)
    assert _extract(1, value) == CounterEvidence(None, None, CounterClass.INVALID)


def test_ignored_cannot_exceed_status() -> None:
    assert _extract(1, 2) == CounterEvidence(None, None, CounterClass.INVALID)


def test_negative_counters_invalid_and_explicit_zero_present() -> None:
    assert _extract(-1, 0).class_ is CounterClass.INVALID
    assert _extract(1, -1).class_ is CounterClass.INVALID
    assert _extract(0, 0) == CounterEvidence(0, 0, CounterClass.PRESENT)


def _extract(status: object, ignored: object) -> CounterEvidence:
    return extract_accepted_counters(
        {"status_packet_count": status, "ignored_temperature_packet_count": ignored}
    )


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def tracker(clock: _Clock) -> AcceptedReadingTracker:
    return AcceptedReadingTracker(clock, max_age_s=2.0)


def _observe(
    tracker: AcceptedReadingTracker,
    admitted_at: float,
    status: int,
    ignored: int = 0,
    *,
    epoch: int = 1,
    bt: object = 196.0,
    et: object = 205,
) -> ReadingFreshness:
    return tracker.observe(
        child_epoch=epoch,
        counters=_extract(status, ignored),
        bean_temp_c=bt,
        env_temp_c=et,
        request_admitted_at=admitted_at,
    )


def _establish_fresh(clock: _Clock, tracker: AcceptedReadingTracker) -> None:
    assert _observe(tracker, 0.0, 10, 2) is ReadingFreshness.UNPROVEN
    clock.now = 1.0
    assert _observe(tracker, 1.0, 11, 2) is ReadingFreshness.FRESH


def test_ignored_rising_with_status_is_not_fresh(
    clock: _Clock, tracker: AcceptedReadingTracker
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.5
    assert _observe(tracker, 1.5, 12, 3) is ReadingFreshness.FRESH
    clock.now = 2.1
    assert _observe(tracker, 2.1, 13, 4) is ReadingFreshness.STALE


@pytest.mark.parametrize(
    ("status", "ignored", "epoch"),
    [(10, 2, 1), (13, 1, 1), (12, 4, 1), (12, 2, 2)],
    ids=["status", "ignored", "accepted", "epoch"],
)
def test_counter_decrease_and_epoch_change_invalid_and_reset(
    clock: _Clock, tracker: AcceptedReadingTracker, status: int, ignored: int, epoch: int
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.5
    assert _observe(tracker, 1.5, status, ignored, epoch=epoch) is ReadingFreshness.INVALID
    assert tracker.recheck() is ReadingFreshness.INVALID
    clock.now = 1.6
    assert _observe(tracker, 1.6, 50, 2, epoch=epoch) is ReadingFreshness.UNPROVEN
    clock.now = 1.7
    assert _observe(tracker, 1.7, 51, 2, epoch=epoch) is ReadingFreshness.FRESH


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, None, "196 C"])
@pytest.mark.parametrize("bad_bean", [True, False])
def test_nan_inf_bt_or_et_invalid(
    clock: _Clock, tracker: AcceptedReadingTracker, bad: object, bad_bean: bool
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.5
    assert (
        _observe(tracker, 1.5, 12, 2, bt=bad if bad_bean else 196.0, et=205.0 if bad_bean else bad)
        is ReadingFreshness.INVALID
    )
    assert tracker.recheck() is ReadingFreshness.INVALID


def test_first_observation_unproven_not_age_zero(tracker: AcceptedReadingTracker) -> None:
    assert tracker.recheck() is ReadingFreshness.UNPROVEN
    assert _observe(tracker, 0.0, 100) is ReadingFreshness.UNPROVEN
    assert tracker.recheck() is ReadingFreshness.UNPROVEN


def test_increase_bound_is_previous_request_admission(
    clock: _Clock, tracker: AcceptedReadingTracker
) -> None:
    clock.now = 0.75
    assert _observe(tracker, 0.0, 10) is ReadingFreshness.UNPROVEN
    clock.now = 1.75
    assert _observe(tracker, 1.0, 11) is ReadingFreshness.FRESH
    clock.now = 2.0
    assert tracker.recheck() is ReadingFreshness.FRESH
    clock.now = 2.0001
    assert tracker.recheck() is ReadingFreshness.STALE


def test_unchanged_does_not_renew(clock: _Clock, tracker: AcceptedReadingTracker) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.9
    assert _observe(tracker, 1.9, 11, 2) is ReadingFreshness.FRESH
    clock.now = 2.1
    assert _observe(tracker, 2.1, 11, 2) is ReadingFreshness.STALE
    clock.now = 2.2
    assert _observe(tracker, 2.2, 12, 2) is ReadingFreshness.FRESH
    clock.now = 4.1001
    assert tracker.recheck() is ReadingFreshness.STALE


@pytest.mark.parametrize("status", [11, 12])
def test_long_gap_unproven(clock: _Clock, tracker: AcceptedReadingTracker, status: int) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 3.001
    assert _observe(tracker, 3.001, status, 2) is ReadingFreshness.UNPROVEN
    clock.now = 3.1
    assert _observe(tracker, 3.1, status + 1, 2) is ReadingFreshness.FRESH


@pytest.mark.parametrize(
    ("evidence", "interrupted"),
    [
        (extract_accepted_counters(None), ReadingFreshness.UNPROVEN),
        (_extract("12", 2), ReadingFreshness.INVALID),
        (CounterEvidence(None, 2, CounterClass.PRESENT), ReadingFreshness.INVALID),
        (CounterEvidence(12, None, CounterClass.PRESENT), ReadingFreshness.INVALID),
        (None, ReadingFreshness.INVALID),
    ],
)
def test_absent_or_failed_observation_requires_new_baseline_then_progress(
    clock: _Clock,
    tracker: AcceptedReadingTracker,
    evidence: CounterEvidence | None,
    interrupted: ReadingFreshness,
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.1
    if evidence is None:
        assert tracker.record_read_failure() is interrupted
    else:
        assert (
            tracker.observe(
                child_epoch=1,
                counters=evidence,
                bean_temp_c=196.0,
                env_temp_c=205.0,
                request_admitted_at=1.1,
            )
            is interrupted
        )
    assert tracker.recheck() is interrupted
    clock.now = 1.2
    assert _observe(tracker, 1.2, 90, 2) is ReadingFreshness.UNPROVEN
    clock.now = 1.3
    assert _observe(tracker, 1.3, 90, 2) is ReadingFreshness.UNPROVEN
    clock.now = 1.4
    assert _observe(tracker, 1.4, 91, 2) is ReadingFreshness.FRESH
    clock.now = 3.31
    assert tracker.recheck() is ReadingFreshness.STALE


@pytest.mark.parametrize("admission", [1.0, 0.5, float("nan"), float("inf"), float("-inf")])
def test_unordered_or_nonfinite_admission_breaks_history(
    clock: _Clock, tracker: AcceptedReadingTracker, admission: float
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = 1.1
    assert _observe(tracker, admission, 12, 2) is ReadingFreshness.INVALID
    clock.now = 1.2
    assert _observe(tracker, 1.2, 13, 2) is ReadingFreshness.UNPROVEN
    clock.now = 1.3
    assert _observe(tracker, 1.3, 14, 2) is ReadingFreshness.FRESH


@pytest.mark.parametrize("now", [-1.0, float("nan"), float("inf")])
def test_invalid_clock_recheck_breaks_history(
    clock: _Clock, tracker: AcceptedReadingTracker, now: float
) -> None:
    _establish_fresh(clock, tracker)
    clock.now = now
    assert tracker.recheck() is ReadingFreshness.INVALID
    clock.now = 1.2
    assert _observe(tracker, 1.2, 13, 2) is ReadingFreshness.UNPROVEN


def test_delayed_response_cannot_create_fresh_evidence(
    clock: _Clock, tracker: AcceptedReadingTracker
) -> None:
    assert _observe(tracker, 0.0, 10) is ReadingFreshness.UNPROVEN
    clock.now = 2.001
    assert _observe(tracker, 1.0, 11) is ReadingFreshness.STALE
    assert tracker.recheck() is ReadingFreshness.STALE


def test_exact_age_request_gap_retains_ordered_history(
    clock: _Clock, tracker: AcceptedReadingTracker
) -> None:
    assert _observe(tracker, 0.0, 10) is ReadingFreshness.UNPROVEN
    clock.now = 2.0
    assert _observe(tracker, 2.0, 11) is ReadingFreshness.FRESH
