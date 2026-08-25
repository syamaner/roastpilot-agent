"""Behavioural tests for retained ambient-doctrine evidence (#742)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import roastpilot_agent.ambient_evidence as ambient_evidence
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


def test_unaccounted_tick_reset_cannot_cross_corroborate() -> None:
    """An unaccounted restart is unusable rather than duplicate-token evidence."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=5, timestamp="2026-08-25T12:00:00+00:00"),
            _snapshot(row_id=2, tick=0, timestamp="2026-08-25T12:01:00+00:00"),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA


def test_same_generation_duplicate_token_never_counts_fresh() -> None:
    """A fixed finite token cannot rebase freshness without a reset or recovery."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            _snapshot(row_id=2, tick=2, timestamp="2026-08-25T12:00:10+00:00", token=1.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.NO_CORROBORATED_FRESH_READING
    assert evidence.fresh_retained_development_snapshot_count == 0


@pytest.mark.parametrize(
    "malformed_raw_state",
    (
        "{not json",
        "[]",
        "{}",
        json.dumps(
            {
                "ambient_status": {
                    "mode": "yoctopuce",
                    "status": "ok",
                    "ambient_running": True,
                    "temperature_c": "23.0",
                    "humidity_percent": 50.0,
                    "pressure_hpa": 1000.0,
                    "last_reading_monotonic_seconds": 2.0,
                }
            }
        ),
        json.dumps(
            {
                "ambient_status": {
                    "mode": "yoctopuce",
                    "status": "ok",
                    "ambient_running": True,
                    "temperature_c": 23.0,
                    "humidity_percent": 50.0,
                    "pressure_hpa": 1000.0,
                    "last_reading_monotonic_seconds": float("inf"),
                }
            }
        ),
    ),
)
def test_malformed_ambient_row_blocks_later_evidence(malformed_raw_state: str) -> None:
    """Malformed retained ambient data cannot be repaired by later good rows."""
    malformed_row = _snapshot(
        row_id=2,
        tick=2,
        timestamp="2026-08-25T12:00:10+00:00",
        token=2.0,
    )
    malformed_row["raw_state_json"] = malformed_raw_state
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (_recovery(event_id=1),),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            malformed_row,
            _snapshot(row_id=3, tick=3, timestamp="2026-08-25T12:00:20+00:00", token=2.0),
            _snapshot(row_id=4, tick=4, timestamp="2026-08-25T12:00:30+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA


@pytest.mark.parametrize("invalid_running", (0, 1, "true"))
def test_non_bool_ambient_running_is_malformed_and_cannot_be_repaired(
    invalid_running: object,
) -> None:
    """Raw Pydantic-coercible running values are not admitted retained evidence."""
    malformed_row = _snapshot(
        row_id=2,
        tick=2,
        timestamp="2026-08-25T12:00:10+00:00",
        token=2.0,
    )
    raw_state = json.loads(str(malformed_row["raw_state_json"]))
    assert isinstance(raw_state, dict)
    raw_state_mapping = cast("dict[str, object]", raw_state)
    raw_status = raw_state_mapping["ambient_status"]
    assert isinstance(raw_status, dict)
    raw_status_mapping = cast("dict[str, object]", raw_status)
    raw_status_mapping["ambient_running"] = invalid_running
    malformed_row["raw_state_json"] = json.dumps(raw_state)
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (_recovery(event_id=1),),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            malformed_row,
            _snapshot(row_id=3, tick=3, timestamp="2026-08-25T12:00:20+00:00", token=2.0),
            _snapshot(row_id=4, tick=4, timestamp="2026-08-25T12:00:30+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA


def test_invalid_retained_status_after_bool_guard_is_malformed() -> None:
    """Closed retained-status validation still rejects an invalid mode after raw checks."""
    malformed_row = _snapshot(
        row_id=2,
        tick=2,
        timestamp="2026-08-25T12:00:10+00:00",
        token=2.0,
    )
    raw_state = json.loads(str(malformed_row["raw_state_json"]))
    assert isinstance(raw_state, dict)
    raw_status = cast("dict[str, object]", raw_state)["ambient_status"]
    assert isinstance(raw_status, dict)
    cast("dict[str, object]", raw_status)["mode"] = "unexpected"
    malformed_row["raw_state_json"] = json.dumps(raw_state)
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (_recovery(event_id=1),),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            malformed_row,
            _snapshot(row_id=3, tick=3, timestamp="2026-08-25T12:00:20+00:00", token=2.0),
            _snapshot(row_id=4, tick=4, timestamp="2026-08-25T12:00:30+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA


def test_non_live_retained_projection_is_absent() -> None:
    """A valid stopped retained runtime projects no ambient triad."""
    status = ambient_evidence._RetainedAmbientStatus.model_validate(  # pyright: ignore[reportPrivateUsage]
        {
            "mode": "yoctopuce",
            "status": "ok",
            "ambient_running": False,
            "temperature_c": 23.0,
            "humidity_percent": 50.0,
            "pressure_hpa": 1000.0,
        }
    )
    assert ambient_evidence._retained_live_ambient(status) == (None, None, None)  # pyright: ignore[reportPrivateUsage]


def test_stopped_ambient_resets_then_later_recorroboration_can_be_observed() -> None:
    """A valid stopped runtime is a non-fresh denominator row, not malformed data."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            _snapshot(
                row_id=2,
                tick=2,
                timestamp="2026-08-25T12:00:10+00:00",
                token=1.0,
                running=False,
            ),
            _snapshot(row_id=3, tick=3, timestamp="2026-08-25T12:00:20+00:00", token=2.0),
            _snapshot(row_id=4, tick=4, timestamp="2026-08-25T12:00:30+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.OBSERVED
    assert evidence.retained_development_snapshot_fraction == 0.25


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


def test_model_rejects_inverted_counts_and_non_finite_fraction() -> None:
    """The aggregate cannot express inverted counts or an unrepresentable fraction."""
    with pytest.raises(ValueError, match="count cannot invert"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.NOT_PROVEN,
            not_proven_reason=NotProvenReason.NO_DEVELOPMENT_SNAPSHOTS,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=1,
            fresh_retained_development_snapshot_count=2,
            retained_development_snapshot_fraction=1.0,
        )
    with pytest.raises(ValueError, match="fraction must be finite"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.NOT_PROVEN,
            not_proven_reason=NotProvenReason.NO_DEVELOPMENT_SNAPSHOTS,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=0,
            fresh_retained_development_snapshot_count=0,
            retained_development_snapshot_fraction=float("inf"),
        )


def test_model_rejects_effective_retirement_and_invalid_verdict_reasons() -> None:
    """Model invariants prevent false positive and reason-less evidence claims."""
    retired = ambient_evidence.DoctrineRecoveryEpisode(
        event_id=1,
        configured_enabled=True,
        effective_enabled=False,
        state=DoctrineRecoveryState.RETIRED,
    )
    with pytest.raises(ValueError, match="cannot be effective"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.NOT_PROVEN,
            not_proven_reason=NotProvenReason.DOCTRINE_RETIRED,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=True,
            recovery_episodes=(retired,),
            retained_development_snapshot_count=0,
            fresh_retained_development_snapshot_count=0,
            retained_development_snapshot_fraction=0.0,
        )
    with pytest.raises(ValueError, match="positive evidence has no failure reason"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.OBSERVED,
            not_proven_reason=NotProvenReason.DOCTRINE_DISABLED,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=1,
            fresh_retained_development_snapshot_count=1,
            retained_development_snapshot_fraction=1.0,
        )
    with pytest.raises(ValueError, match="positive evidence needs"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.OBSERVED,
            not_proven_reason=None,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=1,
            fresh_retained_development_snapshot_count=0,
            retained_development_snapshot_fraction=0.0,
        )
    with pytest.raises(ValueError, match="not_proven evidence needs a reason"):
        AmbientDoctrineEvidence(
            verdict=AmbientEvidenceVerdict.NOT_PROVEN,
            not_proven_reason=None,
            configured_enabled=True,
            effective_throughout=True,
            ever_retired=False,
            recovery_episodes=(),
            retained_development_snapshot_count=0,
            fresh_retained_development_snapshot_count=0,
            retained_development_snapshot_fraction=0.0,
        )


@pytest.mark.parametrize(
    "payload_json",
    (
        None,
        "not json",
        "[]",
        "{}",
        json.dumps({RECOVERY_PAYLOAD_KEY: {"configured_enabled": True}}),
        json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": "true",
                    "effective_enabled": True,
                    "state": "preserved",
                }
            }
        ),
        json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": True,
                    "effective_enabled": True,
                    "state": "unknown",
                }
            }
        ),
        json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": True,
                    "effective_enabled": True,
                    "state": "future",
                }
            }
        ),
        json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": True,
                    "effective_enabled": False,
                    "state": "preserved",
                }
            }
        ),
        json.dumps(
            {
                RECOVERY_PAYLOAD_KEY: {
                    "configured_enabled": False,
                    "effective_enabled": False,
                    "state": "retired",
                }
            }
        ),
    ),
)
def test_recovery_grammar_fails_closed(payload_json: object) -> None:
    """Every malformed recovery payload remains a typed unknown episode."""
    episode = ambient_evidence._episode_from_row(  # pyright: ignore[reportPrivateUsage]
        {"id": True, "payload_json": payload_json}
    )
    assert episode.event_id == 0
    assert episode.state is DoctrineRecoveryState.UNKNOWN


