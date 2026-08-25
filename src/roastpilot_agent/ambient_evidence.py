"""Conservative, read-only ambient-doctrine evidence derived from retained state.

The evidence in this module deliberately describes retained DEVELOPMENT
snapshots, not controller ticks or an advisor's reasoning.  It is a corpus
claim only: uncertain historical data is represented as ``not_proven``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, model_validator

RECOVERY_PAYLOAD_KEY = "ambient_fan_doctrine_recovery"
AMBIENT_EVIDENCE_CLAIM = (
    "Fresh ambient was durably observed while effective; the fraction is over retained "
    "DEVELOPMENT telemetry snapshots. It is not evidence the advisor reasoned on ambient "
    "and not controller-tick or advisor-decision coverage."
)
"""The sole public interpretation of the retained ambient-evidence fraction."""


class DoctrineRecoveryState(Enum):
    """The recorded effective doctrine state for one recovery episode."""

    PRESERVED = "preserved"
    RETIRED = "retired"
    UNKNOWN = "unknown"


class AmbientEvidenceVerdict(Enum):
    """Whether retained state proves fresh ambient doctrine input."""

    OBSERVED = "observed"
    NOT_PROVEN = "not_proven"


class NotProvenReason(Enum):
    """Closed reasons a retained-state claim cannot be promoted."""

    RUN_OR_CONFIG_UNAVAILABLE = "run_or_config_unavailable"
    DOCTRINE_DISABLED = "doctrine_disabled"
    DOCTRINE_RETIRED = "doctrine_retired"
    RECOVERY_STATE_UNKNOWN = "recovery_state_unknown"
    NO_DEVELOPMENT_SNAPSHOTS = "no_development_snapshots"
    UNUSABLE_CLOCK_OR_DATA = "unusable_clock_or_data"
    NO_CORROBORATED_FRESH_READING = "no_corroborated_fresh_reading"


class FractionBasis(Enum):
    """The exact denominator represented by ambient coverage."""

    RETAINED_DEVELOPMENT_SNAPSHOTS = "retained_development_snapshots"


class _AmbientDoctrineConfig(Protocol):
    """The frozen doctrine fields required by offline retained-state derivation."""

    @property
    def enabled(self) -> bool:
        """Whether the frozen doctrine was enabled."""
        ...

    @property
    def max_reading_age_seconds(self) -> float:
        """The frozen retained-reading age bound."""
        ...


class _FrozenControllerConfig(Protocol):
    """The neutral frozen-controller projection needed by this offline module."""

    @property
    def ambient_fan_doctrine(self) -> _AmbientDoctrineConfig:
        """The frozen ambient doctrine projection."""
        ...


class _RetainedAmbientStatus(BaseModel):
    """Local, closed retained-JSON projection; it deliberately imports no live MCP path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["disabled", "yoctopuce"]
    status: Literal["disabled", "unavailable", "ok"]
    reason: str | None = None
    ambient_running: bool = False
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    last_reading_monotonic_seconds: float | None = None


