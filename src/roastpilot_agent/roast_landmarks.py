"""Fail-closed helpers for persisted roast landmark provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime

from roastpilot_agent.models import RoastEventSource


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
        if not math.isfinite(seconds) or not math.isfinite(delta):
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
    """Linearly interpolate a finite value in a non-decreasing sample series.

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
    previous: tuple[float, float] | None = None
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
        if previous is None:
            if target < x:
                return None
            if target == x:
                return y
            previous = (x, y)
            continue
        prev_x, prev_y = previous
        if x < prev_x:
            return None
        if target == x:
            return y
        if target < x:
            if x == prev_x:
                return None
            return prev_y + ((target - prev_x) / (x - prev_x)) * (y - prev_y)
        previous = (x, y)
    return None
