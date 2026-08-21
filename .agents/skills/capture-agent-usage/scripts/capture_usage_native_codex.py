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
import selectors
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import uuid4

from capture_usage_models import (
    NATIVE_CODEX_ACCEPTED_ORIGINS,
    NATIVE_CODEX_CONFIG_SHA256,
    NATIVE_CODEX_REPOSITORY,
    NATIVE_CODEX_ROLE_INSTRUCTION_SHA256,
    NATIVE_CODEX_ROLE_SHA256,
    NativeCodexRole,
    NativeCodexTaskStatus,
    NativeCodexUsageRecord,
)

MAX_PROVIDER_FILES = 4096
MAX_PROVIDER_ENTRIES = 4096
MAX_PROVIDER_DEPTH = 8
MAX_PROVIDER_FILE_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PROVIDER_LINES = 100_000
# Observed Codex 0.147.0 tool-output event: 1,006,736 bytes.  Keep this fixed
# two-MiB bound below the separate eight-MiB rollout-file cap.
MAX_EVENT_BYTES = 2 * 1024 * 1024
_GIT_ENV = {
    "PATH": os.defpath,
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
MAX_JSON_NESTING = 64
MAX_COMMITTED_FILE_BYTES = 64 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 5
MAX_CHECKOUT_ENTRIES = 262_144
MAX_CHECKOUT_DEPTH = 32
MAX_NPM_BIN_TARGET_BYTES = 4096
MAX_GIT_ADMIN_FILE_BYTES = 4096
MAX_GIT_ADMIN_ENTRIES = 8192
MAX_GIT_ADMIN_DEPTH = 16
# A linked worktree's shared common directory can change while another worktree
# commits. Require two clean scans, permitting only bounded transient churn.
GIT_ADMIN_SHARED_SCAN_ATTEMPTS = 4
GIT_ADMIN_SHARED_STABLE_SCANS = 2
_ROLE_EXPECTATIONS: dict[NativeCodexRole, tuple[str, str]] = {
    NativeCodexRole.ENGINEER_BE: ("agents/engineer-be.toml", "high"),
    NativeCodexRole.ENGINEER_FE: ("agents/engineer-fe.toml", "high"),
    NativeCodexRole.REPAIR: ("agents/repair.toml", "medium"),
}
_ASCII_PYTHON_VENV_LINK_NAME = re.compile(r"python(?:3(?:\.\d+)?)?")
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


def _python_venv_link_name(name: str) -> bool:
    """Accept only standard Python interpreter aliases for this runtime's venv layout."""
    return bool(_ASCII_PYTHON_VENV_LINK_NAME.fullmatch(name)) or (
        name == "𝜋thon"
        and sys.version_info[:2] == (3, 14)
        and sys.getfilesystemencoding() == "utf-8"
    )


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


_ACTIVE_GIT_WORKTREE: _Root | None = None


@dataclass(frozen=True)
class _GitFile:
    """One held Git-administration regular file and its immutable attestation."""

    descriptor: int
    device: int
    inode: int
    ctime_ns: int
    size: int
    digest: str


@dataclass(frozen=True)
class _GitAdministration:
    """Held Git administrative identities for a normal or linked worktree."""

    dotgit_file: _GitFile | None
    git_directory: _Root
    common_file: _GitFile | None
    common_directory: _Root | None


@dataclass(frozen=True)
class _InventoryEntry:
    """One pre-READY rollout descriptor and the generation it bound."""

    name: str
    descriptor: int
    device: int
    inode: int
    generation: tuple[int, int, int]
    size: int


@dataclass
class _Inventory:
    """Descriptor-owning pre-READY provider inventory."""

    entries: dict[tuple[int, int, int], _InventoryEntry]

    def __len__(self) -> int:
        """Return the number of bound pre-READY rollout generations."""
        return len(self.entries)

    def __iter__(self) -> Iterator[tuple[int, int, int]]:
        """Iterate bound generations for compatibility with set-like callers."""
        return iter(self.entries)

    def __eq__(self, other: object) -> bool:
        """Compare by generation only; descriptors intentionally remain private."""
        return set(self.entries) == other

    def close(self) -> None:
        """Close every retained provider descriptor exactly once."""
        for entry in self.entries.values():
            with suppress(OSError):
                _close(entry.descriptor)


def _fail() -> None:
    raise NativeCodexCaptureError("native Codex capture is invalid")


def _close(descriptor: int) -> None:
    """Close one owned descriptor through a module-local test seam."""
    os.close(descriptor)


def _fchdir(descriptor: int) -> None:
    """Bind a Git child to an inherited worktree descriptor."""
    os.fchdir(descriptor)


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


def _provider_directory(descriptor: int, *, expected: tuple[int, int] | None = None) -> None:
    """Require one provider-owned, non-writable directory descriptor."""
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink < 2
            or stat.S_IMODE(status.st_mode) & 0o022
            or (expected is not None and (status.st_dev, status.st_ino) != expected)
        ):
            _fail()
    except OSError:
        _fail()


def _generation(status: os.stat_result) -> tuple[int, int, int]:
    """Return the immutable-enough rollout generation identity for one scan."""
    return status.st_dev, status.st_ino, status.st_ctime_ns


def _walk(root: _Root) -> Iterator[tuple[str, int, os.stat_result]]:
    """Yield every regular rollout through no-follow directory descriptors."""

    entries_seen = 0

    def descend(
        directory: int, prefix: str, depth: int
    ) -> Iterator[tuple[str, int, os.stat_result]]:
        nonlocal entries_seen
        if depth > MAX_PROVIDER_DEPTH:
            _fail()
        _provider_directory(directory)
        scandir_descriptor = -1
        iterator: os.ScandirIterator[str] | None = None
        try:
            # Keep ownership of the duplicated descriptor explicit.  CPython currently
            # closes descriptor arguments from ScandirIterator.close(), but that is not
            # an attestation boundary this traversal relies upon.
            scandir_descriptor = os.dup(directory)
            iterator = os.scandir(scandir_descriptor)
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_PROVIDER_ENTRIES:
                    _fail()
                name = entry.name
                if name in {".", ".."} or "/" in name:
                    _fail()
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                except OSError:
                    _fail()
                status = os.fstat(child)
                relative = f"{prefix}/{name}" if prefix else name
                if stat.S_ISREG(status.st_mode):
                    if (
                        not name.endswith(".jsonl")
                        or status.st_uid != os.geteuid()
                        or status.st_nlink != 1
                        or stat.S_IMODE(status.st_mode) & 0o022
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
                        or stat.S_IMODE(status.st_mode) & 0o022
                    ):
                        _fail()
                    yield from descend(child, relative, depth + 1)
                finally:
                    os.close(child)
        except OSError:
            _fail()
        finally:
            if iterator is not None:
                with suppress(OSError):
                    iterator.close()
            if scandir_descriptor >= 0:
                with suppress(OSError):
                    _close(scandir_descriptor)

    duplicate = os.dup(root.descriptor)
    try:
        yield from descend(duplicate, "", 0)
    finally:
        os.close(duplicate)


