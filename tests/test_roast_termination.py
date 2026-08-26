"""Behavioural tests for fail-closed persisted termination classification."""

from __future__ import annotations

import json

import pytest

from roastpilot_agent.models import DropReason, RoastCommand, RoastEventKind
from roastpilot_agent.roast_termination import (
    DropEventAnchor,
    EvidencePosition,
    TerminationClassification,
    TerminationEventRow,
    TerminationEvidenceKind,
    classify_termination,
    select_first_executed_drop_event,
)


def _event(
    event_id: int,
    kind: RoastEventKind,
    payload: object = None,
    *,
    recorded_at_utc: str | None = None,
) -> TerminationEventRow:
    """Build one raw event projection with optional JSON payload evidence."""
    return TerminationEventRow(
        event_id=event_id,
        kind=kind.value,
        payload_json=None if payload is None else json.dumps(payload),
        recorded_at_utc=recorded_at_utc,
    )


def _drop_payload(reason: DropReason | None = None) -> dict[str, str]:
    """Build one valid executed bean-drop payload."""
    payload = {"command": RoastCommand.DROP_BEANS.value, "source": "advisor"}
    if reason is not None:
        payload["reason"] = reason.value
    return payload


def test_post_drop_emergency_stop_is_abnormal_after_drop() -> None:
    """A cooling-tail e-stop and corroborating fault retain a valid drop."""
    result = classify_termination(
        run_found=True,
        run_outcome="faulted",
        drop_anchor=DropEventAnchor(2, "2026-08-26T12:00:00+00:00"),
        event_rows=[_event(3, RoastEventKind.FAULT)],
        emergency_stop_recorded_at_utc=["2026-08-26T12:02:43+00:00"],
    )

    assert result.classification is TerminationClassification.ABNORMAL_AFTER_DROP
    assert result.terminated_abnormally is False
    assert [(item.kind, item.position) for item in result.evidence] == [
        (TerminationEvidenceKind.EMERGENCY_STOP_VERDICT, EvidencePosition.AFTER_DROP),
        (TerminationEvidenceKind.ABNORMAL_RUN_OUTCOME, EvidencePosition.AFTER_DROP),
    ]


def test_emergency_stop_before_the_drop_is_abnormal() -> None:
    """An e-stop before the boundary fails closed despite a later drop."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(2, "2026-08-26T12:00:00+00:00"),
        event_rows=(),
        emergency_stop_recorded_at_utc=["2026-08-26T11:59:59+00:00"],
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert result.terminated_abnormally is True


def test_failed_ceiling_guard_then_development_target_drop_is_abnormal() -> None:
    """A failed guard attempt remains evidence even when another drop succeeds."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(2, "2026-08-26T12:00:00+00:00"),
        event_rows=(
            _event(1, RoastEventKind.COMMAND_FAILED, _drop_payload(DropReason.CEILING_GUARD)),
            _event(
                2,
                RoastEventKind.COMMAND_EXECUTED,
                _drop_payload(DropReason.DEVELOPMENT_TARGET),
            ),
        ),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert len(result.evidence) == 1
    assert result.evidence[0].kind is TerminationEvidenceKind.CEILING_GUARD_DROP_FAILED
    assert result.evidence[0].position is EvidencePosition.BEFORE_OR_AT_DROP


def test_same_tick_guard_drop_classifies_from_command_evidence_alone() -> None:
    """A boundary guard-drop is abnormal without a telemetry witness."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, "2026-08-26T12:00:00+00:00"),
        event_rows=(
            _event(1, RoastEventKind.COMMAND_EXECUTED, _drop_payload(DropReason.CEILING_GUARD)),
        ),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert result.evidence[0].kind is TerminationEvidenceKind.CEILING_GUARD_DROP_EXECUTED
    assert result.evidence[0].position is EvidencePosition.BEFORE_OR_AT_DROP


def test_no_drop_positions_every_abnormality_before_or_at_drop() -> None:
    """Without a proven boundary, all abnormal evidence remains conservative."""
    result = classify_termination(
        run_found=True,
        run_outcome="faulted",
        drop_anchor=None,
        event_rows=(_event(1, RoastEventKind.FAULT),),
        emergency_stop_recorded_at_utc=["2026-08-26T12:00:01+00:00"],
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert all(item.position is EvidencePosition.BEFORE_OR_AT_DROP for item in result.evidence)


@pytest.mark.parametrize("timestamp", ["", "not-a-time", None])
def test_malformed_or_missing_cross_table_timestamp_fails_closed(timestamp: str | None) -> None:
    """Malformed and absent e-stop timestamps never prove post-drop ordering."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, "2026-08-26T12:00:00+00:00"),
        event_rows=(),
        emergency_stop_recorded_at_utc=[timestamp],
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP


def test_equal_cross_table_timestamps_fail_closed() -> None:
    """Only a strict later timestamp is post-drop evidence."""
    timestamp = "2026-08-26T12:00:00+00:00"
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, timestamp),
        event_rows=(),
        emergency_stop_recorded_at_utc=[timestamp],
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP


