"""Behavioural tests for retained ambient-doctrine evidence (#742)."""

from __future__ import annotations

import json

import pytest

from roastpilot_agent.ambient_evidence import (
    RECOVERY_PAYLOAD_KEY,
    AmbientDoctrineEvidence,
    AmbientEvidenceVerdict,
    DoctrineRecoveryState,
    NotProvenReason,
    derive_ambient_doctrine_evidence,
)
from roastpilot_agent.config import AmbientFanDoctrine, ControllerConfig


def _controller(*, enabled: bool = True, max_age: float = 90.0) -> ControllerConfig:
    """Build a frozen-controller equivalent for one evidence case."""
    return ControllerConfig(
        ambient_fan_doctrine=AmbientFanDoctrine(
            enabled=enabled,
            max_reading_age_seconds=max_age,
        )
    )


def _snapshot(
    *,
    row_id: int,
    tick: int,
    timestamp: str,
    phase: str = "development",
    token: float = 1.0,
    running: bool = True,
) -> dict[str, object]:
    """Build a retained raw-state row with a complete ambient status."""
    return {
        "id": row_id,
        "tick": tick,
        "recorded_at_utc": timestamp,
        "agent_phase": phase,
        "raw_state_json": json.dumps(
            {
                "ambient_status": {
                    "mode": "yoctopuce",
                    "status": "ok",
                    "ambient_running": running,
                    "temperature_c": 23.0,
                    "humidity_percent": 50.0,
                    "pressure_hpa": 1000.0,
                    "last_reading_monotonic_seconds": token,
                }
            }
        ),
    }


def _recovery(*, event_id: int, state: str = "preserved") -> dict[str, object]:
    """Build one fully typed recovery event row."""
    return {
        "id": event_id,
        "payload_json": json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": True,
                    "effective_enabled": state == "preserved",
                    "state": state,
                }
            }
        ),
    }


def test_first_token_is_uncorroborated_then_changed_token_is_observed() -> None:
    """A retained token must advance before it can establish fresh evidence."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            _snapshot(row_id=2, tick=2, timestamp="2026-08-25T12:00:10+00:00", token=2.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.OBSERVED
    assert evidence.fresh_retained_development_snapshot_count == 1
    assert evidence.retained_development_snapshot_fraction == 0.5


def test_duplicate_token_and_tick_reset_cannot_cross_corroborate() -> None:
    """A restart episode resets token identity and first-token evidence."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=5, timestamp="2026-08-25T12:00:00+00:00"),
            _snapshot(row_id=2, tick=0, timestamp="2026-08-25T12:01:00+00:00"),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.NO_CORROBORATED_FRESH_READING


def test_retirement_is_sticky_even_when_later_recovery_preserves() -> None:
    """A later preserved episode cannot rewrite an earlier retirement."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (_recovery(event_id=1, state="retired"), _recovery(event_id=2)),
        (),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.DOCTRINE_RETIRED
    assert evidence.ever_retired is True
    assert [episode.state for episode in evidence.recovery_episodes] == [
        DoctrineRecoveryState.RETIRED,
        DoctrineRecoveryState.PRESERVED,
    ]


def test_legacy_recovery_payload_fails_closed() -> None:
    """A pre-evidence recovery event has no default-preserved interpretation."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        ({"id": 1, "payload_json": "{}"},),
        (),
    )
    assert evidence.not_proven_reason is NotProvenReason.RECOVERY_STATE_UNKNOWN
    assert evidence.recovery_episodes[0].state is DoctrineRecoveryState.UNKNOWN


@pytest.mark.parametrize(
    ("age", "expected"),
    [(90, AmbientEvidenceVerdict.OBSERVED), (91, AmbientEvidenceVerdict.NOT_PROVEN)],
)
def test_frozen_age_bound_is_closed(age: int, expected: AmbientEvidenceVerdict) -> None:
    """Exactly the frozen age bound is fresh; any value above it is not."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(max_age=90.0),
        (),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            _snapshot(
                row_id=2,
                tick=2,
                timestamp="2026-08-25T12:00:01+00:00",
                phase="roasting_pre_first_crack",
                token=2.0,
            ),
            _snapshot(
                row_id=3,
                tick=3,
                timestamp=(f"2026-08-25T12:{(age + 1) // 60:02d}:{(age + 1) % 60:02d}+00:00"),
                token=2.0,
            ),
        ),
    )
    assert evidence.verdict is expected


def test_disabled_doctrine_and_charge_like_data_are_not_evidence() -> None:
    """A complete retained reading cannot promote a disabled doctrine."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(enabled=False),
        (),
        (_snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00"),),
    )
    assert evidence.not_proven_reason is NotProvenReason.DOCTRINE_DISABLED


def test_positive_model_requires_effective_fresh_evidence() -> None:
    """The typed aggregate rejects a hand-constructed false-positive result."""
    with pytest.raises(ValueError, match="positive evidence"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.OBSERVED,
            not_proven_reason=None,
            configured_enabled=True,
            effective_throughout=False,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=1,
            fresh_retained_development_snapshot_count=0,
            retained_development_snapshot_fraction=0.0,
        )
