"""Closed parser for one owned Claude 2.1.231 parent transcript."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capture_usage_models import ClaudeModelUsage, NativeClaudeRole, bounded_jsonl_lines

MAX_TRANSCRIPT_ROW_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_ROWS = 500_000
MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
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
        raise TranscriptError("transcript usage is invalid")
    return value


def _open_component(parent: int, component: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(component, flags, dir_fd=parent)
    except OSError as exc:
        raise TranscriptError("owned Claude transcript is unavailable") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise TranscriptError("owned Claude transcript is unavailable")
    return descriptor


def _project_name(cwd: Path) -> str:
    return "".join(character if character.isalnum() else "-" for character in str(cwd))


def _transcript_fd(cwd: Path, session_id: str) -> int:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise TranscriptError("platform cannot securely read Claude transcripts")
    root = Path.home() / ".claude"
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TranscriptError("owned Claude transcript is unavailable") from exc
    try:
        projects = _open_component(descriptor, "projects")
        os.close(descriptor)
        project = _open_component(projects, _project_name(cwd))
        os.close(projects)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        transcript = os.open(f"{session_id}.jsonl", flags, dir_fd=project)
        os.close(project)
    except Exception:
        with __import__("contextlib").suppress(OSError):
            os.close(descriptor)
        raise
    status = os.fstat(transcript)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_size > MAX_TRANSCRIPT_BYTES
    ):
        os.close(transcript)
        raise TranscriptError("owned Claude transcript is invalid")
    return transcript


def _require_no_subagents(cwd: Path, session_id: str) -> None:
    directory = Path.home() / ".claude" / "projects" / _project_name(cwd) / session_id
    try:
        status = directory.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise TranscriptError("owned Claude session tree is invalid")
    if (directory / "subagents").exists():
        raise TranscriptError("native Claude worker has subagents")


def parse_owned_transcript(
    cwd: Path, session_id: str, role: NativeClaudeRole, effort: str
) -> TranscriptUsage:
    """Read and verify one exact Claude parent transcript without retaining content."""
    descriptor = _transcript_fd(cwd, session_id)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            rows = bounded_jsonl_lines(
                stream,
                max_event_bytes=MAX_TRANSCRIPT_ROW_BYTES,
                max_event_count=MAX_TRANSCRIPT_ROWS,
                max_stream_bytes=MAX_TRANSCRIPT_BYTES,
            )
            agent_settings = 0
            seen: dict[tuple[str, str | None, str], tuple[int, int, int, int]] = {}
            model: str | None = None
            for line in rows:
                try:
                    row = json.loads(line, object_pairs_hook=_reject_duplicates)
                except (json.JSONDecodeError, TranscriptError) as exc:
                    raise TranscriptError("owned Claude transcript is invalid") from exc
                if (
                    not isinstance(row, dict)
                    or row.get("type") not in _ALLOWED_TYPES
                    or row.get("sessionId") != session_id
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
                if row["type"] == "agent-setting":
                    if row.get("agentSetting") != role.value:
                        raise TranscriptError("owned Claude transcript role is invalid")
                    agent_settings += 1
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
                message_id = message.get("id")
                row_model = message.get("model")
                usage = message.get("usage")
                if (
                    not isinstance(message_id, str)
                    or not isinstance(row_model, str)
                    or not isinstance(usage, dict)
                ):
                    raise TranscriptError("owned Claude transcript is invalid")
                usage_keys = set(usage)
                if (
                    usage_keys - (_USAGE_REQUIRED | _USAGE_OPTIONAL)
                    or not usage_keys >= _USAGE_REQUIRED
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
                key = (session_id, parent if isinstance(parent, str) else None, message_id)
                previous = seen.get(key)
                if previous is not None and previous != values:
                    raise TranscriptError("owned Claude transcript usage conflicts")
                seen[key] = values
                if model is None:
                    model = row_model
                elif model != row_model:
                    raise TranscriptError("owned Claude transcript model is invalid")
            if agent_settings != 1 or not seen or model is None:
                raise TranscriptError("owned Claude transcript is incomplete")
    finally:
        # fdopen owns the descriptor on its normal path.
        pass
    _require_no_subagents(cwd, session_id)
    totals = tuple(sum(value[index] for value in seen.values()) for index in range(4))
    model_usage = ClaudeModelUsage(
        model=model,
        input_tokens=totals[0],
        cached_input_tokens=totals[1],
        cache_creation_input_tokens=totals[2],
        output_tokens=totals[3],
    )
    return TranscriptUsage(
        session_id,
        model,
        len(seen),
        totals[0],
        totals[1],
        totals[2],
        totals[3],
        (model_usage,),
    )
