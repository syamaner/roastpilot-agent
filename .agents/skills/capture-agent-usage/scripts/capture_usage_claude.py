"""Fail-closed parser for the frozen Claude stream-json event grammar."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any, BinaryIO

from capture_usage_models import (
    BoundedStreamError,
    ClaudeModelUsage,
    EstimateBasis,
    ParsedUsage,
    bounded_jsonl_lines,
)

CLAUDE_EVENT_TYPES = frozenset({"system", "user", "assistant", "rate_limit_event", "result"})
"""Opaque event types observed unchanged in sanitized 2.1.228 and 2.1.231 fixtures."""
CLAUDE_SYSTEM_SUBTYPES = frozenset({"hook_started", "hook_response", "init"})
"""System subtypes observed unchanged in sanitized 2.1.228 and 2.1.231 fixtures."""
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
"""Exact terminal usage keys observed unchanged in Claude Code 2.1.228 and 2.1.231."""
CLAUDE_SUCCESS_SUBTYPE = "success"
CLAUDE_FAILURE_SUBTYPES = frozenset(
    {
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_during_execution",
    }
)
"""Closed failure subtypes observed unchanged in Claude Code 2.1.228 and 2.1.231."""
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
"""Exact model usage keys observed unchanged in Claude Code 2.1.228 and 2.1.231."""
CLAUDE_PERMISSION_MODES = frozenset({"default", "plan"})
"""Permission vocabularies observed in sanitized Claude init events."""


class ClaudeUsageParseError(ValueError):
    """Raised when a Claude stream cannot prove complete whole-tree usage."""


class ClaudeAuthorityError(ClaudeUsageParseError):
    """Raised when a Claude launch cannot prove its fixed authority boundary."""


class ClaudeUsageMissingTerminalError(ClaudeUsageParseError):
    """Raised only when a complete Claude stream lacks a result usage event."""


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

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Build one JSON object only when every key is unique."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ClaudeUsageParseError("Claude event contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        event = json.loads(line, object_pairs_hook=reject_duplicate_keys)
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


def _validate_init_authority(event: Mapping[str, Any], require_launch_authority: bool) -> None:
    """Validate the observed init authority fields without retaining their contents."""
    tools = event.get("tools")
    mcp_servers = event.get("mcp_servers")
    permission_mode = event.get("permissionMode")
    if (
        not isinstance(tools, list)
        or not isinstance(mcp_servers, list)
        or not isinstance(permission_mode, str)
        or permission_mode not in CLAUDE_PERMISSION_MODES
    ):
        raise ClaudeAuthorityError("Claude init authority is malformed")
    if require_launch_authority and (tools != [] or mcp_servers != [] or permission_mode != "plan"):
        raise ClaudeAuthorityError("Claude init authority is not attested")


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


def _finite_sum(values: Iterable[float], field: str) -> float:
    """Return a deterministic finite sum of already-validated model estimates."""
    try:
        total = math.fsum(values)
    except OverflowError as exc:
        raise ClaudeUsageParseError(f"malformed Claude usage field: {field}") from exc
    if not math.isfinite(total) or total < 0:
        raise ClaudeUsageParseError(f"malformed Claude usage field: {field}")
    return total


def _terminal_usage(event: Mapping[str, Any]) -> ParsedUsage:
    """Validate top-level usage and normalize totals from whole-tree model usage."""
    subtype = event.get("subtype")
    is_error = event.get("is_error")
    if not (
        (subtype == CLAUDE_SUCCESS_SUBTYPE and is_error is False)
        or (subtype in CLAUDE_FAILURE_SUBTYPES and is_error is True)
    ):
        raise ClaudeUsageParseError("Claude terminal result status is invalid")
    usage = event.get("usage")
    if not isinstance(usage, dict):
        raise ClaudeUsageParseError("Claude result event is missing usage")
    if set(usage) != CLAUDE_RESULT_USAGE_KEYS:
        raise ClaudeUsageParseError("malformed Claude terminal usage schema")
    for field in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ):
        _non_negative_integer(usage[field], field)
    _non_negative_number(event.get("total_cost_usd"), "total_cost_usd")
    model_usage = _model_usage(event.get("modelUsage"))
    model_costs = tuple(item.estimated_usd for item in model_usage)
    if any(cost is None for cost in model_costs):
        raise ClaudeUsageParseError("malformed Claude modelUsage entry")
    return ParsedUsage(
        input_tokens=sum(item.input_tokens for item in model_usage),
        cached_input_tokens=sum(item.cached_input_tokens for item in model_usage),
        cache_creation_input_tokens=sum(item.cache_creation_input_tokens for item in model_usage),
        output_tokens=sum(item.output_tokens for item in model_usage),
        claude_model_usage=model_usage,
        claude_terminal_success=is_error is False,
        estimated_usd=_finite_sum(
            (cost for cost in model_costs if cost is not None), "modelUsage costUSD sum"
        ),
        estimate_basis=EstimateBasis.CLIENT_SIDE_ESTIMATE,
    )


def parse_claude_stream(stream: BinaryIO, *, require_launch_authority: bool) -> ParsedUsage:
    """Parse a complete Claude stream using only the terminal result-level totals.

    Args:
        stream: Binary JSONL stdout from the fixed Claude harness command.
        require_launch_authority: Whether the init event must prove the fixed
            no-tools, no-MCP, plan-permission launch boundary.

    Returns:
        Normalized aggregate and per-model whole-tree usage.

    Raises:
        ClaudeUsageParseError: If the event grammar, init authority, result usage,
            or model totals are invalid.
    """
    parsed: ParsedUsage | None = None
    saw_init = False
    saw_pre_init_activity = False
    try:
        for line in bounded_jsonl_lines(stream):
            if not line.strip():
                raise ClaudeUsageParseError("blank Claude JSONL event")
            event = _event_from_line(line)
            if event["type"] == "system" and event.get("subtype") == "init":
                if saw_init:
                    raise ClaudeAuthorityError("Claude init authority is duplicated")
                if require_launch_authority and saw_pre_init_activity:
                    raise ClaudeAuthorityError("Claude init authority is not attested")
                _validate_init_authority(event, require_launch_authority)
                saw_init = True
                continue
            if event["type"] != "result":
                if not saw_init:
                    saw_pre_init_activity = True
                continue
            if parsed is not None:
                raise ClaudeUsageParseError("multiple Claude terminal result events")
            if require_launch_authority and not saw_init:
                raise ClaudeAuthorityError("Claude init authority is not attested")
            parsed = _terminal_usage(event)
    except BoundedStreamError as exc:
        raise ClaudeUsageParseError(str(exc)) from exc
    if parsed is None:
        raise ClaudeUsageMissingTerminalError("Claude stream has no terminal result usage event")
    return parsed
