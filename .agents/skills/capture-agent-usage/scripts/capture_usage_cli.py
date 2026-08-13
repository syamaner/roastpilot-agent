"""Safe non-launching commands for the local agent-usage capture pilot.

The contract-required live harness runner is deliberately absent until its prompt-to-
external-service authorization is explicitly approved. These commands only parse local
sanitized streams or append closed metadata records.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TypeVar

from capture_usage_claude import ClaudeUsageParseError, parse_claude_stream
from capture_usage_codex import CodexUsageParseError, parse_codex_stream
from capture_usage_models import (
    AGENT_USAGE_SCHEMA_VERSION,
    USAGE_RECORD_ADAPTER,
    CapacitySnapshotRecord,
    CapacitySource,
    CapacityStatus,
    FindingLens,
    FindingSeverity,
    HarnessFamily,
    OutcomeRecord,
    UsageRecord,
)

DEFAULT_SINK = Path(".agent-usage/usage.jsonl")


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
    """Reject absolute and traversal sink paths before filesystem access."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("sink path must be a relative non-traversing path")
    return path


def append_record(path: Path, record: UsageRecord) -> None:
    """Validate and append one record through a private, no-follow JSONL sink.

    Args:
        path: The relative output sink selected by the closed CLI grammar.
        record: A normalized closed-schema record to validate before appending.

    Raises:
        CaptureUsageError: If the sink is unsafe or cannot be securely opened.
    """
    if path.is_absolute() or ".." in path.parts:
        raise CaptureUsageError("sink path must be relative and non-traversing")
    validated = USAGE_RECORD_ADAPTER.validate_python(record)
    directory = path.parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_status = directory.lstat()
        if stat.S_ISLNK(directory_status.st_mode) or not stat.S_ISDIR(directory_status.st_mode):
            raise CaptureUsageError("usage sink directory is not a real directory")
        os.chmod(directory, 0o700)
        if not hasattr(os, "O_NOFOLLOW"):
            raise CaptureUsageError("platform does not support no-follow usage sink opens")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise CaptureUsageError("could not securely open usage sink") from exc
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (validated.model_dump_json() + "\n").encode("utf-8"))
    except OSError as exc:
        raise CaptureUsageError("could not append usage record") from exc
    finally:
        os.close(descriptor)


def _print_parsed_json(value: object) -> None:
    """Print only normalized parser fields for explicit local inspection."""
    print(value.model_dump_json())  # type: ignore[union-attr]


def parse_codex_command(arguments: argparse.Namespace) -> int:
    """Parse a supplied sanitized Codex JSONL file without creating a record."""
    with arguments.stream.open("r", encoding="utf-8") as stream:
        _print_parsed_json(parse_codex_stream(stream))
    return 0


def parse_claude_command(arguments: argparse.Namespace) -> int:
    """Parse a supplied sanitized Claude JSONL file without creating a record."""
    with arguments.stream.open("r", encoding="utf-8") as stream:
        _print_parsed_json(parse_claude_stream(stream))
    return 0


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
        counts.setdefault(lens, {})[severity] = count
    append_record(
        arguments.output,
        OutcomeRecord(
            captured_at=arguments.captured_at or _utc_now(),
            task_id=arguments.task_id,
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
    """Build the safe, non-launching command grammar for the capture utility."""
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

    outcome = commands.add_parser("annotate-outcome", help="append closed task outcome metadata")
    outcome.add_argument("--task-id", required=True)
    outcome.add_argument("--finding-count", type=_finding_count, action="append", required=True)
    outcome.add_argument("--repair-commit-count", type=int, required=True)
    outcome.add_argument("--final-gate-passed", action="store_true")
    outcome.add_argument("--captured-at", type=_parse_time)
    _add_output_option(outcome)
    outcome.set_defaults(handler=annotate_outcome)

    capacity = commands.add_parser("snapshot-capacity", help="append qualitative capacity metadata")
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
    except (
        CaptureUsageError,
        CodexUsageParseError,
        ClaudeUsageParseError,
        OSError,
        ValueError,
    ) as exc:
        _exit_with_error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
