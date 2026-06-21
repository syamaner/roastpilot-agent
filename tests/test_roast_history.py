"""Tests for the per-tick context builder (#275 — D40.3 / D40.5).

The roast-so-far curve window + milestone summary, the development time + DTR,
and the model's own decision trace — assembled into a BOUNDED payload. These
tests pin the payload bound (the acceptance), the milestone summary, the decision
trace, and the validation-supported FC-ETA (#229 KEEP). Pure context assembly:
nothing here actuates hardware or evaluates safety. Temperatures are Celsius.
"""

from __future__ import annotations

import pytest

from roastpilot_agent.roast_history import (
    DEFAULT_CURVE_WINDOW_SAMPLES,
    DEFAULT_DECISION_TRACE_ENTRIES,
    DecisionTraceEntry,
    PerTickContextPayload,
    RoastCurveSample,
    RoastHistory,
    RoastMilestone,
    RoastMilestoneKind,
    estimate_first_crack_eta_seconds,
)


def _sample(t: float, bean: float, *, heat: int = 100, fan: int = 20) -> RoastCurveSample:
    return RoastCurveSample(
        elapsed_since_charge_seconds=t,
        bean_temp_c=bean,
        env_temp_c=bean + 20.0,
        heat_percent=heat,
        fan_percent=fan,
        bean_ror_c_per_min=12.0,
    )


def _decision(t: float, *, heat: int = 80, fan: int = 30) -> DecisionTraceEntry:
    return DecisionTraceEntry(
        elapsed_since_charge_seconds=t,
        target_heat=heat,
        target_fan=fan,
        should_drop=False,
        confidence=0.8,
    )


# --- Payload bound (the acceptance) -----------------------------------------


def test_curve_window_is_bounded_not_a_raw_dump() -> None:
    """The curve window keeps only the most recent ``curve_window_samples`` —
    a long roast can never grow the payload past the bound (the #275 acceptance:
    windowed full-res, NOT a raw 100+-point dump)."""
    history = RoastHistory(curve_window_samples=10, decision_trace_entries=5)
    # Feed 200 samples — a ~3 min roast at the 1 s tick, far past the window.
    for i in range(200):
        history.record_sample(_sample(float(i), 80.0 + i))
    window = history.curve_window()
    assert len(window) == 10
    # The window is the MOST RECENT 10, newest last.
    assert window[0].elapsed_since_charge_seconds == 190.0
    assert window[-1].elapsed_since_charge_seconds == 199.0
    payload = history.build_payload(
        development_elapsed_seconds=None,
        development_time_ratio=None,
        first_crack_eta_seconds=None,
    )
    assert len(payload.curve_window) == 10


def test_decision_trace_is_bounded() -> None:
    """The decision trace keeps only the most recent ``decision_trace_entries``
    (#218: bounded recommendation history, newest last)."""
    history = RoastHistory(curve_window_samples=60, decision_trace_entries=4)
    for i in range(50):
        history.record_decision(_decision(float(i), heat=70 + (i % 5)))
    trace = history.decision_trace()
    assert len(trace) == 4
    assert [e.elapsed_since_charge_seconds for e in trace] == [46.0, 47.0, 48.0, 49.0]


def test_default_bounds_match_named_constants() -> None:
    """The default bounds are the explicit named constants (acceptance: make the
    window/summary sizes explicit)."""
    history = RoastHistory()
    assert history.curve_window_samples == DEFAULT_CURVE_WINDOW_SAMPLES
    assert history.decision_trace_entries == DEFAULT_DECISION_TRACE_ENTRIES


@pytest.mark.parametrize(
    ("curve", "trace"),
    [(0, 5), (5, 0), (-1, 5)],
)
def test_non_positive_bounds_rejected(curve: int, trace: int) -> None:
    """A bound below 1 is a configuration error (an empty/negative window is
    meaningless)."""
    with pytest.raises(ValueError):
        RoastHistory(curve_window_samples=curve, decision_trace_entries=trace)


# --- Reset ------------------------------------------------------------------


def test_reset_clears_all_history() -> None:
    """A new run/preheat starts fresh: curve, decisions, and milestones clear."""
    history = RoastHistory()
    history.record_sample(_sample(1.0, 90.0))
    history.record_decision(_decision(1.0))
    history.record_milestone(
        RoastMilestone(
            kind=RoastMilestoneKind.FIRST_CRACK,
            elapsed_since_charge_seconds=300.0,
            bean_temp_c=176.0,
        )
    )
    history.reset()
    assert history.curve_window() == []
    assert history.decision_trace() == []
    assert history.milestones() == []


# --- Milestone summary ------------------------------------------------------


