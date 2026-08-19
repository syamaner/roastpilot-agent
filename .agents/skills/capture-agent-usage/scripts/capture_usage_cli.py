"""Parent-only, metadata-only local agent-usage capture commands."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NoReturn, TypeVar

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from uuid import uuid4

from capture_usage_claude import (
    ClaudeAuthorityError,
    ClaudeUsageMissingTerminalError,
    ClaudeUsageParseError,
    parse_claude_stream,
)
from capture_usage_codex import (
    CodexUsageMissingTerminalError,
    CodexUsageParseError,
    parse_codex_stream,
)
from capture_usage_models import (
    AGENT_USAGE_SCHEMA_VERSION,
    ALL_BOUND_ROOT_ENVIRONMENT_KEYS,
    BOUND_ROOT_POLICIES,
    EVIDENCE_BUNDLE_FILES,
    EVIDENCE_CHUNK_BYTES,
    EVIDENCE_MANIFEST_NAME,
    EVIDENCE_MAX_FILE_BYTES,
    EVIDENCE_MAX_MANIFEST_BYTES,
    EVIDENCE_MAX_TOTAL_BYTES,
    EVIDENCE_PAYLOAD_FILES,
    EVIDENCE_ROOT_ENVIRONMENT_KEY,
    EVIDENCE_SCHEMA_VERSION,
    MAX_STREAM_BYTES,
    NATIVE_WORKER_USAGE_SCHEMA_VERSION,
    PLAN_ROOT_ENVIRONMENT_KEY,
    SKILL_VERSION,
    USAGE_RECORD_ADAPTER,
    VALIDATION_ENVIRONMENT_KEYS,
    VALIDATION_ENVIRONMENT_ROLES,
    VALIDATION_ROLE_COMMANDS,
    BoundRoot,
    BoundRootKind,
    BoundRootPolicy,
    CapacitySnapshotRecord,
    CapacitySource,
    CapacityStatus,
    ClaudeModelUsage,
    FindingLens,
    FindingSeverity,
    HarnessFamily,
    NativeClaudeRole,
    NativeWorkerUsageRecord,
    OutcomeRecord,
    ParsedUsage,
    RoleCapability,
    TaskUsageRecord,
    UsageRecord,
    ValidationCommandKind,
    render_allowed_tools,
    render_validation_commands,
)
from capture_usage_transcript import (
    HANDBACK_SCHEMA_VERSION,
    TIMESTAMP_SKEW_SECONDS,
    TranscriptError,
    TranscriptUsage,
    parse_owned_transcript,
    reject_existing_owned_session,
)

DEFAULT_SINK = Path(".agent-usage/usage.jsonl")
SINK_ROOT = ".agent-usage"
MAX_PROMPT_BYTES = 65_536
"""Prompt cap limits one explicitly-authorized stdin payload to 64 KiB."""
LAUNCH_TIMEOUT_SECONDS = 3600
NATIVE_LAUNCH_TIMEOUT_SECONDS = 14_400
VERSION_TIMEOUT_SECONDS = 5
TERMINATE_GRACE_SECONDS = 1
_SEMVER = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_VERIFIED_HARNESS_VERSIONS = {
    HarnessFamily.CODEX: "0.147.0",
    HarnessFamily.CLAUDE: "2.1.233",
}
_GIT_TIMEOUT_SECONDS = 5
_GIT_OUTPUT_LIMIT = 4096
_PROTECTED_ATTRIBUTION_ROLES = frozenset({role.value for role in NativeClaudeRole}) | {"repair"}
"""Every registered native-capable role plus `repair`, closed over D163 registration.

`repair` has no `.claude/agents/*.md` file (it is Codex-only); every other
protected value is a live :class:`NativeClaudeRole` member, so this set can
never silently drop a role added to that enum.
"""
NATIVE_PERMISSION_MODES: dict[RoleCapability, str] = {
    RoleCapability.READ_ONLY: "dontAsk",
    RoleCapability.WRITE: "auto",
}
"""The one frozen native permission-mode value per capability (D166, §2.2).

No caller input may select or override either value; the native argv, the
committed-frontmatter guard, and the transcript permission-mode attestation
all index this same closed mapping.
"""
_VALIDATION_ENVIRONMENT_KEYS = VALIDATION_ENVIRONMENT_KEYS
"""Local alias of the model-owned closed key set (D169, §2.2), kept for the
existing private-attribute test surface (``usage_cli._VALIDATION_ENVIRONMENT_KEYS``).
Stripped from every native launch's inherited environment first, then
reinstated with exactly these values only when the role's active bound root
is :data:`~capture_usage_models.BoundRootKind.VALIDATION` (D166, §2.4)."""
_PLAN_ENVIRONMENT_KEYS = frozenset({PLAN_ROOT_ENVIRONMENT_KEY})
_EVIDENCE_ENVIRONMENT_KEYS = frozenset({EVIDENCE_ROOT_ENVIRONMENT_KEY})
_ALL_BOUND_ROOT_ENVIRONMENT_KEYS = ALL_BOUND_ROOT_ENVIRONMENT_KEYS
_VALIDATION_DIRECTORY_MODE = 0o700
_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_GENERATED_AT_MAX_LEN = 40
"""Conservative bound on the ``generated_at`` field before attempting to parse it."""
"""Closed 40-lowercase-hex full commit sha grammar (D169, §2.3, §2.4)."""
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""Closed 64-lowercase-hex SHA-256 digest grammar (D169, §2.4)."""
_EVIDENCE_FILE_MODE = 0o400
"""The exact required mode for every one of the nine evidence bundle entries."""
_PLAN_ORIGIN_URLS = frozenset(
    {
        "https://github.com/syamaner/roastpilot-plan.git",
        "git@github.com:syamaner/roastpilot-plan.git",
    }
)
"""The closed plan-repo origin allowlist (D169, §2.3)."""


class CaptureUsageError(ValueError):
    """Raised for metadata-safe capture failures."""


def _utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for normalized records."""
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime:
    """Parse an explicit ISO-8601 timestamp as an aware datetime."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _sink_path(value: str) -> Path:
    """Accept only relative file descendants of the private sink root."""
    path = Path(value)
    if not _is_confined_sink_path(path):
        raise argparse.ArgumentTypeError("sink path must be a .agent-usage descendant")
    return path


def _is_confined_sink_path(path: Path) -> bool:
    """Return whether ``path`` names a non-traversing file beneath ``.agent-usage``."""
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0] == SINK_ROOT
        and path.name not in {"", "."}
    )


def _open_sink_parent(path: Path) -> tuple[int, str]:
    """Open or create the relative sink parent without following symlinks."""
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise CaptureUsageError("platform cannot securely traverse usage sink directories")
    if not path.parts or path.name in {"", "."}:
        raise CaptureUsageError("sink path must name a file")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(".", flags)
    try:
        for component in path.parts[:-1]:
            try:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                os.fchmod(child_descriptor, 0o700)
            except OSError:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
    except OSError:
        os.close(descriptor)
        raise
    return descriptor, path.name


def append_record(path: Path, record: UsageRecord) -> None:
    """Validate and append one record through a private, no-follow JSONL sink.

    Args:
        path: The relative output sink selected by the closed CLI grammar.
        record: A normalized closed-schema record to validate before appending.

    Raises:
        CaptureUsageError: If the sink is unsafe or cannot be securely opened.
    """
    if not _is_confined_sink_path(path):
        raise CaptureUsageError("sink path must be confined to .agent-usage")
    validated = USAGE_RECORD_ADAPTER.validate_python(record)
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor, filename = _open_sink_parent(path)
        descriptor = os.open(
            filename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent_descriptor,
        )
    except (CaptureUsageError, OSError) as exc:
        raise CaptureUsageError("could not securely open usage sink") from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    try:
        assert descriptor is not None
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise OSError("unsafe usage sink target")
        os.fchmod(descriptor, 0o600)
        payload = (validated.model_dump_json() + "\n").encode("utf-8")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        offset = os.lseek(descriptor, 0, os.SEEK_END)
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short usage record write")
        except OSError:
            os.ftruncate(descriptor, offset)
            raise
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        raise CaptureUsageError("could not append usage record") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _print_parsed_json(value: ParsedUsage) -> None:
    """Print only normalized parser fields for explicit local inspection."""
    print(value.model_dump_json())


def parse_codex_command(arguments: argparse.Namespace) -> int:
    """Parse a supplied sanitized Codex JSONL file without creating a record."""
    with _open_stream_input(arguments.stream) as stream:
        _print_parsed_json(parse_codex_stream(stream))
    return 0


def parse_claude_command(arguments: argparse.Namespace) -> int:
    """Parse a supplied sanitized Claude JSONL file without creating a record."""
    _print_parsed_json(
        parse_claude_stream(
            BytesIO(_input_bytes(arguments.stream, MAX_STREAM_BYTES)),
            require_launch_authority=False,
        )
    )
    return 0


def _open_stream_input(path: Path) -> BinaryIO:
    """Open one regular no-follow input stream without materializing its contents."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise CaptureUsageError("platform cannot securely open prompt input")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CaptureUsageError("input file cannot be safely opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CaptureUsageError("input file is not an accepted regular input")
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        return stream
    except CaptureUsageError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise CaptureUsageError("input file cannot be safely opened") from exc


def _input_bytes(path: Path, maximum: int = MAX_PROMPT_BYTES) -> bytes:
    """Read one regular no-follow input into a fixed transient byte buffer."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise CaptureUsageError("platform cannot securely open prompt input")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CaptureUsageError("input file cannot be safely opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CaptureUsageError("input file is not an accepted regular input")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            prompt = stream.read(maximum + 1)
        if len(prompt) > maximum:
            raise CaptureUsageError("input file is not an accepted regular input")
        return prompt
    except CaptureUsageError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise CaptureUsageError("input file cannot be safely opened") from exc


