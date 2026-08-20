"""Fail-closed, long-lived metadata capture for registered Codex leaves.

The parent starts ``supervise-native-codex`` before dispatching its named leaf.
This process keeps descriptor-relative bindings open, writes one READY frame,
then accepts exactly one terminal task status on stdin.  It never launches Codex.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from capture_usage_models import NativeCodexRole, NativeCodexTaskStatus, NativeCodexUsageRecord

MAX_PROVIDER_FILES = 4096
MAX_PROVIDER_DEPTH = 8
MAX_PROVIDER_FILE_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PROVIDER_LINES = 100_000
# Observed Codex 0.147.0 tool-output event: 1,006,736 bytes.  Keep this fixed
# two-MiB bound below the separate eight-MiB rollout-file cap.
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_JSON_NESTING = 64
MAX_COMMITTED_FILE_BYTES = 64 * 1024
_ROLE_EXPECTATIONS: dict[NativeCodexRole, tuple[str, str]] = {
    NativeCodexRole.ENGINEER_BE: ("agents/engineer-be.toml", "high"),
    NativeCodexRole.ENGINEER_FE: ("agents/engineer-fe.toml", "high"),
    NativeCodexRole.REPAIR: ("agents/repair.toml", "medium"),
}
_ROLE_SHA256: dict[NativeCodexRole, str] = {
    NativeCodexRole.ENGINEER_BE: "bcad195fce15322e489cc836d3b846953994fd136f442fff6c338f69c490d74f",
    NativeCodexRole.ENGINEER_FE: "4da74886a9c5e4b7cad4b6e7ed858f0f7e596f76189bd07240b77e9cd5c13831",
    NativeCodexRole.REPAIR: "4671a9d8b84b500208f2b603e81f255d64d678fc11ebbf4982b7bf8ddca0fa7d",
}
_ROOT_TYPES = {
    "session_meta",
    "turn_context",
    "event_msg",
    "response_item",
    "inter_agent_communication_metadata",
    "world_state",
}
_EVENT_TYPES = {"task_started", "token_count", "item_completed", "task_complete"}
_RESPONSE_TYPES = {
    "reasoning",
    "message",
    "agent_message",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
}
_SUBAGENT_SESSION_META_KEYS = {
    "agent_nickname",
    "agent_path",
    "agent_role",
    "base_instructions",
    "cli_version",
    "context_window",
    "cwd",
    "git",
    "history_mode",
    "id",
    "model_provider",
    "multi_agent_version",
    "originator",
    "parent_thread_id",
    "session_id",
    "source",
    "thread_source",
    "timestamp",
}
_ROOT_SESSION_META_KEYS = {
    "base_instructions",
    "cli_version",
    "context_window",
    "cwd",
    "git",
    "history_mode",
    "id",
    "model_provider",
    "originator",
    "session_id",
    "source",
    "thread_source",
    "timestamp",
}
_GIT_KEYS = {"branch", "commit_hash", "repository_url"}
_TURN_CONTEXT_KEYS = {
    "approval_policy",
    "approvals_reviewer",
    "collaboration_mode",
    "comp_hash",
    "current_date",
    "cwd",
    "effort",
    "file_system_sandbox_policy",
    "model",
    "multi_agent_version",
    "permission_profile",
    "personality",
    "realtime_active",
    "sandbox_policy",
    "summary",
    "timezone",
    "turn_id",
    "workspace_roots",
}
_EVENT_KEYS = {
    "task_started": {
        "collaboration_mode_kind",
        "model_context_window",
        "started_at",
        "turn_id",
        "type",
    },
    "task_complete": {
        "completed_at",
        "duration_ms",
        "last_agent_message",
        "started_at",
        "time_to_first_token_ms",
        "turn_id",
        "type",
    },
    "item_completed": {"completed_at_ms", "item", "started_at_ms", "thread_id", "turn_id", "type"},
    "token_count": {"info", "rate_limits", "type"},
}
_RESPONSE_ITEM_KEYS: dict[str, tuple[set[str], ...]] = {
    "function_call": (
        {
            "arguments",
            "call_id",
            "id",
            "internal_chat_message_metadata_passthrough",
            "name",
            "type",
        },
    ),
    "agent_message": (
        {
            "author",
            "content",
            "id",
            "internal_chat_message_metadata_passthrough",
            "recipient",
            "type",
        },
    ),
    "custom_tool_call": (
        {
            "call_id",
            "id",
            "input",
            "internal_chat_message_metadata_passthrough",
            "name",
            "status",
            "type",
        },
    ),
    "custom_tool_call_output": (
        {"call_id", "id", "internal_chat_message_metadata_passthrough", "output", "type"},
    ),
    "function_call_output": (
        {"call_id", "id", "internal_chat_message_metadata_passthrough", "output", "type"},
    ),
    "message": (
        {"content", "id", "internal_chat_message_metadata_passthrough", "role", "type"},
        {"content", "id", "internal_chat_message_metadata_passthrough", "phase", "role", "type"},
    ),
    "reasoning": (
        {
            "encrypted_content",
            "id",
            "internal_chat_message_metadata_passthrough",
            "summary",
            "type",
        },
    ),
}


class NativeCodexCaptureError(ValueError):
    """Raised with a fixed, content-free native-Codex capture failure."""


@dataclass(frozen=True)
class _Root:
    """One held directory descriptor and stable identity."""

    descriptor: int
    device: int
    inode: int
    path: str = ""


def _fail() -> None:
    raise NativeCodexCaptureError("native Codex capture is invalid")


def _safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and all(char.isascii() and (char.isalnum() or char in "._:-") for char in value)
    )


def _open_root(raw: str, *, private: bool) -> _Root:
    """Open an owned root without following the final pathname component."""
    fd: int | None = None
    try:
        if not os.path.isabs(raw):
            _fail()
        canonical = os.path.abspath(raw)
        fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        for part in canonical.split(os.path.sep)[1:]:
            if not part:
                continue
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
        status = os.fstat(fd)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            os.close(fd)
            fd = None
            _fail()
        if private and stat.S_IMODE(status.st_mode) != 0o700:
            os.close(fd)
            fd = None
            _fail()
        result = _Root(fd, status.st_dev, status.st_ino, canonical)
        fd = None
        return result
    except OSError:
        _fail()
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)


def _read_relative(root: _Root, parts: tuple[str, ...]) -> bytes:
    """Read one regular file through held root-relative no-follow descriptors."""
    descriptor = os.dup(root.descriptor)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
        )
        try:
            status = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_nlink != 1
            ):
                _fail()
            with os.fdopen(file_descriptor, "rb", closefd=False) as file_handle:
                content = file_handle.read(MAX_COMMITTED_FILE_BYTES + 1)
                if len(content) > MAX_COMMITTED_FILE_BYTES:
                    _fail()
                return content
        finally:
            os.close(file_descriptor)
    except OSError:
        _fail()
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _assert_root(root: _Root, *, private: bool = False) -> None:
    try:
        status = os.fstat(root.descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or (status.st_dev, status.st_ino) != (root.device, root.inode)
            or (private and stat.S_IMODE(status.st_mode) != 0o700)
        ):
            _fail()
    except OSError:
        _fail()


def _walk(root: _Root) -> Iterator[tuple[str, int, os.stat_result]]:
    """Yield every regular rollout through no-follow directory descriptors."""

    def descend(
        directory: int, prefix: str, depth: int
    ) -> Iterator[tuple[str, int, os.stat_result]]:
        if depth > MAX_PROVIDER_DEPTH:
            _fail()
        try:
            names = os.listdir(directory)
        except OSError:
            _fail()
        for name in names:
            if name in {".", ".."} or "/" in name:
                _fail()
            try:
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            except OSError:
                _fail()
            status = os.fstat(child)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISREG(status.st_mode):
                if (
                    not name.endswith(".jsonl")
                    or status.st_uid != os.geteuid()
                    or status.st_nlink != 1
                ):
                    os.close(child)
                    _fail()
                yield relative, child, status
                continue
            try:
                if (
                    not stat.S_ISDIR(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or status.st_nlink < 2
                ):
                    _fail()
                yield from descend(child, relative, depth + 1)
            finally:
                os.close(child)

    duplicate = os.dup(root.descriptor)
    try:
        yield from descend(duplicate, "", 0)
    finally:
        os.close(duplicate)


def _inventory(root: _Root) -> dict[str, tuple[int, int]]:
    """Snapshot immutable provider rollout identities, not mutable file sizes."""
    result: dict[str, tuple[int, int]] = {}
    for name, fd, status in _walk(root):
        try:
            result[name] = (status.st_dev, status.st_ino)
            if len(result) > MAX_PROVIDER_FILES:
                _fail()
        finally:
            os.close(fd)
    return result


def _git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            _fail()
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        _fail()


def _git_identity(repository: str, branch: str, base: str, *, final: bool) -> str:
    if (
        repository != "syamaner/roastpilot-agent"
        or not re.fullmatch(r"[0-9a-f]{40}", base)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch)
        or branch.startswith("-")
        or ".." in branch
    ):
        _fail()
    if _git(["remote", "get-url", "origin"]) not in {
        "https://github.com/syamaner/roastpilot-agent.git",
        "git@github.com:syamaner/roastpilot-agent.git",
    }:
        _fail()
    head = _git(["rev-parse", "HEAD"])
    status_args = ["status", "--porcelain", "--ignored"] if not final else ["status", "--porcelain"]
    if _git(["branch", "--show-current"]) != branch or _git(status_args):
        _fail()
    if _git(["rev-parse", "--verify", f"{base}^{{commit}}"]) != base or (
        not final and head != base
    ):
        _fail()
    return head


def _registered_role(root: _Root, role: NativeCodexRole) -> tuple[str, str, str, str]:
    """Parse and attest the complete registered-Codex role closure."""
    try:
        config_bytes = _read_relative(root, (".codex", "config.toml"))
        config = tomllib.loads(config_bytes.decode("utf-8"))
        agents = config.get("agents")
        if not isinstance(agents, dict) or agents.get("enabled") is not True:
            _fail()
        expected = {member.value for member in NativeCodexRole}
        if (
            set(config) != {"project_doc_max_bytes", "agents"}
            or set(agents) != {"enabled", "max_concurrent_threads_per_session", *expected}
            or agents["max_concurrent_threads_per_session"] != 3
        ):
            _fail()
        relpath, effort = _ROLE_EXPECTATIONS[role]
        registration = agents.get(role.value)
        if (
            not isinstance(registration, dict)
            or set(registration) != {"description", "config_file"}
            or not isinstance(registration["description"], str)
            or registration["config_file"] != relpath
        ):
            _fail()
        role_bytes = _read_relative(root, (".codex", *tuple(relpath.split("/"))))
        definition = tomllib.loads(role_bytes.decode("utf-8"))
        if (
            set(definition)
            != {"model", "model_reasoning_effort", "developer_instructions", "agents"}
            or definition.get("model") != "gpt-5.6-terra"
            or definition.get("model_reasoning_effort") != effort
            or definition.get("agents") != {"enabled": False}
            or not isinstance(definition.get("developer_instructions"), str)
            or hashlib.sha256(role_bytes).hexdigest() != _ROLE_SHA256[role]
        ):
            _fail()
        # The committed role boundary itself proves a leaf cannot invoke Claude.
        return (
            hashlib.sha256(config_bytes).hexdigest(),
            hashlib.sha256(role_bytes).hexdigest(),
            effort,
            role.value,
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        _fail()


def _json(raw: bytes) -> dict[str, Any]:
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                _fail()
            value[key] = item
        return value

    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                quoted = False
            continue
        if byte == ord('"'):
            quoted = True
        elif byte in {ord("{"), ord("[")}:
            depth += 1
            if depth > MAX_JSON_NESTING:
                _fail()
        elif byte in {ord("}"), ord("]")}:
            depth -= 1
            if depth < 0:
                _fail()
    if quoted or escaped or depth:
        _fail()
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            _fail()
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail()


def _string(value: Any) -> str:
    if not _safe_identifier(value):
        _fail()
    return value


def _agent_path(value: Any) -> str:
    """Validate the canonical, parent-derived native agent path."""
    if not isinstance(value, str) or len(value) > 256 or not value.startswith("/root/"):
        _fail()
    if not _safe_identifier(value.removeprefix("/root/")):
        _fail()
    return value


def _opaque_text(value: Any) -> None:
    """Bound an unretained provider text field without interpreting its contents."""
    if not isinstance(value, str) or len(value) > MAX_EVENT_BYTES:
        _fail()


def _totals(value: Any) -> tuple[int, int, int, int, int, int]:
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail()
    result = tuple(value[key] for key in keys)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in result):
        _fail()
    return result  # type: ignore[return-value]


def _parse_rollout(
    fd: int, binding: dict[str, str]
) -> tuple[str, tuple[int, int, int, int, int, int], str, bool]:
    """Stream and validate the admitted 0.147.0 metadata grammar only."""
    seen_meta = seen_context = seen_started = seen_complete = False
    session = ""
    parent_thread = ""
    matches = False
    is_subagent = False
    totals: tuple[int, int, int, int, int, int] | None = None
    with os.fdopen(os.dup(fd), "rb") as stream:
        number = 0
        read_bytes = 0
        while True:
            raw = stream.readline(MAX_EVENT_BYTES + 1)
            if raw == b"":
                break
            number += 1
            read_bytes += len(raw)
            if (
                number > MAX_PROVIDER_LINES
                or read_bytes > MAX_PROVIDER_FILE_BYTES
                or len(raw) > MAX_EVENT_BYTES
                or not raw.endswith(b"\n")
            ):
                _fail()
            event = _json(raw)
            if (
                set(event) != {"ordinal", "payload", "timestamp", "type"}
                or not isinstance(event["type"], str)
                or event["type"] not in _ROOT_TYPES
                or not isinstance(event["payload"], dict)
                or isinstance(event["ordinal"], bool)
                or not isinstance(event["ordinal"], int)
                or event["ordinal"] < 0
            ):
                _fail()
            _opaque_text(event["timestamp"])
            kind, payload = event["type"], event["payload"]
            if kind == "session_meta":
                if seen_meta or seen_context:
                    _fail()
                source, git = payload.get("source"), payload.get("git")
                if not isinstance(git, dict) or set(git) != _GIT_KEYS:
                    _fail()
                if set(payload) == _ROOT_SESSION_META_KEYS:
                    if source != "cli" or payload["thread_source"] != "user":
                        _fail()
                elif set(payload) == _SUBAGENT_SESSION_META_KEYS:
                    if not isinstance(source, dict) or set(source) != {"subagent"}:
                        _fail()
                    source_subagent = source["subagent"]
                    if not isinstance(source_subagent, dict) or set(source_subagent) != {
                        "thread_spawn"
                    }:
                        _fail()
                    spawn = source_subagent["thread_spawn"]
                    if not isinstance(spawn, dict):
                        _fail()
                    if set(spawn) != {
                        "parent_thread_id",
                        "depth",
                        "agent_path",
                        "agent_nickname",
                        "agent_role",
                    }:
                        _fail()
                    if type(spawn["depth"]) is not int:
                        _fail()
                    parent_thread = _string(spawn["parent_thread_id"])
                    agent_path = _agent_path(spawn["agent_path"])
                    agent_role = _string(spawn["agent_role"])
                    nickname = _string(spawn["agent_nickname"])
                    if (
                        payload["agent_path"] != agent_path
                        or payload["agent_role"] != agent_role
                        or payload["parent_thread_id"] != parent_thread
                        or payload["agent_nickname"] != nickname
                        or payload["thread_source"] != "subagent"
                    ):
                        _fail()
                    is_subagent = True
                    matches = (
                        parent_thread == binding["parent_thread_id"]
                        and spawn["depth"] == 1
                        and agent_path == binding["agent_path"]
                        and agent_role == binding["role"]
                    )
                else:
                    _fail()
                if (
                    _string(payload.get("id")) == ""
                    or payload.get("originator") != "codex-tui"
                    or payload.get("cli_version") != "0.147.0"
                ):
                    _fail()
                session = _string(payload["id"])
                seen_meta = True
            elif not seen_meta:
                _fail()
            elif kind == "turn_context":
                if (
                    not seen_meta
                    or seen_context
                    or set(payload) != _TURN_CONTEXT_KEYS
                    or (
                        is_subagent
                        and (
                            payload.get("model") != "gpt-5.6-terra"
                            or payload.get("effort") != binding["effort"]
                        )
                    )
                ):
                    _fail()
                seen_context = True
            elif kind == "event_msg":
                if (
                    not isinstance(payload.get("type"), str)
                    or payload["type"] not in _EVENT_TYPES
                    or set(payload) != _EVENT_KEYS[payload["type"]]
                ):
                    _fail()
                if payload["type"] == "token_count":
                    if not seen_started or seen_complete:
                        _fail()
                    info = payload["info"]
                    if not isinstance(info, dict) or set(info) != {
                        "last_token_usage",
                        "model_context_window",
                        "total_token_usage",
                    }:
                        _fail()
                    current = _totals(info["total_token_usage"])
                    if totals and any(new < old for old, new in zip(totals, current, strict=True)):
                        _fail()
                    totals = current
                elif payload["type"] == "task_started":
                    if seen_started or seen_complete:
                        _fail()
                    seen_started = True
                elif payload["type"] == "task_complete":
                    if not seen_started or seen_complete:
                        _fail()
                    seen_complete = True
            elif kind == "response_item":
                subtype = payload.get("type")
                if (
                    not isinstance(subtype, str)
                    or subtype not in _RESPONSE_TYPES
                    or set(payload) not in _RESPONSE_ITEM_KEYS[subtype]
                ):
                    _fail()
            elif kind == "inter_agent_communication_metadata":
                if set(payload) != {"trigger_turn"}:
                    _fail()
            elif kind == "world_state" and set(payload) != {"full", "state"}:
                _fail()
    if not seen_meta or (is_subagent and (not seen_context or not seen_complete or totals is None)):
        _fail()
    return session, totals or (0, 0, 0, 0, 0, 0), parent_thread, matches


def _candidate_binding(fd: int, binding: dict[str, str]) -> bool | None:
    """Classify a first metadata row without rejecting an active unrelated rollout."""
    raw = b""
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.readline(MAX_EVENT_BYTES + 1)
        os.lseek(fd, 0, os.SEEK_SET)
        if len(raw) > MAX_EVENT_BYTES or not raw.endswith(b"\n"):
            return None
        event = _json(raw)
        if (
            set(event) != {"ordinal", "payload", "timestamp", "type"}
            or event.get("type") != "session_meta"
            or not isinstance(event.get("payload"), dict)
        ):
            return None
        payload = event["payload"]
        source = payload.get("source")
        if not isinstance(source, dict):
            return False
        spawn = (
            source.get("subagent", {}).get("thread_spawn")
            if isinstance(source.get("subagent"), dict)
            else None
        )
        if not isinstance(spawn, dict):
            return False
        return (
            type(spawn.get("depth")) is int
            and spawn.get("parent_thread_id") == binding["parent_thread_id"]
            and spawn.get("agent_path") == binding["agent_path"]
            and spawn.get("agent_role") == binding["role"]
        )
    except NativeCodexCaptureError:
        # A malformed row that already claims every bound identity must be rejected by
        # the full parser, rather than being mistaken for unrelated active activity.
        return all(value.encode("utf-8") in raw for value in binding.values())
    except OSError:
        # A descriptor race remains fail-closed when the full parser reopens it.
        return True


@dataclass(frozen=True)
class _CandidateMetadata:
    """Bounded first-row classification retained while selecting a leaf."""

    bound_leaf: bool | None
    parent_thread_id: str | None


def _candidate_metadata(fd: int, binding: dict[str, str]) -> _CandidateMetadata:
    """Read only first-row identity needed for the closed two-pass selection."""
    bound_leaf = _candidate_binding(fd, binding)
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.readline(MAX_EVENT_BYTES + 1)
        os.lseek(fd, 0, os.SEEK_SET)
        if len(raw) > MAX_EVENT_BYTES or not raw.endswith(b"\n"):
            return _CandidateMetadata(bound_leaf, None)
        event = _json(raw)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return _CandidateMetadata(bound_leaf, None)
        source = payload.get("source")
        if not isinstance(source, dict):
            return _CandidateMetadata(bound_leaf, None)
        subagent = source.get("subagent")
        if not isinstance(subagent, dict):
            return _CandidateMetadata(bound_leaf, None)
        spawn = subagent.get("thread_spawn")
        if not isinstance(spawn, dict):
            return _CandidateMetadata(bound_leaf, None)
        parent = spawn.get("parent_thread_id")
        return _CandidateMetadata(bound_leaf, parent if _safe_identifier(parent) else None)
    except (NativeCodexCaptureError, OSError):
        return _CandidateMetadata(bound_leaf, None)


def _new_rollouts(
    root: _Root, before: dict[str, tuple[int, int]]
) -> list[tuple[str, int, os.stat_result]]:
    result: list[tuple[str, int, os.stat_result]] = []
    total = 0
    try:
        for name, fd, status in _walk(root):
            if before.get(name) == (status.st_dev, status.st_ino):
                os.close(fd)
                continue
            if status.st_size > MAX_PROVIDER_FILE_BYTES:
                os.close(fd)
                _fail()
            total += status.st_size
            if total > MAX_PROVIDER_TOTAL_BYTES:
                os.close(fd)
                _fail()
            result.append((name, fd, status))
            if len(result) > MAX_PROVIDER_FILES:
                _fail()
    except BaseException:
        for _name, fd, _status in result:
            with suppress(OSError):
                os.close(fd)
        raise
    return result


def _ancestor_identities(root: _Root) -> set[tuple[int, int]]:
    """Return held descriptor ancestors without trusting mutable path strings."""
    result: set[tuple[int, int]] = set()
    descriptor = os.dup(root.descriptor)
    try:
        while True:
            current = os.fstat(descriptor)
            identity = (current.st_dev, current.st_ino)
            if identity in result:
                return result
            result.add(identity)
            parent = os.open(
                "..",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                parent_status = os.fstat(parent)
            except OSError:
                os.close(parent)
                raise
            finally:
                os.close(descriptor)
            if (parent_status.st_dev, parent_status.st_ino) == identity:
                os.close(parent)
                return result
            descriptor = parent
    except OSError:
        _fail()
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _reject_root_overlap(*roots: _Root) -> None:
    """Reject equal, ancestor, and descendant roots using held identities."""
    ancestors = [_ancestor_identities(root) for root in roots]
    identities = [(root.device, root.inode) for root in roots]
    for index, identity in enumerate(identities):
        for other_index in range(index + 1, len(identities)):
            if identity in ancestors[other_index] or identities[other_index] in ancestors[index]:
                _fail()


def _terminal_line() -> bytes:
    """Read exactly one already-framed terminal line without waiting for EOF."""
    descriptor = sys.stdin.fileno()
    line = os.read(descriptor, MAX_EVENT_BYTES + 1)
    if (
        not line
        or len(line) > MAX_EVENT_BYTES
        or not line.endswith(b"\n")
        or line.count(b"\n") != 1
    ):
        _fail()
    try:
        os.set_blocking(descriptor, False)
        try:
            if os.read(descriptor, 1):
                _fail()
        except BlockingIOError:
            pass
    except OSError:
        _fail()
    finally:
        with suppress(OSError):
            os.set_blocking(descriptor, True)
    return line


def _descendant(base: str, head: str) -> bool:
    if base == head:
        return False
    try:
        return (
            subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "merge-base", "--is-ancestor", base, head],
                check=False,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        _fail()


def _reattest_usage_root(root: _Root) -> None:
    """Require the canonical usage-root path to retain the held identity."""
    if not root.path:
        _fail()
    current: _Root | None = None
    try:
        current = _open_root(root.path, private=True)
        if (current.device, current.inode) != (root.device, root.inode):
            _fail()
    finally:
        if current is not None:
            os.close(current.descriptor)


def _provider_home() -> str:
    """Return the only provider-state homes admitted to native capture."""
    try:
        account_home = pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        _fail()
    if not isinstance(account_home, str) or not os.path.isabs(account_home):
        _fail()
    default = os.path.abspath(os.path.join(account_home, ".codex"))
    supplied = os.environ.get("CODEX_HOME")
    if supplied is None:
        return default
    if not os.path.isabs(supplied):
        _fail()
    canonical = os.path.abspath(supplied)
    if canonical in {default, "/opt/codex"}:
        return canonical
    _fail()


def supervise_native_codex(arguments: Any) -> int:
    """Run one parent-owned READY/status/result protocol without launching a child."""
    values = (arguments.task_id, arguments.slice_id, arguments.parent_task_id, arguments.task_name)
    parent = os.environ.get("CODEX_THREAD_ID")
    if not all(_safe_identifier(value) for value in values) or not _safe_identifier(parent):
        _fail()
    role = NativeCodexRole(arguments.role)
    codex_home = _provider_home()
    usage: _Root | None = None
    provider: _Root | None = None
    worktree: _Root | None = None
    try:
        usage = _open_root(arguments.usage_root, private=True)
        provider = _open_root(os.path.join(codex_home, "sessions"), private=False)
        worktree = _open_root(os.getcwd(), private=False)
        started = datetime.now(UTC)
        started_monotonic = time.monotonic()
        _reject_root_overlap(usage, provider, worktree)
        launch = _git_identity(
            arguments.repository, arguments.branch, arguments.base_sha, final=False
        )
        config_sha, role_sha, effort, canonical = _registered_role(worktree, role)
        before = _inventory(provider)
        binding = {
            "parent_thread_id": parent,
            "role": canonical,
            "agent_path": f"/root/{arguments.task_name}",
            "effort": effort,
        }
        ready = {"type": "READY", "binding_id": str(uuid4())}
        sys.stdout.write(json.dumps(ready, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        line = _terminal_line()
        terminal = _json(line)
        if (
            set(terminal) != {"type", "binding_id", "task_status"}
            or terminal["type"] != "TERMINAL"
            or terminal["binding_id"] != ready["binding_id"]
        ):
            _fail()
        status = NativeCodexTaskStatus(terminal["task_status"])
        _assert_root(provider)
        candidates = _new_rollouts(provider, before)
        classified: list[tuple[int, os.stat_result, _CandidateMetadata]] = []
        matched: list[tuple[int, str, tuple[int, int, int, int, int, int]]] = []
        observed_total = 0
        fully_scanned = True
        try:
            for _name, fd, _stat in candidates:
                try:
                    metadata = _candidate_metadata(fd, binding)
                    classified.append((fd, _stat, metadata))
                    if metadata.bound_leaf is not True:
                        continue
                    session, totals, _spawned_from, matches = _parse_rollout(fd, binding)
                    try:
                        observed_total += os.fstat(fd).st_size
                    except OSError:
                        # Test doubles and already-closed unrelated candidates have no live size.
                        observed_total += _stat.st_size
                    if observed_total > MAX_PROVIDER_TOTAL_BYTES:
                        _fail()
                    if matches:
                        matched.append((fd, session, totals))
                except NativeCodexCaptureError:
                    raise
            if len(matched) != 1:
                _fail()
            selected_fd, leaf, totals = matched[0]
            subagent_count = 0
            for fd, _stat, metadata in classified:
                if fd == selected_fd:
                    continue
                if metadata.parent_thread_id != leaf:
                    if metadata.bound_leaf is None:
                        fully_scanned = False
                    continue
                try:
                    _session, _child_totals, spawned_from, _matches = _parse_rollout(fd, binding)
                    try:
                        observed_total += os.fstat(fd).st_size
                    except OSError:
                        observed_total += _stat.st_size
                    if observed_total > MAX_PROVIDER_TOTAL_BYTES:
                        _fail()
                    if spawned_from != leaf:
                        _fail()
                    subagent_count += 1
                except NativeCodexCaptureError:
                    # A malformed or incomplete rollout that names the leaf might be a
                    # child. Capture remains useful, but whole-tree proof is withheld.
                    fully_scanned = False
        finally:
            for held_fd, _stat, _metadata in classified:
                with suppress(OSError):
                    os.close(held_fd)
        whole = fully_scanned and subagent_count == 0
        final = _git_identity(
            arguments.repository, arguments.branch, arguments.base_sha, final=True
        )
        success = status is NativeCodexTaskStatus.SUCCESS
        if not _descendant(arguments.base_sha, final) and final != arguments.base_sha:
            _fail()
        if success and final == arguments.base_sha:
            _fail()
        completed = datetime.now(UTC)
        record = NativeCodexUsageRecord(
            captured_at=completed,
            task_id=arguments.task_id,
            slice_id=arguments.slice_id,
            parent_task_id=arguments.parent_task_id,
            task_name=arguments.task_name,
            native_role=role,
            config_sha256=config_sha,
            role_sha256=role_sha,
            effort=effort,
            repository=arguments.repository,
            branch=arguments.branch,
            base_sha=arguments.base_sha,
            launch_head_sha=launch,
            final_head_sha=final,
            parent_thread_id=parent,
            leaf_session_id=leaf,
            exit_code=None,
            task_status=status,
            success=success,
            started_at=started,
            completed_at=completed,
            elapsed_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            input_tokens=totals[0],
            cached_input_tokens=totals[1],
            cache_write_input_tokens=totals[2],
            output_tokens=totals[3],
            reasoning_output_tokens=totals[4],
            total_tokens=totals[5],
            whole_tree_verified=whole,
            subagent_count=subagent_count,
        )
        from capture_usage_cli import append_record  # local avoids import cycle

        _reattest_usage_root(usage)
        append_record(arguments.output, record, root_descriptor=usage.descriptor)
        sys.stdout.write(
            json.dumps(
                {"type": "RESULT", "binding_id": ready["binding_id"], "success": success},
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
        return 0
    finally:
        for root in (worktree, provider, usage):
            if root is not None:
                with suppress(OSError):
                    os.close(root.descriptor)