def test_milestones_keep_first_occurrence_and_sort_by_time() -> None:
    """A milestone is one-shot (first crossing wins) and the summary is ordered
    by occurrence time."""
    history = RoastHistory()
    history.record_milestone(
        RoastMilestone(
            kind=RoastMilestoneKind.FIRST_CRACK,
            elapsed_since_charge_seconds=300.0,
            bean_temp_c=176.0,
        )
    )
    history.record_milestone(
        RoastMilestone(
            kind=RoastMilestoneKind.TURNING_POINT,
            elapsed_since_charge_seconds=55.0,
            bean_temp_c=82.0,
        )
    )
    # A second TURNING_POINT must be ignored (first crossing is the landmark).
    history.record_milestone(
        RoastMilestone(
            kind=RoastMilestoneKind.TURNING_POINT,
            elapsed_since_charge_seconds=70.0,
            bean_temp_c=90.0,
        )
    )
    summary = history.milestones()
    assert [m.kind for m in summary] == [
        RoastMilestoneKind.TURNING_POINT,
        RoastMilestoneKind.FIRST_CRACK,
    ]
    assert summary[0].elapsed_since_charge_seconds == 55.0  # first crossing kept
    assert history.has_milestone(RoastMilestoneKind.TURNING_POINT)
    assert not history.has_milestone(RoastMilestoneKind.RECOVERY)


# --- Development time + DTR (two distinct values) ----------------------------


def test_payload_carries_dev_time_and_dtr_as_distinct_values() -> None:
    """Development time (duration) and DTR (a share) are two distinct payload
    fields (the #275 acceptance — reuse the existing clocks, do not conflate)."""
    history = RoastHistory()
    payload = history.build_payload(
        development_elapsed_seconds=90.0,
        development_time_ratio=0.15,
        first_crack_eta_seconds=None,
    )
    assert payload.development_elapsed_seconds == 90.0
    assert payload.development_time_ratio == 0.15
    assert payload.development_elapsed_seconds != payload.development_time_ratio


# --- FC-ETA (the #229 KEEP feature) -----------------------------------------


def test_fc_eta_extrapolates_a_warming_curve() -> None:
    """A steadily-warming curve projects a positive ETA to the FC target."""
    # 1 °C/s warming, currently at 160 °C, target 176 °C → ~16 s to FC.
    curve = [_sample(float(i), 150.0 + float(i)) for i in range(11)]  # 150..160 °C
    eta = estimate_first_crack_eta_seconds(curve, fc_target_bean_temp_c=176.0)
    assert eta is not None
    assert abs(eta - 16.0) < 0.5


def test_fc_eta_none_when_too_few_samples() -> None:
    """With fewer than the minimum samples there is no slope to project."""
    curve = [_sample(0.0, 150.0), _sample(1.0, 151.0)]
    assert estimate_first_crack_eta_seconds(curve, fc_target_bean_temp_c=176.0) is None


def test_fc_eta_none_when_no_time_span() -> None:
    """A window whose recent samples share one timestamp has no slope (guards a
    divide-by-zero on a degenerate clock)."""
    same_time = [_sample(5.0, 150.0 + float(i)) for i in range(6)]
    assert estimate_first_crack_eta_seconds(same_time, fc_target_bean_temp_c=176.0) is None


def test_fc_eta_none_when_not_warming() -> None:
    """A flat/falling curve cannot reach the FC target → no estimate."""
    flat = [_sample(float(i), 150.0) for i in range(10)]
    assert estimate_first_crack_eta_seconds(flat, fc_target_bean_temp_c=176.0) is None


def test_fc_eta_none_when_already_at_target() -> None:
    """At/through the FC band, FC is the detector's call, not an ETA."""
    hot = [_sample(float(i), 178.0 + float(i)) for i in range(10)]
    assert estimate_first_crack_eta_seconds(hot, fc_target_bean_temp_c=176.0) is None


# --- Full payload assembly --------------------------------------------------


def test_build_payload_assembles_window_summary_and_trace() -> None:
    """The payload carries the curve window, milestone summary, and decision
    trace together (the curve-to-t PLUS the decisions, per D40.5)."""
    history = RoastHistory(curve_window_samples=5, decision_trace_entries=3)
    for i in range(8):
        history.record_sample(_sample(float(i), 100.0 + i))
    for i in range(4):
        history.record_decision(_decision(float(i)))
    history.record_milestone(
        RoastMilestone(
            kind=RoastMilestoneKind.FIRST_CRACK,
            elapsed_since_charge_seconds=300.0,
            bean_temp_c=176.0,
        )
    )
    payload = history.build_payload(
        development_elapsed_seconds=42.0,
        development_time_ratio=0.12,
        first_crack_eta_seconds=None,
    )
    assert isinstance(payload, PerTickContextPayload)
    assert len(payload.curve_window) == 5  # bounded
    assert len(payload.decision_trace) == 3  # bounded
    assert [m.kind for m in payload.milestones] == [RoastMilestoneKind.FIRST_CRACK]
    assert payload.development_elapsed_seconds == 42.0
    assert payload.development_time_ratio == 0.12


def test_milestone_kind_is_plain_enum_not_strenum() -> None:
    """House rule D15: the milestone kind is a plain ``Enum`` so a string
    comparison is a type error (the value is a wire form, not the member)."""
    assert RoastMilestoneKind.FIRST_CRACK is not RoastMilestoneKind.FIRST_CRACK.value
    assert RoastMilestoneKind.FIRST_CRACK.value == "first_crack"