def _prompt_bytes(path: Path) -> bytes:
    """Read one bounded regular prompt without exposing its contents."""
    try:
        return _input_bytes(path)
    except CaptureUsageError as exc:
        raise CaptureUsageError("prompt file cannot be safely opened") from exc


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate, then kill and reap a child process group and its descendants."""
    if hasattr(process, "pid"):
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    try:
        if process.poll() is None:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if hasattr(process, "pid"):
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        process.kill()
    if process.poll() is None:
        process.wait()


def _close_stdin(process: subprocess.Popen[bytes]) -> None:
    """Close child stdin on every completion or error path."""
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()


def _write_prompt(process: subprocess.Popen[bytes], prompt: bytes) -> bool:
    """Send bounded transient prompt bytes and report only a fixed success state."""
    assert process.stdin is not None
    try:
        if process.stdin.write(prompt) != len(prompt):
            return False
        flush = getattr(process.stdin, "flush", None)
        if flush is not None:
            flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    finally:
        try:
            _close_stdin(process)
        except (OSError, ValueError):
            return False
    return True


def _terminate_for_deadline(process: subprocess.Popen[bytes], timed_out: threading.Event) -> None:
    """Fail closed at the run deadline, including surviving group descendants."""
    timed_out.set()
    _stop_process(process)


def _start_deadline(
    process: subprocess.Popen[bytes], seconds: int, timed_out: threading.Event
) -> threading.Timer:
    """Start a daemon deadline that owns only process termination."""
    deadline = threading.Timer(seconds, lambda: _terminate_for_deadline(process, timed_out))
    deadline.daemon = True
    deadline.start()
    return deadline


def _cancel_deadline(deadline: threading.Timer | None) -> None:
    """Join a deadline callback so it cannot race record persistence."""
    if deadline is not None:
        deadline.cancel()
        deadline.join()


def _resolved_executable(harness: HarnessFamily) -> str:
    """Resolve the fixed executable selected by the closed harness enum."""
    executable = shutil.which("codex" if harness is HarnessFamily.CODEX else "claude")
    if executable is None:
        raise CaptureUsageError("selected harness executable is unavailable")
    return executable


def _git_output(arguments: list[str]) -> tuple[int, str]:
    """Run a fixed Git query with bounded, metadata-only output."""
    process: subprocess.Popen[bytes] | None = None
    deadline: threading.Timer | None = None
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            cwd=".",
            start_new_session=True,
        )
        assert process.stdout is not None
        deadline = _start_deadline(process, _GIT_TIMEOUT_SECONDS, timed_out)
        output = process.stdout.read(_GIT_OUTPUT_LIMIT + 1)
        returncode = process.wait(timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureUsageError("Git worktree metadata is unavailable") from exc
    finally:
        _cancel_deadline(deadline)
        if process is not None:
            _stop_process(process)
            if process.stdout is not None:
                process.stdout.close()
    if timed_out.is_set() or len(output) > _GIT_OUTPUT_LIMIT:
        raise CaptureUsageError("Git worktree metadata is unavailable")
    try:
        return returncode, output.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise CaptureUsageError("Git worktree metadata is unavailable") from exc


def _validate_worktree_metadata(arguments: argparse.Namespace) -> None:
    """Bind explicit run metadata to the current clean RoastPilot worktree."""
    origin_status, origin = _git_output(["remote", "get-url", "origin"])
    if (
        origin_status != 0
        or origin
        not in {
            "https://github.com/syamaner/roastpilot-agent.git",
            "git@github.com:syamaner/roastpilot-agent.git",
        }
        or arguments.repository != "syamaner/roastpilot-agent"
    ):
        raise CaptureUsageError("repository metadata does not match the current worktree")
    branch_status, branch = _git_output(["branch", "--show-current"])
    head_status, head = _git_output(["rev-parse", "HEAD"])
    supplied_head_status, supplied_head = _git_output(
        ["rev-parse", "--verify", f"{arguments.head_sha}^{{commit}}"]
    )
    if (
        branch_status != 0
        or head_status != 0
        or supplied_head_status != 0
        or branch != arguments.branch
        or head != supplied_head
    ):
        raise CaptureUsageError("branch or head metadata does not match the current worktree")
    base_status, supplied_base = _git_output(
        ["rev-parse", "--verify", f"{arguments.base_sha}^{{commit}}"]
    )
    merge_base_status, merge_base = _git_output(["merge-base", "HEAD", "origin/main"])
    if base_status != 0 or merge_base_status != 0 or supplied_base != merge_base:
        raise CaptureUsageError("base metadata does not match the current worktree")
    status_code, status = _git_output(["status", "--porcelain"])
    if status_code != 0 or status:
        raise CaptureUsageError("current worktree is not clean")
    arguments.head_sha = head
    arguments.base_sha = merge_base


@dataclass(frozen=True)
class _NativeRolePin:
    """Committed native role identity and capability attestation."""

    model: str
    effort: str
    capability: RoleCapability


def _native_role_pin(role: NativeClaudeRole) -> _NativeRolePin:
    """Read the exact committed model, effort, and tools for one native role."""
    path = Path(".claude") / "agents" / f"{role.value}.md"
    content: str | None = None
    with suppress(CaptureUsageError, UnicodeDecodeError):
        content = _input_bytes(path).decode("utf-8", "strict")
    if content is None:
        raise CaptureUsageError("native agent frontmatter is unavailable")
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise CaptureUsageError("native agent frontmatter is invalid")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise CaptureUsageError("native agent frontmatter is invalid") from None
    frontmatter = lines[1:end]
    values = {
        key: [line.removeprefix(f"{key}: ") for line in frontmatter if line.startswith(f"{key}: ")]
        for key in ("model", "effort", "tools")
    }
    if (
        any(len(value) != 1 for value in values.values())
        or values["effort"][0] not in _SUPPORTED_EFFORTS
        or not re.fullmatch(r"claude-(?:sonnet|opus)-5", values["model"][0])
    ):
        raise CaptureUsageError("native agent frontmatter is invalid")
    tools = tuple(token.strip() for token in values["tools"][0].split(","))
    allowed_tools = frozenset({"Read", "Grep", "Glob", "Bash", "Edit", "Write"})
    if (
        not tools
        or len(set(tools)) != len(tools)
        or any(tool not in allowed_tools for tool in tools)
    ):
        raise CaptureUsageError("native agent frontmatter is invalid")
    capability = (
        RoleCapability.WRITE if {"Edit", "Write"} & set(tools) else RoleCapability.READ_ONLY
    )
    permission_mode_lines = [
        line.removeprefix("permissionMode: ")
        for line in frontmatter
        if line.startswith("permissionMode: ")
    ]
    if len(permission_mode_lines) > 1 or (
        permission_mode_lines and permission_mode_lines[0] != NATIVE_PERMISSION_MODES[capability]
    ):
        raise CaptureUsageError("native agent frontmatter is invalid")
    return _NativeRolePin(values["model"][0], values["effort"][0], capability)


def _validate_native_worktree(
    arguments: argparse.Namespace, capability: RoleCapability, *, post_exit: bool
) -> str:
    """Attest the native worker's exact repository, branch, and commit provenance.

    The pre-exit call binds the supplied ``base_sha`` to the exact launch ``HEAD``
    (never an ``origin/main`` merge-base, which a worktree serialized behind an
    advancing default branch would fail to equal even though its exact base is
    still attested). The post-exit call re-attests the same repository, branch,
    and clean-tree invariants, then enforces the read-only unchanged-head or
    write descendant-head invariant against that same attested base.
    """
    origin_status, origin = _git_output(["remote", "get-url", "origin"])
    if (
        origin_status != 0
        or origin
        not in {
            "https://github.com/syamaner/roastpilot-agent.git",
            "git@github.com:syamaner/roastpilot-agent.git",
        }
        or arguments.repository != "syamaner/roastpilot-agent"
    ):
        raise CaptureUsageError("native repository metadata does not match the current worktree")
    branch_status, branch = _git_output(["branch", "--show-current"])
    head_status, head = _git_output(["rev-parse", "HEAD"])
    base_status, base = _git_output(["rev-parse", "--verify", f"{arguments.base_sha}^{{commit}}"])
    status_args = (
        ["status", "--porcelain"]
        if post_exit and capability is RoleCapability.WRITE
        else ["status", "--porcelain", "--ignored"]
    )
    clean_status, dirty = _git_output(status_args)
    if (
        branch_status != 0
        or head_status != 0
        or base_status != 0
        or clean_status != 0
        or branch != arguments.branch
        or dirty
    ):
        raise CaptureUsageError("native worktree attestation failed")
    if not post_exit:
        if head != base:
            raise CaptureUsageError("native worktree attestation failed")
        arguments.base_sha = base
        return head
    if capability is RoleCapability.READ_ONLY:
        if head != base:
            raise CaptureUsageError("native worktree attestation failed")
        return head
    ancestry_status, _ = _git_output(["merge-base", "--is-ancestor", base, head])
    if ancestry_status != 0 or head == base:
        raise CaptureUsageError("native worktree attestation failed")
    return head


def _require_validation_directory(descriptor: int, *, expect_mode: int | None) -> None:
    """Require an already-opened descriptor to name an owned directory.

    Args:
        descriptor: An open, no-follow directory descriptor.
        expect_mode: The exact required permission bits, or ``None`` when mode
            is intentionally unconstrained (the ``venv`` directory itself).

    Raises:
        OSError: If the descriptor is not a directory, is not owned by the
            current effective user, or (when constrained) has the wrong mode.
    """
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise OSError("validation directory ownership or type is invalid")
    if expect_mode is not None and stat.S_IMODE(status.st_mode) != expect_mode:
        raise OSError("validation directory mode is invalid")


_VALIDATION_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
"""The closed positive grammar for one bound-root path segment (D168, generalized D169).