class DoctrineRecoveryEpisode(BaseModel):
    """One recovery event in durable event-id order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: int
    configured_enabled: bool | None = None
    effective_enabled: bool | None = None
    state: DoctrineRecoveryState


class AmbientDoctrineEvidence(BaseModel):
    """Conservative aggregate for one run's retained ambient evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AmbientEvidenceVerdict
    not_proven_reason: NotProvenReason | None
    fraction_basis: FractionBasis = FractionBasis.RETAINED_DEVELOPMENT_SNAPSHOTS
    configured_enabled: bool | None
    effective_throughout: bool
    ever_retired: bool
    recovery_episodes: tuple[DoctrineRecoveryEpisode, ...]
    retained_development_snapshot_count: int
    fresh_retained_development_snapshot_count: int
    retained_development_snapshot_fraction: float

    @model_validator(mode="after")
    def _validate_consistency(self) -> AmbientDoctrineEvidence:
        """Reject internally inconsistent positive or fractional claims."""
        if not (
            0
            <= self.fresh_retained_development_snapshot_count
            <= self.retained_development_snapshot_count
        ):
            raise ValueError("fresh retained DEVELOPMENT count cannot invert")
        if not math.isfinite(self.retained_development_snapshot_fraction) or not (
            0.0 <= self.retained_development_snapshot_fraction <= 1.0
        ):
            raise ValueError("retained DEVELOPMENT fraction must be finite and in [0, 1]")
        if self.effective_throughout and (
            self.ever_retired
            or any(
                episode.state in (DoctrineRecoveryState.RETIRED, DoctrineRecoveryState.UNKNOWN)
                for episode in self.recovery_episodes
            )
        ):
            raise ValueError("retired or unknown recovery cannot be effective throughout")
        if self.verdict is AmbientEvidenceVerdict.OBSERVED:
            if self.not_proven_reason is not None:
                raise ValueError("positive evidence has no failure reason")
            if not self.effective_throughout or self.fresh_retained_development_snapshot_count < 1:
                raise ValueError("positive evidence needs effective fresh DEVELOPMENT evidence")
        elif self.not_proven_reason is None:
            raise ValueError("not_proven evidence needs a reason")
        return self


def _is_finite_number(value: object) -> bool:
    """Whether ``value`` is a finite numeric scalar but not a boolean."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow an untrusted JSON value to a mapping with string keys."""
    if not isinstance(value, dict):
        return None
    raw_mapping = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in raw_mapping):
        return None
    return cast("Mapping[str, object]", raw_mapping)


def _not_proven(
    *,
    configured_enabled: bool | None,
    effective_throughout: bool,
    ever_retired: bool,
    episodes: tuple[DoctrineRecoveryEpisode, ...],
    development_count: int,
    fresh_count: int,
    reason: NotProvenReason,
) -> AmbientDoctrineEvidence:
    """Build a normalized failure result without ever raising on corpus rows."""
    fraction = fresh_count / development_count if development_count else 0.0
    return AmbientDoctrineEvidence(
        verdict=AmbientEvidenceVerdict.NOT_PROVEN,
        not_proven_reason=reason,
        configured_enabled=configured_enabled,
        effective_throughout=effective_throughout,
        ever_retired=ever_retired,
        recovery_episodes=episodes,
        retained_development_snapshot_count=development_count,
        fresh_retained_development_snapshot_count=fresh_count,
        retained_development_snapshot_fraction=fraction,
    )


def _episode_from_row(row: Mapping[str, object]) -> DoctrineRecoveryEpisode:
    """Parse one recovery row; malformed or legacy payloads remain explicit unknowns."""
    event_id = row.get("id")
    safe_id = event_id if isinstance(event_id, int) and not isinstance(event_id, bool) else 0
    raw_payload = row.get("payload_json")
    if not isinstance(raw_payload, str):
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    try:
        root = json.loads(raw_payload)
    except (TypeError, ValueError, RecursionError):
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    root_mapping = _object_mapping(root)
    if root_mapping is None:
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    payload = _object_mapping(root_mapping.get(RECOVERY_PAYLOAD_KEY))
    if payload is None or set(payload) != {
        "configured_enabled",
        "effective_enabled",
        "state",
    }:
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    configured = payload["configured_enabled"]
    effective = payload["effective_enabled"]
    state = payload["state"]
    if (
        not isinstance(configured, bool)
        or not isinstance(effective, bool)
        or not isinstance(state, str)
    ):
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    try:
        parsed_state = DoctrineRecoveryState(state)
    except ValueError:
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    if parsed_state is DoctrineRecoveryState.UNKNOWN:
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    if parsed_state is DoctrineRecoveryState.PRESERVED and configured != effective:
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    if parsed_state is DoctrineRecoveryState.RETIRED and (not configured or effective):
        return DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN)
    return DoctrineRecoveryEpisode(
        event_id=safe_id,
        configured_enabled=configured,
        effective_enabled=effective,
        state=parsed_state,
    )


