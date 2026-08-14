"""Fail-closed parser for the frozen Claude stream-json event grammar."""

from __future__ import annotations

import codecs
import json
import math
from collections.abc import Iterable, Mapping
from contextlib import suppress
from enum import Enum
from typing import Any, BinaryIO, Protocol

from capture_usage_codex import (
    READ_CHUNK_BYTES,
    CodexUsageParseError,
    _JsonRootScanner,  # pyright: ignore[reportPrivateUsage]
)
from capture_usage_models import (
    MAX_EVENT_BYTES,
    BoundedStreamError,
    ClaudeModelUsage,
    EstimateBasis,
    ParsedUsage,
    SafeIdentifier,
    bounded_jsonl_lines,
)
from pydantic import TypeAdapter

NATIVE_MAX_OPAQUE_EVENT_BYTES = 4 * 1024 * 1024
NATIVE_MAX_EVENT_COUNT = 500_000
NATIVE_MAX_STREAM_BYTES = 256 * 1024 * 1024

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
_SAFE_IDENTIFIER_ADAPTER = TypeAdapter(SafeIdentifier)


class ClaudeUsageParseError(ValueError):
    """Raised when a Claude stream cannot prove complete whole-tree usage."""


class ClaudeAuthorityError(ClaudeUsageParseError):
    """Raised when a Claude launch cannot prove its fixed authority boundary."""


class ClaudeUsageMissingTerminalError(ClaudeUsageParseError):
    """Raised only when a complete Claude stream lacks a result usage event."""


class ClaudeAuthorityMode(Enum):
    """Closed init-attestation modes for the Claude stream parser."""

    MEASUREMENT = "MEASUREMENT"
    NATIVE = "NATIVE"


class _ReadableBinaryStream(Protocol):
    """The bounded-read interface accepted from native Claude stdout."""

    def read(self, size: int, /) -> bytes:
        """Read at most ``size`` bytes."""


class _ClaudeRootScanner(_JsonRootScanner):
    """Extend the shared structural scanner with one bounded root subtype value."""

    def __init__(self) -> None:
        """Initialize root-type scanning and exact subtype duplicate detection."""
        super().__init__()
        self.root_subtype: str | None = None
        self._root_subtype_count = 0

    @property
    def root_type(self) -> str | None:
        """Return the one bounded root type captured by the structural scanner."""
        value = getattr(self, "_root_type_value", None)
        return value if isinstance(value, str) else None

    def _emit(self, kind: str, value: str | None, truncated: bool) -> None:
        """Capture a root string subtype before applying the shared JSON state update."""
        stack = getattr(self, "_stack", ())
        if len(stack) == 1:
            _container, state, key = stack[-1]
            if state in {"value", "value_or_end"} and key == "subtype":
                self._root_subtype_count += 1
                if self._root_subtype_count != 1:
                    raise CodexUsageParseError("Claude event contains duplicate JSON keys")
                if kind != "string" or truncated:
                    raise CodexUsageParseError("malformed native Claude JSON event")
                self.root_subtype = value
        super()._emit(kind, value, truncated)


def _native_read_chunk(stream: _ReadableBinaryStream) -> bytes:
    """Read promptly from a pipe without requesting an opaque-event-sized allocation."""
    read1 = getattr(stream, "read1", None)
    chunk = read1(READ_CHUNK_BYTES) if callable(read1) else stream.read(READ_CHUNK_BYTES)
    if not isinstance(chunk, bytes) or len(chunk) > READ_CHUNK_BYTES:
        raise ClaudeUsageParseError("native usage stream read exceeds size limit")
    return chunk