Replaces the D167 negative character predicate. A rendered validation-role
rule is ``Bash(<command containing this path>)``, so any provider allow-rule
or shell metacharacter surviving inside the path could re-scope or truncate
that rule (§2.5) — closing the grammar positively, rather than blocklisting
characters, keeps it closed for every future rule-rendering consumer, and the
same closed grammar now gates the PLAN and EVIDENCE roots too, which are
never rule-rendered but share the same disjointness and descriptor-open
requirements."""


def _is_valid_bound_root_path(value: str) -> bool:
    """Return whether ``value`` is a well-formed absolute bound-root path.

    Applied identically to the raw ``--validation-root``/``--plan-root``/
    ``--evidence-root`` argument and to its resolved ``os.path.realpath``
    value before either is used for overlap, descriptor, rule-rendering, or
    argv purposes (D167, tightened D168, generalized D169 §2.2). A closed
    positive per-segment grammar: every non-empty segment must match
    ``^[A-Za-z0-9._-]{1,255}$`` and must not be ``.`` or ``..``. This is
    strictly stronger than the prior negative predicate — it continues to
    reject whitespace, ASCII control characters (including DEL), quotes,
    backslashes, empty segments (double or trailing slash), and ``..``
    traversal, and it additionally rejects every provider allow-rule and
    shell metacharacter, for example ``( ) : * , ; & | $`` backtick
    ``< > ! ? [ ] { } #`` and ``~``.

    Args:
        value: The candidate path, raw or resolved.

    Returns:
        ``True`` only when the value satisfies the closed grammar.
    """
    if not value or not value.startswith("/"):
        return False
    segments = value.split("/")
    if segments[0] != "":
        # value.startswith("/") above guarantees split("/")[0] == ""; defensive
        # invariant backstop, not reachable here.
        return False  # pragma: no cover
    return all(
        segment not in (".", "..") and _VALIDATION_PATH_SEGMENT.fullmatch(segment) is not None
        for segment in segments[1:]
    )


def _validate_bound_root_shape(raw: str, *, error_message: str) -> str:
    """Validate the shared closed bound-root grammar, disjointness, and shape (D169, §2.2).

    Every :class:`~capture_usage_models.BoundRootKind` shares this core: the
    closed positive path grammar on both the raw and resolved forms,
    disjointness from the current worktree and from ``~/.claude``, and a
    no-follow descriptor open proving directory type, current-euid ownership,
    and exactly mode ``0700``. Kind-specific checks (validation ``venv``
    shape, plan git identity, evidence bundle listing) run only after this
    shared shape passes.

    Args:
        raw: The exact caller-supplied root argument value.
        error_message: The one fixed, path-free error for this bound-root kind.

    Returns:
        The canonical resolved root path.

    Raises:
        CaptureUsageError: If the grammar, location, ownership, or mode is
            invalid. The message never echoes the supplied path.
    """
    if not _is_valid_bound_root_path(raw):
        raise CaptureUsageError(error_message)
    try:
        cwd_real = os.path.realpath(Path.cwd())
        root_real = os.path.realpath(raw)
        home_claude_real = os.path.realpath(Path.home() / ".claude")
    except OSError:
        raise CaptureUsageError(error_message) from None
    if not _is_valid_bound_root_path(root_real):
        raise CaptureUsageError(error_message)
    if (
        Path(root_real).is_relative_to(Path(cwd_real))
        or Path(cwd_real).is_relative_to(Path(root_real))
        or Path(root_real).is_relative_to(Path(home_claude_real))
    ):
        raise CaptureUsageError(error_message)
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(raw, flags)
    except OSError:
        raise CaptureUsageError(error_message) from None
    try:
        _require_validation_directory(descriptor, expect_mode=_VALIDATION_DIRECTORY_MODE)
    except OSError:
        raise CaptureUsageError(error_message) from None
    finally:
        os.close(descriptor)
    return root_real


def _validate_validation_root(raw: str) -> str:
    """Validate one parent-provisioned external validation root without mutating it.

    The root, its ``cache`` and ``tmp`` children, its ``venv`` directory, the
    ``venv/pyvenv.cfg`` marker, and the ``venv/bin/python`` interpreter are all
    attested by descriptor before any value derived from the path is trusted
    (D166, §2.4). The shared closed grammar and shape core
    (:func:`_validate_bound_root_shape`) runs first (D167, generalized D169).
    Every failure raises the single fixed, path-free error.

    Args:
        raw: The exact ``--validation-root`` argument value.

    Returns:
        The canonical resolved root path.

    Raises:
        CaptureUsageError: If the path grammar, location, ownership, mode, or
            venv shape is invalid. The message never echoes the supplied path.
    """
    root_real = _validate_bound_root_shape(raw, error_message="validation environment is invalid")
    descriptors: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        root_fd = os.open(raw, flags)
        descriptors.append(root_fd)
        _require_validation_directory(root_fd, expect_mode=_VALIDATION_DIRECTORY_MODE)
        for name in ("cache", "tmp"):
            child_fd = os.open(name, flags, dir_fd=root_fd)
            descriptors.append(child_fd)
            _require_validation_directory(child_fd, expect_mode=_VALIDATION_DIRECTORY_MODE)
        venv_fd = os.open("venv", flags, dir_fd=root_fd)
        descriptors.append(venv_fd)
        _require_validation_directory(venv_fd, expect_mode=None)
        cfg_fd = os.open("pyvenv.cfg", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=venv_fd)
        descriptors.append(cfg_fd)
        cfg_status = os.fstat(cfg_fd)
        if (
            not stat.S_ISREG(cfg_status.st_mode)
            or cfg_status.st_uid != os.geteuid()
            or cfg_status.st_nlink != 1
        ):
            raise OSError("validation venv marker is invalid")
        interpreter_status = os.stat("bin/python", dir_fd=venv_fd)
        if (
            not stat.S_ISREG(interpreter_status.st_mode)
            or not interpreter_status.st_mode & stat.S_IXUSR
            or interpreter_status.st_size == 0
        ):
            raise OSError("validation venv interpreter is invalid")
    except OSError:
        raise CaptureUsageError("validation environment is invalid") from None
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
    return root_real


def _validation_environment_values(root: str) -> dict[str, str]:
    """Build the closed validation-role environment map from one validated root.

    Args:
        root: The canonical resolved validation root.

    Returns:
        The exact eleven-key mapping of §2.4, every value derived only from
        ``root``.
    """
    cache = os.path.join(root, "cache")
    tmp = os.path.join(root, "tmp")
    return {
        "ROASTPILOT_VALIDATION_ROOT": root,
        "ROASTPILOT_VALIDATION_PYTHON": os.path.join(root, "venv", "bin", "python"),
        "ROASTPILOT_VALIDATION_TMP": tmp,
        "TMPDIR": tmp,
        "XDG_CACHE_HOME": cache,
        "PYTHONPYCACHEPREFIX": os.path.join(cache, "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUFF_CACHE_DIR": os.path.join(cache, "ruff"),
        "COVERAGE_FILE": os.path.join(tmp, "coverage"),
        "PIP_CACHE_DIR": os.path.join(cache, "pip"),
        "PYTEST_ADDOPTS": f"-o cache_dir={os.path.join(cache, 'pytest')}",
    }


def _plan_identity_checks(root: str, sha: str) -> None:
    """Run the five git-based plan-root identity checks (D169, §2.3).

    Args:
        root: The canonical resolved plan-root path.
        sha: The exact 40-lowercase-hex ``--plan-sha`` value.

    Raises:
        CaptureUsageError: If any identity check fails, git is unavailable, or
            the worktree is not byte-clean including ignored paths. The
            message never echoes the root, the sha, or git output.
    """
    try:
        toplevel_status, toplevel = _git_output(["-C", root, "rev-parse", "--show-toplevel"])
        origin_status, origin = _git_output(["-C", root, "remote", "get-url", "origin"])
        head_status, head = _git_output(["-C", root, "rev-parse", "HEAD"])
        verify_status, verified = _git_output(
            ["-C", root, "rev-parse", "--verify", f"{sha}^{{commit}}"]
        )
        status_status, status = _git_output(["-C", root, "status", "--porcelain", "--ignored"])
    except CaptureUsageError:
        raise CaptureUsageError("plan root is invalid") from None
    try:
        toplevel_real = os.path.realpath(toplevel) if toplevel_status == 0 else None
    except OSError:
        raise CaptureUsageError("plan root is invalid") from None
    if (
        toplevel_status != 0
        or toplevel_real != root
        or origin_status != 0
        or origin not in _PLAN_ORIGIN_URLS
        or head_status != 0
        or verify_status != 0
        or head != verified
        or status_status != 0
        or status
    ):
        raise CaptureUsageError("plan root is invalid")


def _plan_root_device_inode(root: str) -> tuple[int, int]:
    """Return the plan root's ``(st_dev, st_ino)`` pair for post-exit drift detection."""
    try:
        status = os.stat(root)
    except OSError:
        raise CaptureUsageError("plan root is invalid") from None
    return status.st_dev, status.st_ino


