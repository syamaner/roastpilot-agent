"""Fail-closed, bounded parser for the frozen Codex ``exec --json`` grammar."""

from __future__ import annotations

import codecs
import json
from collections.abc import Mapping
from typing import Any, Protocol

from capture_usage_models import (
    MAX_EVENT_BYTES,
    MAX_EVENT_COUNT,
    MAX_STREAM_BYTES,
    EstimateBasis,
    ParsedUsage,
)

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
OPAQUE_ITEM_TYPES = frozenset({"item.started", "item.updated", "item.completed"})
"""The only oversized root event discriminators that may be streamed and discarded."""
MAX_CODEX_OPAQUE_EVENT_BYTES = 8_388_608
"""Maximum bytes for one streamed-and-discarded opaque item event."""
MAX_CODEX_OPAQUE_TOTAL_BYTES = 67_108_864
"""Maximum aggregate bytes for streamed-and-discarded opaque item events."""
READ_CHUNK_BYTES = 65_536
"""Largest read request and retained-line carry buffer bound."""
_STRING_CAPTURE_LIMIT = 64
MAX_JSON_NESTING_DEPTH = 64
"""Maximum shallow JSON container depth accepted by the frozen Codex grammar."""


class CodexUsageParseError(ValueError):
    """Raised when a Codex stream cannot prove a complete usage result."""


class CodexUsageMissingTerminalError(CodexUsageParseError):
    """Raised only when a complete Codex stream lacks terminal usage."""


class _ReadableBinaryStream(Protocol):
    """The minimal bounded-read interface accepted from a fixed harness stdout pipe."""

    def read(self, size: int, /) -> bytes:
        """Read at most ``size`` binary bytes from the stream."""


