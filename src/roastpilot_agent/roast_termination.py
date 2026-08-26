"""Fail-closed classification of persisted roast-termination evidence.

The classifier is deliberately pure: it turns the bounded read projections from
``RoastStore`` into stable provenance without opening the roast database or
issuing a roaster command.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import cast

from roastpilot_agent.models import DropReason, RoastCommand, RoastEventKind
from roastpilot_agent.roast_landmarks import parse_utc


class TerminationClassification(Enum):
    """The relationship between abnormal termination evidence and bean drop."""

    NORMAL = "normal"
    ABNORMAL_BEFORE_OR_AT_DROP = "abnormal_before_or_at_drop"
    ABNORMAL_AFTER_DROP = "abnormal_after_drop"


class TerminationEvidenceKind(Enum):
    """The durable signal that contributed to termination classification."""

    ABNORMAL_RUN_OUTCOME = "abnormal_run_outcome"
    EMERGENCY_STOP_VERDICT = "emergency_stop_verdict"
    CEILING_GUARD_DROP_EXECUTED = "ceiling_guard_drop_executed"
    CEILING_GUARD_DROP_FAILED = "ceiling_guard_drop_failed"
    FAULT_EVENT = "fault_event"
    UNKNOWN_DROP_REASON = "unknown_drop_reason"


class EvidencePosition(Enum):
    """Whether evidence is before/at or strictly after the first bean drop."""

    BEFORE_OR_AT_DROP = "before_or_at_drop"
    AFTER_DROP = "after_drop"


class PersistedNonRoastCommand(Enum):
    """Non-MCP command names deliberately persisted in command-failure events."""

    SET_TARGETS = "set_targets"
    SHUTDOWN_HEAT_OFF = "shutdown_heat_off"
    MCP_STOP = "mcp_stop"


@dataclass(frozen=True)
class DropEventAnchor:
    """The durable identity and timestamp of the first executed bean drop."""

    event_id: int
    recorded_at_utc: str | None


@dataclass(frozen=True)
class TerminationEventRow:
    """One raw ``roast_events`` projection used by the termination classifier."""

    event_id: int
    kind: str
    payload_json: str | None
    recorded_at_utc: str | None = None


@dataclass(frozen=True)
class TerminationEvidence:
    """One ordered item of durable termination provenance."""

    kind: TerminationEvidenceKind
    position: EvidencePosition


@dataclass(frozen=True)
class RunTermination:
    """A run's termination classification and deterministic evidence trace."""

    classification: TerminationClassification
    evidence: tuple[TerminationEvidence, ...]

    @property
    def terminated_abnormally(self) -> bool:
        """Whether the run is unscorable because evidence is before or at drop."""
        return self.classification is TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP


def _parse_payload_object(payload_json: str | None) -> dict[str, object] | None:
    """Parse one persisted command payload through a closed object grammar."""
    if not isinstance(payload_json, str):
        return None
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    return cast(dict[str, object], payload) if isinstance(payload, dict) else None


def _event_position(event_id: int, drop_anchor: DropEventAnchor | None) -> EvidencePosition:
    """Position same-table evidence using durable insertion identifiers only."""
    if drop_anchor is not None and event_id > drop_anchor.event_id:
        return EvidencePosition.AFTER_DROP
    return EvidencePosition.BEFORE_OR_AT_DROP


def _cross_table_position(
    recorded_at_utc: str | None, drop_anchor: DropEventAnchor | None
) -> EvidencePosition:
    """Position cross-table evidence only when UTC timestamps prove it is later."""
    if drop_anchor is None:
        return EvidencePosition.BEFORE_OR_AT_DROP
    drop_time = parse_utc(drop_anchor.recorded_at_utc)
    evidence_time = parse_utc(recorded_at_utc)
    if drop_time is not None and evidence_time is not None and evidence_time > drop_time:
        return EvidencePosition.AFTER_DROP
    return EvidencePosition.BEFORE_OR_AT_DROP


def _unknown_evidence() -> TerminationEvidence:
    """Return the conservative provenance item for unusable command evidence."""
    return TerminationEvidence(
        kind=TerminationEvidenceKind.UNKNOWN_DROP_REASON,
        position=EvidencePosition.BEFORE_OR_AT_DROP,
    )


def _parse_event_kind(value: str) -> RoastEventKind | None:
    """Convert one persisted event kind to its typed vocabulary, or fail closed."""
    try:
        return RoastEventKind(value)
    except ValueError:
        return None


def _parse_command(value: object) -> RoastCommand | PersistedNonRoastCommand | None:
    """Convert one persisted command to its closed typed vocabulary, or fail closed."""
    if not isinstance(value, str):
        return None
    try:
        return RoastCommand(value)
    except ValueError:
        try:
            return PersistedNonRoastCommand(value)
        except ValueError:
            return None


def _is_ordinary_executed_lever_payload(payload: dict[str, object]) -> bool:
    """Return whether a commandless payload has the persisted lever-event shape."""
    heat = payload.get("heat_percent")
    fan = payload.get("fan_percent")
    return (
        "command" not in payload
        and isinstance(heat, int)
        and not isinstance(heat, bool)
        and isinstance(fan, int)
        and not isinstance(fan, bool)
    )


def _parse_drop_reason(value: object) -> DropReason | None:
    """Convert one persisted drop reason to its typed vocabulary, or fail closed."""
    if not isinstance(value, str):
        return None
    try:
        return DropReason(value)
    except ValueError:
        return None


