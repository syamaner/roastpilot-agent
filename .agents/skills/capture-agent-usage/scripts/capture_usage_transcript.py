"""Closed parser for one owned Claude parent transcript bound to a probe."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from capture_usage_models import ClaudeModelUsage, NativeClaudeRole, bounded_jsonl_lines

MAX_TRANSCRIPT_ROW_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_ROWS = 500_000
MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
TIMESTAMP_SKEW_SECONDS = 120
MAX_HANDBACK_BYTES = 262_144
"""256 KiB of UTF-8: larger than a full planner contract, far below the 4 MiB
row cap, so this is the bound specific to a value that crosses the local
provider-transcript store boundary to the launching parent (D166)."""
HANDBACK_SCHEMA_VERSION = 1
"""Schema version of the bounded READ_ONLY handback payload (D166, §2.3)."""
_HANDBACK_BLOCK_TYPES = frozenset({"text", "thinking"})
"""The only two terminal-assistant-row content block types this parser admits."""
_HANDBACK_TEXT_BLOCK_KEYS = frozenset({"type", "text"})
"""Exact observed key set of a ``text`` content block (lead-supplied 2.1.233 probe)."""
_HANDBACK_STRIP_CHARACTERS = " \t\n\r\x0b\x0c"
"""ASCII whitespace only, per the closed emptiness rule (§2.3 point 7)."""
_ALLOWED_TYPES = frozenset(
    {
        "agent-setting",
        "ai-title",
        "assistant",
        "attachment",
        "atis-latch",
        "last-prompt",
        "mode",
        "queue-operation",
        "user",
    }
)
"""Row types observed across the five committed 2.1.233 transcript fixtures.

``mode`` and ``ai-title`` are metadata-only rows with independently closed shapes.
"""
_MODE_ROW_KEYS = frozenset({"type", "mode", "sessionId"})
"""Exact observed keys of the metadata-only ``mode`` row (story-planner.jsonl)."""
_AI_TITLE_ROW_KEYS = frozenset({"type", "aiTitle", "sessionId"})
"""Exact observed keys of the metadata-only ``ai-title`` row."""
_ATIS_LATCH_ROW_KEYS = frozenset({"atis", "sessionId", "type"})
"""Exact observed keys of a discarded ``atis-latch`` metadata row."""
_USAGE_REQUIRED = frozenset(
    {"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"}
)
_USAGE_OPTIONAL = frozenset(
    {
        "output_tokens_details",
        "server_tool_use",
        "service_tier",
        "cache_creation",
        "inference_geo",
        "iterations",
        "speed",
    }
)


class TranscriptError(ValueError):
    """Raised for content-free transcript verification failures."""


@dataclass(frozen=True)
class TranscriptUsage:
    """Closed numeric usage extracted from a verified parent transcript."""

    session_id: str
    model: str
    usage_message_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    model_usage: tuple[ClaudeModelUsage, ...]
    handback_text: str | None = None
    """The bounded READ_ONLY handback text, or ``None`` when not requested.

    Populated only when the caller passes ``require_handback=True`` to
    :func:`parse_owned_transcript`; otherwise no content block is ever read
    (D166, §2.3 point 1).
    """


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TranscriptError("transcript contains duplicate JSON keys")
        result[key] = value
    return result


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TranscriptError("owned Claude transcript usage is invalid")
    return value


def _project_name(cwd: Path) -> str:
    """Encode a worktree path with Claude 2.1.233's fixed project grammar."""
    return "".join(
        character if character.isascii() and character.isalnum() else "-" for character in str(cwd)
    )


def _close(descriptor: int | None) -> None:
    if descriptor is not None:
        with suppress(OSError):
            os.close(descriptor)