def _validate_plan_root(raw: str, sha: str | None) -> BoundRoot:
    """Validate one parent-provisioned exact-sha byte-clean plan worktree (D169, §2.3).

    Args:
        raw: The exact ``--plan-root`` argument value.
        sha: The exact ``--plan-sha`` argument value.

    Returns:
        The bound :data:`~capture_usage_models.BoundRootKind.PLAN` root, with
        a ``reattest`` closure that re-runs identity plus inode/device
        equality after the native child exits.

    Raises:
        CaptureUsageError: If the sha grammar, root shape, or git identity is
            invalid. The message never echoes the root or the sha.
    """
    if sha is None or not _FULL_SHA_PATTERN.fullmatch(sha):
        raise CaptureUsageError("plan root is invalid")
    root_real = _validate_bound_root_shape(raw, error_message="plan root is invalid")
    _plan_identity_checks(root_real, sha)
    before = _plan_root_device_inode(root_real)

    def reattest() -> None:
        _plan_identity_checks(root_real, sha)
        if _plan_root_device_inode(root_real) != before:
            raise CaptureUsageError("plan root is invalid")

    return BoundRoot(kind=BoundRootKind.PLAN, path=root_real, reattest=reattest)


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject a JSON object (at any nesting depth) carrying a duplicate key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureUsageError("evidence bundle is invalid")
        result[key] = value
    return result


