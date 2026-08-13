"""Fail-closed parser for the frozen Claude stream-json event grammar."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, BinaryIO

from capture_usage_models import (
    BoundedStreamError,
    ClaudeModelUsage,
    EstimateBasis,
    ParsedUsage,
    bounded_jsonl_lines,
)

CLAUDE_EVENT_TYPES = frozenset({"system", "user", "assistant", "rate_limit_event", "result"})
"""Opaque event types observed in the sanitized Claude Code 2.1.228 fixture."""
CLAUDE_SYSTEM_SUBTYPES = frozenset({"hook_started", "hook_response", "init"})
"""System subtypes observed in the sanitized Claude Code 2.1.228 fixture."""
CLAUDE_RESULT_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "cache_creation",
        "inference_geo",
        "iterations",
        "output_tokens_details",
        "server_tool_use",
        "service_tier",
        "speed",
    }
)
"""Exact terminal usage keys observed in Claude Code 2.1.228."""
CLAUDE_MODEL_USAGE_KEYS = frozenset(
    {
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "webSearchRequests",
        "costUSD",
        "contextWindow",
        "maxOutputTokens",
        "canonicalModel",
        "provider",
    }
)
"""Exact per-model usage keys observed in Claude Code 2.1.228."""


class ClaudeUsageParseError(ValueError):
    """Raised when a Claude stream cannot prove complete whole-tree usage."""


def _non_negative_integer(value: object, field: str) -> int:
    """Validate one token field without accepting booleans or partial values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaudeUsageParseError(f"malformed Claude usage field: {field}")
    return value


def _non_negative_number(value: object, field: str) -> float:
    """Validate a client-side estimate while rejecting booleans and negatives."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ClaudeUsageParseError(f"malformed Claude usage field: {field}")
    return float(value)


def _event_from_line(line: str) -> Mapping[str, Any]:
    """Decode one JSONL object and enforce the fixture's top-level grammar."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ClaudeUsageParseError("malformed Claude JSONL event") from exc
    if not isinstance(event, dict):
        raise ClaudeUsageParseError("malformed Claude event object")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise ClaudeUsageParseError("Claude event is missing a string type discriminator")
    if event_type not in CLAUDE_EVENT_TYPES:
        raise ClaudeUsageParseError("unknown Claude event type")
    if event_type == "system":
        subtype = event.get("subtype")
        if subtype not in CLAUDE_SYSTEM_SUBTYPES:
            raise ClaudeUsageParseError("unknown Claude system subtype")
    return event


def _model_usage(model_usage: object) -> tuple[ClaudeModelUsage, ...]:
    """Normalize terminal whole-tree per-model usage without message summation."""
    if not isinstance(model_usage, dict) or not model_usage:
        raise ClaudeUsageParseError("Claude terminal result is missing modelUsage")
    parsed: list[ClaudeModelUsage] = []
    for model, usage in model_usage.items():
        if not isinstance(model, str) or not isinstance(usage, dict):
            raise ClaudeUsageParseError("malformed Claude modelUsage entry")
        if set(usage) != CLAUDE_MODEL_USAGE_KEYS:
            raise ClaudeUsageParseError("malformed Claude modelUsage schema")
        parsed.append(
            ClaudeModelUsage(
                model=model,
                input_tokens=_non_negative_integer(usage["inputTokens"], "inputTokens"),
                cached_input_tokens=_non_negative_integer(
                    usage["cacheReadInputTokens"], "cacheReadInputTokens"
                ),
                cache_creation_input_tokens=_non_negative_integer(
                    usage["cacheCreationInputTokens"], "cacheCreationInputTokens"
                ),
                output_tokens=_non_negative_integer(usage["outputTokens"], "outputTokens"),
                estimated_usd=_non_negative_number(usage["costUSD"], "costUSD"),
            )
        )
    return tuple(parsed)


def _terminal_usage(event: Mapping[str, Any]) -> ParsedUsage:
    """Extract the sole result-level whole-tree total from a terminal event."""
    usage = event.get("usage")
    if not isinstance(usage, dict):
        raise ClaudeUsageParseError("Claude result event is missing usage")
    if set(usage) != CLAUDE_RESULT_USAGE_KEYS:
        raise ClaudeUsageParseError("malformed Claude terminal usage schema")
    total_cost = event.get("total_cost_usd")
    return ParsedUsage(
        input_tokens=_non_negative_integer(usage["input_tokens"], "input_tokens"),
        cached_input_tokens=_non_negative_integer(
            usage["cache_read_input_tokens"], "cache_read_input_tokens"
        ),
        cache_creation_input_tokens=_non_negative_integer(
            usage["cache_creation_input_tokens"], "cache_creation_input_tokens"
        ),
        output_tokens=_non_negative_integer(usage["output_tokens"], "output_tokens"),
        claude_model_usage=_model_usage(event.get("modelUsage")),
        estimated_usd=_non_negative_number(total_cost, "total_cost_usd"),
        estimate_basis=EstimateBasis.CLIENT_SIDE_ESTIMATE,
    )


def parse_claude_stream(stream: BinaryIO) -> ParsedUsage:
    """Parse a complete Claude stream using only the terminal result-level totals.

    Args:
        stream: Binary JSONL stdout from the fixed Claude harness command.

    Returns:
        Normalized aggregate and per-model whole-tree usage.

    Raises:
        ClaudeUsageParseError: If the event grammar, result usage, or model totals are invalid.
    """
    parsed: ParsedUsage | None = None
    try:
        for line in bounded_jsonl_lines(stream):
            if not line.strip():
                raise ClaudeUsageParseError("blank Claude JSONL event")
            event = _event_from_line(line)
            if event["type"] != "result":
                continue
            if parsed is not None:
                raise ClaudeUsageParseError("multiple Claude terminal result events")
            parsed = _terminal_usage(event)
    except BoundedStreamError as exc:
        raise ClaudeUsageParseError(str(exc)) from exc
    if parsed is None:
        raise ClaudeUsageParseError("Claude stream has no terminal result usage event")
    return parsed
