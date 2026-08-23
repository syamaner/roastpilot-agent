"""Shared fail-closed resolution of persisted roast landmark provenance.

The helpers in this module use only the Python standard library so offline
research and fixture-export scripts can resolve the same MCP first-crack onset
without importing the application package.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast


def first_crack_event_source(payload_json: object) -> str | None:
    """Return a first-crack event's string provenance, if its payload is valid.

    Args:
        payload_json: The persisted JSON payload value.

    Returns:
        The exact ``source`` string, or ``None`` for malformed or incomplete
        payloads.
    """
    if not isinstance(payload_json, str):
        return None
    try:
        payload: Any = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload_map = cast(dict[str, object], payload)
    source = payload_map.get("source")
    return source if isinstance(source, str) else None


def is_mcp_first_crack_source(source: object) -> bool:
    """Whether a first-crack event has the exact accepted MCP provenance.

    Args:
        source: A decoded provenance value.

    Returns:
        ``True`` only for the exact lower-case ``"mcp"`` value.
    """
    return source == "mcp"


def parse_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 instant and normalize it to UTC.

    Naive timestamps are treated as UTC. Type, syntax, and normalization errors
    fail closed as ``None``.

    Args:
        value: Candidate ISO-8601 timestamp.

    Returns:
        The UTC-normalized instant, or ``None`` when unusable.
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


def first_crack_onset_utc(raw_states: Iterable[object]) -> tuple[str | None, int]:
    """Choose the earliest distinct first-crack status onset from raw states.

    Valid JSON objects must contain ``first_crack_status.detected_at_utc`` as a
    nonempty string. Equal parsed instants collapse even when rendered with
    different offsets; distinct valid instants choose the earliest. Unparseable
    strings are retained only for legacy exporter fallback diagnostics.

    Args:
        raw_states: Raw JSON state values in their deterministic persistence order.

    Returns:
        The selected raw onset spelling and count of distinct valid instants plus
        distinct unparseable strings. If every candidate is unparseable, returns
        the first such string so callers can retain established fallback behavior.
    """
    parsed_by_instant: dict[datetime, str] = {}
    unparseable_seen: set[str] = set()
    unparseable_order: list[str] = []
    for raw_state in raw_states:
        if not isinstance(raw_state, str):
            continue
        try:
            document: Any = json.loads(raw_state)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        document_map = cast(dict[str, object], document)
        status = document_map.get("first_crack_status")
        if not isinstance(status, dict):
            continue
        status_map = cast(dict[str, object], status)
        detected_at = status_map.get("detected_at_utc")
        if not isinstance(detected_at, str) or not detected_at:
            continue
        parsed = parse_utc(detected_at)
        if parsed is None:
            if detected_at not in unparseable_seen:
                unparseable_seen.add(detected_at)
                unparseable_order.append(detected_at)
            continue
        if parsed not in parsed_by_instant:
            parsed_by_instant[parsed] = detected_at
    count = len(parsed_by_instant) + len(unparseable_order)
    if parsed_by_instant:
        earliest = min(parsed_by_instant)
        return parsed_by_instant[earliest], count
    if unparseable_order:
        return unparseable_order[0], count
    return None, 0


def utc_to_run_seconds(
    target_iso: object, clock_anchors: Iterable[tuple[object, object]]
) -> float | None:
    """Map a UTC instant to a run clock using the nearest valid anchor.

    Iteration order is significant: an equal-distance tie retains the first
    usable anchor. Negative and non-finite mapped clocks fail closed.

    Args:
        target_iso: Candidate UTC instant to map.
        clock_anchors: ``(recorded_at_utc, run_seconds)`` pairs in stable order.

    Returns:
        Mapped run seconds, or ``None`` when no safe mapping exists.
    """
    target = parse_utc(target_iso)
    if target is None:
        return None
    nearest_seconds: float | None = None
    nearest_delta: float | None = None
    nearest_distance: float | None = None
    for recorded_at, run_seconds in clock_anchors:
        recorded = parse_utc(recorded_at)
        if recorded is None:
            continue
        if isinstance(run_seconds, bool) or not isinstance(run_seconds, (float, int, str)):
            continue
        try:
            seconds = float(run_seconds)
            delta = (target - recorded).total_seconds()
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
    if not math.isfinite(mapped) or mapped < 0.0:
        return None
    return mapped