def _snapshot_status(row: Mapping[str, object]) -> tuple[_RetainedAmbientStatus | None, bool]:
    """Parse one retained status, returning whether its retained shape was malformed."""
    raw_state = row.get("raw_state_json")
    if not isinstance(raw_state, str):
        return None, True
    try:
        root = json.loads(raw_state)
    except (TypeError, ValueError, RecursionError):
        return None, True
    root_mapping = _object_mapping(root)
    if root_mapping is None:
        return None, True
    raw_status = _object_mapping(root_mapping.get("ambient_status"))
    if raw_status is None:
        return None, True
    try:
        status = _RetainedAmbientStatus.model_validate(raw_status)
    except Exception:  # noqa: BLE001 - malformed retained JSON is not evidence
        return None, True
    if not _retained_ambient_is_live(status):
        return status, False
    for field in (
        "temperature_c",
        "humidity_percent",
        "pressure_hpa",
        "last_reading_monotonic_seconds",
    ):
        if not _is_finite_number(raw_status.get(field)):
            return None, True
    return status, False


def _retained_ambient_is_live(status: _RetainedAmbientStatus) -> bool:
    """Whether the retained MCP status says the ambient runtime was live."""
    return status.status == "ok" and status.ambient_running


def _retained_live_ambient(
    status: _RetainedAmbientStatus,
) -> tuple[float | None, float | None, float | None]:
    """Project the retained live triad without importing the live MCP module."""
    if not _retained_ambient_is_live(status):
        return None, None, None
    return status.temperature_c, status.humidity_percent, status.pressure_hpa


def _retained_ambient_token(status: _RetainedAmbientStatus) -> float | None:
    """Return the finite opaque retained reading token, if one was recorded."""
    token = status.last_reading_monotonic_seconds
    return token if token is not None and math.isfinite(token) else None