def _validate_evidence_manifest_schema(
    data: object, pr: int, attested_head: str
) -> dict[str, dict[str, object]]:
    """Validate the closed evidence manifest grammar (D169, §2.4).

    Args:
        data: The parsed manifest JSON value.
        pr: The exact ``--evidence-pr`` value the manifest must match.
        attested_head: The pre-launch attested launch ``HEAD`` the manifest's
            ``head_sha`` must match.

    Returns:
        The validated ``files`` mapping, keyed by the eight payload names.

    Raises:
        CaptureUsageError: If any manifest field is missing, malformed, or
            mismatched.
    """
    expected_keys = {
        "evidence_schema_version",
        "repository",
        "pull_request",
        "head_sha",
        "base_sha",
        "generated_at",
        "files",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise CaptureUsageError("evidence bundle is invalid")
    if data.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise CaptureUsageError("evidence bundle is invalid")
    if data.get("repository") != "syamaner/roastpilot-agent":
        raise CaptureUsageError("evidence bundle is invalid")
    pull_request = data.get("pull_request")
    if (
        not isinstance(pull_request, int)
        or isinstance(pull_request, bool)
        or pull_request <= 0
        or pull_request != pr
    ):
        raise CaptureUsageError("evidence bundle is invalid")
    head_sha = data.get("head_sha")
    if (
        not isinstance(head_sha, str)
        or _FULL_SHA_PATTERN.fullmatch(head_sha) is None
        or head_sha != attested_head
    ):
        raise CaptureUsageError("evidence bundle is invalid")
    base_sha = data.get("base_sha")
    if not isinstance(base_sha, str) or _FULL_SHA_PATTERN.fullmatch(base_sha) is None:
        raise CaptureUsageError("evidence bundle is invalid")
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or len(generated_at) > _GENERATED_AT_MAX_LEN:
        raise CaptureUsageError("evidence bundle is invalid")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    # Reject a timezone-naive value and any non-zero UTC offset explicitly rather
    # than silently normalizing it: `generated_at` must already be RFC3339 UTC
    # (a `Z` or `+00:00` form), never an offset that requires conversion.
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CaptureUsageError("evidence bundle is invalid")
    if parsed > _utc_now() + timedelta(seconds=TIMESTAMP_SKEW_SECONDS):
        raise CaptureUsageError("evidence bundle is invalid")
    files = data.get("files")
    if not isinstance(files, dict) or set(files) != set(EVIDENCE_PAYLOAD_FILES):
        raise CaptureUsageError("evidence bundle is invalid")
    validated_files: dict[str, dict[str, object]] = {}
    for name, entry in files.items():
        if not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"}:
            raise CaptureUsageError("evidence bundle is invalid")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(digest, str) or _SHA256_HEX_PATTERN.fullmatch(digest) is None:
            raise CaptureUsageError("evidence bundle is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise CaptureUsageError("evidence bundle is invalid")
        validated_files[name] = {"sha256": digest, "bytes": size}
    return validated_files


def _evidence_listing(root_fd: int) -> frozenset[str]:
    """Return the exact nine-entry bundle listing, rejecting any drift from it."""
    try:
        names = os.listdir(root_fd)
    except OSError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    listing = frozenset(names)
    if listing != EVIDENCE_BUNDLE_FILES:
        raise CaptureUsageError("evidence bundle is invalid")
    return listing


def _open_evidence_entry(root_fd: int, name: str) -> int:
    """Open and attest one bundle entry: regular, owned, single-linked, mode 0400."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
    except OSError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != _EVIDENCE_FILE_MODE
        ):
            raise CaptureUsageError("evidence bundle is invalid")
    except CaptureUsageError:
        os.close(descriptor)
        raise
    return descriptor


def _read_bounded_evidence_file(descriptor: int, maximum: int) -> bytes:
    """Read one already-opened, no-follow entry, bounded by ``maximum`` bytes."""
    try:
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(maximum + 1)
    except OSError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    if len(data) > maximum:
        raise CaptureUsageError("evidence bundle is invalid")
    return data


def _hash_evidence_payload(descriptor: int, *, remaining_budget: int) -> tuple[str, int]:
    """Stream one payload file in bounded chunks, enforcing per-file and aggregate caps."""
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "rb") as stream:
            while True:
                chunk = stream.read(EVIDENCE_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > EVIDENCE_MAX_FILE_BYTES or total > remaining_budget:
                    raise CaptureUsageError("evidence bundle is invalid")
                digest.update(chunk)
    except OSError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    return digest.hexdigest(), total


@dataclass(frozen=True)
class _EvidenceSnapshot:
    """The comparable pre/post state of one evidence bundle (D169, §2.4)."""

    listing: frozenset[str]
    device_inode: tuple[int, int]
    manifest_bytes: bytes
    digests: tuple[tuple[str, str, int], ...]


def _evidence_bundle_state(root: str, pr: int, attested_head: str) -> _EvidenceSnapshot:
    """Fully validate structure, manifest grammar, and payload integrity; return a snapshot.

    Args:
        root: The canonical resolved, already shape-validated evidence root.
        pr: The exact ``--evidence-pr`` value.
        attested_head: The pre-launch attested launch ``HEAD``.

    Returns:
        A comparable snapshot used both to accept the bundle pre-launch and to
        detect any drift post-exit.

    Raises:
        CaptureUsageError: If any structural, manifest, or integrity check
            fails. The message never echoes the root, a digest, or a payload
            byte.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_fd = os.open(root, flags)
    except OSError:
        raise CaptureUsageError("evidence bundle is invalid") from None
    try:
        root_status = os.fstat(root_fd)
        if not stat.S_ISDIR(root_status.st_mode) or root_status.st_uid != os.geteuid():
            raise CaptureUsageError("evidence bundle is invalid")
        device_inode = (root_status.st_dev, root_status.st_ino)
        listing = _evidence_listing(root_fd)
        manifest_descriptor = _open_evidence_entry(root_fd, EVIDENCE_MANIFEST_NAME)
        manifest_bytes = _read_bounded_evidence_file(
            manifest_descriptor, EVIDENCE_MAX_MANIFEST_BYTES
        )
        try:
            manifest = json.loads(manifest_bytes, object_pairs_hook=_reject_duplicate_manifest_keys)
        except json.JSONDecodeError:
            raise CaptureUsageError("evidence bundle is invalid") from None
        files = _validate_evidence_manifest_schema(manifest, pr, attested_head)
        remaining_budget = EVIDENCE_MAX_TOTAL_BYTES
        digests: list[tuple[str, str, int]] = []
        for name in EVIDENCE_PAYLOAD_FILES:
            descriptor = _open_evidence_entry(root_fd, name)
            digest, size = _hash_evidence_payload(descriptor, remaining_budget=remaining_budget)
            remaining_budget -= size
            expected = files[name]
            if digest != expected["sha256"] or size != expected["bytes"]:
                raise CaptureUsageError("evidence bundle is invalid")
            digests.append((name, digest, size))
    finally:
        os.close(root_fd)
    return _EvidenceSnapshot(listing, device_inode, manifest_bytes, tuple(digests))


def _validate_evidence_bundle(raw: str, pr: int | None, *, attested_head: str) -> BoundRoot:
    """Validate one parent-built, hash-verified PR evidence bundle (D169, §2.4).

    Args:
        raw: The exact ``--evidence-root`` argument value.
        pr: The exact ``--evidence-pr`` argument value.
        attested_head: The pre-launch attested launch ``HEAD``.

    Returns:
        The bound :data:`~capture_usage_models.BoundRootKind.EVIDENCE` root,
        with a ``reattest`` closure that re-validates the full bundle state
        after the native child exits.

    Raises:
        CaptureUsageError: If the root shape, listing, manifest, or integrity
            is invalid. The message never echoes the root, a digest, or a
            payload byte.
    """
    if pr is None:
        raise CaptureUsageError("evidence bundle is invalid")
    root_real = _validate_bound_root_shape(raw, error_message="evidence bundle is invalid")
    before = _evidence_bundle_state(root_real, pr, attested_head)

    def reattest() -> None:
        after = _evidence_bundle_state(root_real, pr, attested_head)
        if after != before:
            raise CaptureUsageError("evidence bundle is invalid")

    return BoundRoot(kind=BoundRootKind.EVIDENCE, path=root_real, reattest=reattest)


@dataclass(frozen=True)
class _BoundRootRequest:
    """The raw caller-supplied root/companion values for one native launch (D169, §2.2)."""

    validation_root: str | None
    plan_root: str | None
    plan_sha: str | None
    evidence_root: str | None
    evidence_pr: int | None


_PolicyValueGetter = Callable[[_BoundRootRequest], tuple[object | None, object | None]]
_POLICY_VALUES: dict[BoundRootKind, _PolicyValueGetter] = {
    BoundRootKind.VALIDATION: lambda request: (request.validation_root, None),
    BoundRootKind.PLAN: lambda request: (request.plan_root, request.plan_sha),
    BoundRootKind.EVIDENCE: lambda request: (request.evidence_root, request.evidence_pr),
}
"""Closed per-kind accessor for the raw root/companion CLI values (D169, §2.2)."""


def _admitting_policy(role: NativeClaudeRole) -> BoundRootPolicy | None:
    """Return the one policy admitting ``role`` as required or optional, if any.

    Every admitted role set is pairwise disjoint across the three policies
    (closure-tested), so at most one policy ever admits a given role.
    """
    for policy in BOUND_ROOT_POLICIES.values():
        if role in policy.required_roles or role in policy.optional_roles:
            return policy
    return None


def _bound_root_presence(
    role: NativeClaudeRole, request: _BoundRootRequest
) -> BoundRootKind | None:
    """Validate presence/pairing for every policy against ``role`` (D169, §2.2).

    Args:
        role: The registered native role.
        request: The raw caller-supplied root/companion values.

    Returns:
        The one active bound-root kind, or ``None`` when no root is supplied
        (always valid for a role with no required policy).

    Raises:
        CaptureUsageError: If a required pair is missing, a pair is supplied
            for a role no policy admits, or a root is supplied without its
            required companion (or vice versa) for any role.
    """
    active: BoundRootKind | None = None
    for policy in BOUND_ROOT_POLICIES.values():
        root_value, companion_value = _POLICY_VALUES[policy.kind](request)
        has_root = root_value is not None
        if policy.companion_option is None:
            present = has_root
        else:
            has_companion = companion_value is not None
            if has_root != has_companion:
                raise CaptureUsageError("validation environment is invalid")
            present = has_root and has_companion
        if role in policy.required_roles:
            if not present:
                raise CaptureUsageError("validation environment is invalid")
        elif role in policy.optional_roles:
            pass
        elif present:
            raise CaptureUsageError("validation environment is invalid")
        if present:
            active = policy.kind
    return active


def _resolve_bound_root(
    role: NativeClaudeRole, request: _BoundRootRequest, *, attested_head: str
) -> BoundRoot | None:
    """Validate presence, then dispatch to the one active kind's deep validation (D169, §2.2).

    Args:
        role: The registered native role.
        request: The raw caller-supplied root/companion values.
        attested_head: The pre-launch attested launch ``HEAD``, used only by
            the EVIDENCE kind's manifest ``head_sha`` check.

    Returns:
        The one bound root for this launch, or ``None`` when none applies.
    """
    active = _bound_root_presence(role, request)
    if active is None:
        return None
    if active is BoundRootKind.VALIDATION:
        assert request.validation_root is not None
        path = _validate_validation_root(request.validation_root)
        return BoundRoot(kind=BoundRootKind.VALIDATION, path=path, reattest=None)
    if active is BoundRootKind.PLAN:
        assert request.plan_root is not None
        return _validate_plan_root(request.plan_root, request.plan_sha)
    assert request.evidence_root is not None
    return _validate_evidence_bundle(
        request.evidence_root, request.evidence_pr, attested_head=attested_head
    )


@dataclass(frozen=True)
class _NativeLaunchEnvironment:
    """Frozen native launch environment and the one bound root, if any (D169, §2.2).

    Both fields are derived from exactly one :func:`_resolve_bound_root` call:
    ``environment`` is the stripped, conditionally-repopulated child
    environment, and ``bound_root`` is ``None`` when no root applies to this
    launch or the one validated :class:`~capture_usage_models.BoundRoot`
    otherwise. Carrying the two together closes the gap where an argv builder
    that only received the environment would have had to re-derive or
    re-validate the root itself.
    """

    environment: dict[str, str]
    bound_root: BoundRoot | None


def _resolve_native_environment(
    role: NativeClaudeRole, request: _BoundRootRequest, *, attested_head: str
) -> _NativeLaunchEnvironment:
    """Validate bound-root presence and shape, then build the closed child env.

    Args:
        role: The registered native role.
        request: The raw caller-supplied root/companion values.
        attested_head: The pre-launch attested launch ``HEAD``.

    Returns:
        The frozen environment-and-root value for this launch, built from
        exactly one root validation.

    Raises:
        CaptureUsageError: If a required pair is missing or mismatched for
            ``role``, or the supplied root/bundle is invalid.
    """
    bound_root = _resolve_bound_root(role, request, attested_head=attested_head)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _ALL_BOUND_ROOT_ENVIRONMENT_KEYS
    }
    if bound_root is not None:
        if bound_root.kind is BoundRootKind.VALIDATION:
            environment.update(_validation_environment_values(bound_root.path))
        elif bound_root.kind is BoundRootKind.PLAN:
            environment[PLAN_ROOT_ENVIRONMENT_KEY] = bound_root.path
        else:
            environment[EVIDENCE_ROOT_ENVIRONMENT_KEY] = bound_root.path
    return _NativeLaunchEnvironment(environment=environment, bound_root=bound_root)


def _emit_handback(
    role: NativeClaudeRole,
    pin: _NativeRolePin,
    arguments: argparse.Namespace,
    session_id: str,
    text: str,
) -> None:
    """Emit exactly one framed, content-safe handback line to stdout (R1, R2).

    Args:
        role: The registered native role that produced the final response.
        pin: The committed model/effort/capability pin for that role.
        arguments: The parsed CLI namespace, read only for task/slice identifiers.
        session_id: The exact bound session identifier.
        text: The validated, non-empty, bounded terminal handback text.
    """
    encoded = text.encode("utf-8")
    payload = {
        "handback_schema_version": HANDBACK_SCHEMA_VERSION,
        "tool_version": SKILL_VERSION,
        "native_role": role.value,
        "role_capability": pin.capability.value,
        "session_id": session_id,
        "task_id": arguments.task_id,
        "slice_id": arguments.slice_id,
        "byte_length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "text": text,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _harness_version(executable: str) -> str:
    """Return only the first semantic version from fixed executable output."""
    process: subprocess.Popen[bytes] | None = None
    deadline: threading.Timer | None = None
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
        assert process.stdout is not None
        deadline = _start_deadline(process, VERSION_TIMEOUT_SECONDS, timed_out)
        output = process.stdout.read(4097)
        returncode = process.wait(timeout=VERSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureUsageError("selected harness version is unavailable") from exc
    finally:
        _cancel_deadline(deadline)
        if process is not None:
            _stop_process(process)
            if process.stdout is not None:
                process.stdout.close()
    if timed_out.is_set():
        raise CaptureUsageError("selected harness version is unavailable")
    if len(output) > 4096:
        raise CaptureUsageError("selected harness version is invalid")
    match = _SEMVER.search(output.decode("utf-8", "ignore"))
    if returncode != 0 or match is None:
        raise CaptureUsageError("selected harness version is invalid")
    return match.group(1)


def _launch_argv(
    harness: HarnessFamily, executable: str, model: str, effort: str | None
) -> list[str]:
    """Build the fixed, closed harness argv with stdin prompt delivery."""
    if harness is HarnessFamily.CODEX:
        argv = [
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--sandbox",
            "read-only",
            "--model",
            model,
        ]
        argv.extend(["-c", "agents.enabled=false"])
        if effort is not None:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        return [*argv, "-"]
    argv = [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools",
        "",
        "--permission-mode",
        "plan",
        "--model",
        model,
    ]
    if effort is not None:
        argv.extend(["--effort", effort])
    return argv


def _native_claude_argv(
    executable: str,
    role: NativeClaudeRole,
    capability: RoleCapability,
    effort: str,
    session_id: str,
    bound_root: BoundRoot | None,
) -> list[str]:
    """Build the exact capability-bound native-Claude worker argv.

    ``bound_root`` must already be the return value of one
    :func:`_resolve_bound_root` call (never a raw, unvalidated path). For any
    role admitted by a bound-root policy, this appends one
    ``--add-dir <bound_root.path>`` pair immediately before
    ``--permission-mode`` (D167, generalized D169 §2.2), and — only when
    ``bound_root.kind`` is
    :data:`~capture_usage_models.BoundRootKind.VALIDATION` — a trailing
    ``--allowedTools`` option carrying that role's committed rule tuple,
    rendered only from ``bound_root.path`` through
    :func:`~capture_usage_models.render_allowed_tools` (D168, §2.3). A PLAN or
    EVIDENCE bound root never renders ``--allowedTools``: those roles read
    with ``Read``/``Grep``/``Glob``, which need path access, not command
    permission (D169, §2.3 I3). ``--allowedTools`` is placed last, after
    ``--effort``, because the option is variadic: anything appended after it
    would be consumed as an additional rule rather than as a new argv
    element. Every non-admitted role's argv is byte-identical to the pre-D167
    shape.

    Args:
        executable: The resolved ``claude`` executable path.
        role: The registered native role.
        capability: The role's derived READ_ONLY/WRITE capability.
        effort: The role's committed reasoning effort.
        session_id: The bound session identifier.
        bound_root: The one validated bound root for this launch, or ``None``.

    Returns:
        The exact native launch argv.

    Raises:
        CaptureUsageError: If ``bound_root`` presence or kind disagrees with
            the policy that admits ``role``, or a VALIDATION role renders no
            allow-list rules from ``bound_root.path``.
    """
    policy = _admitting_policy(role)
    required = policy is not None and role in policy.required_roles
    if bound_root is None:
        if required:
            raise CaptureUsageError("validation environment is invalid")
    elif policy is None or bound_root.kind is not policy.kind:
        raise CaptureUsageError("validation environment is invalid")
    argv = [
        executable,
        "--agent",
        role.value,
        "--setting-sources",
        "project",
        "-p",
        "--session-id",
        session_id,
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if bound_root is not None:
        argv.extend(["--add-dir", bound_root.path])
    argv.extend(
        [
            "--permission-mode",
            NATIVE_PERMISSION_MODES[capability],
            "--effort",
            effort,
        ]
    )
    if bound_root is not None and bound_root.kind is BoundRootKind.VALIDATION:
        rules = render_allowed_tools(role, bound_root.path)
        if not rules:
            raise CaptureUsageError("validation environment is invalid")
        argv.append("--allowedTools")
        argv.extend(rules)
    return argv


def _validate_native_metadata(
    arguments: argparse.Namespace, role: NativeClaudeRole, pin: _NativeRolePin
) -> None:
    """Validate caller metadata through the closed native record model before launch."""
    now = _utc_now()
    try:
        NativeWorkerUsageRecord(
            captured_at=now,
            task_id=arguments.task_id,
            slice_id=arguments.slice_id,
            native_role=role,
            role_capability=pin.capability,
            model=pin.model,
            effort=pin.effort,
            repository=arguments.repository,
            branch=arguments.branch,
            base_sha=arguments.base_sha,
            final_head_sha=(
                arguments.base_sha if pin.capability is RoleCapability.READ_ONLY else "a" * 40
            ),
            parent_task_id=arguments.parent_task_id,
            session_id="00000000-0000-4000-8000-000000000000",
            subagent_count=0,
            usage_message_count=1,
            started_at=now,
            completed_at=now,
            elapsed_ms=0,
            exit_code=1,
            success=False,
            harness_version="0.0.0",
            input_tokens=0,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=0,
            claude_model_usage=(
                ClaudeModelUsage(
                    model=pin.model,
                    input_tokens=0,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    output_tokens=0,
                ),
            ),
            whole_tree_verified=True,
        )
    except ValueError:
        raise CaptureUsageError("native capture metadata is invalid") from None


def _native_record_from_usage(
    arguments: argparse.Namespace,
    role: NativeClaudeRole,
    pin: _NativeRolePin,
    version: str,
    final_head_sha: str,
    exit_code: int,
    usage: TranscriptUsage,
    completed_at: datetime,
    elapsed_ms: int,
) -> NativeWorkerUsageRecord:
    """Build one complete native record from attested and parsed metadata only."""
    return NativeWorkerUsageRecord(
        captured_at=completed_at,
        task_id=arguments.task_id,
        slice_id=arguments.slice_id,
        native_role=role,
        role_capability=pin.capability,
        model=usage.model,
        effort=pin.effort,
        repository=arguments.repository,
        branch=arguments.branch,
        base_sha=arguments.base_sha,
        final_head_sha=final_head_sha,
        parent_task_id=arguments.parent_task_id,
        session_id=usage.session_id,
        subagent_count=0,
        usage_message_count=usage.usage_message_count,
        started_at=arguments.started_at,
        completed_at=completed_at,
        elapsed_ms=elapsed_ms,
        exit_code=exit_code,
        success=exit_code == 0,
        harness_version=version,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        output_tokens=usage.output_tokens,
        claude_model_usage=usage.model_usage,
    )


def _bound_root_request(arguments: argparse.Namespace) -> _BoundRootRequest:
    """Build the raw bound-root request from the parsed ``run-native-claude`` namespace."""
    return _BoundRootRequest(
        validation_root=arguments.validation_root,
        plan_root=arguments.plan_root,
        plan_sha=arguments.plan_sha,
        evidence_root=arguments.evidence_root,
        evidence_pr=arguments.evidence_pr,
    )


def run_native_claude_command(arguments: argparse.Namespace) -> int:
    """Launch one registered Claude implementation worker and append complete usage."""
    role = NativeClaudeRole(arguments.role)
    pin = _native_role_pin(role)
    require_handback = pin.capability is RoleCapability.READ_ONLY
    _validate_native_metadata(arguments, role, pin)
    attested_head = _validate_native_worktree(arguments, pin.capability, post_exit=False)
    launch_environment = _resolve_native_environment(
        role, _bound_root_request(arguments), attested_head=attested_head
    )
    if os.environ.get("CLAUDE_CONFIG_DIR") is not None:
        raise CaptureUsageError("native Claude config directory is not permitted")
    session_id = str(uuid4())
    try:
        reject_existing_owned_session(Path.cwd(), session_id)
    except TranscriptError:
        raise CaptureUsageError("native Claude session path is invalid") from None
    prompt = _prompt_bytes(arguments.prompt_file)
    process: subprocess.Popen[bytes] | None = None
    deadline: threading.Timer | None = None
    timed_out = threading.Event()
    writer: threading.Thread | None = None
    writer_result = [False]
    try:
        executable = _resolved_executable(HarnessFamily.CLAUDE)
        version = _harness_version(executable)
        if version != _VERIFIED_HARNESS_VERSIONS[HarnessFamily.CLAUDE]:
            raise CaptureUsageError("selected harness version is not verified")
        _validate_native_worktree(arguments, pin.capability, post_exit=False)
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        arguments.started_at = started_at
        process = subprocess.Popen(
            _native_claude_argv(
                executable,
                role,
                pin.capability,
                pin.effort,
                session_id,
                launch_environment.bound_root,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            cwd=".",
            env=launch_environment.environment,
            start_new_session=True,
        )
        deadline = _start_deadline(process, NATIVE_LAUNCH_TIMEOUT_SECONDS, timed_out)
        writer = threading.Thread(
            target=lambda: writer_result.__setitem__(0, _write_prompt(process, prompt)),
            daemon=True,
        )
        writer.start()
        writer.join()
        if not writer_result[0]:
            raise CaptureUsageError("prompt delivery failed")
        exit_code = process.wait(timeout=NATIVE_LAUNCH_TIMEOUT_SECONDS)
        if timed_out.is_set():
            raise CaptureUsageError("native Claude run timed out")
        completed_at = _utc_now()
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        try:
            usage = parse_owned_transcript(
                Path.cwd(),
                session_id,
                role,
                pin.effort,
                expected_permission_mode=NATIVE_PERMISSION_MODES[pin.capability],
                require_handback=require_handback,
                started_at=started_at,
                completed_at=completed_at,
            )
        except TranscriptError:
            raise CaptureUsageError("native Claude transcript is invalid") from None
        if usage.model != pin.model:
            raise CaptureUsageError("native Claude transcript is invalid")
        if pin.capability is RoleCapability.WRITE and usage.handback_text is not None:
            # require_handback is derived from pin.capability at this call site, so
            # parse_owned_transcript(require_handback=False) always returns handback_text
            # None for WRITE; this is a defensive invariant backstop, not reachable here.
            raise CaptureUsageError("native Claude transcript is invalid")  # pragma: no cover
        if require_handback and usage.handback_text is None:
            # require_handback=True forces parse_owned_transcript to either return a
            # non-None handback_text or raise TranscriptError (caught above); this is a
            # defensive invariant backstop, not reachable here.
            raise CaptureUsageError("native Claude transcript is invalid")  # pragma: no cover
        final_head_sha = _validate_native_worktree(arguments, pin.capability, post_exit=True)
        bound_root = launch_environment.bound_root
        if bound_root is not None and bound_root.reattest is not None:
            bound_root.reattest()
        append_record(
            arguments.output,
            _native_record_from_usage(
                arguments,
                role,
                pin,
                version,
                final_head_sha,
                exit_code,
                usage,
                completed_at,
                elapsed_ms,
            ),
        )
        if usage.handback_text is not None:
            _emit_handback(role, pin, arguments, usage.session_id, usage.handback_text)
        return 0
    except OSError:
        if writer is not None:
            writer.join(timeout=TERMINATE_GRACE_SECONDS)
            if not writer_result[0]:
                raise CaptureUsageError("prompt delivery failed") from None
        raise CaptureUsageError("native Claude run failed") from None
    except subprocess.TimeoutExpired:
        raise CaptureUsageError("native Claude run timed out") from None
    finally:
        if writer is not None:
            writer.join(timeout=TERMINATE_GRACE_SECONDS)
        _cancel_deadline(deadline)
        if process is not None:
            with suppress(OSError, ValueError):
                _close_stdin(process)
            _stop_process(process)


def _record_from_usage(
    arguments: argparse.Namespace,
    version: str,
    exit_code: int,
    usage: ParsedUsage,
    completed_at: datetime,
    elapsed_ms: int,
) -> TaskUsageRecord:
    """Build one closed task record from explicit metadata and parsed usage only."""
    started_at = arguments.started_at
    return TaskUsageRecord(
        captured_at=completed_at,
        task_id=arguments.task_id,
        slice_id=arguments.slice_id,
        harness=arguments.harness,
        role=arguments.role,
        model=arguments.model,
        effort=arguments.effort,
        repository=arguments.repository,
        branch=arguments.branch,
        base_sha=arguments.base_sha,
        head_sha=arguments.head_sha,
        started_at=started_at,
        completed_at=completed_at,
        elapsed_ms=elapsed_ms,
        exit_code=exit_code,
        success=exit_code == 0,
        harness_version=version,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        claude_model_usage=usage.claude_model_usage,
        estimated_usd=usage.estimated_usd,
        estimate_basis=usage.estimate_basis,
        whole_tree_verified=arguments.whole_tree_verified,
        usage_complete=True,
        parent_task_id=arguments.parent_task_id,
    )


def run_command(arguments: argparse.Namespace) -> int:
    """Launch one selected harness with a bounded prompt and append closed metadata."""
    _validate_run_metadata(arguments)
    _validate_worktree_metadata(arguments)
    prompt = _prompt_bytes(arguments.prompt_file)
    process: subprocess.Popen[bytes] | None = None
    deadline: threading.Timer | None = None
    timed_out = threading.Event()
    try:
        executable = _resolved_executable(arguments.harness)
        version = _harness_version(executable)
        if version != _VERIFIED_HARNESS_VERSIONS[arguments.harness]:
            raise CaptureUsageError("selected harness version is not verified")
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        arguments.started_at = started_at
        process = subprocess.Popen(
            _launch_argv(arguments.harness, executable, arguments.model, arguments.effort),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            cwd=".",
            start_new_session=True,
        )
        assert process.stdout is not None
        deadline = _start_deadline(process, LAUNCH_TIMEOUT_SECONDS, timed_out)
        writer_result = [False]
        writer = threading.Thread(
            target=lambda: writer_result.__setitem__(0, _write_prompt(process, prompt)), daemon=True
        )
        writer.start()
        try:
            if arguments.harness is HarnessFamily.CODEX:
                usage = parse_codex_stream(process.stdout)
            else:
                usage = parse_claude_stream(process.stdout, require_launch_authority=True)
            writer.join()
            if not writer_result[0]:
                raise CaptureUsageError("prompt delivery failed") from None
        except (CodexUsageMissingTerminalError, ClaudeUsageMissingTerminalError):
            writer.join()
            if not writer_result[0]:
                raise CaptureUsageError("prompt delivery failed") from None
            exit_code = process.wait(timeout=LAUNCH_TIMEOUT_SECONDS)
            if timed_out.is_set():
                raise CaptureUsageError("harness run timed out") from None
            if exit_code == 0:
                raise CaptureUsageError("successful harness run has no terminal usage") from None
            completed_at = _utc_now()
            elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
            _validate_worktree_metadata(arguments)
            append_record(
                arguments.output,
                TaskUsageRecord(
                    captured_at=completed_at,
                    task_id=arguments.task_id,
                    slice_id=arguments.slice_id,
                    harness=arguments.harness,
                    role=arguments.role,
                    model=arguments.model,
                    effort=arguments.effort,
                    repository=arguments.repository,
                    branch=arguments.branch,
                    base_sha=arguments.base_sha,
                    head_sha=arguments.head_sha,
                    started_at=started_at,
                    completed_at=completed_at,
                    elapsed_ms=elapsed_ms,
                    exit_code=exit_code,
                    success=False,
                    harness_version=version,
                    usage_complete=False,
                    whole_tree_verified=False,
                    parent_task_id=arguments.parent_task_id,
                ),
            )
            return 0
        except ClaudeAuthorityError:
            raise CaptureUsageError("Claude launch authority is not attested") from None
        except (CodexUsageParseError, ClaudeUsageParseError):
            raise CaptureUsageError("harness usage stream is invalid") from None
        exit_code = process.wait(timeout=LAUNCH_TIMEOUT_SECONDS)
        if timed_out.is_set():
            raise CaptureUsageError("harness run timed out")
        if (
            arguments.harness is HarnessFamily.CLAUDE
            and usage.claude_terminal_success is not None
            and usage.claude_terminal_success is not (exit_code == 0)
        ):
            raise CaptureUsageError("Claude terminal status disagrees with harness exit")
        completed_at = _utc_now()
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        _validate_worktree_metadata(arguments)
        append_record(
            arguments.output,
            _record_from_usage(
                arguments,
                version,
                exit_code,
                usage,
                completed_at,
                elapsed_ms,
            ),
        )
        return 0
    except OSError as exc:
        if "writer" in locals():
            writer.join(timeout=TERMINATE_GRACE_SECONDS)
            if not writer_result[0]:
                raise CaptureUsageError("prompt delivery failed") from None
        raise CaptureUsageError("harness run failed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureUsageError("harness run timed out") from exc
    finally:
        if "writer" in locals():
            writer.join(timeout=TERMINATE_GRACE_SECONDS)
        _cancel_deadline(deadline)
        if process is not None:
            with suppress(OSError, ValueError):
                _close_stdin(process)
            _stop_process(process)
            if process.stdout is not None:
                process.stdout.close()


def _validate_run_metadata(arguments: argparse.Namespace) -> None:
    """Validate every caller-supplied record field before external launch."""
    normalized_role = re.sub(r"[._:-]+", "-", arguments.role.casefold()).rstrip("-")
    if normalized_role in _PROTECTED_ATTRIBUTION_ROLES:
        raise CaptureUsageError("measurement capture role is not permitted")
    if arguments.effort is not None and arguments.effort not in _SUPPORTED_EFFORTS:
        raise CaptureUsageError("effort is not supported by the selected harness")
    now = _utc_now()
    TaskUsageRecord(
        captured_at=now,
        task_id=arguments.task_id,
        slice_id=arguments.slice_id,
        harness=arguments.harness,
        role=arguments.role,
        model=arguments.model,
        effort=arguments.effort,
        repository=arguments.repository,
        branch=arguments.branch,
        base_sha=arguments.base_sha,
        head_sha=arguments.head_sha,
        started_at=now,
        completed_at=now,
        elapsed_ms=0,
        exit_code=1,
        success=False,
        harness_version="0.0.0",
        usage_complete=True,
        whole_tree_verified=arguments.whole_tree_verified,
        parent_task_id=arguments.parent_task_id,
    )


def _evidence_pr(value: str) -> int:
    """Parse one closed positive-integer ``--evidence-pr`` value (D169, §2.4)."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("evidence PR number must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("evidence PR number must be a positive integer")
    return parsed


def _finding_count(value: str) -> tuple[FindingLens, FindingSeverity, int]:
    """Parse one closed ``LENS:SEVERITY:COUNT`` outcome option."""
    lens, separator, remainder = value.partition(":")
    severity, separator_two, count_text = remainder.partition(":")
    if not separator or not separator_two:
        raise argparse.ArgumentTypeError("finding count must be LENS:SEVERITY:COUNT")
    try:
        count = int(count_text)
        if count < 0:
            raise ValueError
        return FindingLens[lens], FindingSeverity[severity], count
    except (KeyError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "finding count uses unknown lens, severity, or count"
        ) from exc


def annotate_outcome(arguments: argparse.Namespace) -> int:
    """Append closed pilot outcome metadata with no reviewer text or findings body."""
    counts: dict[FindingLens, dict[FindingSeverity, int]] = {}
    for lens, severity, count in arguments.finding_count:
        if severity in counts.setdefault(lens, {}):
            raise CaptureUsageError("duplicate finding count is not permitted")
        counts.setdefault(lens, {})[severity] = count
    append_record(
        arguments.output,
        OutcomeRecord(
            captured_at=arguments.captured_at or _utc_now(),
            task_id=arguments.task_id,
            slice_id=arguments.slice_id,
            finding_counts=counts,
            repair_commit_count=arguments.repair_commit_count,
            final_gate_passed=arguments.final_gate_passed,
        ),
    )
    return 0


def snapshot_capacity(arguments: argparse.Namespace) -> int:
    """Append a qualitative capacity snapshot without retaining raw CLI output."""
    append_record(
        arguments.output,
        CapacitySnapshotRecord(
            captured_at=arguments.captured_at or _utc_now(),
            task_id=arguments.task_id,
            slice_id=arguments.slice_id,
            family=arguments.family,
            status=arguments.status,
            source=arguments.source,
        ),
    )
    return 0


_MAX_PYTEST_ARG_TOKENS = 32
_MAX_PYTEST_ARG_TOKEN_BYTES = 256


def _pytest_arg_token(value: str) -> str:
    """Parse one closed ``--pytest-arg`` token (D169, §2.5).

    The grammar exists only for line framing and bounding, never for
    capability: the underlying pytest prefix rule already admits arbitrary
    pytest arguments (the accepted D168 residual). Only newline, carriage
    return, and NUL are rejected, plus the per-token byte cap.
    """
    if "\n" in value or "\r" in value or "\x00" in value:
        raise argparse.ArgumentTypeError("pytest argument token contains a forbidden byte")
    if len(value.encode("utf-8")) > _MAX_PYTEST_ARG_TOKEN_BYTES:
        raise argparse.ArgumentTypeError("pytest argument token exceeds the byte cap")
    return value


def _shlex_split(value: str) -> list[str]:
    """Split one shell-quoted command string, converting malformed input to the fixed error."""
    try:
        return shlex.split(value)
    except ValueError:
        raise CaptureUsageError("validation environment is invalid") from None


def _render_prefix_run_command(prefix: str, tokens: tuple[str, ...]) -> str:
    """Render one concrete, mechanically-covered ``RUN`` command for a PREFIX rule.

    Args:
        prefix: The table-rendered prefix command.
        tokens: Zero or more caller-supplied ``--pytest-arg`` tokens, in order.

    Returns:
        ``prefix`` alone when ``tokens`` is empty, else ``prefix`` followed by
        the shell-quoted tokens.

    Raises:
        CaptureUsageError: If the rendered command does not round-trip through
            ``shlex.split`` as exactly the prefix tokens followed by the
            supplied tokens, or does not begin with the literal prefix text.
    """
    run_command = prefix if not tokens else prefix + " " + " ".join(shlex.quote(t) for t in tokens)
    if _shlex_split(run_command) != [*_shlex_split(prefix), *tokens]:
        raise CaptureUsageError("validation environment is invalid")
    if run_command != prefix and not run_command.startswith(prefix + " "):
        # run_command is always built as exactly prefix, or prefix + " " + quoted
        # tokens, above — it is therefore always either equal to prefix or literally
        # prefixed by "prefix "; this is a defensive invariant backstop that the
        # round-trip check above would already have caught, not reachable here.
        raise CaptureUsageError("validation environment is invalid")  # pragma: no cover
    return run_command


def print_validation_commands_command(arguments: argparse.Namespace) -> int:
    """Print one validation role's ``ALLOW``/``RUN`` gate command lines (parent-only).

    Validates ``--validation-root`` exactly once through the shared
    :func:`_validate_validation_root` call, then prints one ``ALLOW EXACT
    <command>`` or ``ALLOW PREFIX <command-prefix>`` authorization-descriptor
    line per :data:`~capture_usage_models.VALIDATION_ROLE_COMMANDS` entry for
    the selected role, followed by one concrete, runnable ``RUN <command>``
    line per entry (D169, §2.5). Every command is rendered through
    :func:`~capture_usage_models.render_validation_commands` — the same
    render function :func:`_native_claude_argv` uses to build the
    ``--allowedTools`` rule tuple, so the printed commands and the bound
    rules can never diverge (D168, §2.2, §2.6). Writes no sink record and
    exposes no rule, tool, model, effort, or permission override; the parent
    copies this output verbatim into the lead-authored role brief. All
    output is built before anything is written, so a failure never emits
    partial output.

    Args:
        arguments: The parsed CLI namespace.

    Returns:
        ``0`` on success.

    Raises:
        CaptureUsageError: If ``role`` is not a validation role, the root is
            invalid, the role renders no commands, ``--pytest-arg`` is
            supplied for a role with no ``PREFIX`` entry, more than 32
            tokens are supplied, or the mechanical ``RUN`` coverage proof
            fails for any entry.
    """
    role = NativeClaudeRole(arguments.role)
    if role not in VALIDATION_ENVIRONMENT_ROLES:
        raise CaptureUsageError("validation environment is invalid")
    tokens: tuple[str, ...] = tuple(arguments.pytest_arg or ())
    if len(tokens) > _MAX_PYTEST_ARG_TOKENS:
        raise CaptureUsageError("validation environment is invalid")
    commands = VALIDATION_ROLE_COMMANDS.get(role, ())
    if tokens and not any(command.kind is ValidationCommandKind.PREFIX for command in commands):
        raise CaptureUsageError("validation environment is invalid")
    root = _validate_validation_root(arguments.validation_root)
    rendered = render_validation_commands(role, root)
    if not rendered:
        raise CaptureUsageError("validation environment is invalid")
    allow_lines: list[str] = []
    run_lines: list[str] = []
    for command, text in zip(commands, rendered, strict=True):
        allow_lines.append(f"ALLOW {command.kind.value} {text}")
        if command.kind is ValidationCommandKind.EXACT:
            run_lines.append(f"RUN {text}")
        else:
            run_lines.append(f"RUN {_render_prefix_run_command(text, tokens)}")
    sys.stdout.write("\n".join([*allow_lines, *run_lines]) + "\n")
    sys.stdout.flush()
    return 0


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    """Add the shared relative output option to one subcommand parser."""
    parser.add_argument("--output", type=_sink_path, default=DEFAULT_SINK)


EnumMember = TypeVar("EnumMember")


def _enum_option(enum_type: type[EnumMember], value: str) -> EnumMember:
    """Parse an uppercase member name from one closed enum type."""
    try:
        return enum_type[value.upper()]  # type: ignore[index]
    except KeyError as exc:
        raise argparse.ArgumentTypeError(f"unknown {enum_type.__name__} value") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the closed parent-only command grammar for the capture utility."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-version",
        action="version",
        version=(
            f"generic={AGENT_USAGE_SCHEMA_VERSION} "
            f"native-worker={NATIVE_WORKER_USAGE_SCHEMA_VERSION}"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    parse_codex = commands.add_parser("parse-codex", help="parse a sanitized Codex stream")
    parse_codex.add_argument("stream", type=Path)
    parse_codex.set_defaults(handler=parse_codex_command)

    parse_claude = commands.add_parser("parse-claude", help="parse a sanitized Claude stream")
    parse_claude.add_argument("stream", type=Path)
    parse_claude.set_defaults(handler=parse_claude_command)

    run = commands.add_parser("run", help="opt-in parent-only selected harness capture")
    run.add_argument(
        "--harness", type=lambda value: _enum_option(HarnessFamily, value), required=True
    )
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument("--task-id", required=True)
    run.add_argument("--slice-id", required=True)
    run.add_argument("--role", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--effort")
    run.add_argument("--repository", required=True)
    run.add_argument("--branch", required=True)
    run.add_argument("--base-sha", required=True)
    run.add_argument("--head-sha", required=True)
    run.add_argument("--parent-task-id")
    run.add_argument("--whole-tree-verified", action="store_true")
    _add_output_option(run)
    run.set_defaults(handler=run_command)

    native = commands.add_parser(
        "run-native-claude", help="capture one registered Claude implementation worker"
    )
    native.add_argument(
        "--role", choices=tuple(role.value for role in NativeClaudeRole), required=True
    )
    native.add_argument("--prompt-file", type=Path, required=True)
    native.add_argument("--task-id", required=True)
    native.add_argument("--slice-id", required=True)
    native.add_argument("--parent-task-id", required=True)
    native.add_argument("--repository", required=True)
    native.add_argument("--branch", required=True)
    native.add_argument("--base-sha", required=True)
    native.add_argument("--validation-root", default=None)
    native.add_argument("--plan-root", default=None)
    native.add_argument("--plan-sha", default=None)
    native.add_argument("--evidence-root", default=None)
    native.add_argument("--evidence-pr", type=_evidence_pr, default=None)
    _add_output_option(native)
    native.set_defaults(handler=run_native_claude_command)

    print_validation = commands.add_parser(
        "print-validation-commands",
        help="print one validation role's exact gate commands (parent-only)",
    )
    print_validation.add_argument(
        "--role", choices=tuple(role.value for role in NativeClaudeRole), required=True
    )
    print_validation.add_argument("--validation-root", required=True)
    print_validation.add_argument(
        "--pytest-arg", type=_pytest_arg_token, action="append", default=None
    )
    print_validation.set_defaults(handler=print_validation_commands_command)

    outcome = commands.add_parser("annotate-outcome", help="append closed task outcome metadata")
    outcome.add_argument("--task-id", required=True)
    outcome.add_argument("--slice-id", required=True)
    outcome.add_argument("--finding-count", type=_finding_count, action="append", required=True)
    outcome.add_argument("--repair-commit-count", type=int, required=True)
    outcome.add_argument("--final-gate-passed", action="store_true")
    outcome.add_argument("--captured-at", type=_parse_time)
    _add_output_option(outcome)
    outcome.set_defaults(handler=annotate_outcome)

    capacity = commands.add_parser("snapshot-capacity", help="append qualitative capacity metadata")
    capacity.add_argument("--task-id", required=True)
    capacity.add_argument("--slice-id", required=True)
    capacity.add_argument(
        "--family", type=lambda value: _enum_option(HarnessFamily, value), required=True
    )
    capacity.add_argument(
        "--status", type=lambda value: _enum_option(CapacityStatus, value), required=True
    )
    capacity.add_argument(
        "--source", type=lambda value: _enum_option(CapacitySource, value), required=True
    )
    capacity.add_argument("--captured-at", type=_parse_time)
    _add_output_option(capacity)
    capacity.set_defaults(handler=snapshot_capacity)
    return parser


def _exit_with_error(message: str) -> NoReturn:
    """Exit without echoing parser input content."""
    raise SystemExit(f"capture-agent-usage: {message}") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Run a safe subcommand and present only metadata-safe errors."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    failure: str | None = None
    try:
        return int(arguments.handler(arguments))
    except CaptureUsageError as exc:
        failure = str(exc)
    except CodexUsageParseError:
        failure = "Codex usage stream is invalid"
    except ClaudeAuthorityError:
        failure = "Claude launch authority is not attested"
    except ClaudeUsageParseError:
        failure = "Claude usage stream is invalid"
    except OSError:
        failure = "local filesystem operation failed"
    except ValueError:
        failure = "metadata input is invalid"
    _exit_with_error(failure)


if __name__ == "__main__":
    sys.exit(main())