def _inventory(root: _Root) -> _Inventory:
    """Bind every pre-READY rollout descriptor until final reconciliation."""
    _provider_directory(root.descriptor, expected=(root.device, root.inode))
    result: dict[tuple[int, int, int], _InventoryEntry] = {}
    try:
        for name, fd, status in _walk(root):
            generation = _generation(status)
            if generation in result or len(result) >= MAX_PROVIDER_FILES:
                os.close(fd)
                _fail()
            result[generation] = _InventoryEntry(
                name, fd, status.st_dev, status.st_ino, generation, status.st_size
            )
    except BaseException:
        _Inventory(result).close()
        raise
    return _Inventory(result)


def _inventory_generations(
    inventory: _Inventory | set[tuple[int, int, int]],
) -> set[tuple[int, int, int]]:
    """Return generations from real or test-double inventories."""
    return set(inventory)


def _close_inventory(inventory: _Inventory | set[tuple[int, int, int]]) -> None:
    """Close only descriptors owned by a production inventory."""
    if isinstance(inventory, _Inventory):
        inventory.close()


def _git(args: list[str], *, worktree: _Root | None = None) -> str:
    """Run Git with bounded live pipe draining and no provider diagnostics."""
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        command = ["git"]
        popen_kwargs: dict[str, Any] = {}
        environment = _GIT_ENV
        bound_worktree = worktree if worktree is not None else _ACTIVE_GIT_WORKTREE
        if bound_worktree is not None:
            _assert_worktree_root(bound_worktree)
            # Bind Git's discovery cwd through the held descriptor.  Explicitly
            # override any repository-local core.worktree with a fixed relative
            # worktree resolved from that descriptor-bound cwd.
            environment = {**_GIT_ENV, "GIT_WORK_TREE": "."}
            popen_kwargs["pass_fds"] = (bound_worktree.descriptor,)
            popen_kwargs["preexec_fn"] = partial(_fchdir, bound_worktree.descriptor)
        process = subprocess.Popen(
            [*command, "-c", "core.fsmonitor=false", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **popen_kwargs,
        )
        if process.stdout is None or process.stderr is None:
            _fail()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail()
            for key, _events in selector.select(remaining):
                chunk = os.read(key.fd, MAX_GIT_OUTPUT_BYTES + 1)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                target.extend(chunk)
                if len(target) > MAX_GIT_OUTPUT_BYTES:
                    _fail()
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            _fail()
        return stdout.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        _fail()
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                with suppress(OSError):
                    process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        process.kill()
                    with suppress(subprocess.SubprocessError):
                        process.wait(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()


def _git_identity(
    repository: str,
    branch: str,
    base: str,
    *,
    final: bool,
    worktree: _Root | None = None,
) -> str:
    if (
        repository != NATIVE_CODEX_REPOSITORY
        or not re.fullmatch(r"[0-9a-f]{40}", base)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch)
        or branch.startswith("-")
        or ".." in branch
    ):
        _fail()

    def invoke(args: list[str]) -> str:
        return _git(args) if worktree is None else _git(args, worktree=worktree)

    if invoke(["remote", "get-url", "origin"]) not in NATIVE_CODEX_ACCEPTED_ORIGINS:
        _fail()
    head = invoke(["rev-parse", "HEAD"])
    status_args = (
        ["status", "--porcelain", "--untracked-files=all", "--ignored"]
        if not final
        else ["status", "--porcelain", "--untracked-files=all"]
    )
    if invoke(["branch", "--show-current"]) != branch or invoke(status_args):
        _fail()
    _assert_index_clean(worktree)
    if invoke(["rev-parse", "HEAD"]) != head:
        _fail()
    if invoke(["rev-parse", "--verify", f"{base}^{{commit}}"]) != base or (
        not final and head != base
    ):
        _fail()
    return head


def _assert_index_clean(worktree: _Root | None = None) -> None:
    """Reject index flags that can hide tracked checkout changes."""
    rows = (
        _git(["ls-files", "-v", "-t", "-z"])
        if worktree is None
        else _git(["ls-files", "-v", "-t", "-z"], worktree=worktree)
    ).split("\0")
    if not rows or rows.pop() != "" or len(rows) > MAX_CHECKOUT_ENTRIES:
        _fail()
    for row in rows:
        if len(row) < 3 or row[1] != " ":
            _fail()
        marker = row[0]
        if marker in {"h", "s", "S"}:
            _fail()


def _checkout_directory(descriptor: int) -> None:
    """Require one owned, non-writable tracked-checkout directory."""
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink < 2
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            _fail()
    except OSError:
        _fail()


def _git_administration_directory(descriptor: int) -> None:
    """Require one owned, non-writable Git administrative directory."""
    _checkout_directory(descriptor)


def _open_git_file(directory: int, name: str) -> tuple[_GitFile, bytes]:
    """Open and attest a bounded Git administrative file without following links."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) & 0o022
            or initial.st_size > MAX_GIT_ADMIN_FILE_BYTES
        ):
            _fail()
        content = os.read(descriptor, MAX_GIT_ADMIN_FILE_BYTES + 1)
        final = os.fstat(descriptor)
        if (
            len(content) > MAX_GIT_ADMIN_FILE_BYTES
            or _generation(initial) != _generation(final)
            or initial.st_size != final.st_size
        ):
            _fail()
        result = _GitFile(
            descriptor,
            initial.st_dev,
            initial.st_ino,
            initial.st_ctime_ns,
            initial.st_size,
            hashlib.sha256(content).hexdigest(),
        )
        descriptor = None
        return result, content
    except OSError:
        _fail()
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _git_directory_path(content: bytes, base: str, *, marker: bytes) -> str:
    """Decode one closed Git path-pointer file without retaining its contents."""
    if not content.endswith(b"\n") or content.count(b"\n") != 1 or not content.startswith(marker):
        _fail()
    raw = content[len(marker) : -1]
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail()
    if not value or "\x00" in value:
        _fail()
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(base, value))


def _open_git_administration(worktree: _Root) -> _GitAdministration:
    """Hold the safe normal or linked-worktree Git administration topology."""
    dotgit_descriptor: int | None = None
    dotgit_file: _GitFile | None = None
    git_directory: _Root | None = None
    common_file: _GitFile | None = None
    common_directory: _Root | None = None
    try:
        dotgit_descriptor = os.open(
            ".git",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=worktree.descriptor,
        )
        status = os.fstat(dotgit_descriptor)
        if stat.S_ISDIR(status.st_mode):
            _git_administration_directory(dotgit_descriptor)
            git_directory = _Root(
                dotgit_descriptor,
                status.st_dev,
                status.st_ino,
                os.path.join(worktree.path, ".git"),
            )
            dotgit_descriptor = None
            result = _GitAdministration(None, git_directory, None, None)
            git_directory = None
            return result
        os.close(dotgit_descriptor)
        dotgit_descriptor = None
        dotgit_file, dotgit_content = _open_git_file(worktree.descriptor, ".git")
        git_directory = _open_root(
            _git_directory_path(dotgit_content, worktree.path, marker=b"gitdir: "), private=False
        )
        _git_administration_directory(git_directory.descriptor)
        common_file, common_content = _open_git_file(git_directory.descriptor, "commondir")
        common_directory = _open_root(
            _git_directory_path(common_content, git_directory.path, marker=b""), private=False
        )
        _git_administration_directory(common_directory.descriptor)
        result = _GitAdministration(dotgit_file, git_directory, common_file, common_directory)
        dotgit_file = None
        git_directory = None
        common_file = None
        common_directory = None
        return result
    except OSError:
        _fail()
    finally:
        if dotgit_descriptor is not None:
            with suppress(OSError):
                os.close(dotgit_descriptor)
        for descriptor in (dotgit_file, common_file):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor.descriptor)
        for root in (git_directory, common_directory):
            if root is not None:
                with suppress(OSError):
                    os.close(root.descriptor)


def _same_git_file(left: _GitFile | None, right: _GitFile | None) -> bool:
    """Compare Git administration file identity and bounded exact bytes by digest."""
    if left is None or right is None:
        return left is right
    return (
        left.device,
        left.inode,
        left.ctime_ns,
        left.size,
        left.digest,
    ) == (
        right.device,
        right.inode,
        right.ctime_ns,
        right.size,
        right.digest,
    )


def _assert_git_administration_tree(root: _Root) -> None:
    """Walk one Git administrative tree through bounded no-follow descriptors."""
    entries_seen = 0

    def descend(directory: int, depth: int) -> None:
        nonlocal entries_seen
        if depth > MAX_GIT_ADMIN_DEPTH:
            _fail()
        _git_administration_directory(directory)
        scandir_descriptor = os.dup(directory)
        iterator: os.ScandirIterator[str] | None = None
        try:
            iterator = os.scandir(scandir_descriptor)
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_GIT_ADMIN_ENTRIES or entry.name in {".", ".."}:
                    _fail()
                status = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(status.st_mode):
                    _fail()
                if stat.S_ISDIR(status.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                    try:
                        descend(child, depth + 1)
                    finally:
                        os.close(child)
                    continue
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or status.st_nlink != 1
                    or stat.S_IMODE(status.st_mode) & 0o022
                ):
                    _fail()
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    current = os.fstat(child)
                    if (
                        _generation(current) != _generation(status)
                        or current.st_size != status.st_size
                    ):
                        _fail()
                finally:
                    os.close(child)
        except OSError:
            _fail()
        finally:
            if iterator is not None:
                with suppress(OSError):
                    iterator.close()
            with suppress(OSError):
                _close(scandir_descriptor)

    descriptor = os.dup(root.descriptor)
    try:
        descend(descriptor, 0)
    finally:
        os.close(descriptor)


def _assert_git_administration_trees(administration: _GitAdministration) -> None:
    """Attest each distinct Git administration root exactly once."""
    _assert_git_administration_tree(administration.git_directory)
    common = administration.common_directory
    if common is None or (common.device, common.inode) == (
        administration.git_directory.device,
        administration.git_directory.inode,
    ):
        return
    stable_scans = 0
    for _attempt in range(GIT_ADMIN_SHARED_SCAN_ATTEMPTS):
        try:
            _assert_git_administration_tree(common)
        except NativeCodexCaptureError:
            stable_scans = 0
            continue
        stable_scans += 1
        if stable_scans == GIT_ADMIN_SHARED_STABLE_SCANS:
            return
    _fail()


def _assert_git_administration(worktree: _Root, expected: _GitAdministration) -> None:
    """Require the canonical Git administration topology to retain its held identities."""
    current: _GitAdministration | None = None
    try:
        current = _open_git_administration(worktree)
        _assert_git_administration_trees(current)
        if (
            not _same_git_file(current.dotgit_file, expected.dotgit_file)
            or (current.git_directory.device, current.git_directory.inode)
            != (expected.git_directory.device, expected.git_directory.inode)
            or not _same_git_file(current.common_file, expected.common_file)
            or (
                (current.common_directory is None) != (expected.common_directory is None)
                or (
                    current.common_directory is not None
                    and expected.common_directory is not None
                    and (current.common_directory.device, current.common_directory.inode)
                    != (expected.common_directory.device, expected.common_directory.inode)
                )
            )
        ):
            _fail()
    finally:
        if current is not None:
            _close_git_administration(current)


def _close_git_administration(administration: _GitAdministration) -> None:
    """Close the descriptors retained for Git-administration identity proof."""
    for file in (administration.dotgit_file, administration.common_file):
        if file is not None:
            with suppress(OSError):
                os.close(file.descriptor)
    for root in (administration.git_directory, administration.common_directory):
        if root is not None:
            with suppress(OSError):
                os.close(root.descriptor)


def _assert_checkout(root: _Root) -> None:
    """Attest every tracked file and containing directory through held descriptors."""
    _assert_worktree_root(root)
    _assert_checkout_directories(root)
    paths = _git(["ls-files", "-z"], worktree=root).split("\0")
    if not paths or paths.pop() != "" or len(paths) > MAX_CHECKOUT_ENTRIES:
        _fail()
    for path in paths:
        parts = path.split("/")
        if (
            not path
            or len(parts) > MAX_CHECKOUT_DEPTH
            or any(not part or part in {".", ".."} for part in parts)
        ):
            _fail()
        descriptor = os.dup(root.descriptor)
        try:
            _checkout_directory(descriptor)
            for part in parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                _checkout_directory(descriptor)
            file_descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            try:
                status = os.fstat(file_descriptor)
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or status.st_nlink != 1
                    or stat.S_IMODE(status.st_mode) & 0o022
                ):
                    _fail()
            finally:
                os.close(file_descriptor)
        except OSError:
            _fail()
        finally:
            with suppress(OSError):
                os.close(descriptor)


def _assert_npm_bin_symlink(
    root: _Root, directory: int, relative: str, name: str, status: os.stat_result
) -> None:
    """Attest one narrowly admitted npm executable symlink without resolving host paths."""
    parts = tuple(relative.split("/"))
    if (
        status.st_uid != os.geteuid()
        or len(parts) < 4
        or len(parts) > MAX_CHECKOUT_DEPTH
        or parts[:2] != ("web", "node_modules")
        or parts[-2] != ".bin"
        or not parts[-1]
        or any(not part or part in {".", ".."} for part in parts)
    ):
        _fail()
    try:
        target = os.readlink(name, dir_fd=directory)
        encoded_target = target.encode("utf-8")
        current_link = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except (OSError, UnicodeError):
        _fail()
    if (
        not target
        or "\x00" in target
        or target.startswith("/")
        or len(encoded_target) > MAX_NPM_BIN_TARGET_BYTES
        or not stat.S_ISLNK(current_link.st_mode)
        or current_link.st_uid != os.geteuid()
        or _generation(current_link) != _generation(status)
        or current_link.st_size != status.st_size
    ):
        _fail()
    target_parts = tuple(target.split("/"))
    if len(target_parts) > MAX_CHECKOUT_DEPTH or any(not part for part in target_parts):
        _fail()
    normalized = list(parts[:-1])
    for part in target_parts:
        if part == ".":
            continue
        if part == "..":
            if not normalized:
                _fail()
            normalized.pop()
            continue
        normalized.append(part)
        if len(normalized) > MAX_CHECKOUT_DEPTH:
            _fail()
    if len(normalized) < 3 or tuple(normalized[:2]) != ("web", "node_modules"):
        _fail()

    descriptor = os.dup(root.descriptor)
    try:
        root_status = os.fstat(descriptor)
        if (root_status.st_dev, root_status.st_ino) != (root.device, root.inode):
            _fail()
        _checkout_directory(descriptor)
        for part in normalized[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            _checkout_directory(descriptor)
        target_name = normalized[-1]
        initial = os.stat(target_name, dir_fd=descriptor, follow_symlinks=False)
        child = os.open(
            target_name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=descriptor,
        )
        try:
            current = os.fstat(child)
            final = os.stat(target_name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_uid != os.geteuid()
                or current.st_nlink != 1
                or stat.S_IMODE(current.st_mode) & 0o022
                or _generation(initial) != _generation(current)
                or initial.st_size != current.st_size
                or _generation(final) != _generation(current)
                or final.st_size != current.st_size
            ):
                _fail()
        finally:
            os.close(child)
    except OSError:
        _fail()
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _assert_checkout_directories(root: _Root) -> None:
    """Inspect every checkout directory except Git's separately attested administration."""
    entries_seen = 0

    def descend(directory: int, prefix: str, depth: int) -> None:
        nonlocal entries_seen
        if depth > MAX_CHECKOUT_DEPTH:
            _fail()
        _checkout_directory(directory)
        scandir_descriptor = os.dup(directory)
        iterator: os.ScandirIterator[str] | None = None
        try:
            iterator = os.scandir(scandir_descriptor)
            for entry in iterator:
                if entry.name in {".", ".."}:
                    _fail()
                if depth == 0 and entry.name == ".git":
                    continue
                entries_seen += 1
                if entries_seen > MAX_CHECKOUT_ENTRIES:
                    _fail()
                status = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if stat.S_ISLNK(status.st_mode):
                    linux_lib64 = prefix == ".venv" and entry.name == "lib64"
                    if not linux_lib64 and (
                        prefix != ".venv/bin" or not _python_venv_link_name(entry.name)
                    ):
                        _assert_npm_bin_symlink(root, directory, relative, entry.name, status)
                        continue
                    if status.st_uid != os.geteuid():
                        _fail()
                    target = os.readlink(entry.name, dir_fd=directory)
                    target_name = os.path.basename(target)
                    if (
                        not target
                        or "\x00" in target
                        or len(target.encode("utf-8")) > MAX_GIT_ADMIN_FILE_BYTES
                        or (linux_lib64 and target != "lib")
                        or (
                            not linux_lib64
                            and (
                                not _python_venv_link_name(target_name)
                                or (not os.path.isabs(target) and "/" in target)
                            )
                        )
                    ):
                        _fail()
                    if not linux_lib64 and os.path.isabs(target):
                        base_executable = getattr(sys, "_base_executable", None)
                        if (
                            not isinstance(base_executable, str)
                            or not base_executable
                            or "\x00" in base_executable
                            or not os.path.isabs(base_executable)
                        ):
                            base_executable = sys.executable
                        try:
                            target_status = os.stat(target, follow_symlinks=True)
                            base_status = os.stat(base_executable, follow_symlinks=True)
                            current_link = os.stat(
                                entry.name, dir_fd=directory, follow_symlinks=False
                            )
                        except OSError:
                            _fail()
                        if (
                            not stat.S_ISREG(target_status.st_mode)
                            or target_status.st_uid not in {os.geteuid(), 0}
                            or stat.S_IMODE(target_status.st_mode) & 0o022
                            or (target_status.st_dev, target_status.st_ino)
                            != (base_status.st_dev, base_status.st_ino)
                            or _generation(current_link) != _generation(status)
                            or current_link.st_size != status.st_size
                        ):
                            _fail()
                    continue
                if not stat.S_ISDIR(status.st_mode):
                    if (
                        not stat.S_ISREG(status.st_mode)
                        or status.st_uid != os.geteuid()
                        or status.st_nlink != 1
                        or stat.S_IMODE(status.st_mode) & 0o022
                    ):
                        _fail()
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                    try:
                        current = os.fstat(child)
                        if (
                            _generation(current) != _generation(status)
                            or current.st_size != status.st_size
                        ):
                            _fail()
                    finally:
                        os.close(child)
                    continue
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    descend(child, relative, depth + 1)
                finally:
                    os.close(child)
        except OSError:
            _fail()
        finally:
            if iterator is not None:
                with suppress(OSError):
                    iterator.close()
            with suppress(OSError):
                _close(scandir_descriptor)

    descriptor = os.dup(root.descriptor)
    try:
        descend(descriptor, "", 0)
    finally:
        os.close(descriptor)


def _attested_origin(worktree: _Root | None = None) -> str:
    """Return the exact accepted origin spelling for rollout provenance binding."""
    origin = (
        _git(["remote", "get-url", "origin"])
        if worktree is None
        else _git(["remote", "get-url", "origin"], worktree=worktree)
    )
    if origin not in NATIVE_CODEX_ACCEPTED_ORIGINS:
        _fail()
    return origin


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
            or config["project_doc_max_bytes"] != 131072
            or hashlib.sha256(config_bytes).hexdigest() != NATIVE_CODEX_CONFIG_SHA256
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
            or hashlib.sha256(role_bytes).hexdigest() != NATIVE_CODEX_ROLE_SHA256[role]
            or (
                hashlib.sha256(definition["developer_instructions"].encode("utf-8")).hexdigest()
                != NATIVE_CODEX_ROLE_INSTRUCTION_SHA256[role]
            )
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


def _instruction_digest(value: Any) -> str:
    """Return a bounded fingerprint without retaining provider instructions."""
    if not isinstance(value, str):
        _fail()
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_COMMITTED_FILE_BYTES:
        _fail()
    return hashlib.sha256(encoded).hexdigest()


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
    input_tokens, cached_input, _cache_write, output_tokens, reasoning_output, total = result
    if (
        cached_input > input_tokens
        or reasoning_output > output_tokens
        or total != input_tokens + output_tokens
    ):
        _fail()
    return result  # type: ignore[return-value]


def _leaf_launch_boundary(payload: dict[str, Any], binding: dict[str, str]) -> None:
    """Validate the exact 0.147.0 managed leaf authority without retaining paths."""
    worktree = binding.get("worktree_path")
    if worktree is None:
        return
    filesystem = {
        "kind": "restricted",
        "entries": [
            {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
            {"path": {"type": "path", "path": worktree}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "slash_tmp"}}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"},
            {
                "path": {"type": "path", "path": os.path.join(worktree, ".git")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
            {
                "path": {"type": "path", "path": os.path.join(worktree, ".agents")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
            {
                "path": {"type": "path", "path": os.path.join(worktree, ".codex")},
                "access": "read",
                "missing_path_behavior": "skip",
            },
        ],
    }
    if (
        payload.get("workspace_roots") != [worktree]
        or payload.get("sandbox_policy")
        != {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": False,
            "exclude_slash_tmp": False,
        }
        or payload.get("file_system_sandbox_policy") != filesystem
        or payload.get("permission_profile")
        != {"type": "managed", "file_system": filesystem, "network": "restricted"}
        or payload.get("approval_policy") != "on-request"
        or payload.get("approvals_reviewer") != "auto_review"
        or payload.get("realtime_active") is not False
        or payload.get("collaboration_mode")
        != {
            "mode": "default",
            "settings": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": binding["effort"],
                "developer_instructions": None,
            },
        }
    ):
        _fail()


def _parse_rollout(
    fd: int, binding: dict[str, str]
) -> tuple[str, tuple[int, int, int, int, int, int], str, bool, int]:
    """Stream and validate the admitted 0.147.0 metadata grammar only."""
    seen_meta = seen_context = seen_started = seen_complete = False
    session = ""
    parent_thread = ""
    matches = False
    is_subagent = False
    totals: tuple[int, int, int, int, int, int] | None = None
    turn_id: str | None = None
    try:
        status = os.fstat(fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            _fail()
    except OSError:
        _fail()
    with os.fdopen(os.dup(fd), "rb") as stream:
        number = 0
        expected_ordinal = 0
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
                or event["ordinal"] != expected_ordinal
            ):
                _fail()
            expected_ordinal += 1
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
                        or payload["multi_agent_version"] != "v2"
                        or payload["model_provider"] != "openai"
                    ):
                        _fail()
                    is_subagent = True
                    matches = (
                        parent_thread == binding["parent_thread_id"]
                        and spawn["depth"] == 1
                        and agent_path == binding["agent_path"]
                        and agent_role == binding["role"]
                    )
                    if matches and any(
                        (key == "worktree_path" and payload["cwd"] != expected)
                        or (key == "launch_head" and git["commit_hash"] != expected)
                        or (key == "branch" and git["branch"] != expected)
                        or (key == "repository_url" and git["repository_url"] != expected)
                        for key, expected in binding.items()
                        if key in {"worktree_path", "launch_head", "branch", "repository_url"}
                    ):
                        matches = False
                    if (
                        matches
                        and "instruction_sha256" in binding
                        and (
                            _instruction_digest(payload["base_instructions"])
                            != binding["instruction_sha256"]
                        )
                    ):
                        matches = False
                else:
                    _fail()
                session = _string(payload.get("id"))
                if (
                    session == ""
                    or _string(payload.get("session_id")) != session
                    or payload.get("originator") != "codex-tui"
                    or payload.get("cli_version") != "0.147.0"
                ):
                    _fail()
                seen_meta = True
            elif not seen_meta:
                _fail()
            elif kind == "turn_context":
                if (
                    not seen_meta
                    or seen_context
                    or seen_started
                    or seen_complete
                    or set(payload) != _TURN_CONTEXT_KEYS
                    or (
                        is_subagent
                        and (
                            payload.get("model") != "gpt-5.6-terra"
                            or payload.get("effort") != binding["effort"]
                            or payload.get("multi_agent_version") != "v2"
                            or (
                                "worktree_path" in binding
                                and payload.get("cwd") != binding["worktree_path"]
                            )
                        )
                    )
                ):
                    _fail()
                context_turn_id = _string(payload["turn_id"])
                if turn_id is not None and turn_id != context_turn_id:
                    _fail()
                turn_id = context_turn_id
                if is_subagent:
                    _leaf_launch_boundary(payload, binding)
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
                    event_turn_id = _string(payload["turn_id"])
                    if (
                        seen_started
                        or seen_complete
                        or (turn_id is not None and turn_id != event_turn_id)
                    ):
                        _fail()
                    turn_id = event_turn_id
                    seen_started = True
                elif payload["type"] == "task_complete":
                    event_turn_id = _string(payload["turn_id"])
                    if (
                        not seen_started
                        or not seen_context
                        or seen_complete
                        or (turn_id is not None and turn_id != event_turn_id)
                    ):
                        _fail()
                    turn_id = event_turn_id
                    seen_complete = True
                elif payload["type"] == "item_completed":
                    event_turn_id = _string(payload["turn_id"])
                    if turn_id is not None and turn_id != event_turn_id:
                        _fail()
                    turn_id = event_turn_id
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
    return session, totals or (0, 0, 0, 0, 0, 0), parent_thread, matches, read_bytes


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
            return None
        spawn = (
            source.get("subagent", {}).get("thread_spawn")
            if isinstance(source.get("subagent"), dict)
            else None
        )
        if not isinstance(spawn, dict):
            return None
        matches_identity = (
            spawn.get("parent_thread_id") == binding["parent_thread_id"]
            and spawn.get("agent_path") == binding["agent_path"]
            and spawn.get("agent_role") == binding["role"]
        )
        if not matches_identity:
            return False
        if type(spawn.get("depth")) is int and spawn["depth"] == 1:
            return True
        return None
    except NativeCodexCaptureError:
        # A newline-terminated malformed row cannot be proved unrelated from fragments.
        # Retain it as indeterminate so a selected sibling withholds whole-tree proof.
        return None
    except OSError:
        # A descriptor race remains fail-closed when the full parser reopens it.
        return True


@dataclass(frozen=True)
class _CandidateMetadata:
    """Bounded first-row classification retained while selecting a leaf."""

    bound_leaf: bool | None
    parent_thread_id: str | None
    consumed: int = 0


def _candidate_metadata(fd: int, binding: dict[str, str]) -> _CandidateMetadata:
    """Read only first-row identity needed for the closed two-pass selection."""
    raw = b""
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.readline(MAX_EVENT_BYTES + 1)
        os.lseek(fd, 0, os.SEEK_SET)
        if len(raw) > MAX_EVENT_BYTES or not raw.endswith(b"\n"):
            return _CandidateMetadata(None, None, len(raw))
        event = _json(raw)
        if (
            set(event) != {"ordinal", "payload", "timestamp", "type"}
            or event.get("type") != "session_meta"
            or not isinstance(event.get("payload"), dict)
        ):
            return _CandidateMetadata(None, None, len(raw))
        payload = event["payload"]
        top_parent = payload.get("parent_thread_id")
        retained_parent = top_parent if _safe_identifier(top_parent) else None
        source = payload.get("source")
        if not isinstance(source, dict):
            return _CandidateMetadata(None, retained_parent, len(raw))
        subagent = source.get("subagent")
        if not isinstance(subagent, dict):
            return _CandidateMetadata(None, retained_parent, len(raw))
        spawn = subagent.get("thread_spawn")
        if not isinstance(spawn, dict):
            return _CandidateMetadata(None, retained_parent, len(raw))
        parent = spawn.get("parent_thread_id")
        if retained_parent is not None and _safe_identifier(parent) and retained_parent != parent:
            return _CandidateMetadata(None, None, len(raw))
        matches_identity = (
            parent == binding["parent_thread_id"]
            and spawn.get("agent_path") == binding["agent_path"]
            and spawn.get("agent_role") == binding["role"]
        )
        bound_leaf: bool | None
        if not matches_identity:
            bound_leaf = False
        elif type(spawn.get("depth")) is int and spawn["depth"] == 1:
            bound_leaf = True
        else:
            bound_leaf = None
        return _CandidateMetadata(
            bound_leaf, parent if _safe_identifier(parent) else retained_parent, len(raw)
        )
    except (NativeCodexCaptureError, OSError):
        return _CandidateMetadata(None, None, len(raw))


def _new_rollouts(
    root: _Root, before: _Inventory | set[tuple[int, int, int]]
) -> list[tuple[str, int, os.stat_result]]:
    result: list[tuple[str, int, os.stat_result]] = []
    total = 0
    before_generations = _inventory_generations(before)
    before_inodes = {(device, inode) for device, inode, _ctime_ns in before_generations}
    try:
        for name, fd, status in _walk(root):
            generation = _generation(status)
            if generation in before_generations or (status.st_dev, status.st_ino) in before_inodes:
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


def _reconcile_rollouts(
    root: _Root,
    before: _Inventory | set[tuple[int, int, int]],
    candidates: list[tuple[str, int, os.stat_result]],
    relaxed_descriptors: frozenset[int] = frozenset(),
) -> None:
    """Prove pre-READY bindings and post-READY generations survived unchanged.

    A parent rollout is allowed to append after READY, so its retained descriptor
    checks device/inode and liveness rather than ctime or size.  The final path
    must nevertheless resolve through the provider tree to that same safe inode.
    The selected leaf is handled separately and remains size-immutable.
    """
    expected_names = {name for name, _fd, _status in candidates}
    observed: dict[str, os.stat_result] = {}
    final_inventory: _Inventory | set[tuple[int, int, int]] | None = None
    try:
        final_inventory = _inventory(root)
        if not isinstance(final_inventory, _Inventory):
            _fail()
        for entry in final_inventory.entries.values():
            observed[entry.name] = os.fstat(entry.descriptor)
        if isinstance(before, _Inventory):
            for entry in before.entries.values():
                held = os.fstat(entry.descriptor)
                path_status = observed.get(entry.name)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or held.st_uid != os.geteuid()
                    or held.st_nlink <= 0
                    or (held.st_dev, held.st_ino) != (entry.device, entry.inode)
                    or path_status is None
                    or not stat.S_ISREG(path_status.st_mode)
                    or path_status.st_uid != os.geteuid()
                    or path_status.st_nlink <= 0
                    or (path_status.st_dev, path_status.st_ino) != (entry.device, entry.inode)
                ):
                    _fail()
        observed_new = {
            name: status
            for name, status in observed.items()
            if _generation(status) not in _inventory_generations(before)
            and (status.st_dev, status.st_ino)
            not in {(device, inode) for device, inode, _ctime in _inventory_generations(before)}
        }
        if set(observed_new) != expected_names:
            _fail()
        for name, descriptor, initial in candidates:
            current = observed_new[name]
            if descriptor not in relaxed_descriptors:
                if _generation(current) != _generation(initial):
                    _fail()
                continue
            held = os.fstat(descriptor)
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.geteuid()
                or held.st_nlink <= 0
                or stat.S_IMODE(held.st_mode) & 0o022
                or (held.st_dev, held.st_ino) != (initial.st_dev, initial.st_ino)
                or held.st_size < initial.st_size
                or not stat.S_ISREG(current.st_mode)
                or current.st_uid != os.geteuid()
                or current.st_nlink <= 0
                or stat.S_IMODE(current.st_mode) & 0o022
                or (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino)
                or current.st_size < initial.st_size
            ):
                _fail()
    except OSError:
        _fail()
    finally:
        if final_inventory is not None:
            _close_inventory(final_inventory)


def _assert_selected_rollout(
    root: _Root,
    descriptor: int,
    generation: tuple[int, int, int],
    size: int,
    expected_inventory: set[tuple[int, int, int]],
    name: str | None = None,
    before: _Inventory | set[tuple[int, int, int]] | None = None,
    candidates: list[tuple[str, int, os.stat_result]] | None = None,
    relaxed_descriptors: frozenset[int] = frozenset(),
) -> None:
    """Require selected usage evidence to remain the exact parsed provider file."""
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) & 0o022
            or _generation(status) != generation
            or status.st_size != size
        ):
            _fail()
    except OSError:
        _fail()
    if name is not None:
        found = False
        try:
            for current_name, current_fd, current_status in _walk(root):
                try:
                    if current_name == name:
                        found = True
                        if (
                            _generation(current_status) != generation
                            or current_status.st_size != size
                            or current_status.st_nlink <= 0
                        ):
                            _fail()
                finally:
                    os.close(current_fd)
            if not found:
                _fail()
        except OSError:
            _fail()
    _reattest_provider_root(root)
    if before is not None and candidates is not None:
        _reconcile_rollouts(root, before, candidates, relaxed_descriptors)
        return
    final_inventory = _inventory(root)
    try:
        if _inventory_generations(final_inventory) != expected_inventory:
            _fail()
    finally:
        _close_inventory(final_inventory)


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