def select_first_executed_drop_event(
    rows: Sequence[TerminationEventRow],
) -> DropEventAnchor | None:
    """Select the first successfully executed ``drop_beans`` event from raw rows.

    Args:
        rows: ``COMMAND_EXECUTED`` event rows in durable insertion order.

    Returns:
        The first proven drop anchor, or ``None`` when no closed-grammar
        command payload proves that an executed bean drop occurred.
    """
    for row in rows:
        event_kind = _parse_event_kind(row.kind)
        if event_kind is not RoastEventKind.COMMAND_EXECUTED:
            continue
        payload = _parse_payload_object(row.payload_json)
        if payload is None:
            continue
        command = _parse_command(payload.get("command"))
        if command is RoastCommand.DROP_BEANS:
            return DropEventAnchor(
                event_id=row.event_id,
                recorded_at_utc=row.recorded_at_utc,
            )
    return None


def classify_termination(
    *,
    run_found: bool,
    run_outcome: str | None,
    drop_anchor: DropEventAnchor | None,
    event_rows: Sequence[TerminationEventRow],
    emergency_stop_recorded_at_utc: Sequence[str | None],
) -> RunTermination:
    """Classify abnormal termination relative to the first executed bean drop.

    Events are ordered by durable ``roast_events.id``; emergency-stop verdicts
    are ordered by their store query. Missing, malformed, or unknown command
    evidence is intentionally conservative and is never allowed to escape as a
    parsing error.

    Args:
        run_found: Whether the requested ``roast_runs`` row exists.
        run_outcome: Persisted outcome, if present.
        drop_anchor: First durable executed ``drop_beans`` event, if any.
        event_rows: Ordered command/fault event projections.
        emergency_stop_recorded_at_utc: Ordered emergency-stop verdict times.

    Returns:
        Stable termination classification and its ordered provenance.
    """
    event_evidence: list[TerminationEvidence] = []
    corroborator_positions: list[EvidencePosition] = []

    for row in event_rows:
        event_kind = _parse_event_kind(row.kind)
        if event_kind is None:
            event_evidence.append(_unknown_evidence())
            continue
        event_position = _event_position(row.event_id, drop_anchor)
        if event_kind is RoastEventKind.FAULT:
            event_evidence.append(
                TerminationEvidence(
                    kind=TerminationEvidenceKind.FAULT_EVENT,
                    position=event_position,
                )
            )
            corroborator_positions.append(event_position)
            continue
        if event_kind not in (RoastEventKind.COMMAND_EXECUTED, RoastEventKind.COMMAND_FAILED):
            event_evidence.append(_unknown_evidence())
            continue

        payload = _parse_payload_object(row.payload_json)
        if payload is None:
            event_evidence.append(_unknown_evidence())
            continue
        command = _parse_command(payload.get("command"))
        if command is None:
            if (
                event_kind is RoastEventKind.COMMAND_EXECUTED
                and _is_ordinary_executed_lever_payload(payload)
            ):
                continue
            event_evidence.append(_unknown_evidence())
            continue
        if isinstance(command, PersistedNonRoastCommand):
            continue
        if command is not RoastCommand.DROP_BEANS:
            continue
        if "reason" not in payload:
            continue
        reason = _parse_drop_reason(payload["reason"])
        if reason is None:
            event_evidence.append(_unknown_evidence())
            continue
        if reason is DropReason.DEVELOPMENT_TARGET:
            continue
        if reason is DropReason.CEILING_GUARD:
            if event_kind is RoastEventKind.COMMAND_EXECUTED:
                event_evidence.append(
                    TerminationEvidence(
                        kind=TerminationEvidenceKind.CEILING_GUARD_DROP_EXECUTED,
                        position=event_position,
                    )
                )
            else:
                event_evidence.append(
                    TerminationEvidence(
                        kind=TerminationEvidenceKind.CEILING_GUARD_DROP_FAILED,
                        position=event_position,
                    )
                )
            continue
        event_evidence.append(_unknown_evidence())

    emergency_evidence = tuple(
        TerminationEvidence(
            kind=TerminationEvidenceKind.EMERGENCY_STOP_VERDICT,
            position=_cross_table_position(recorded_at_utc, drop_anchor),
        )
        for recorded_at_utc in emergency_stop_recorded_at_utc
    )
    corroborator_positions.extend(evidence.position for evidence in emergency_evidence)

    outcome_evidence: tuple[TerminationEvidence, ...] = ()
    abnormal_outcome = True
    if run_found and run_outcome == "completed":
        abnormal_outcome = False
    if abnormal_outcome:
        outcome_position = EvidencePosition.BEFORE_OR_AT_DROP
        if (
            drop_anchor is not None
            and corroborator_positions
            and all(position is EvidencePosition.AFTER_DROP for position in corroborator_positions)
        ):
            outcome_position = EvidencePosition.AFTER_DROP
        outcome_evidence = (
            TerminationEvidence(
                kind=TerminationEvidenceKind.ABNORMAL_RUN_OUTCOME,
                position=outcome_position,
            ),
        )

    evidence = tuple(event_evidence) + emergency_evidence + outcome_evidence
    if not evidence:
        classification = TerminationClassification.NORMAL
    elif any(item.position is EvidencePosition.BEFORE_OR_AT_DROP for item in evidence):
        classification = TerminationClassification.ABNORMAL_BEFORE_OR_AT_DROP
    else:
        classification = TerminationClassification.ABNORMAL_AFTER_DROP
    return RunTermination(classification=classification, evidence=evidence)
