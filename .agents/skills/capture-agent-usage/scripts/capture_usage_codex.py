"""Fail-closed parser for the frozen Codex ``exec --json`` event grammar."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, BinaryIO

from capture_usage_models import BoundedStreamError, EstimateBasis, ParsedUsage, bounded_jsonl_lines

CODEX_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.failed",
        "turn.completed",
    }
)
"""Opaque event types observed in the sanitized Codex 0.147.0 fixture."""


class CodexUsageParseError(ValueError):
    """Raised when a Codex stream cannot prove a complete usage result."""


class CodexUsageMissingTerminalError(CodexUsageParseError):
    """Raised only when a complete Codex stream lacks terminal usage."""


def _non_negative_integer(value: object, field: str) -> int:
    """Validate one token field without accepting booleans or partial values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexUsageParseError(f"malformed Codex usage field: {field}")
    return value


def _event_from_line(line: str) -> Mapping[str, Any]:
    """Decode one JSONL object and reject non-object or missing-type events."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Build one JSON object only when every key is unique."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CodexUsageParseError("Codex event contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        event = json.loads(line, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CodexUsageParseError("malformed Codex JSONL event") from exc
    if not isinstance(event, dict):
        raise CodexUsageParseError("malformed Codex event object")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise CodexUsageParseError("Codex event is missing a string type discriminator")
    if event_type not in CODEX_EVENT_TYPES:
        raise CodexUsageParseError("unknown Codex event type")
    return event


def parse_codex_stream(stream: BinaryIO) -> ParsedUsage:
    """Parse a complete Codex JSONL stream using only terminal usage fields.

    Args:
        stream: Binary JSONL stdout from the fixed Codex harness command.

    Returns:
        Normalized token totals from the one required ``turn.completed`` event.

    Raises:
        CodexUsageParseError: If the event grammar, usage object, or terminal event is invalid.
    """
    parsed: ParsedUsage | None = None
    terminal_marker_seen = False
    try:
        for line in bounded_jsonl_lines(stream):
            if not line.strip():
                raise CodexUsageParseError("blank Codex JSONL event")
            event = _event_from_line(line)
            if event["type"] not in {"turn.failed", "turn.completed"}:
                continue
            if terminal_marker_seen:
                raise CodexUsageParseError("multiple Codex terminal usage events")
            terminal_marker_seen = True
            if event["type"] == "turn.failed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                raise CodexUsageParseError("Codex turn.completed event is missing usage")
            expected = {
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            }
            if set(usage) != expected:
                raise CodexUsageParseError("malformed Codex terminal usage schema")
            parsed = ParsedUsage(
                input_tokens=_non_negative_integer(usage["input_tokens"], "input_tokens"),
                cached_input_tokens=_non_negative_integer(
                    usage["cached_input_tokens"], "cached_input_tokens"
                ),
                cache_creation_input_tokens=_non_negative_integer(
                    usage["cache_write_input_tokens"], "cache_write_input_tokens"
                ),
                output_tokens=_non_negative_integer(usage["output_tokens"], "output_tokens"),
                reasoning_output_tokens=_non_negative_integer(
                    usage["reasoning_output_tokens"], "reasoning_output_tokens"
                ),
                estimate_basis=EstimateBasis.NOT_EXPOSED,
            )
    except BoundedStreamError as exc:
        raise CodexUsageParseError(str(exc)) from exc
    if parsed is None:
        raise CodexUsageMissingTerminalError(
            "Codex stream has no terminal turn.completed usage event"
        )
    return parsed