def _assert_worktree_root(root: _Root) -> None:
    """Require the held assigned worktree to remain owned and non-writable."""
    try:
        status = os.fstat(root.descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or (status.st_dev, status.st_ino) != (root.device, root.inode)
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            _fail()
    except OSError:
        _fail()


def _reattest_worktree_root(root: _Root) -> None:
    """Require the canonical worktree path to still resolve to its held directory."""
    reopened: _Root | None = None
    try:
        _assert_worktree_root(root)
        reopened = _open_root(root.path, private=False)
        _assert_worktree_root(reopened)
        if (reopened.device, reopened.inode) != (root.device, root.inode):
            _fail()
    finally:
        if reopened is not None:
            with suppress(OSError):
                os.close(reopened.descriptor)


def _reattest_provider_root(root: _Root) -> None:
    """Require the canonical provider sessions path to retain its held identity."""
    if not root.path:
        _fail()
    reopened: _Root | None = None
    try:
        reopened = _open_root(root.path, private=False)
        _provider_directory(reopened.descriptor, expected=(root.device, root.inode))
    finally:
        if reopened is not None:
            with suppress(OSError):
                os.close(reopened.descriptor)


def _terminal_line() -> bytes:
    """Read exactly one terminal line and require an already-observed EOF."""
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
            if os.read(descriptor, 1) != b"":
                _fail()
        except BlockingIOError:
            _fail()
    except OSError:
        _fail()
    finally:
        with suppress(OSError):
            os.set_blocking(descriptor, True)
    return line


def _descendant(base: str, head: str, *, worktree: _Root | None = None) -> bool:
    if base == head:
        return False
    try:
        command = ["git"]
        run_kwargs: dict[str, Any] = {}
        environment = _GIT_ENV
        bound_worktree = worktree if worktree is not None else _ACTIVE_GIT_WORKTREE
        if bound_worktree is not None:
            _assert_worktree_root(bound_worktree)
            environment = {**_GIT_ENV, "GIT_WORK_TREE": "."}
            run_kwargs["pass_fds"] = (bound_worktree.descriptor,)
            run_kwargs["preexec_fn"] = partial(_fchdir, bound_worktree.descriptor)
        return (
            subprocess.run(
                [
                    *command,
                    "-c",
                    "core.fsmonitor=false",
                    "merge-base",
                    "--is-ancestor",
                    base,
                    head,
                ],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                **run_kwargs,
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
    global _ACTIVE_GIT_WORKTREE
    values = (arguments.task_id, arguments.slice_id, arguments.parent_task_id, arguments.task_name)
    parent = os.environ.get("CODEX_THREAD_ID")
    if not all(_safe_identifier(value) for value in values) or not _safe_identifier(parent):
        _fail()
    role = NativeCodexRole(arguments.role)
    codex_home = _provider_home()
    usage: _Root | None = None
    provider: _Root | None = None
    worktree: _Root | None = None
    git_administration: _GitAdministration | None = None
    selected_fd: int | None = None
    selected_generation: tuple[int, int, int] | None = None
    selected_size = 0
    selected_name: str | None = None
    relaxed_descriptors: set[int] = set()
    before: _Inventory | set[tuple[int, int, int]] | None = None
    prior_git_worktree = _ACTIVE_GIT_WORKTREE
    try:
        usage = _open_root(arguments.usage_root, private=True)
        provider = _open_root(os.path.join(codex_home, "sessions"), private=False)
        worktree = _open_root(os.getcwd(), private=False)
        _ACTIVE_GIT_WORKTREE = worktree
        _reject_root_overlap(usage, provider, worktree)
        _assert_worktree_root(worktree)
        git_administration = _open_git_administration(worktree)
        _assert_git_administration_trees(git_administration)
        _assert_checkout(worktree)
        launch = _git_identity(
            arguments.repository,
            arguments.branch,
            arguments.base_sha,
            final=False,
        )
        config_sha, role_sha, effort, canonical = _registered_role(worktree, role)
        before = _inventory(provider)
        origin = _attested_origin()
        binding = {
            "parent_thread_id": parent,
            "role": canonical,
            "agent_path": f"/root/{arguments.task_name}",
            "effort": effort,
            "worktree_path": worktree.path,
            "launch_head": launch,
            "repository_url": origin,
            "branch": arguments.branch,
            "instruction_sha256": NATIVE_CODEX_ROLE_INSTRUCTION_SHA256[role],
        }
        _assert_checkout(worktree)
        _assert_git_administration(worktree, git_administration)
        if (
            _git_identity(
                arguments.repository,
                arguments.branch,
                arguments.base_sha,
                final=False,
            )
            != launch
            or _attested_origin() != origin
        ):
            _fail()
        ready = {"type": "READY", "binding_id": str(uuid4())}
        started = datetime.now(UTC)
        started_monotonic = time.monotonic()
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
        elapsed_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))
        # Wall time may regress while the parent waits; retain a coherent audit
        # interval derived solely from the READY-to-terminal monotonic duration.
        completed = started + timedelta(milliseconds=elapsed_ms)
        _assert_root(provider)
        candidates = _new_rollouts(provider, before)
        classified: list[tuple[int, os.stat_result, _CandidateMetadata]] = []
        matched: list[tuple[str, int, str, tuple[int, int, int, int, int, int]]] = []
        observed_total = 0
        fully_scanned = True
        subagent_count = 0
        whole = False
        expected_inventory: set[tuple[int, int, int]] = set()
        try:
            for _name, fd, _stat in candidates:
                try:
                    metadata = _candidate_metadata(fd, binding)
                    classified.append((fd, _stat, metadata))
                    observed_total += metadata.consumed
                    if observed_total > MAX_PROVIDER_TOTAL_BYTES:
                        _fail()
                    if metadata.bound_leaf is not True:
                        continue
                    session, totals, _spawned_from, matches, consumed = _parse_rollout(fd, binding)
                    observed_total += consumed
                    if observed_total > MAX_PROVIDER_TOTAL_BYTES:
                        _fail()
                    if not matches:
                        _fail()
                    matched.append((_name, fd, session, totals))
                except NativeCodexCaptureError:
                    raise
            if len(matched) != 1:
                _fail()
            selected_name, selected_fd, leaf, totals = matched[0]
            selected_status = next(
                status for fd, status, _metadata in classified if fd == selected_fd
            )
            selected_generation = _generation(selected_status)
            selected_size = selected_status.st_size
            for fd, _stat, metadata in classified:
                if fd == selected_fd:
                    continue
                if (
                    metadata.bound_leaf is False
                    and metadata.parent_thread_id is not None
                    and metadata.parent_thread_id != leaf
                ):
                    relaxed_descriptors.add(fd)
                if metadata.parent_thread_id != leaf:
                    if metadata.bound_leaf is None:
                        fully_scanned = False
                    continue
                try:
                    _session, _child_totals, _spawned_from, _matches, consumed = _parse_rollout(
                        fd, binding
                    )
                    observed_total += consumed
                    if observed_total > MAX_PROVIDER_TOTAL_BYTES:
                        _fail()
                except NativeCodexCaptureError:
                    # Metadata already proves this rollout names the selected leaf as
                    # parent. Its incomplete or malformed body is therefore an
                    # unaccounted child, never a recordable successful lifecycle.
                    _fail()
                else:
                    # A fully parsed candidate routed here either proves a child or
                    # contradicts its first-row parent binding; both are fail-closed.
                    _fail()
            _reattest_provider_root(provider)
            # Reconcile by retained descriptors, not a second unbound snapshot: an
            # already-existing parent rollout may append after READY, but it may not
            # be replaced, unlinked, or rebound to another provider-tree path.
            expected_inventory = _inventory_generations(before) | {
                _generation(status) for _fd, status, _metadata in classified
            }
            if isinstance(before, _Inventory):
                # Bracket persistence with identity-aware complete scans. A
                # pre-READY parent may grow, but cannot disappear, rename, or
                # rebind; every post-READY candidate is generation-exact.
                _reconcile_rollouts(provider, before, candidates, frozenset(relaxed_descriptors))
                _reconcile_rollouts(provider, before, candidates, frozenset(relaxed_descriptors))
            else:
                # Existing test doubles model only generation sets. Production always
                # reaches the descriptor-owning branch above.
                closing = _inventory(provider)
                try:
                    if _inventory_generations(closing) != expected_inventory:
                        _fail()
                finally:
                    _close_inventory(closing)
                final_test_double_inventory = _inventory(provider)
                try:
                    if _inventory_generations(final_test_double_inventory) != expected_inventory:
                        _fail()
                finally:
                    _close_inventory(final_test_double_inventory)
            whole = fully_scanned and subagent_count == 0
        finally:
            for held_fd, _stat, _metadata in classified:
                if held_fd == selected_fd or held_fd in relaxed_descriptors:
                    continue
                with suppress(OSError):
                    _close(held_fd)
        _reattest_worktree_root(worktree)
        _assert_checkout(worktree)
        _assert_git_administration(worktree, git_administration)
        final = _git_identity(
            arguments.repository,
            arguments.branch,
            arguments.base_sha,
            final=True,
        )
        success = status is NativeCodexTaskStatus.SUCCESS
        if not _descendant(arguments.base_sha, final) and final != arguments.base_sha:
            _fail()
        if success and final == arguments.base_sha:
            _fail()
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
            elapsed_ms=elapsed_ms,
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

        if selected_fd is None or selected_generation is None:
            _fail()
        _assert_selected_rollout(
            provider,
            selected_fd,
            selected_generation,
            selected_size,
            expected_inventory,
            selected_name,
            before,
            candidates,
            frozenset(relaxed_descriptors),
        )
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
        _ACTIVE_GIT_WORKTREE = prior_git_worktree
        if before is not None:
            _close_inventory(before)
        if selected_fd is not None:
            with suppress(OSError):
                _close(selected_fd)
        for descriptor in relaxed_descriptors:
            if descriptor != selected_fd:
                with suppress(OSError):
                    _close(descriptor)
        if git_administration is not None:
            _close_git_administration(git_administration)
        for root in (worktree, provider, usage):
            if root is not None:
                with suppress(OSError):
                    os.close(root.descriptor)