def derive_ambient_doctrine_evidence(
    frozen_controller: _FrozenControllerConfig | None,
    recovery_rows: Sequence[Mapping[str, object]],
    snapshot_rows: Sequence[Mapping[str, object]],
) -> AmbientDoctrineEvidence:
    """Derive a total, conservative evidence result from rows already ordered by id.

    Args:
        frozen_controller: The run's immutable controller configuration.
        recovery_rows: ``RECOVERY_REQUIRED`` event rows in insertion-id order.
        snapshot_rows: Telemetry rows in insertion-id order.

    Returns:
        A typed observed or not-proven result.  Malformed retained state never
        escapes as an exception and cannot produce ``observed``.
    """
    if frozen_controller is None:
        return _not_proven(
            configured_enabled=None,
            effective_throughout=False,
            ever_retired=False,
            episodes=(),
            development_count=0,
            fresh_count=0,
            reason=NotProvenReason.RUN_OR_CONFIG_UNAVAILABLE,
        )
    configured_enabled = frozen_controller.ambient_fan_doctrine.enabled
    episodes = tuple(_episode_from_row(row) for row in recovery_rows)
    ever_retired = any(episode.state is DoctrineRecoveryState.RETIRED for episode in episodes)
    unknown_recovery = any(episode.state is DoctrineRecoveryState.UNKNOWN for episode in episodes)
    if not configured_enabled:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=False,
            ever_retired=ever_retired,
            episodes=episodes,
            development_count=0,
            fresh_count=0,
            reason=NotProvenReason.DOCTRINE_DISABLED,
        )
    if ever_retired:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=False,
            ever_retired=True,
            episodes=episodes,
            development_count=0,
            fresh_count=0,
            reason=NotProvenReason.DOCTRINE_RETIRED,
        )
    if unknown_recovery:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=False,
            ever_retired=False,
            episodes=episodes,
            development_count=0,
            fresh_count=0,
            reason=NotProvenReason.RECOVERY_STATE_UNKNOWN,
        )

    development_count = 0
    fresh_count = 0
    previous_tick: int | None = None
    previous_timestamp: datetime | None = None
    token: float | None = None
    corroborated_at: datetime | None = None
    unusable_clock = False
    tick_reset_count = 0
    for row in snapshot_rows:
        raw_tick = row.get("tick")
        if not isinstance(raw_tick, int) or isinstance(raw_tick, bool):
            unusable_clock = True
            continue
        reset = previous_tick is None or raw_tick <= previous_tick
        if previous_tick is not None and reset:
            tick_reset_count += 1
        previous_tick = raw_tick
        if reset:
            token = None
            corroborated_at = None
            previous_timestamp = None

        raw_phase = row.get("agent_phase")
        if not isinstance(raw_phase, str) or raw_phase not in {
            "idle",
            "starting",
            "preheating",
            "roasting_pre_first_crack",
            "development",
            "cooling",
            "complete",
            "faulted",
            "operator_recovery_required",
        }:
            unusable_clock = True
            is_development = False
        else:
            is_development = raw_phase == "development"
        if is_development:
            development_count += 1

        status, malformed = _snapshot_status(row)
        if malformed:
            unusable_clock = True
            token = None
            corroborated_at = None
            continue
        if status is None or not _retained_ambient_is_live(status):
            token = None
            corroborated_at = None
            continue
        triad = _retained_live_ambient(status)
        current_token = _retained_ambient_token(status)
        if current_token is None or not all(_is_finite_number(value) for value in triad):
            token = None
            corroborated_at = None
            continue
        raw_recorded_at = row.get("recorded_at_utc")
        if not isinstance(raw_recorded_at, str):
            unusable_clock = True
            token = None
            corroborated_at = None
            continue
        try:
            observed_at = datetime.fromisoformat(raw_recorded_at)
        except ValueError:
            unusable_clock = True
            token = None
            corroborated_at = None
            continue
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            unusable_clock = True
            token = None
            corroborated_at = None
            continue
        if previous_timestamp is not None and observed_at < previous_timestamp:
            unusable_clock = True
            token = None
            corroborated_at = None
            previous_timestamp = observed_at
            continue
        previous_timestamp = observed_at
        if token is None:
            token = current_token
            continue
        if current_token != token:
            token = current_token
            corroborated_at = observed_at
        if is_development and corroborated_at is not None:
            age_seconds = (observed_at - corroborated_at).total_seconds()
            if (
                math.isfinite(age_seconds)
                and 0.0
                <= age_seconds
                <= frozen_controller.ambient_fan_doctrine.max_reading_age_seconds
            ):
                fresh_count += 1

    if tick_reset_count > len(episodes):
        unusable_clock = True
    if development_count == 0:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=True,
            ever_retired=False,
            episodes=episodes,
            development_count=0,
            fresh_count=0,
            reason=NotProvenReason.NO_DEVELOPMENT_SNAPSHOTS,
        )
    if unusable_clock:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=True,
            ever_retired=False,
            episodes=episodes,
            development_count=development_count,
            fresh_count=fresh_count,
            reason=NotProvenReason.UNUSABLE_CLOCK_OR_DATA,
        )
    if fresh_count == 0:
        return _not_proven(
            configured_enabled=configured_enabled,
            effective_throughout=True,
            ever_retired=False,
            episodes=episodes,
            development_count=development_count,
            fresh_count=0,
            reason=NotProvenReason.NO_CORROBORATED_FRESH_READING,
        )
    return AmbientDoctrineEvidence(
        verdict=AmbientEvidenceVerdict.OBSERVED,
        not_proven_reason=None,
        configured_enabled=configured_enabled,
        effective_throughout=True,
        ever_retired=False,
        recovery_episodes=episodes,
        retained_development_snapshot_count=development_count,
        fresh_retained_development_snapshot_count=fresh_count,
        retained_development_snapshot_fraction=fresh_count / development_count,
    )
