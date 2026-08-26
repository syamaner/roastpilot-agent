"""Fail-closed helpers for persisted roast landmark provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from roastpilot_agent.models import RoastEventSource, RoastPhase


class DropReadingSource(Enum):
    """The persisted evidence used to resolve a roast's drop reading."""

    DROP_EVENT_ANCHOR = "drop_event_anchor"
    LAST_DEVELOPMENT_ROW = "last_development_row"


@dataclass(frozen=True)
class DropReadingRow:
    """One telemetry row needed to derive a persisted drop reading."""

    agent_phase: str
    bean_temp_c: float | None
    development_percent: float | None
    recorded_at_utc: str | None


@dataclass(frozen=True)
class DropReading:
    """The achieved drop temperature and controller-frozen DTR."""

    bean_temp_c: float
    development_percent: float
    source: DropReadingSource


def select_drop_reading(
    rows: Sequence[DropReadingRow], drop_event_recorded_at_utc: str | None
) -> DropReading | None:
    """Resolve an authoritative drop temperature and DTR from persisted rows.

    Temperature is selected from the telemetry row nearest the executed
    ``drop_beans`` event, while DTR is the last finite controller-frozen value
    in insertion order. The values deliberately come from different rows:
    taking temperature from the frozen-DTR row can select a cooling tail, and
    taking DTR from the temperature row loses it when boundary telemetry was
    suppressed by the logging throttle.

    Args:
        rows: Telemetry rows in durable insertion order.
        drop_event_recorded_at_utc: Executed ``drop_beans`` event timestamp.

    Returns:
        A complete drop reading, or ``None`` when neither the event anchor nor
        the last-development-row fallback has both required values.
    """
    if not rows:
        return None
    drop_time = parse_utc(drop_event_recorded_at_utc)
    if drop_time is not None:
        frozen_dtr = rows[-1].development_percent
        for row in rows:
            dtr = row.development_percent
            if dtr is not None:
                frozen_dtr = dtr

        nearest_temp: float | None = None
        nearest_distance: float | None = None
        for row in rows:
            row_time = parse_utc(row.recorded_at_utc)
            temperature = row.bean_temp_c
            if row_time is None or temperature is None:
                continue
            distance = abs((row_time - drop_time).total_seconds())
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance
                nearest_temp = temperature
        if nearest_temp is not None and frozen_dtr is not None:
            return DropReading(
                bean_temp_c=nearest_temp,
                development_percent=frozen_dtr,
                source=DropReadingSource.DROP_EVENT_ANCHOR,
            )

    for row in reversed(rows):
        if RoastPhase(str(row.agent_phase)) is not RoastPhase.DEVELOPMENT:
            continue
        temperature = row.bean_temp_c
        dtr = row.development_percent
        if temperature is None or dtr is None:
            return None
        return DropReading(
            bean_temp_c=temperature,
            development_percent=dtr,
            source=DropReadingSource.LAST_DEVELOPMENT_ROW,
        )
    return None


def parse_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 instant and normalize it to UTC.

    Args:
        value: Candidate ISO-8601 value.

    Returns:
        A UTC-aware instant, or ``None`` when the value is unusable.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def is_mcp_first_crack_source(source: object) -> bool:
    """Whether an accepted first-crack source is exactly MCP provenance.

    Args:
        source: Persisted event source value.

    Returns:
        ``True`` only for :attr:`RoastEventSource.MCP`.
    """
    return source == RoastEventSource.MCP.value


def earliest_onset_utc(candidates: Iterable[object]) -> datetime | None:
    """Return the earliest parsed first-crack onset candidate.

    Args:
        candidates: Candidate ISO-8601 strings from a guarded SQL projection.

    Returns:
        The earliest normalized UTC instant, or ``None`` when none parse.
    """
    earliest: datetime | None = None
    for candidate in candidates:
        parsed = parse_utc(candidate)
        if parsed is not None and (earliest is None or parsed < earliest):
            earliest = parsed
    return earliest


