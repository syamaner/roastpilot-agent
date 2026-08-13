"""Parent-only, metadata-only local agent-usage capture commands."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import NoReturn, TypeVar

from capture_usage_claude import (
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
    MAX_STREAM_BYTES,
    USAGE_RECORD_ADAPTER,
    CapacitySnapshotRecord,
    CapacitySource,
    CapacityStatus,
    FindingLens,
    FindingSeverity,
    HarnessFamily,
    OutcomeRecord,
    ParsedUsage,
    TaskUsageRecord,
    UsageRecord,
)

DEFAULT_SINK = Path(".agent-usage/usage.jsonl")
SINK_ROOT = ".agent-usage"
MAX_PROMPT_BYTES = 65_536
"""Prompt cap limits one explicitly-authorized stdin payload to 64 KiB."""
LAUNCH_TIMEOUT_SECONDS = 3600
VERSION_TIMEOUT_SECONDS = 5
TERMINATE_GRACE_SECONDS = 1
_SEMVER = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_VERIFIED_HARNESS_VERSIONS = {
    HarnessFamily.CODEX: "0.147.0",
    HarnessFamily.CLAUDE: "2.1.228",
}
_GIT_TIMEOUT_SECONDS = 5
_GIT_OUTPUT_LIMIT = 4096


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
    _print_parsed_json(
        parse_codex_stream(BytesIO(_input_bytes(arguments.stream, MAX_STREAM_BYTES)))
    )
    return 0


def parse_claude_command(arguments: argparse.Namespace) -> int:
    """Parse a supplied sanitized Claude JSONL file without creating a record."""
    _print_parsed_json(
        parse_claude_stream(BytesIO(_input_bytes(arguments.stream, MAX_STREAM_BYTES)))
    )
    return 0


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
        parser = (
            parse_codex_stream if arguments.harness is HarnessFamily.CODEX else parse_claude_stream
        )
        try:
            usage = parser(process.stdout)
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
        except (CodexUsageParseError, ClaudeUsageParseError):
            raise CaptureUsageError("harness usage stream is invalid") from None
        exit_code = process.wait(timeout=LAUNCH_TIMEOUT_SECONDS)
        if timed_out.is_set():
            raise CaptureUsageError("harness run timed out")
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
    if normalized_role in {"engineer-be", "engineer-fe", "repair"}:
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
        "--schema-version", action="version", version=str(AGENT_USAGE_SCHEMA_VERSION)
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
    raise SystemExit(f"capture-agent-usage: {message}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a safe subcommand and present only metadata-safe errors."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except CaptureUsageError as exc:
        _exit_with_error(str(exc))
    except CodexUsageParseError:
        _exit_with_error("Codex usage stream is invalid")
    except ClaudeUsageParseError:
        _exit_with_error("Claude usage stream is invalid")
    except OSError:
        _exit_with_error("local filesystem operation failed")
    except ValueError:
        _exit_with_error("metadata input is invalid")


if __name__ == "__main__":
    sys.exit(main())
