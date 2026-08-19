# ruff: noqa: E501
"""Fail-closed metadata capture for registered Codex leaf rollouts.

This module deliberately does not launch a worker.  The top-level Codex parent
calls :func:`prepare_native_codex` immediately before its named-role dispatch,
then calls :func:`finalize_native_codex` after that child exits.  Keeping the
provider launch outside this utility prevents a generic ``codex exec`` command
from being mistaken for registered-agent dispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from capture_usage_models import (
    NativeCodexRole,
    NativeCodexTaskStatus,
    NativeCodexUsageRecord,
)

MAX_PROVIDER_FILES = 4096
MAX_PROVIDER_DEPTH = 8
MAX_PROVIDER_FILE_BYTES = 2_097_152
MAX_PROVIDER_TOTAL_BYTES = 16_777_216
MAX_PROVIDER_LINES = 20_000
_CONFIG_NAME = ".codex/config.toml"
_ROLE_EXPECTATIONS: dict[NativeCodexRole, tuple[str, str]] = {
    NativeCodexRole.ENGINEER_BE: ("agents/engineer-be.toml", "high"),
    NativeCodexRole.ENGINEER_FE: ("agents/engineer-fe.toml", "high"),
    NativeCodexRole.REPAIR: ("agents/repair.toml", "medium"),
}


class NativeCodexCaptureError(ValueError):
    """Raised with a fixed, content-free native-Codex capture failure."""


@dataclass(frozen=True)
class _Root:
    """An owned private directory identity retained for one lifecycle step."""

    descriptor: int
    device: int
    inode: int


def _fail() -> None:
    """Raise the one fixed native-Codex capture error."""
    raise NativeCodexCaptureError("native Codex capture is invalid")


def _safe_identifier(value: str) -> bool:
    """Return whether a caller value is compatible with the persisted grammar."""
    return (
        bool(value)
        and len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "._:-")
            for character in value
        )
    )


def _open_private_root(raw: str) -> _Root:
    """Open an external, owned, exact-0700 root without following links."""
    try:
        path = Path(raw)
        if not path.is_absolute() or path.is_symlink():
            _fail()
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o700
        ):
            os.close(descriptor)
            _fail()
        return _Root(descriptor, status.st_dev, status.st_ino)
    except (OSError, ValueError):
        _fail()


def _reattest_root(root: _Root) -> None:
    """Require an open root descriptor to retain its original directory identity."""
    try:
        status = os.fstat(root.descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o700
            or (status.st_dev, status.st_ino) != (root.device, root.inode)
        ):
            _fail()
    except OSError:
        _fail()


def _read_fd(descriptor: int, name: str) -> bytes:
    """Read one small regular no-follow child through an already-held root."""
    try:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:  # noqa: SIM105 - the next operation needs the same held descriptor
            status = os.fstat(child)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_uid != os.geteuid()
                or status.st_size > MAX_PROVIDER_FILE_BYTES
            ):
                _fail()
            chunks: list[bytes] = []
            while True:
                chunk = os.read(child, 65_536)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > MAX_PROVIDER_FILE_BYTES:
                    _fail()
            return b"".join(chunks)
        finally:
            os.close(child)
    except OSError:
        _fail()


def _sha256(data: bytes) -> str:
    """Return a hex digest without retaining source bytes in a record."""
    return hashlib.sha256(data).hexdigest()


def _git(args: list[str]) -> str:
    """Read one bounded Git identity result without accepting command failure."""
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
    """Attest the local repository independently at prepare and finalize."""
    if repository != "syamaner/roastpilot-agent":
        _fail()
    origin = _git(["remote", "get-url", "origin"])
    if origin not in {
        "https://github.com/syamaner/roastpilot-agent.git",
        "git@github.com:syamaner/roastpilot-agent.git",
    }:
        _fail()
    observed_branch = _git(["branch", "--show-current"])
    head = _git(["rev-parse", "HEAD"])
    resolved_base = _git(["rev-parse", "--verify", f"{base}^{{commit}}"])
    dirty = _git(["status", "--porcelain"])
    if observed_branch != branch or dirty or resolved_base != base:
        _fail()
    if not final and head != base:
        _fail()
    return head


def _registered_role(role: NativeCodexRole) -> tuple[str, str, str, str]:
    """Hash and validate the exact committed Codex registration closure."""
    try:
        config = Path(_CONFIG_NAME).read_bytes()
        if b"[agents]" not in config or b"enabled = true" not in config:
            _fail()
        expected_names = {member.value for member in NativeCodexRole}
        found_names = {
            line.split("]", 1)[0].removeprefix("[agents.")
            for line in config.decode("utf-8", "strict").splitlines()
            if line.startswith("[agents.") and line.endswith("]")
        }
        if found_names != expected_names:
            _fail()
        relpath, effort = _ROLE_EXPECTATIONS[role]
        if f'config_file = "{relpath}"'.encode() not in config:
            _fail()
        role_bytes = (Path(".codex") / relpath).read_bytes()
        text = role_bytes.decode("utf-8", "strict")
        if (
            'model = "gpt-5.6-terra"' not in text
            or f'model_reasoning_effort = "{effort}"' not in text
            or "[agents]\nenabled = false" not in text
        ):
            _fail()
        return _sha256(config), _sha256(role_bytes), effort, role.value
    except (OSError, UnicodeDecodeError):
        _fail()


def _provider_root() -> Path:
    """Return the fixed provider-owned sessions root, rejecting overrides."""
    if "CODEX_HOME" in os.environ:
        _fail()
    root = Path.home() / ".codex" / "sessions"
    try:
        status = os.lstat(root)
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
        ):
            _fail()
    except OSError:
        _fail()
    return root


def _provider_inventory() -> dict[str, tuple[int, int, int, int]]:
    """Fully enumerate bounded regular provider rollouts using opaque identities only."""
    root = _provider_root()
    inventory: dict[str, tuple[int, int, int, int]] = {}
    total = 0
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            relative = Path(current).relative_to(root)
            if len(relative.parts) > MAX_PROVIDER_DEPTH:
                _fail()
            for directory in directories:
                status = os.lstat(Path(current) / directory)
                if (
                    stat.S_ISLNK(status.st_mode)
                    or not stat.S_ISDIR(status.st_mode)
                    or status.st_uid != os.geteuid()
                ):
                    _fail()
            for filename in files:
                candidate = Path(current) / filename
                status = os.lstat(candidate)
                if (
                    candidate.suffix != ".jsonl"
                    or stat.S_ISLNK(status.st_mode)
                    or not stat.S_ISREG(status.st_mode)
                    or status.st_nlink != 1
                    or status.st_uid != os.geteuid()
                    or status.st_size > MAX_PROVIDER_FILE_BYTES
                ):
                    _fail()
                total += status.st_size
                key = _sha256(str(relative / filename).encode("utf-8"))
                inventory[key] = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
                if len(inventory) > MAX_PROVIDER_FILES or total > MAX_PROVIDER_TOTAL_BYTES:
                    _fail()
    except OSError:
        _fail()
    return inventory


def _manifest_dir(root: _Root) -> int:
    """Open the private manifest directory, creating it once beneath the sink root."""
    try:
        first = os.open(
            ".agent-usage", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root.descriptor
        )
    except FileNotFoundError:
        os.mkdir(".agent-usage", 0o700, dir_fd=root.descriptor)
        first = os.open(
            ".agent-usage", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root.descriptor
        )
    try:
        with suppress(FileExistsError):
            os.mkdir("native-codex", 0o700, dir_fd=first)
        second = os.open("native-codex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=first)
    finally:
        os.close(first)
    status = os.fstat(second)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        os.close(second)
        _fail()
    return second


def _write_exclusive(directory: int, name: str, payload: dict[str, object]) -> None:
    """Write one exclusive private JSON manifest without embedding filesystem paths."""
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            os.write(fd, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError:
        _fail()


def _read_manifest(directory: int, name: str) -> dict[str, object]:
    """Load one strict metadata-only manifest while rejecting unsafe storage."""
    try:
        data = _read_fd(directory, name)
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {
            "base_sha",
            "branch",
            "config_sha256",
            "inventory",
            "launch_head_sha",
            "parent_task_id",
            "parent_thread_id",
            "repository",
            "role",
            "role_sha256",
            "slice_id",
            "task_id",
            "task_name",
        }:
            _fail()
        return value
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail()


def prepare_native_codex(arguments: Any) -> str:
    """Pre-bind a registered named leaf before the parent dispatches it.

    Args:
        arguments: Parsed closed CLI arguments supplied only by the parent.

    Returns:
        Opaque manifest identifier to carry across the named-role dispatch.
    """
    values = (arguments.task_id, arguments.slice_id, arguments.parent_task_id, arguments.task_name)
    parent = os.environ.get("CODEX_THREAD_ID")
    if not all(
        isinstance(value, str) and _safe_identifier(value) for value in values
    ) or not _safe_identifier(parent or ""):
        _fail()
    role = NativeCodexRole(arguments.role)
    root = _open_private_root(arguments.usage_root)
    try:
        launch = _git_identity(
            arguments.repository, arguments.branch, arguments.base_sha, final=False
        )
        config_hash, role_hash, _effort, _canonical = _registered_role(role)
        inventory = _provider_inventory()
        manifest_id = str(uuid4())
        directory = _manifest_dir(root)
        try:
            _write_exclusive(
                directory,
                f"{manifest_id}.json",
                {
                    "task_id": arguments.task_id,
                    "slice_id": arguments.slice_id,
                    "parent_task_id": arguments.parent_task_id,
                    "task_name": arguments.task_name,
                    "parent_thread_id": parent,
                    "role": role.value,
                    "repository": arguments.repository,
                    "branch": arguments.branch,
                    "base_sha": arguments.base_sha,
                    "launch_head_sha": launch,
                    "config_sha256": config_hash,
                    "role_sha256": role_hash,
                    "inventory": {key: list(value) for key, value in inventory.items()},
                },
            )
        finally:
            os.close(directory)
        return manifest_id
    finally:
        os.close(root.descriptor)


def _json_line(raw: bytes) -> dict[str, Any]:
    """Decode one provider event while rejecting duplicate keys and non-object roots."""

    def no_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates)
        if not isinstance(value, dict):
            _fail()
        return value
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail()


def _string(mapping: dict[str, Any], key: str) -> str:
    """Read one bounded identifier-like provider string without echoing it."""
    value = mapping.get(key)
    if not isinstance(value, str) or len(value) > 128:
        _fail()
    return value


def _tokens(value: Any) -> tuple[int, int, int, int, int, int]:
    """Validate the one exact cumulative usage grammar."""
    if not isinstance(value, dict) or set(value) != {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }:
        _fail()
    result: list[int] = []
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            _fail()
        result.append(item)
    return tuple(result)  # type: ignore[return-value]


def _parse_rollout(
    path: Path, manifest: dict[str, object]
) -> tuple[str, tuple[int, int, int, int, int, int], bool]:
    """Validate one newly-created leaf rollout and return normalized terminal facts."""
    try:
        status = os.lstat(path)
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or status.st_size > MAX_PROVIDER_FILE_BYTES
        ):
            _fail()
        seen_meta = seen_context = False
        session_id: str | None = None
        totals: tuple[int, int, int, int, int, int] | None = None
        child_seen = False
        with path.open("rb") as stream:
            for number, raw in enumerate(stream, start=1):
                if number > MAX_PROVIDER_LINES or len(raw) > 65_536 or not raw.endswith(b"\n"):
                    _fail()
                event = _json_line(raw)
                if (
                    set(event) != {"type", "payload"}
                    or not isinstance(event["type"], str)
                    or not isinstance(event["payload"], dict)
                ):
                    _fail()
                kind, payload = event["type"], event["payload"]
                if kind == "session_meta":
                    if seen_meta or seen_context or totals is not None:
                        _fail()
                    source = payload.get("source")
                    git = payload.get("git")
                    if (
                        not isinstance(source, dict)
                        or not isinstance(git, dict)
                        or set(source) != {"subagent"}
                    ):
                        _fail()
                    subagent = source["subagent"]
                    if not isinstance(subagent, dict) or set(subagent) != {"thread_spawn"}:
                        _fail()
                    spawn = subagent["thread_spawn"]
                    if not isinstance(spawn, dict) or set(spawn) != {
                        "parent_thread_id",
                        "depth",
                        "agent_path",
                        "agent_nickname",
                        "agent_role",
                    }:
                        _fail()
                    if (
                        _string(spawn, "parent_thread_id") != manifest["parent_thread_id"]
                        or spawn.get("depth") != 1
                        or _string(spawn, "agent_path") != manifest["role"]
                        or _string(spawn, "agent_nickname") != manifest["role"]
                        or _string(spawn, "agent_role") != manifest["role"]
                    ):
                        _fail()
                    if (
                        _string(payload, "id") == ""
                        or _string(payload, "originator") != "codex-tui"
                        or _string(payload, "cli_version") != "0.147.0"
                        or _string(git, "commit_hash") != manifest["launch_head_sha"]
                        or _string(git, "branch") != manifest["branch"]
                    ):
                        _fail()
                    session_id = _string(payload, "id")
                    seen_meta = True
                elif kind == "turn_context":
                    if (
                        not seen_meta
                        or seen_context
                        or totals is not None
                        or not isinstance(payload, dict)
                    ):
                        _fail()
                    effort = "high" if manifest["role"] != "repair" else "medium"
                    if (
                        set(payload) != {"model", "effort"}
                        or _string(payload, "model") != "gpt-5.6-terra"
                        or _string(payload, "effort") != effort
                    ):
                        _fail()
                    seen_context = True
                elif kind == "event_msg":
                    if (
                        not seen_context
                        or not isinstance(payload, dict)
                        or set(payload) != {"type", "info"}
                        or payload.get("type") != "token_count"
                        or not isinstance(payload.get("info"), dict)
                        or set(payload["info"]) != {"total_token_usage"}
                    ):
                        _fail()
                    current = _tokens(payload["info"]["total_token_usage"])
                    if totals is not None and any(
                        later < earlier for earlier, later in zip(totals, current, strict=True)
                    ):
                        _fail()
                    totals = current
                else:
                    _fail()
        if not seen_meta or not seen_context or session_id is None or totals is None:
            _fail()
        return session_id, totals, child_seen
    except OSError:
        _fail()


def _new_rollout(manifest: dict[str, object]) -> Path:
    """Require exactly one provider rollout not present in the pre-dispatch inventory."""
    before = manifest["inventory"]
    if not isinstance(before, dict):
        _fail()
    root = _provider_root()
    candidates: list[Path] = []
    for current, _directories, files in os.walk(root, topdown=True, followlinks=False):
        for filename in files:
            candidate = Path(current) / filename
            if candidate.suffix != ".jsonl":
                continue
            status = os.lstat(candidate)
            key = _sha256(str(candidate.relative_to(root)).encode("utf-8"))
            identity = [status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns]
            if before.get(key) != identity:
                candidates.append(candidate)
    if len(candidates) != 1:
        _fail()
    return candidates[0]


def _no_child_rollout(leaf_session_id: str) -> bool:
    """Return true only after a bounded complete scan proves no rollout is its child."""
    root = _provider_root()
    count = 0
    for current, _directories, files in os.walk(root, topdown=True, followlinks=False):
        for filename in files:
            if not filename.endswith(".jsonl"):
                continue
            count += 1
            if count > MAX_PROVIDER_FILES:
                _fail()
            path = Path(current) / filename
            try:
                data = path.read_bytes()
                if len(data) > MAX_PROVIDER_FILE_BYTES:
                    _fail()
            except OSError:
                _fail()
            for raw in data.splitlines():
                event = _json_line(raw)
                if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                    source = event["payload"].get("source")
                    if (
                        isinstance(source, dict)
                        and isinstance(source.get("subagent"), dict)
                        and isinstance(source["subagent"].get("thread_spawn"), dict)
                        and source["subagent"]["thread_spawn"].get("parent_thread_id")
                        == leaf_session_id
                    ):
                        return False
    return True


def finalize_native_codex(arguments: Any) -> tuple[NativeCodexUsageRecord, _Root, int, str]:
    """Finalize exactly one prepared binding after a named registered leaf exits.

    The caller must append the returned record through its existing protected
    sink and then call :func:`complete_native_codex`; a failure leaves the
    manifest in a non-replayable ``.finalizing`` state.
    """
    if not _safe_identifier(arguments.manifest_id) or arguments.manifest_id.count("-") != 4:
        _fail()
    root = _open_private_root(arguments.usage_root)
    directory = _manifest_dir(root)
    name = f"{arguments.manifest_id}.json"
    pending = f"{arguments.manifest_id}.finalizing"
    try:
        os.rename(name, pending, src_dir_fd=directory, dst_dir_fd=directory)
        manifest = _read_manifest(directory, pending)
        role = NativeCodexRole(str(manifest["role"]))
        config_hash, role_hash, effort, _canonical = _registered_role(role)
        if config_hash != manifest["config_sha256"] or role_hash != manifest["role_sha256"]:
            _fail()
        rollout = _new_rollout(manifest)
        session, totals, _child = _parse_rollout(rollout, manifest)
        final_head = _git_identity(
            str(manifest["repository"]),
            str(manifest["branch"]),
            str(manifest["base_sha"]),
            final=True,
        )
        status = NativeCodexTaskStatus(arguments.task_status)
        success = status is NativeCodexTaskStatus.SUCCESS and arguments.exit_code == 0
        if status is NativeCodexTaskStatus.SUCCESS and not success:
            _fail()
        if success:
            if (
                final_head == manifest["base_sha"]
                or _git(["merge-base", "--is-ancestor", str(manifest["base_sha"]), final_head])
                != ""
            ):
                _fail()
        elif final_head != manifest["base_sha"]:
            _fail()
        whole = _no_child_rollout(session)
        record = NativeCodexUsageRecord(
            captured_at=datetime.now(UTC),
            task_id=str(manifest["task_id"]),
            slice_id=str(manifest["slice_id"]),
            parent_task_id=str(manifest["parent_task_id"]),
            task_name=str(manifest["task_name"]),
            native_role=role,
            model="gpt-5.6-terra",
            effort=effort,
            repository=str(manifest["repository"]),
            branch=str(manifest["branch"]),
            base_sha=str(manifest["base_sha"]),
            launch_head_sha=str(manifest["launch_head_sha"]),
            final_head_sha=final_head,
            parent_thread_id=str(manifest["parent_thread_id"]),
            leaf_session_id=session,
            exit_code=arguments.exit_code,
            task_status=status,
            success=success,
            input_tokens=totals[0],
            cached_input_tokens=totals[1],
            cache_write_input_tokens=totals[2],
            output_tokens=totals[3],
            reasoning_output_tokens=totals[4],
            total_tokens=totals[5],
            whole_tree_verified=whole,
        )
        _reattest_root(root)
        return record, root, directory, pending
    except Exception:
        os.close(directory)
        os.close(root.descriptor)
        raise


def complete_native_codex(root: _Root, directory: int, pending: str) -> None:
    """Mark an appended native record final, permanently rejecting replay."""
    try:
        _reattest_root(root)
        os.rename(
            pending,
            pending.removesuffix(".finalizing") + ".finalized",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
    except OSError:
        _fail()
    finally:
        os.close(directory)
        os.close(root.descriptor)