def test_malformed_drop_anchor_timestamp_fails_closed() -> None:
    """An unreadable boundary timestamp cannot prove later cross-table evidence."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, "not-a-time"),
        event_rows=(),
        emergency_stop_recorded_at_utc=["2026-08-26T12:00:01+00:00"],
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP


def test_unknown_drop_reason_fails_closed() -> None:
    """An unknown typed drop reason is conservative provenance."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, "2026-08-26T12:00:00+00:00"),
        event_rows=(
            _event(
                1,
                RoastEventKind.COMMAND_EXECUTED,
                {"command": RoastCommand.DROP_BEANS.value, "reason": "operator_hunch"},
            ),
        ),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert result.evidence[0].kind is TerminationEvidenceKind.UNKNOWN_DROP_REASON


def test_absent_reason_on_advisor_drop_is_normal() -> None:
    """An ordinary advisor/operator drop has no reason evidence by design."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(1, "2026-08-26T12:00:00+00:00"),
        event_rows=(_event(1, RoastEventKind.COMMAND_EXECUTED, _drop_payload()),),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.NORMAL
    assert result.evidence == ()


def test_non_drop_commands_are_ignored() -> None:
    """Other command events do not become termination evidence."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(3, "2026-08-26T12:00:00+00:00"),
        event_rows=(
            _event(
                1,
                RoastEventKind.COMMAND_FAILED,
                {"command": RoastCommand.START_COOLING.value},
            ),
            _event(
                2,
                RoastEventKind.COMMAND_EXECUTED,
                {"command": RoastCommand.MARK_BEANS_ADDED.value},
            ),
        ),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.NORMAL


def test_malformed_command_payload_fails_closed() -> None:
    """Raw malformed command JSON is conservative rather than a SQL/parser crash."""
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(2, "2026-08-26T12:00:00+00:00"),
        event_rows=(TerminationEventRow(1, RoastEventKind.COMMAND_EXECUTED.value, "not-json"),),
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert result.evidence[0].kind is TerminationEvidenceKind.UNKNOWN_DROP_REASON


def test_unknown_event_command_and_reason_values_fail_closed() -> None:
    """Every untyped persisted command vocabulary is conservative provenance."""
    rows = (
        TerminationEventRow(1, "unknown-event", "{}"),
        _event(2, RoastEventKind.COMMAND_EXECUTED, {"command": None}),
        _event(3, RoastEventKind.COMMAND_FAILED, {"command": "unknown-command"}),
        _event(
            4,
            RoastEventKind.COMMAND_EXECUTED,
            {"command": RoastCommand.DROP_BEANS.value, "reason": None},
        ),
        _event(
            5,
            RoastEventKind.SAFETY_ALERT,
            _drop_payload(DropReason.CEILING_GUARD),
        ),
    )
    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=DropEventAnchor(6, "2026-08-26T12:00:00+00:00"),
        event_rows=rows,
        emergency_stop_recorded_at_utc=(),
    )

    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert [item.kind for item in result.evidence] == [
        TerminationEvidenceKind.UNKNOWN_DROP_REASON,
        TerminationEvidenceKind.UNKNOWN_DROP_REASON,
        TerminationEvidenceKind.UNKNOWN_DROP_REASON,
        TerminationEvidenceKind.UNKNOWN_DROP_REASON,
    ]
    assert select_first_executed_drop_event((rows[0],)) is None


def test_uncorroborated_outcome_and_pre_drop_fault_fail_closed() -> None:
    """Outcomes require every available corroborator to be strictly post-drop."""
    uncorroborated = classify_termination(
        run_found=True,
        run_outcome="faulted",
        drop_anchor=DropEventAnchor(1, "2026-08-26T12:00:00+00:00"),
        event_rows=(),
        emergency_stop_recorded_at_utc=(),
    )
    mixed = classify_termination(
        run_found=True,
        run_outcome="faulted",
        drop_anchor=DropEventAnchor(2, "2026-08-26T12:00:00+00:00"),
        event_rows=(_event(1, RoastEventKind.FAULT),),
        emergency_stop_recorded_at_utc=["2026-08-26T12:00:01+00:00"],
    )

    assert uncorroborated.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    assert mixed.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP


def test_event_positioning_and_anchor_selection_use_insertion_id() -> None:
    """Timestamps cannot override durable event insertion order or first drop."""
    failed_guard = _event(
        1,
        RoastEventKind.COMMAND_FAILED,
        _drop_payload(DropReason.CEILING_GUARD),
        recorded_at_utc="2026-08-26T12:10:00+00:00",
    )
    first_drop = _event(
        2,
        RoastEventKind.COMMAND_EXECUTED,
        _drop_payload(),
        recorded_at_utc="2026-08-26T12:00:00+00:00",
    )
    later_drop = _event(
        3,
        RoastEventKind.COMMAND_EXECUTED,
        _drop_payload(),
        recorded_at_utc="2026-08-26T11:00:00+00:00",
    )
    anchor = select_first_executed_drop_event((failed_guard, first_drop, later_drop))
    assert anchor == DropEventAnchor(2, "2026-08-26T12:00:00+00:00")

    result = classify_termination(
        run_found=True,
        run_outcome="completed",
        drop_anchor=anchor,
        event_rows=(failed_guard, first_drop, later_drop),
        emergency_stop_recorded_at_utc=(),
    )
    assert result.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