def test_non_string_json_mapping_keys_fail_closed() -> None:
    """Untyped decoded mappings need string keys before their members are trusted."""
    assert ambient_evidence._object_mapping({1: "value"}) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "raw_state_json",
    (
        None,
        "not json",
        "[]",
        "{}",
        json.dumps({"ambient_status": {"mode": "invalid"}}),
        json.dumps(
            {
                "ambient_status": {
                    "mode": "yoctopuce",
                    "status": "ok",
                    "ambient_running": True,
                    "temperature_c": float("inf"),
                    "humidity_percent": 50.0,
                    "pressure_hpa": 1000.0,
                    "last_reading_monotonic_seconds": 1.0,
                }
            }
        ),
    ),
)
def test_snapshot_status_marks_each_malformed_shape(raw_state_json: object) -> None:
    """Malformed retained state is distinct from a valid but stopped runtime."""
    status, malformed = ambient_evidence._snapshot_status(  # pyright: ignore[reportPrivateUsage]
        {"raw_state_json": raw_state_json}
    )
    assert status is None
    assert malformed is True


def test_derivation_rejects_bad_clock_phase_and_timestamp_forms() -> None:
    """Bad chronology remains terminally not-proven despite later fresh evidence."""
    non_string_timestamp = _snapshot(
        row_id=3,
        tick=3,
        timestamp="2026-08-25T12:00:20+00:00",
        token=2.0,
    )
    non_string_timestamp["recorded_at_utc"] = None
    invalid_rows = (
        _snapshot(row_id=1, tick=True, timestamp="2026-08-25T12:00:00+00:00"),
        _snapshot(row_id=2, tick=2, timestamp="2026-08-25T12:00:10+00:00", phase="unknown"),
        non_string_timestamp,
        _snapshot(row_id=4, tick=4, timestamp="not a timestamp", token=3.0),
        _snapshot(row_id=5, tick=5, timestamp="2026-08-25T12:00:20", token=4.0),
        _snapshot(row_id=6, tick=6, timestamp="2026-08-25T12:00:30+00:00", token=5.0),
        _snapshot(row_id=7, tick=7, timestamp="2026-08-25T12:00:15+00:00", token=6.0),
        _snapshot(row_id=8, tick=8, timestamp="2026-08-25T12:00:40+00:00", token=7.0),
    )
    evidence = derive_ambient_doctrine_evidence(_controller(), (), invalid_rows)
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA


def test_cross_episode_clock_restarts_are_valid() -> None:
    """A tick reset isolates timestamp domains rather than creating a backwards clock."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (_recovery(event_id=1),),
        (
            _snapshot(row_id=1, tick=5, timestamp="2026-08-25T12:01:00+00:00", token=1.0),
            _snapshot(row_id=2, tick=0, timestamp="2026-08-25T12:00:00+00:00", token=2.0),
            _snapshot(row_id=3, tick=1, timestamp="2026-08-25T12:00:10+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.OBSERVED


def test_live_unread_status_resets_without_becoming_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live status without an admitted token is an ordinary non-fresh reset."""

    def _tokenless(_status: object) -> None:
        return None

    monkeypatch.setattr(ambient_evidence, "_retained_ambient_token", _tokenless)
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (_snapshot(row_id=1, tick=1, timestamp="2026-08-25T12:00:00+00:00"),),
    )
    assert evidence.not_proven_reason is NotProvenReason.NO_CORROBORATED_FRESH_READING


def test_missing_config_and_non_development_rows_fail_closed_for_their_own_reasons() -> None:
    """Absent config and an empty retained DEVELOPMENT denominator stay distinct."""
    assert (
        derive_ambient_doctrine_evidence(None, (), ()).not_proven_reason
        is NotProvenReason.RUN_OR_CONFIG_UNAVAILABLE
    )
    no_development = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(
                row_id=1,
                tick=1,
                timestamp="2026-08-25T12:00:00+00:00",
                phase="roasting_pre_first_crack",
            ),
        ),
    )
    assert no_development.not_proven_reason is NotProvenReason.NO_DEVELOPMENT_SNAPSHOTS