def _directory(parent: int | None, component: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = (
            os.open(component, flags)
            if parent is None
            else os.open(component, flags, dir_fd=parent)
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            _close(descriptor)
            raise OSError
        return descriptor
    except OSError:
        raise TranscriptError("owned Claude transcript is unavailable") from None


def _project_directory(cwd: Path) -> int:
    """Open the exact configured project directory, closing every parent FD."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise TranscriptError("platform cannot securely read Claude transcripts")
    root = projects = None
    try:
        root = _directory(None, str(Path.home() / ".claude"))
        projects = _directory(root, "projects")
        project = _directory(projects, _project_name(cwd))
        return project
    finally:
        _close(projects)
        _close(root)


def _transcript_fd(cwd: Path, session_id: str) -> int:
    project = None
    descriptor = None
    try:
        project = _project_directory(cwd)
        descriptor = os.open(
            session_id + ".jsonl",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=project,
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_size > MAX_TRANSCRIPT_BYTES
        ):
            raise OSError
        result, descriptor = descriptor, None
        return result
    except (OSError, TranscriptError):
        pass
    finally:
        _close(descriptor)
        _close(project)
    raise TranscriptError("owned Claude transcript is unavailable")


def reject_existing_owned_session(cwd: Path, session_id: str) -> None:
    """Reject a pre-existing exact parent file or adjacent session directory.

    A missing Claude tree is expected before a new Claude invocation.  Any other
    inability to inspect the expected components fails closed without discovery.
    """
    root = projects = project = descriptor = None
    try:
        root = _directory(None, str(Path.home() / ".claude"))
        projects = _directory(root, "projects")
        try:
            project = os.open(
                _project_name(cwd),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=projects,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise TranscriptError("owned Claude session path is invalid") from None
        for name, flags in (
            (session_id + ".jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC),
            (session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC),
        ):
            try:
                descriptor = os.open(name, flags, dir_fd=project)
            except FileNotFoundError:
                continue
            except OSError:
                raise TranscriptError("owned Claude session path is invalid") from None
            raise TranscriptError("owned Claude session already exists")
    finally:
        _close(descriptor)
        _close(project)
        _close(projects)
        _close(root)


def _require_no_subagents(cwd: Path, session_id: str) -> None:
    """Permit only an absent adjacent session directory; reject every other state."""
    project = directory = child = None
    try:
        project = _project_directory(cwd)
        try:
            directory = os.open(
                session_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=project,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise TranscriptError("owned Claude session tree is invalid") from None
        if not stat.S_ISDIR(os.fstat(directory).st_mode):
            raise TranscriptError("owned Claude session tree is invalid")
        try:
            child = os.open(
                "subagents",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise TranscriptError("owned Claude session tree is invalid") from None
        raise TranscriptError("native Claude worker has subagents")
    except OSError:
        raise TranscriptError("owned Claude session tree is invalid") from None
    finally:
        _close(child)
        _close(directory)
        _close(project)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TranscriptError("owned Claude transcript chronology is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        parsed = None
    if parsed is None:
        raise TranscriptError("owned Claude transcript chronology is invalid")
    return parsed


def _extract_handback(message: dict[str, Any]) -> str:
    """Extract the bounded handback text from one terminal assistant message.

    Args:
        message: The ``message`` object of the terminal (file-order-last)
            ``assistant`` row.

    Returns:
        The ordered, unmodified concatenation of every ``text`` block's text.

    Raises:
        TranscriptError: If the turn is incomplete, carries an unrecognized or
            malformed content block, is empty after stripping ASCII
            whitespace, or exceeds :data:`MAX_HANDBACK_BYTES` once encoded.
    """
    if message.get("stop_reason") != "end_turn":
        raise TranscriptError("owned Claude transcript terminal turn is invalid")
    content = message.get("content")
    if not isinstance(content, list):
        raise TranscriptError("owned Claude transcript terminal turn is invalid")
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise TranscriptError("owned Claude transcript terminal turn is invalid")
        block_type = block.get("type")
        if not isinstance(block_type, str) or block_type not in _HANDBACK_BLOCK_TYPES:
            raise TranscriptError("owned Claude transcript terminal turn is invalid")
        if block_type == "thinking":
            continue
        if set(block) != _HANDBACK_TEXT_BLOCK_KEYS or not isinstance(block.get("text"), str):
            raise TranscriptError("owned Claude transcript terminal turn is invalid")
        texts.append(block["text"])
    handback = "".join(texts)
    if not handback.strip(_HANDBACK_STRIP_CHARACTERS):
        raise TranscriptError("owned Claude transcript terminal turn is invalid")
    if len(handback.encode("utf-8")) > MAX_HANDBACK_BYTES:
        raise TranscriptError("owned Claude transcript terminal turn is invalid")
    return handback


def parse_owned_transcript(
    cwd: Path,
    session_id: str,
    role: NativeClaudeRole,
    effort: str,
    *,
    expected_version: str,
    expected_permission_mode: str,
    require_handback: bool = False,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> TranscriptUsage:
    """Read one exact parent transcript without retaining content or host paths.

    Args:
        cwd: The attested worktree whose Claude project directory is read.
        session_id: The exact generated session identifier to bind.
        role: The committed native role attested against ``agent-setting`` rows.
        effort: The committed effort attested against every assistant row.
        expected_version: The version observed by the caller's one bounded CLI
            probe. Every assistant row must equal this value exactly.
        expected_permission_mode: The single frozen permission-mode value
            (derived by the caller from the committed capability mapping) that
            every row's optional ``permissionMode`` key must equal exactly.
        require_handback: Whether to extract and return the bounded terminal
            handback text. When ``False`` (the default), no content block is
            ever read.
        started_at: Optional run-start bound for assistant-row chronology skew.
        completed_at: Optional run-end bound for assistant-row chronology skew.

    Returns:
        The closed, verified numeric usage (and, when requested, handback text).

    Raises:
        TranscriptError: If any row, identity, chronology, usage, permission-mode,
            or terminal-turn invariant is violated.
    """
    descriptor = _transcript_fd(cwd, session_id)
    seen: dict[tuple[str, str | None, str], tuple[int, int, int, int]] = {}
    model: str | None = None
    agent_settings = 0
    last_timestamp: datetime | None = None
    last_assistant_message: dict[str, Any] | None = None
    permission_mode_seen = False
    invalid = False
    try:
        with os.fdopen(descriptor, "rb") as stream:
            for line in bounded_jsonl_lines(
                stream,
                max_event_bytes=MAX_TRANSCRIPT_ROW_BYTES,
                max_event_count=MAX_TRANSCRIPT_ROWS,
                max_stream_bytes=MAX_TRANSCRIPT_BYTES,
            ):
                try:
                    row = json.loads(line, object_pairs_hook=_reject_duplicates)
                except (json.JSONDecodeError, TranscriptError):
                    row = None
                if row is None:
                    raise TranscriptError("owned Claude transcript is invalid")
                if (
                    not isinstance(row, dict)
                    or row.get("type") not in _ALLOWED_TYPES
                    or row.get("sessionId") != session_id
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
                if "permissionMode" in row:
                    row_permission_mode = row.get("permissionMode")
                    if (
                        not isinstance(row_permission_mode, str)
                        or row_permission_mode != expected_permission_mode
                    ):
                        raise TranscriptError("owned Claude transcript permission mode is invalid")
                    permission_mode_seen = True
                if row["type"] == "atis-latch":
                    if (
                        set(row) != _ATIS_LATCH_ROW_KEYS
                        or not isinstance(row.get("atis"), str)
                        or not isinstance(row.get("sessionId"), str)
                    ):
                        raise TranscriptError("owned Claude transcript is invalid")
                    continue
                if row["type"] == "mode":
                    if set(row) != _MODE_ROW_KEYS or row.get("mode") != "normal":
                        raise TranscriptError("owned Claude transcript is invalid")
                    continue
                if row["type"] == "assistant":
                    timestamp = _timestamp(row.get("timestamp"))
                    if last_timestamp is not None and timestamp < last_timestamp:
                        raise TranscriptError("owned Claude transcript chronology is invalid")
                    last_timestamp = timestamp
                    if (
                        started_at is not None
                        and completed_at is not None
                        and (
                            timestamp
                            < started_at.astimezone(UTC) - timedelta(seconds=TIMESTAMP_SKEW_SECONDS)
                            or timestamp
                            > completed_at.astimezone(UTC)
                            + timedelta(seconds=TIMESTAMP_SKEW_SECONDS)
                        )
                    ):
                        raise TranscriptError("owned Claude transcript chronology is invalid")
                if row["type"] == "agent-setting":
                    if row.get("agentSetting") != role.value:
                        raise TranscriptError("owned Claude transcript role is invalid")
                    agent_settings += 1
                    continue
                if row["type"] == "ai-title":
                    if (
                        set(row) != _AI_TITLE_ROW_KEYS
                        or not isinstance(row.get("aiTitle"), str)
                        or not isinstance(row.get("sessionId"), str)
                    ):
                        raise TranscriptError("owned Claude transcript is invalid")
                    continue
                if row["type"] != "assistant":
                    continue
                message = row.get("message")
                if (
                    not isinstance(message, dict)
                    or "agentId" in row
                    or "mode" in row
                    or row.get("version") != expected_version
                    or row.get("effort") != effort
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
                if require_handback:
                    last_assistant_message = message
                message_id, row_model, usage = (
                    message.get("id"),
                    message.get("model"),
                    message.get("usage"),
                )
                if (
                    not isinstance(message_id, str)
                    or not isinstance(row_model, str)
                    or not isinstance(usage, dict)
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
                if (
                    set(usage) - (_USAGE_REQUIRED | _USAGE_OPTIONAL)
                    or not set(usage) >= _USAGE_REQUIRED
                ):
                    raise TranscriptError("owned Claude transcript usage is invalid")
                values = tuple(
                    _integer(usage[key])
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "output_tokens",
                    )
                )
                parent = row.get("parentUuid")
                if parent is not None and not isinstance(parent, str):
                    raise TranscriptError("owned Claude transcript is invalid")
                key = (session_id, parent, message_id)
                if key in seen and seen[key] != values:
                    raise TranscriptError("owned Claude transcript usage conflicts")
                seen[key] = values
                if model is None:
                    model = row_model
                elif model != row_model:
                    raise TranscriptError("owned Claude transcript model is invalid")
    except TranscriptError:
        raise
    except (OSError, ValueError):
        invalid = True
    if invalid:
        raise TranscriptError("owned Claude transcript is invalid")
    if agent_settings == 0 or not seen or model is None or not permission_mode_seen:
        raise TranscriptError("owned Claude transcript is incomplete")
    handback_text: str | None = None
    if require_handback:
        if last_assistant_message is None:
            # Every successfully validated assistant row sets last_assistant_message
            # before seen[key] is populated (above), so a non-empty seen (already
            # required by the agent_settings/seen/model check above) guarantees this
            # is set too; defensive invariant backstop, not reachable here.
            raise TranscriptError(
                "owned Claude transcript terminal turn is invalid"
            )  # pragma: no cover
        handback_text = _extract_handback(last_assistant_message)
    _require_no_subagents(cwd, session_id)
    totals = tuple(sum(item[index] for item in seen.values()) for index in range(4))
    return TranscriptUsage(
        session_id,
        model,
        len(seen),
        totals[0],
        totals[1],
        totals[2],
        totals[3],
        (
            ClaudeModelUsage(
                model=model,
                input_tokens=totals[0],
                cached_input_tokens=totals[1],
                cache_creation_input_tokens=totals[2],
                output_tokens=totals[3],
            ),
        ),
        handback_text,
    )
