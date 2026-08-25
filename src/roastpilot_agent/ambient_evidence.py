"""Conservative, read-only ambient-doctrine evidence derived from retained state.

The evidence in this module deliberately describes retained DEVELOPMENT
snapshots, not controller ticks or an advisor's reasoning.  It is a corpus
claim only: uncertain historical data is represented as ``not_proven``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, model_validator

from roastpilot_agent.models import RoastPhase

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


@dataclass(frozen=True)
class _ParsedRecovery:
    """Private recovery parse result including restart-generation evidence."""

    episode: DoctrineRecoveryEpisode
    restart_recorded_at: datetime | None


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
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow an untrusted JSON value to a mapping with string keys."""
    if not isinstance(value, dict):
        return None
    raw_mapping = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in raw_mapping):
        return None
    return cast("Mapping[str, object]", raw_mapping)


def _aware_timestamp(value: object) -> datetime | None:
    """Parse one retained UTC timestamp only when it has an explicit offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


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


def _parsed_recovery_from_row(row: Mapping[str, object]) -> _ParsedRecovery:
    """Parse one recovery row and whether its root rule attests a restart."""
    event_id = row.get("id")
    if isinstance(event_id, int) and not isinstance(event_id, bool):
        valid_event_id = True
        safe_id = event_id
    else:
        valid_event_id = False
        safe_id = 0
    raw_payload = row.get("payload_json")
    if not isinstance(raw_payload, str):
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    try:
        root = json.loads(raw_payload)
    except (TypeError, ValueError, RecursionError):
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    root_mapping = _object_mapping(root)
    if root_mapping is None:
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    payload = _object_mapping(root_mapping.get(RECOVERY_PAYLOAD_KEY))
    if payload is None or set(payload) != {
        "configured_enabled",
        "effective_enabled",
        "state",
    }:
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    configured = payload["configured_enabled"]
    effective = payload["effective_enabled"]
    state = payload["state"]
    if (
        not isinstance(configured, bool)
        or not isinstance(effective, bool)
        or not isinstance(state, str)
    ):
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    try:
        parsed_state = DoctrineRecoveryState(state)
    except ValueError:
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    if parsed_state is DoctrineRecoveryState.UNKNOWN:
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    if parsed_state is DoctrineRecoveryState.PRESERVED and configured != effective:
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    if parsed_state is DoctrineRecoveryState.RETIRED and (not configured or effective):
        return _ParsedRecovery(
            DoctrineRecoveryEpisode(event_id=safe_id, state=DoctrineRecoveryState.UNKNOWN),
            None,
        )
    return _ParsedRecovery(
        DoctrineRecoveryEpisode(
            event_id=safe_id,
            configured_enabled=configured,
            effective_enabled=effective,
            state=parsed_state,
        ),
        _aware_timestamp(row.get("recorded_at_utc"))
        if (
            valid_event_id
            and root_mapping.get("rule") == "restart_recovery"
            and root_mapping.get("verdict") == "recovery"
        )
        else None,
    )


def _episode_from_row(  # pyright: ignore[reportUnusedFunction] - grammar test compatibility
    row: Mapping[str, object],
) -> DoctrineRecoveryEpisode:
    """Parse one recovery row; malformed or legacy payloads remain explicit unknowns."""
    return _parsed_recovery_from_row(row).episode


def _normalize_recovery_configuration(
    parsed: _ParsedRecovery,
    configured_enabled: bool,
) -> _ParsedRecovery:
    """Fail closed when a recovery row contradicts the frozen doctrine setting."""
    episode = parsed.episode
    if episode.configured_enabled is None or episode.configured_enabled == configured_enabled:
        return parsed
    return _ParsedRecovery(
        DoctrineRecoveryEpisode(event_id=episode.event_id, state=DoctrineRecoveryState.UNKNOWN),
        None,
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
    if not isinstance(raw_status.get("ambient_running"), bool):
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
    parsed_recoveries = tuple(
        _normalize_recovery_configuration(_parsed_recovery_from_row(row), configured_enabled)
        for row in recovery_rows
    )
    episodes = tuple(parsed.episode for parsed in parsed_recoveries)
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
    previous_live_timestamp: datetime | None = None
    previous_snapshot_recorded_at: object = None
    token: float | None = None
    corroborated_at: datetime | None = None
    unusable_clock = False
    used_restart_event_ids: set[int] = set()
    for row in snapshot_rows:
        raw_tick = row.get("tick")
        raw_recorded_at = row.get("recorded_at_utc")
        if not isinstance(raw_tick, int) or isinstance(raw_tick, bool):
            unusable_clock = True
            previous_snapshot_recorded_at = raw_recorded_at
            continue
        reset = previous_tick is None or raw_tick <= previous_tick
        if previous_tick is not None and reset:
            previous_boundary_at = _aware_timestamp(previous_snapshot_recorded_at)
            first_post_reset_at = _aware_timestamp(raw_recorded_at)
            matching_restarts = [
                parsed
                for parsed in parsed_recoveries
                if parsed.episode.event_id not in used_restart_event_ids
                and parsed.restart_recorded_at is not None
                and previous_boundary_at is not None
                and first_post_reset_at is not None
                and previous_boundary_at < parsed.restart_recorded_at <= first_post_reset_at
            ]
            if len(matching_restarts) != 1:
                unusable_clock = True
            else:
                used_restart_event_ids.add(matching_restarts[0].episode.event_id)
        previous_tick = raw_tick
        previous_snapshot_recorded_at = raw_recorded_at
        if reset:
            token = None
            corroborated_at = None
            previous_timestamp = None
            previous_live_timestamp = None

        observed_at = _aware_timestamp(raw_recorded_at)
        if observed_at is None:
            unusable_clock = True
            token = None
            corroborated_at = None
            previous_live_timestamp = None
            continue
        if previous_timestamp is not None and observed_at < previous_timestamp:
            unusable_clock = True
            token = None
            corroborated_at = None
            previous_live_timestamp = None
            previous_timestamp = observed_at
            continue
        previous_timestamp = observed_at

        raw_phase = row.get("agent_phase")
        try:
            is_development = RoastPhase(raw_phase) is RoastPhase.DEVELOPMENT
        except (TypeError, ValueError):
            unusable_clock = True
            is_development = False
        if is_development:
            development_count += 1

        status, malformed = _snapshot_status(row)
        if malformed:
            unusable_clock = True
            token = None
            corroborated_at = None
            previous_live_timestamp = None
            continue
        if status is None or not _retained_ambient_is_live(status):
            token = None
            corroborated_at = None
            previous_live_timestamp = None
            continue
        triad = _retained_live_ambient(status)
        current_token = _retained_ambient_token(status)
        if current_token is None or not all(_is_finite_number(value) for value in triad):
            token = None
            corroborated_at = None
            previous_live_timestamp = None
            continue
        # The MCP child's epoch is fixed within one agent tick generation.
        # Any future in-run respawn needs an explicit evidence reset/breadcrumb
        # plus dedicated tests before its tokens may corroborate this sequence.
        if token is None:
            token = current_token
            previous_live_timestamp = observed_at
            continue
        if current_token != token:
            token = current_token
            corroborated_at = previous_live_timestamp
        previous_live_timestamp = observed_at
        if is_development and corroborated_at is not None:
            age_seconds = (observed_at - corroborated_at).total_seconds()
            if (
                math.isfinite(age_seconds)
                and 0.0
                <= age_seconds
                <= frozen_controller.ambient_fan_doctrine.max_reading_age_seconds
            ):
                fresh_count += 1

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
            development_count=0,
            fresh_count=0,
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