def test_retained_evidence_import_boundary_is_local_and_transitively_closed() -> None:
    """Offline evidence must not load controller, safety, or the live MCP client."""
    source = Path(ambient_evidence.__file__).read_text(encoding="utf-8")
    assert "roastpilot_agent.mcp_client" not in source
    assert "roastpilot_agent.controller" not in source
    assert "roastpilot_agent.safety" not in source
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import roastpilot_agent.ambient_evidence; "
            "assert not {'roastpilot_agent.controller', 'roastpilot_agent.safety', "
            "'roastpilot_agent.mcp_client'} & set(sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_deeply_nested_retained_json_fails_closed_without_raising() -> None:
    """Hostile retained JSON recursion cannot escape either offline decode boundary."""
    nested = "[" * 2000 + "0" + "]" * 2000
    episode = ambient_evidence._episode_from_row(  # pyright: ignore[reportPrivateUsage]
        {"id": 1, "payload_json": nested}
    )
    status, malformed = ambient_evidence._snapshot_status(  # pyright: ignore[reportPrivateUsage]
        {"raw_state_json": nested}
    )
    assert episode.state is DoctrineRecoveryState.UNKNOWN
    assert status is None
    assert malformed is True


def test_unaccounted_tick_reset_blocks_observed_evidence() -> None:
    """A second process-generation segment needs its own durable recovery episode."""
    evidence = derive_ambient_doctrine_evidence(
        _controller(),
        (),
        (
            _snapshot(row_id=1, tick=4, timestamp="2026-08-25T12:00:00+00:00", token=1.0),
            _snapshot(row_id=2, tick=0, timestamp="2026-08-25T12:01:00+00:00", token=2.0),
            _snapshot(row_id=3, tick=1, timestamp="2026-08-25T12:01:10+00:00", token=3.0),
        ),
    )
    assert evidence.verdict is AmbientEvidenceVerdict.NOT_PROVEN
    assert evidence.not_proven_reason is NotProvenReason.UNUSABLE_CLOCK_OR_DATA