def _native_stream_events(
    stream: _ReadableBinaryStream,
) -> Iterable[tuple[str, bytes | None]]:
    """Yield retained events and discard structurally valid oversized opaque events."""
    event_count = 0
    stream_bytes = 0
    event_bytes = 0
    retained = bytearray()
    scanner = _ClaudeRootScanner()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def complete_event() -> tuple[str, bytes | None]:
        """Finish one event and return content only when it fits the retained bound."""
        nonlocal event_count, event_bytes, retained, scanner, decoder
        event_count += 1
        if event_count > NATIVE_MAX_EVENT_COUNT:
            raise ClaudeUsageParseError("native usage stream exceeds event count limit")
        try:
            scanner.finish()
        except CodexUsageParseError:
            raise ClaudeUsageParseError("malformed native Claude JSON event") from None
        event_type = scanner.root_type
        if not isinstance(event_type, str) or event_type not in CLAUDE_EVENT_TYPES:
            raise ClaudeUsageParseError("unknown native Claude event type")
        if event_bytes > MAX_EVENT_BYTES:
            opaque = event_type in {"user", "assistant", "rate_limit_event"} or (
                event_type == "system" and scanner.root_subtype in {"hook_started", "hook_response"}
            )
            if not opaque:
                raise ClaudeUsageParseError("native retained event exceeds size limit")
            result: tuple[str, bytes | None] = (event_type, None)
        else:
            result = (event_type, bytes(retained))
        event_bytes = 0
        retained = bytearray()
        scanner = _ClaudeRootScanner()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        return result

    def consume_segment(segment: bytes, has_newline: bool) -> tuple[str, bytes | None] | None:
        """Account, incrementally validate, and conditionally retain one segment."""
        nonlocal event_bytes, stream_bytes
        event_bytes += len(segment)
        stream_bytes += len(segment)
        if event_bytes > NATIVE_MAX_OPAQUE_EVENT_BYTES:
            raise ClaudeUsageParseError("native usage stream event exceeds size limit")
        if stream_bytes > NATIVE_MAX_STREAM_BYTES:
            raise ClaudeUsageParseError("native usage stream exceeds total byte limit")
        remaining = MAX_EVENT_BYTES - len(retained)
        if remaining > 0:
            retained.extend(segment[:remaining])
        invalid_utf8 = False
        text = ""
        try:
            text = decoder.decode(segment, final=False)
        except UnicodeDecodeError:
            invalid_utf8 = True
        if invalid_utf8:
            raise ClaudeUsageParseError("native usage stream contains invalid UTF-8") from None
        try:
            if has_newline:
                if not text.endswith("\n") or decoder.getstate()[0]:
                    raise ClaudeUsageParseError("native usage stream contains invalid UTF-8")
                scanner.consume(text[:-1])
                return complete_event()
            scanner.consume(text)
        except CodexUsageParseError:
            raise ClaudeUsageParseError("malformed native Claude JSON event") from None
        return None

    while True:
        chunk = _native_read_chunk(stream)
        if chunk == b"":
            break
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                consume_segment(chunk[offset:], False)
                break
            completed = consume_segment(chunk[offset : newline + 1], True)
            assert completed is not None
            yield completed
            offset = newline + 1
    invalid_utf8 = False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        invalid_utf8 = True
    if invalid_utf8:
        raise ClaudeUsageParseError("native usage stream contains invalid UTF-8") from None
    if event_bytes:
        raise ClaudeUsageParseError("native usage stream contains a partial event")


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

    malformed_json = False
    event: object = None
    try:
        event = json.loads(line, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError:
        malformed_json = True
    if malformed_json:
        raise ClaudeUsageParseError("malformed Claude JSONL event") from None
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


def _validate_init_authority(
    event: Mapping[str, Any], require_launch_authority: bool, authority_mode: ClaudeAuthorityMode
) -> str | None:
    """Validate the observed init authority fields without retaining their contents."""
    tools = event.get("tools")
    mcp_servers = event.get("mcp_servers")
    permission_mode = event.get("permissionMode")
    if (
        not isinstance(tools, list)
        or not isinstance(mcp_servers, list)
        or not isinstance(permission_mode, str)
        or (
            authority_mode is not ClaudeAuthorityMode.NATIVE
            and permission_mode not in CLAUDE_PERMISSION_MODES
        )
    ):
        raise ClaudeAuthorityError("Claude init authority is malformed")
    if authority_mode is ClaudeAuthorityMode.NATIVE:
        model = event.get("model")
        if (
            tools == []
            or mcp_servers != []
            or permission_mode != "auto"
            or not isinstance(model, str)
        ):
            raise ClaudeAuthorityError("Claude init authority is not attested")
        validated_model: str | None = None
        with suppress(ValueError):
            validated_model = _SAFE_IDENTIFIER_ADAPTER.validate_python(model)
        if validated_model is None:
            raise ClaudeAuthorityError("Claude init authority is malformed") from None
        return validated_model
    if require_launch_authority and (tools != [] or mcp_servers != [] or permission_mode != "plan"):
        raise ClaudeAuthorityError("Claude init authority is not attested")
    return None


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
    canonical_values = tuple(usage["canonicalModel"] for usage in event["modelUsage"].values())
    canonical_names = (
        tuple(value for value in canonical_values if isinstance(value, str))
        if all(isinstance(value, str) for value in canonical_values)
        else None
    )
    return ParsedUsage(
        input_tokens=sum(item.input_tokens for item in model_usage),
        cached_input_tokens=sum(item.cached_input_tokens for item in model_usage),
        cache_creation_input_tokens=sum(item.cache_creation_input_tokens for item in model_usage),
        output_tokens=sum(item.output_tokens for item in model_usage),
        claude_model_usage=model_usage,
        claude_model_canonical_names=canonical_names,
        claude_terminal_success=is_error is False,
        estimated_usd=_finite_sum(
            (cost for cost in model_costs if cost is not None), "modelUsage costUSD sum"
        ),
        estimate_basis=EstimateBasis.CLIENT_SIDE_ESTIMATE,
    )


def parse_claude_stream(
    stream: BinaryIO,
    *,
    require_launch_authority: bool,
    authority_mode: ClaudeAuthorityMode = ClaudeAuthorityMode.MEASUREMENT,
) -> ParsedUsage:
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
    init_model: str | None = None
    bounded_failure: str | None = None
    try:
        events: Iterable[tuple[str, bytes | None]]
        if authority_mode is ClaudeAuthorityMode.NATIVE:
            events = _native_stream_events(stream)
        else:
            events = (("", line.encode("utf-8")) for line in bounded_jsonl_lines(stream))
        for _opaque_type, raw_line in events:
            if raw_line is None:
                if not saw_init:
                    saw_pre_init_activity = True
                continue
            line = raw_line.decode("utf-8")
            if not line.strip():
                raise ClaudeUsageParseError("blank Claude JSONL event")
            event = _event_from_line(line)
            if event["type"] == "system" and event.get("subtype") == "init":
                if saw_init:
                    raise ClaudeAuthorityError("Claude init authority is duplicated")
                if (
                    require_launch_authority or authority_mode is ClaudeAuthorityMode.NATIVE
                ) and saw_pre_init_activity:
                    raise ClaudeAuthorityError("Claude init authority is not attested")
                init_model = _validate_init_authority(
                    event, require_launch_authority, authority_mode
                )
                saw_init = True
                continue
            if event["type"] != "result":
                if not saw_init:
                    saw_pre_init_activity = True
                continue
            if parsed is not None:
                raise ClaudeUsageParseError("multiple Claude terminal result events")
            if (
                require_launch_authority or authority_mode is ClaudeAuthorityMode.NATIVE
            ) and not saw_init:
                raise ClaudeAuthorityError("Claude init authority is not attested")
            parsed = _terminal_usage(event)
    except BoundedStreamError as exc:
        bounded_failure = str(exc)
    if bounded_failure is not None:
        raise ClaudeUsageParseError(bounded_failure) from None
    if parsed is None:
        raise ClaudeUsageMissingTerminalError("Claude stream has no terminal result usage event")
    if authority_mode is ClaudeAuthorityMode.NATIVE:
        if init_model is None or parsed.claude_model_usage is None:
            raise ClaudeAuthorityError("Claude init authority is not attested")
        if parsed.claude_model_canonical_names is None:
            raise ClaudeUsageParseError("malformed Claude modelUsage entry")
        if init_model not in {
            item.model for item in parsed.claude_model_usage
        } and init_model not in set(parsed.claude_model_canonical_names):
            raise ClaudeAuthorityError("Claude init authority is not attested")
        return parsed.model_copy(update={"claude_init_model": init_model})
    return parsed