class _JsonRootScanner:
    """Incrementally validate one JSON root object without retaining its payload."""

    def __init__(self) -> None:
        """Initialize a root-object scanner with bounded string token capture."""
        self._stack: list[tuple[str, str, str | None]] = []
        self._root_started = False
        self._root_finished = False
        self._string: list[str] = []
        self._string_truncated = False
        self._string_state = "none"
        self._unicode_digits: list[str] = []
        self._primitive_kind: str | None = None
        self._primitive_state = ""
        self._root_type_count = 0
        self._root_type_value: str | None = None

    @property
    def is_opaque_item(self) -> bool:
        """Return whether this complete root has exactly one allowed item type."""
        return self._root_type_count == 1 and self._root_type_value in OPAQUE_ITEM_TYPES

    def consume(self, text: str) -> None:
        """Consume decoded JSON characters without preserving opaque payload values."""
        index = 0
        while index < len(text):
            character = text[index]
            if self._string_state != "none":
                self._consume_string(character)
                index += 1
                continue
            if self._primitive_kind is not None:
                if self._is_delimiter(character):
                    self._finish_primitive()
                    continue
                self._consume_primitive(character)
                index += 1
                continue
            if character in " \t\r":
                index += 1
                continue
            if character == '"':
                self._string_state = "string"
                self._string = []
                self._string_truncated = False
            elif character in "{}[],:":
                self._consume_punctuation(character)
            elif character in "-0123456789":
                self._primitive_kind = "number"
                self._primitive_state = (
                    "minus" if character == "-" else ("zero" if character == "0" else "integer")
                )
            elif character in "tfn":
                self._primitive_kind = "literal"
                self._primitive_state = {"t": "true:1", "f": "false:1", "n": "null:1"}[character]
            else:
                self._reject()
            index += 1

    def finish(self) -> None:
        """Require a complete, single root object at the JSONL newline boundary."""
        if self._string_state != "none":
            self._reject()
        if self._primitive_kind is not None:
            self._finish_primitive()
        if not self._root_started or not self._root_finished or self._stack:
            self._reject()

    def _reject(self) -> None:
        """Raise a content-free structural error for every scanner failure class."""
        raise CodexUsageParseError("malformed Codex JSON event")

    @staticmethod
    def _is_delimiter(character: str) -> bool:
        """Return whether a character terminates a JSON scalar token."""
        return character in " \t\r{}[],:"

    def _append_string_character(self, character: str) -> None:
        """Keep only the bounded string prefix needed for root discriminator checks."""
        if len(self._string) < _STRING_CAPTURE_LIMIT:
            self._string.append(character)
        else:
            self._string_truncated = True

    def _consume_string(self, character: str) -> None:
        """Consume one JSON string character, including strictly validated escapes."""
        if self._string_state == "string":
            if character == '"':
                self._string_state = "none"
                self._emit("string", "".join(self._string), self._string_truncated)
            elif character == "\\":
                self._string_state = "escape"
            elif ord(character) < 0x20:
                self._reject()
            else:
                self._append_string_character(character)
            return
        if self._string_state == "escape":
            escaped = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }.get(character)
            if escaped is not None:
                self._append_string_character(escaped)
                self._string_state = "string"
            elif character == "u":
                self._unicode_digits = []
                self._string_state = "unicode"
            else:
                self._reject()
            return
        if character not in "0123456789abcdefABCDEF":
            self._reject()
        self._unicode_digits.append(character)
        if len(self._unicode_digits) == 4:
            self._append_string_character(chr(int("".join(self._unicode_digits), 16)))
            self._string_state = "string"

    def _consume_primitive(self, character: str) -> None:
        """Validate a JSON literal or number with a bounded deterministic automaton."""
        if self._primitive_kind == "literal":
            expected, position = self._primitive_state.split(":")
            offset = int(position)
            if offset >= len(expected) or character != expected[offset]:
                self._reject()
            self._primitive_state = f"{expected}:{offset + 1}"
            return
        transitions = {
            "minus": {"digit": "zero_or_integer"},
            "zero": {".": "fraction_start", "eE": "exponent_start"},
            "integer": {"digit": "integer", ".": "fraction_start", "eE": "exponent_start"},
            "fraction_start": {"digit": "fraction"},
            "fraction": {"digit": "fraction", "eE": "exponent_start"},
            "exponent_start": {"+-": "exponent_sign", "digit": "exponent"},
            "exponent_sign": {"digit": "exponent"},
            "exponent": {"digit": "exponent"},
        }
        state = self._primitive_state
        if character in "0123456789":
            category = "digit"
        elif character == ".":
            category = character
        elif character in "+-":
            category = "+-"
        elif character in "eE":
            category = "eE"
        else:
            self._reject()
            return
        next_state = transitions.get(state, {}).get(category)
        if next_state == "zero_or_integer":
            next_state = "zero" if character == "0" else "integer"
        if next_state is None:
            self._reject()
        self._primitive_state = next_state

    def _finish_primitive(self) -> None:
        """Emit only JSON scalars that ended in an accepting state."""
        assert self._primitive_kind is not None
        if self._primitive_kind == "literal":
            expected, position = self._primitive_state.split(":")
            if int(position) != len(expected):
                self._reject()
        elif self._primitive_state not in {"zero", "integer", "fraction", "exponent"}:
            self._reject()
        self._primitive_kind = None
        self._primitive_state = ""
        self._emit("primitive", None, False)

    def _consume_punctuation(self, character: str) -> None:
        """Consume structural punctuation through the root-only JSON grammar."""
        if character in "[{":
            if not self._expect_value():
                self._reject()
            if len(self._stack) >= MAX_JSON_NESTING_DEPTH:
                self._reject()
            if character == "{":
                self._stack.append(("object", "key_or_end", None))
            else:
                self._stack.append(("array", "value_or_end", None))
            self._root_started = True
            return
        if character in "]}":
            self._close_container(character)
            return
        if character == ":":
            if not self._stack or self._stack[-1][0] != "object" or self._stack[-1][1] != "colon":
                self._reject()
            kind, _state, key = self._stack.pop()
            self._stack.append((kind, "value", key))
            return
        if character == ",":
            if not self._stack:
                self._reject()
            kind, state, _key = self._stack.pop()
            if state != "comma_or_end":
                self._reject()
            self._stack.append((kind, "key" if kind == "object" else "value", None))
            return
        self._reject()

    def _expect_value(self) -> bool:
        """Return whether the current parser location admits a complete JSON value."""
        if not self._root_started:
            return True
        if not self._stack:
            return False
        return self._stack[-1][1] in {"value", "value_or_end"}

    def _emit(self, kind: str, value: str | None, truncated: bool) -> None:
        """Feed a completed scalar token into the enclosing object or array state."""
        if not self._root_started or not self._stack:
            self._reject()
        container, state, key = self._stack.pop()
        if container == "object" and state in {"key_or_end", "key"}:
            if kind != "string":
                self._reject()
            self._stack.append((container, "colon", value if not truncated else None))
            return
        if state not in {"value", "value_or_end"}:
            self._reject()
        if len(self._stack) == 0 and key == "type":
            self._root_type_count += 1
            if self._root_type_count != 1:
                raise CodexUsageParseError("Codex event contains duplicate JSON keys")
            if kind != "string":
                self._reject()
            self._root_type_value = None if truncated else value
        self._stack.append((container, "comma_or_end", None))

    def _close_container(self, character: str) -> None:
        """Close an object or array only from legal states and propagate its value."""
        if not self._stack:
            self._reject()
        container, state, _key = self._stack[-1]
        expected = "}" if container == "object" else "]"
        if character != expected or state not in {"key_or_end", "value_or_end", "comma_or_end"}:
            self._reject()
        self._stack.pop()
        if not self._stack:
            self._root_finished = True
            return
        parent, parent_state, parent_key = self._stack.pop()
        if parent_state not in {"value", "value_or_end"}:
            self._reject()
        if len(self._stack) == 0 and parent_key == "type":
            self._reject()
        self._stack.append((parent, "comma_or_end", None))