def is_onset_within_event_window(onset: datetime, started_at: object, event_at: object) -> bool:
    """Whether an onset lies inclusively within its run/event bounds.

    Args:
        onset: Previously parsed onset instant.
        started_at: Persisted run start timestamp.
        event_at: Persisted accepted first-crack event timestamp.

    Returns:
        ``True`` only when every value is usable and the onset is in bounds.
    """
    if getattr(onset, "tzinfo", None) is None:
        return False
    try:
        onset_utc = onset.astimezone(UTC)
    except (OverflowError, ValueError):
        return False
    started_utc = parse_utc(started_at)
    event_utc = parse_utc(event_at)
    return (
        started_utc is not None and event_utc is not None and started_utc <= onset_utc <= event_utc
    )


def earliest_onset_within_event_window(
    candidates: Iterable[object], started_at: object, event_at: object
) -> datetime | None:
    """Return the earliest parsed onset within trusted event bounds.

    Args:
        candidates: Persisted candidate onset values.
        started_at: Persisted run start timestamp.
        event_at: Persisted accepted first-crack event timestamp.

    Returns:
        The earliest normalized in-window UTC instant, or ``None`` when none
        are usable.
    """
    earliest: datetime | None = None
    for candidate in candidates:
        onset = parse_utc(candidate)
        if (
            onset is not None
            and is_onset_within_event_window(onset, started_at, event_at)
            and (earliest is None or onset < earliest)
        ):
            earliest = onset
    return earliest


def utc_to_run_seconds(target: object, anchors: Iterable[tuple[object, object]]) -> float | None:
    """Map a UTC instant to the nearest finite, non-negative run-clock anchor.

    Args:
        target: Target wall-clock instant.
        anchors: Stable ``(recorded_at_utc, run_seconds)`` anchor pairs.

    Returns:
        Mapped run seconds, or ``None`` when no safe mapping exists.
    """
    target_utc = parse_utc(target)
    if target_utc is None:
        return None
    nearest_seconds: float | None = None
    nearest_delta: float | None = None
    nearest_distance: float | None = None
    for recorded_at, run_seconds in anchors:
        recorded_utc = parse_utc(recorded_at)
        if recorded_utc is None or isinstance(run_seconds, bool):
            continue
        if not isinstance(run_seconds, (float, int, str)):
            continue
        try:
            seconds = float(run_seconds)
            delta = (target_utc - recorded_utc).total_seconds()
        except (OverflowError, TypeError, ValueError):
            continue
        if not math.isfinite(seconds) or seconds < 0.0 or not math.isfinite(delta):
            continue
        distance = abs(delta)
        if nearest_distance is None or distance < nearest_distance:
            nearest_distance = distance
            nearest_delta = delta
            nearest_seconds = seconds
    if nearest_seconds is None or nearest_delta is None:
        return None
    mapped = nearest_seconds + nearest_delta
    return mapped if math.isfinite(mapped) and mapped >= 0.0 else None


def interpolate_at(t: object, samples: Iterable[tuple[object, object]]) -> float | None:
    """Linearly interpolate a finite value in a strictly increasing sample series.

    Args:
        t: Requested sample coordinate.
        samples: Ordered ``(coordinate, value)`` pairs.

    Returns:
        The exact or linearly interpolated value, or ``None`` for invalid,
        out-of-range, empty, or strictly decreasing input.
    """
    if isinstance(t, bool) or not isinstance(t, (float, int, str)):
        return None
    try:
        target = float(t)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target):
        return None
    points: list[tuple[float, float]] = []
    for raw_x, raw_y in samples:
        if isinstance(raw_x, bool) or isinstance(raw_y, bool):
            return None
        if not isinstance(raw_x, (float, int, str)) or not isinstance(raw_y, (float, int, str)):
            return None
        try:
            x, y = float(raw_x), float(raw_y)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        if points and x <= points[-1][0]:
            return None
        points.append((x, y))
    previous: tuple[float, float] | None = None
    for x, y in points:
        if previous is None:
            if target < x:
                return None
            if target == x:
                return y
            previous = (x, y)
            continue
        prev_x, prev_y = previous
        if target == x:
            return y
        if target < x:
            return prev_y + ((target - prev_x) / (x - prev_x)) * (y - prev_y)
        previous = (x, y)
    return None
