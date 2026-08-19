"""Fail-closed, long-lived metadata capture for registered Codex leaves.

The parent starts ``supervise-native-codex`` before dispatching its named leaf.
This process keeps descriptor-relative bindings open, writes one READY frame,
then accepts exactly one terminal task status on stdin.  It never launches Codex.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tomllib
from collections.abc import Iterator
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
MAX_EVENT_BYTES = 256 * 1024
_CONFIG_NAME = ".codex/config.toml"
_ROLE_EXPECTATIONS: dict[NativeCodexRole, tuple[str, str]] = {
    NativeCodexRole.ENGINEER_BE: ("agents/engineer-be.toml", "high"),
    NativeCodexRole.ENGINEER_FE: ("agents/engineer-fe.toml", "high"),
    NativeCodexRole.REPAIR: ("agents/repair.toml", "medium"),
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


class NativeCodexCaptureError(ValueError):
    """Raised with a fixed, content-free native-Codex capture failure."""


@dataclass(frozen=True)
class _Root:
    """One held directory descriptor and stable identity."""

    descriptor: int
    device: int
    inode: int


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
    try:
        if not os.path.isabs(raw):
            _fail()
        fd = os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        status = os.fstat(fd)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            os.close(fd)
            _fail()
        if private and stat.S_IMODE(status.st_mode) != 0o700:
            os.close(fd)
            _fail()
        return _Root(fd, status.st_dev, status.st_ino)
    except OSError:
        _fail()


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
            if stat.S_ISDIR(status.st_mode):
                if status.st_uid != os.geteuid() or status.st_nlink < 2:
                    os.close(child)
                    _fail()
                yield from descend(child, relative, depth + 1)
                os.close(child)
            elif stat.S_ISREG(status.st_mode):
                if (
                    not name.endswith(".jsonl")
                    or status.st_uid != os.geteuid()
                    or status.st_nlink != 1
                ):
                    os.close(child)
                    _fail()
                yield relative, child, status
            else:
                os.close(child)
                _fail()

    yield from descend(os.dup(root.descriptor), "", 0)


def _inventory(root: _Root) -> dict[str, tuple[int, int]]:
    """Snapshot immutable provider rollout identities, not mutable file sizes."""
    result: dict[str, tuple[int, int]] = {}
    total = 0
    for name, fd, status in _walk(root):
        try:
            if status.st_size > MAX_PROVIDER_FILE_BYTES:
                _fail()
            total += status.st_size
            result[name] = (status.st_dev, status.st_ino)
            if len(result) > MAX_PROVIDER_FILES or total > MAX_PROVIDER_TOTAL_BYTES:
                _fail()
        finally:
            os.close(fd)
    return result


def _git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args], check=False, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            _fail()
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        _fail()


def _git_identity(repository: str, branch: str, base: str, *, final: bool) -> str:
    if repository != "syamaner/roastpilot-agent":
        _fail()
    if _git(["remote", "get-url", "origin"]) not in {
        "https://github.com/syamaner/roastpilot-agent.git",
        "git@github.com:syamaner/roastpilot-agent.git",
    }:
        _fail()
    head = _git(["rev-parse", "HEAD"])
    if _git(["branch", "--show-current"]) != branch or _git(["status", "--porcelain"]):
        _fail()
    if _git(["rev-parse", "--verify", f"{base}^{{commit}}"]) != base or (
        not final and head != base
    ):
        _fail()
    return head


def _registered_role(role: NativeCodexRole) -> tuple[str, str, str, str]:
    """Parse and attest the complete registered-Codex role closure."""
    try:
        with open(_CONFIG_NAME, "rb") as config_file:
            config_bytes = config_file.read()
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
        with open(os.path.join(".codex", relpath), "rb") as role_file:
            role_bytes = role_file.read()
        definition = tomllib.loads(role_bytes.decode("utf-8"))
        if (
            set(definition)
            != {"model", "model_reasoning_effort", "developer_instructions", "agents"}
            or definition.get("model") != "gpt-5.6-terra"
            or definition.get("model_reasoning_effort") != effort
            or definition.get("agents") != {"enabled": False}
            or not isinstance(definition.get("developer_instructions"), str)
            or "invoke Claude Code or any other model" not in definition["developer_instructions"]
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

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            _fail()
        return value
    except (UnicodeDecodeError, json.JSONDecodeError):
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
    seen_meta = seen_context = seen_complete = False
    session = ""
    parent_thread = ""
    matches = False
    totals: tuple[int, int, int, int, int, int] | None = None
    with os.fdopen(os.dup(fd), "rb") as stream:
        for number, raw in enumerate(stream, 1):
            if number > MAX_PROVIDER_LINES or len(raw) > MAX_EVENT_BYTES or not raw.endswith(b"\n"):
                _fail()
            event = _json(raw)
            if (
                set(event) != {"type", "payload"}
                or event["type"] not in _ROOT_TYPES
                or not isinstance(event["payload"], dict)
            ):
                _fail()
            kind, payload = event["type"], event["payload"]
            if kind == "session_meta":
                if seen_meta or seen_context:
                    _fail()
                source, git = payload.get("source"), payload.get("git")
                if (
                    not isinstance(source, dict)
                    or not isinstance(git, dict)
                    or set(source) != {"subagent"}
                    or set(source["subagent"]) != {"thread_spawn"}
                ):
                    _fail()
                spawn = source["subagent"]["thread_spawn"]
                if not isinstance(spawn, dict) or set(spawn) != {
                    "parent_thread_id",
                    "depth",
                    "agent_path",
                    "agent_nickname",
                    "agent_role",
                }:
                    _fail()
                parent_thread = _string(spawn["parent_thread_id"])
                agent_path = _agent_path(spawn["agent_path"])
                agent_role = _string(spawn["agent_role"])
                if not _safe_identifier(spawn["agent_nickname"]):
                    _fail()
                matches = (
                    parent_thread == binding["parent_thread_id"]
                    and spawn["depth"] == 1
                    and agent_path == binding["agent_path"]
                    and agent_role == binding["role"]
                )
                if (
                    _string(payload.get("id")) == ""
                    or payload.get("originator") != "codex-tui"
                    or payload.get("cli_version") != "0.147.0"
                ):
                    _fail()
                session = _string(payload["id"])
                seen_meta = True
            elif kind == "turn_context":
                if (
                    not seen_meta
                    or seen_context
                    or payload.get("model") != "gpt-5.6-terra"
                    or payload.get("effort") != binding["effort"]
                    or len(payload) != 19
                ):
                    _fail()
                seen_context = True
            elif kind == "event_msg":
                if (
                    not seen_context
                    or set(payload) != {"type", "info"}
                    or payload["type"] not in _EVENT_TYPES
                ):
                    _fail()
                if payload["type"] == "token_count":
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
                elif payload["type"] == "task_complete":
                    seen_complete = True
            elif kind == "response_item":
                subtype = payload.get("type")
                if subtype not in _RESPONSE_TYPES:
                    _fail()
            elif kind in {"inter_agent_communication_metadata", "world_state"}:
                if not payload:
                    _fail()
    if not seen_meta or not seen_context or not seen_complete or totals is None:
        _fail()
    return session, totals, parent_thread, matches


def _new_rollouts(
    root: _Root, before: dict[str, tuple[int, int]]
) -> list[tuple[str, int, os.stat_result]]:
    result: list[tuple[str, int, os.stat_result]] = []
    total = 0
    for name, fd, status in _walk(root):
        if status.st_size > MAX_PROVIDER_FILE_BYTES:
            os.close(fd)
            _fail()
        total += status.st_size
        if total > MAX_PROVIDER_TOTAL_BYTES:
            os.close(fd)
            _fail()
        if before.get(name) != (status.st_dev, status.st_ino):
            result.append((name, fd, status))
        else:
            os.close(fd)
    if len(result) > MAX_PROVIDER_FILES:
        _fail()
    return result


def _descendant(base: str, head: str) -> bool:
    return (
        base != head
        and subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head], check=False
        ).returncode
        == 0
    )


def supervise_native_codex(arguments: Any) -> int:
    """Run one parent-owned READY/status/result protocol without launching a child."""
    if "CODEX_HOME" in os.environ:
        _fail()
    values = (arguments.task_id, arguments.slice_id, arguments.parent_task_id, arguments.task_name)
    parent = os.environ.get("CODEX_THREAD_ID")
    if not all(_safe_identifier(value) for value in values) or not _safe_identifier(parent):
        _fail()
    role = NativeCodexRole(arguments.role)
    usage = _open_root(arguments.usage_root, private=True)
    provider = _open_root(os.path.expanduser("~/.codex/sessions"), private=False)
    started = datetime.now(UTC)
    try:
        if os.path.commonpath(
            [os.path.realpath(arguments.usage_root), os.path.realpath(os.getcwd())]
        ) in {os.path.realpath(arguments.usage_root), os.path.realpath(os.getcwd())}:
            _fail()
        launch = _git_identity(
            arguments.repository, arguments.branch, arguments.base_sha, final=False
        )
        config_sha, role_sha, effort, canonical = _registered_role(role)
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
        line = sys.stdin.buffer.readline(MAX_EVENT_BYTES)
        if not line or not line.endswith(b"\n") or sys.stdin.buffer.readline(1):
            _fail()
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
        matched: list[tuple[int, str, tuple[int, int, int, int, int, int]]] = []
        child_parents: list[str] = []
        for _name, fd, _stat in candidates:
            try:
                session, totals, spawned_from, matches = _parse_rollout(fd, binding)
                child_parents.append(spawned_from)
                if matches:
                    matched.append((fd, session, totals))
                else:
                    os.close(fd)
            except NativeCodexCaptureError:
                os.close(fd)
                raise
        if len(matched) != 1:
            _fail()
        selected_fd, leaf, totals = matched[0]
        os.close(selected_fd)
        # Every new rollout was fully parsed; a spawned child is therefore visible.
        whole = leaf not in child_parents
        final = _git_identity(
            arguments.repository, arguments.branch, arguments.base_sha, final=True
        )
        success = status is NativeCodexTaskStatus.SUCCESS
        if (success and not _descendant(arguments.base_sha, final)) or (
            not success and final != arguments.base_sha
        ):
            _fail()
        completed = datetime.now(UTC)
        record = NativeCodexUsageRecord(
            captured_at=completed,
            task_id=arguments.task_id,
            slice_id=arguments.slice_id,
            parent_task_id=arguments.parent_task_id,
            task_name=arguments.task_name,
            native_role=role,
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
            elapsed_ms=max(0, int((completed - started).total_seconds() * 1000)),
            input_tokens=totals[0],
            cached_input_tokens=totals[1],
            cache_write_input_tokens=totals[2],
            output_tokens=totals[3],
            reasoning_output_tokens=totals[4],
            total_tokens=totals[5],
            whole_tree_verified=whole,
        )
        from capture_usage_cli import append_record  # local avoids import cycle

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
        os.close(provider.descriptor)
        os.close(usage.descriptor)