def _non_negative_integer(value: object, field: str) -> int:
    """Validate one token field without accepting booleans or partial values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexUsageParseError(f"malformed Codex usage field: {field}")
    return value


def _event_from_line(line: str) -> Mapping[str, Any]:
    """Decode one retained JSONL object and reject duplicate keys or unknown types."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Build one JSON object only when every key is unique."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CodexUsageParseError("Codex event contains duplicate JSON keys")
            result[key] = value
        return result

    malformed_json = False
    event: object = None
    try:
        event = json.loads(line, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError:
        malformed_json = True
    if malformed_json:
        raise CodexUsageParseError("malformed Codex JSONL event") from None
    if not isinstance(event, dict):
        raise CodexUsageParseError("malformed Codex event object")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise CodexUsageParseError("Codex event is missing a string type discriminator")
    if event_type not in CODEX_EVENT_TYPES:
        raise CodexUsageParseError("unknown Codex event type")
    return event


def _apply_retained_event(
    line: bytes, terminal_marker_seen: bool, parsed: ParsedUsage | None
) -> tuple[bool, ParsedUsage | None]:
    """Strictly decode a retained event and apply frozen terminal usage semantics."""
    invalid_utf8 = False
    decoded = ""
    try:
        decoded = line.decode("utf-8")
    except UnicodeDecodeError:
        invalid_utf8 = True
    if invalid_utf8:
        raise CodexUsageParseError("usage stream contains invalid UTF-8") from None
    if not decoded.strip():
        raise CodexUsageParseError("blank Codex JSONL event")
    event = _event_from_line(decoded)
    if event["type"] in OPAQUE_ITEM_TYPES:
        return terminal_marker_seen, parsed
    if event["type"] not in {"turn.failed", "turn.completed"}:
        return terminal_marker_seen, parsed
    if terminal_marker_seen:
        raise CodexUsageParseError("multiple Codex terminal usage events")
    if event["type"] == "turn.failed":
        return True, parsed
    usage = event.get("usage")
    expected = {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
    if not isinstance(usage, dict):
        raise CodexUsageParseError("Codex turn.completed event is missing usage")
    if set(usage) != expected:
        raise CodexUsageParseError("malformed Codex terminal usage schema")
    return True, ParsedUsage(
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


def parse_codex_stream(stream: _ReadableBinaryStream) -> ParsedUsage:
    """Parse a bounded Codex JSONL stream while streaming opaque item payload bytes.

    Args:
        stream: Binary JSONL stdout from the fixed Codex harness command.

    Returns:
        Normalized token totals from the one required retained ``turn.completed`` event.

    Raises:
        CodexUsageParseError: If stream bounds, JSON structure, or frozen usage grammar fails.
    """
    event_count = 0
    retained_total_bytes = 0
    opaque_total_bytes = 0
    event_bytes = 0
    retained_line = bytearray()
    scanner = _JsonRootScanner()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    terminal_marker_seen = False
    parsed: ParsedUsage | None = None

    def complete_event() -> None:
        """Validate one newline-terminated event and reset bounded per-event state."""
        nonlocal event_count, retained_total_bytes, opaque_total_bytes, event_bytes
        nonlocal retained_line, scanner, terminal_marker_seen, parsed
        event_count += 1
        if event_count > MAX_EVENT_COUNT:
            raise CodexUsageParseError("usage stream exceeds event count limit")
        scanner.finish()
        if scanner.is_opaque_item:
            if event_bytes > MAX_CODEX_OPAQUE_EVENT_BYTES:
                raise CodexUsageParseError("Codex opaque event exceeds size limit")
            opaque_total_bytes += event_bytes
            if opaque_total_bytes > MAX_CODEX_OPAQUE_TOTAL_BYTES:
                raise CodexUsageParseError("Codex opaque stream exceeds total byte limit")
            if event_bytes <= MAX_EVENT_BYTES:
                terminal_marker_seen, parsed = _apply_retained_event(
                    bytes(retained_line), terminal_marker_seen, parsed
                )
        else:
            if event_bytes > MAX_EVENT_BYTES:
                raise CodexUsageParseError("Codex retained event exceeds size limit")
            retained_total_bytes += event_bytes
            if retained_total_bytes > MAX_STREAM_BYTES:
                raise CodexUsageParseError("usage stream exceeds total byte limit")
            terminal_marker_seen, parsed = _apply_retained_event(
                bytes(retained_line), terminal_marker_seen, parsed
            )
        event_bytes = 0
        retained_line = bytearray()
        scanner = _JsonRootScanner()

    def consume_segment(segment: bytes, has_newline: bool) -> None:
        """Account, decode, scan, and optionally retain one bounded byte segment."""
        nonlocal event_bytes
        event_bytes += len(segment)
        if event_bytes > MAX_CODEX_OPAQUE_EVENT_BYTES:
            raise CodexUsageParseError("Codex opaque event exceeds size limit")
        remaining = MAX_EVENT_BYTES - len(retained_line)
        if remaining > 0:
            retained_line.extend(segment[:remaining])
        invalid_utf8 = False
        text = ""
        try:
            text = decoder.decode(segment, final=False)
        except UnicodeDecodeError:
            invalid_utf8 = True
        if invalid_utf8:
            raise CodexUsageParseError("usage stream contains invalid UTF-8") from None
        if has_newline:
            if not text.endswith("\n") or decoder.getstate()[0]:
                raise CodexUsageParseError("usage stream contains invalid UTF-8")
            scanner.consume(text[:-1])
            complete_event()
        else:
            scanner.consume(text)

    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if chunk == b"":
            break
        if not isinstance(chunk, bytes) or len(chunk) > READ_CHUNK_BYTES:
            raise CodexUsageParseError("usage stream read exceeds size limit")
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                consume_segment(chunk[offset:], False)
                break
            consume_segment(chunk[offset : newline + 1], True)
            offset = newline + 1
    invalid_utf8 = False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        invalid_utf8 = True
    if invalid_utf8:
        raise CodexUsageParseError("usage stream contains invalid UTF-8") from None
    if event_bytes:
        raise CodexUsageParseError("usage stream contains a partial event")
    if parsed is None:
        raise CodexUsageMissingTerminalError(
            "Codex stream has no terminal turn.completed usage event"
        )
    return parsed
