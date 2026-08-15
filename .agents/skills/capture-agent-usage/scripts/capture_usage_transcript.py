"""Closed parser for one owned Claude 2.1.231 parent transcript."""

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
_ALLOWED_TYPES = frozenset(
    {
        "agent-setting",
        "ai-title",
        "assistant",
        "attachment",
        "last-prompt",
        "queue-operation",
        "user",
    }
)
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
    """Encode a worktree path with Claude 2.1.231's fixed project grammar."""
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
        raise TranscriptError("owned Claude transcript is unavailable") from None
    finally:
        _close(descriptor)
        _close(project)


def reject_existing_owned_session(cwd: Path, session_id: str) -> None:
    """Reject a pre-existing exact parent file or adjacent session directory.

    A missing Claude tree is expected before a new Claude invocation.  Any other
    inability to inspect the expected components fails closed without discovery.
    """
    project = descriptor = None
    try:
        try:
            project = _project_directory(cwd)
        except TranscriptError:
            return
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
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise TranscriptError("owned Claude transcript chronology is invalid") from None


def parse_owned_transcript(
    cwd: Path,
    session_id: str,
    role: NativeClaudeRole,
    effort: str,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> TranscriptUsage:
    """Read one exact parent transcript without retaining content or host paths."""
    descriptor = _transcript_fd(cwd, session_id)
    seen: dict[tuple[str, str | None, str], tuple[int, int, int, int]] = {}
    model: str | None = None
    agent_settings = 0
    last_timestamp: datetime | None = None
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
                    raise TranscriptError("owned Claude transcript is invalid") from None
                if (
                    not isinstance(row, dict)
                    or row.get("type") not in _ALLOWED_TYPES
                    or row.get("sessionId") != session_id
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
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
                if row["type"] != "assistant":
                    continue
                message = row.get("message")
                if (
                    not isinstance(message, dict)
                    or "agentId" in row
                    or row.get("version") != "2.1.231"
                    or row.get("effort") != effort
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
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
    except (OSError, ValueError):
        raise TranscriptError("owned Claude transcript is invalid") from None
    if agent_settings != 1 or not seen or model is None:
        raise TranscriptError("owned Claude transcript is incomplete")
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
    )
