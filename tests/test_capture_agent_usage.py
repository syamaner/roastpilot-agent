"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import capture_usage_cli as usage_cli
import pytest
from capture_usage_claude import (
    CLAUDE_EVENT_TYPES,
    CLAUDE_MODEL_USAGE_KEYS,
    CLAUDE_RESULT_USAGE_KEYS,
    CLAUDE_SYSTEM_SUBTYPES,
    ClaudeUsageParseError,
    parse_claude_stream,
)
from capture_usage_cli import CaptureUsageError, append_record, main
from capture_usage_codex import CodexUsageParseError, parse_codex_stream
from capture_usage_models import (
    MAX_EVENT_BYTES,
    MAX_EVENT_COUNT,
    MAX_STREAM_BYTES,
    CapacitySnapshotRecord,
    CapacitySource,
    CapacityStatus,
    EstimateBasis,
    HarnessFamily,
    ParsedUsage,
    TaskUsageRecord,
)
from pydantic import ValidationError

_REAL_VALIDATE_WORKTREE_METADATA = usage_cli._validate_worktree_metadata  # pyright: ignore[reportPrivateUsage]
FIXTURES = Path(__file__).parent / "fixtures" / "agent-usage"


@pytest.fixture(autouse=True)
def isolate_run_metadata_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep launcher tests provider-free unless they exercise Git admission directly."""

    def skip_worktree_validation(_arguments: argparse.Namespace) -> None:
        return None

    monkeypatch.setattr(usage_cli, "_validate_worktree_metadata", skip_worktree_validation)


def _stream(*events: str) -> BytesIO:
    """Build a binary JSONL stream matching the future fixed subprocess stdout type."""
    return BytesIO("".join(events).encode("utf-8"))


def _codex_terminal_event() -> bytes:
    """Return one complete synthetic Codex terminal event as binary JSONL."""
    return (
        b'{"type":"turn.completed","usage":{"input_tokens":1,'
        b'"cached_input_tokens":0,"cache_write_input_tokens":0,'
        b'"output_tokens":1,"reasoning_output_tokens":0}}\n'
    )


def _opaque_codex_event(size: int) -> bytes:
    """Return a complete opaque Codex event with exactly ``size`` bytes."""
    prefix = b'{"type":"turn.started","opaque":"'
    suffix = b'"}\n'
    assert size >= len(prefix) + len(suffix)
    return prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix


def _task_record() -> TaskUsageRecord:
    """Build a minimal complete task record without user or harness contents."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    return TaskUsageRecord(
        captured_at=now,
        task_id="811",
        slice_id="capture-usage",
        harness=HarnessFamily.CODEX,
        role="measurement-pilot",
        model="gpt-5.6-terra",
        repository="syamaner/roastpilot-agent",
        branch="feature/811-capture-agent-usage",
        base_sha="2bed7013",
        head_sha="4a3cca6",
        started_at=now,
        completed_at=now,
        elapsed_ms=0,
        exit_code=0,
        success=True,
        harness_version="0.147.0",
        input_tokens=120,
        cached_input_tokens=40,
        cache_creation_input_tokens=10,
        output_tokens=12,
        reasoning_output_tokens=3,
        usage_complete=True,
    )


def test_codex_fixture_extracts_terminal_usage_without_content() -> None:
    """A tool-using Codex fixture retains only terminal usage totals."""
    with (FIXTURES / "codex-0.147.0.jsonl").open("rb") as stream:
        usage = parse_codex_stream(stream)

    assert usage.input_tokens == 120
    assert usage.cached_input_tokens == 40
    assert usage.cache_creation_input_tokens == 10
    assert usage.output_tokens == 12
    assert usage.reasoning_output_tokens == 3
    serialized = usage.model_dump_json()
    assert "SANITIZED_MESSAGE" not in serialized
    assert "item_0" not in serialized
    assert "SANITIZED_COMMAND" not in serialized
    assert "SANITIZED_TOOL_OUTPUT" not in serialized


def test_claude_fixture_uses_whole_tree_terminal_model_usage() -> None:
    """Only per-model whole-tree totals survive conflicting messages and top-level usage."""
    with (FIXTURES / "claude-2.1.228.jsonl").open("rb") as stream:
        usage = parse_claude_stream(stream)

    assert usage.input_tokens == 16
    assert usage.cached_input_tokens == 20
    assert usage.cache_creation_input_tokens == 30
    assert usage.output_tokens == 20
    assert usage.estimated_usd == pytest.approx(0.123)
    assert usage.estimate_basis is EstimateBasis.CLIENT_SIDE_ESTIMATE
    assert usage.claude_terminal_success is True
    assert usage.claude_model_usage is not None
    assert [
        (
            item.model,
            item.input_tokens,
            item.cached_input_tokens,
            item.cache_creation_input_tokens,
            item.output_tokens,
            item.estimated_usd,
        )
        for item in usage.claude_model_usage
    ] == [
        ("synthetic-primary", 5, 8, 13, 7, 0.045),
        ("synthetic-secondary", 11, 12, 17, 13, 0.078),
    ]
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    assert terminal["usage"]["input_tokens"] != usage.input_tokens
    assert terminal["total_cost_usd"] != usage.estimated_usd
    assert "SANITIZED_MESSAGE" not in usage.model_dump_json()
    assert "SANITIZED_TOOL_RESULT" not in usage.model_dump_json()


def test_claude_2_1_231_fixture_matches_frozen_grammar_without_content() -> None:
    """The admitted Claude fixture changes neither grammar nor retained content."""
    fixture = FIXTURES / "claude-2.1.231.jsonl"
    events = [json.loads(line) for line in fixture.read_text().splitlines()]
    with fixture.open("rb") as stream:
        usage = parse_claude_stream(stream)

    assert {event["type"] for event in events} <= CLAUDE_EVENT_TYPES
    assert (
        frozenset({"system", "user", "assistant", "rate_limit_event", "result"})
        == CLAUDE_EVENT_TYPES
    )
    assert {
        event["subtype"] for event in events if event["type"] == "system"
    } <= CLAUDE_SYSTEM_SUBTYPES
    assert frozenset({"hook_started", "hook_response", "init"}) == CLAUDE_SYSTEM_SUBTYPES
    terminal = events[-1]
    assert set(terminal["usage"]) == CLAUDE_RESULT_USAGE_KEYS
    assert all(
        set(model_usage) == CLAUDE_MODEL_USAGE_KEYS
        for model_usage in terminal["modelUsage"].values()
    )
    assert usage.input_tokens == 5
    assert usage.cached_input_tokens == 11
    assert usage.cache_creation_input_tokens == 13
    assert usage.output_tokens == 7
    assert usage.estimated_usd == pytest.approx(0.017)
    serialized = usage.model_dump_json()
    for sentinel in ("SANITIZED_MESSAGE", "SANITIZED_RESULT", "request_0", "message_0"):
        assert sentinel not in serialized

    terminal["usage"]["SANITIZED_MESSAGE"] = 0
    with pytest.raises(ClaudeUsageParseError) as exc_info:
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))
    assert "SANITIZED_MESSAGE" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("parser", "event"),
    [
        (parse_codex_stream, '{"type":"unexpected"}\n'),
        (parse_claude_stream, '{"type":"unexpected"}\n'),
    ],
)
def test_unknown_events_fail_closed(parser: object, event: str) -> None:
    """Schema drift cannot be silently skipped into a partial total."""
    with pytest.raises((CodexUsageParseError, ClaudeUsageParseError)):
        parser(_stream(event))  # type: ignore[operator]


def test_parser_errors_do_not_echo_untrusted_discriminators() -> None:
    """Event type and subtype drift reports only a fixed safe category."""
    sentinel = "SENTINEL_EVENT_CONTENT"
    with pytest.raises(CodexUsageParseError) as codex_error:
        parse_codex_stream(_stream(json.dumps({"type": sentinel}) + "\n"))
    assert sentinel not in str(codex_error.value)

    with pytest.raises(ClaudeUsageParseError) as claude_type_error:
        parse_claude_stream(_stream(json.dumps({"type": sentinel}) + "\n"))
    assert sentinel not in str(claude_type_error.value)

    with pytest.raises(ClaudeUsageParseError) as claude_subtype_error:
        parse_claude_stream(_stream(json.dumps({"type": "system", "subtype": sentinel}) + "\n"))
    assert sentinel not in str(claude_subtype_error.value)


def test_malformed_or_missing_terminal_usage_fails_closed() -> None:
    """A zero-exit-like stream without required terminal usage cannot normalize."""
    with pytest.raises(CodexUsageParseError, match="terminal"):
        parse_codex_stream(_stream('{"type":"turn.started"}\n'))
    with pytest.raises(ClaudeUsageParseError, match="terminal"):
        parse_claude_stream(_stream('{"type":"assistant"}\n'))
    with pytest.raises(CodexUsageParseError, match="schema"):
        parse_codex_stream(_stream('{"type":"turn.completed","usage":{"input_tokens":1}}\n'))
    with pytest.raises(CodexUsageParseError):
        parse_codex_stream(_stream('{"type":"unexpected"}\n'))


def test_codex_updated_and_failed_events_are_recognized_without_terminal_usage() -> None:
    """Observed opaque updates and failed turns take the missing-terminal path."""
    with pytest.raises(CodexUsageParseError, match="terminal"):
        parse_codex_stream(_stream('{"type":"item.updated"}\n{"type":"turn.failed"}\n'))


@pytest.mark.parametrize(
    "stream",
    [
        _codex_terminal_event() + b'{"type":"turn.failed"}\n',
        b'{"type":"turn.failed"}\n' + _codex_terminal_event(),
        b'{"type":"turn.failed"}\n{"type":"turn.failed"}\n',
        _codex_terminal_event() + _codex_terminal_event(),
    ],
)
def test_codex_multiple_terminal_markers_fail_closed(stream: bytes) -> None:
    """Failed and completed terminal markers cannot coexist or repeat in either order."""
    with pytest.raises(CodexUsageParseError, match="multiple Codex terminal"):
        parse_codex_stream(BytesIO(stream))


def test_nonzero_recognized_codex_failure_appends_one_incomplete_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognized failed turn is an incomplete failed capture, not parser drift."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")

    class Input:
        """Minimal writable child stdin."""

        closed = False

        def write(self, value: bytes) -> int:
            return len(value)

        def close(self) -> None:
            self.closed = True

    class Process:
        """Completed version or failed-turn harness stub."""

        def __init__(self, output: bytes, code: int) -> None:
            self.stdin = Input()
            self.stdout = BytesIO(output)
            self.code = code

        def poll(self) -> int:
            return self.code

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.code

        def terminate(self) -> None:
            raise AssertionError("completed process must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed process must not kill")

    def fake_popen(argv: list[str], **_: object) -> Process:
        if argv[-1] == "--version":
            return Process(b"codex 0.147.0\n", 0)
        return Process(b'{"type":"turn.failed"}\n', 1)

    def fake_which(_: str) -> str:
        return "/stub/codex"

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    assert (
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
                "--whole-tree-verified",
            ]
        )
        == 0
    )
    records = (tmp_path / ".agent-usage/usage.jsonl").read_text().splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert not record["success"] and not record["usage_complete"]
    assert record["input_tokens"] is None and record["estimated_usd"] is None
    assert not record["whole_tree_verified"]


def test_binary_stream_ingestion_rejects_overlong_partial_and_invalid_utf8_events() -> None:
    """The reader rejects unsafe input before decoding or parsing event content."""
    sentinel = b"SENTINEL_OVERSIZED_EVENT"
    with pytest.raises(CodexUsageParseError) as overlong:
        parse_codex_stream(BytesIO(b'{"type":"' + sentinel * MAX_EVENT_BYTES + b'"}\n'))
    assert "size limit" in str(overlong.value)
    assert sentinel.decode() not in str(overlong.value)

    with pytest.raises(CodexUsageParseError, match="partial event"):
        parse_codex_stream(BytesIO(b'{"type":"turn.started"}'))
    with pytest.raises(ClaudeUsageParseError, match="invalid UTF-8"):
        parse_claude_stream(BytesIO(b"\xff\n"))
    with pytest.raises(CodexUsageParseError, match="malformed Codex JSONL event"):
        parse_codex_stream(BytesIO(b"{not-json}\n"))


def test_binary_stream_ingestion_rejects_event_count_and_total_byte_overflow() -> None:
    """Finite caps bound both endless short streams and many valid large events."""
    with pytest.raises(CodexUsageParseError, match="event count limit"):
        parse_codex_stream(BytesIO(b'{"type":"turn.started"}\n' * (MAX_EVENT_COUNT + 1)))

    oversized_total = _opaque_codex_event(MAX_EVENT_BYTES) * (
        MAX_STREAM_BYTES // MAX_EVENT_BYTES + 1
    )
    with pytest.raises(CodexUsageParseError, match="total byte limit"):
        parse_codex_stream(BytesIO(oversized_total))


def test_binary_stream_ingestion_accepts_an_exact_event_byte_boundary() -> None:
    """A complete event at the per-event limit remains valid and is not retained."""
    usage = parse_codex_stream(
        BytesIO(_opaque_codex_event(MAX_EVENT_BYTES) + _codex_terminal_event())
    )
    assert usage.input_tokens == 1
    assert "opaque" not in usage.model_dump_json()


def test_binary_stream_ingestion_checks_trailing_schema_after_terminal_usage() -> None:
    """A terminal usage event never suppresses later stream-schema drift."""
    with pytest.raises(CodexUsageParseError, match="unknown Codex event type"):
        parse_codex_stream(BytesIO(_codex_terminal_event() + b'{"type":"unexpected"}\n'))


@pytest.mark.parametrize(
    "stream",
    [
        b'{"type":"turn.started","type":"turn.started"}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":2}}\n',
    ],
)
def test_codex_duplicate_json_keys_fail_closed(stream: bytes) -> None:
    """Duplicate discriminators or nested token keys cannot be normalized ambiguously."""
    with pytest.raises(CodexUsageParseError, match="duplicate JSON keys"):
        parse_codex_stream(BytesIO(stream))


@pytest.mark.parametrize("removed_key", ["speed", "input_tokens"])
def test_claude_terminal_usage_missing_installed_key_fails_closed(removed_key: str) -> None:
    """Every installed terminal usage key is required even when not retained."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    del terminal["usage"][removed_key]
    with pytest.raises(ClaudeUsageParseError, match="usage schema"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))


def test_claude_terminal_usage_extra_or_model_usage_drift_fails_closed() -> None:
    """Unknown terminal or per-model schema keys cannot be silently accepted."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="usage schema"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))


def test_claude_duplicate_json_keys_fail_closed() -> None:
    """Duplicate result or model-usage keys cannot select a retained value silently."""
    duplicate_result = b'{"type":"result","type":"result"}\n'
    duplicate_model = (
        b'{"type":"result","subtype":"success","is_error":false,"usage":{},'
        b'"modelUsage":{"model":{"inputTokens":1,"inputTokens":2}}}\n'
    )
    for stream in (duplicate_result, duplicate_model):
        with pytest.raises(ClaudeUsageParseError, match="duplicate JSON keys"):
            parse_claude_stream(BytesIO(stream))


@pytest.mark.parametrize(
    ("field", "value"),
    [("subtype", "error"), ("subtype", "unknown"), ("is_error", True), ("is_error", "false")],
)
def test_claude_terminal_requires_observed_success_status(field: str, value: object) -> None:
    """Only the observed success/false result status may supply terminal usage."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal[field] = value
    with pytest.raises(ClaudeUsageParseError, match="status is invalid"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))


@pytest.mark.parametrize(
    "subtype",
    [
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_during_execution",
    ],
)
def test_claude_observed_failure_statuses_retain_terminal_usage(subtype: str) -> None:
    """Observed failure statuses still provide whole-tree terminal totals for failed runs."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["subtype"] = subtype
    terminal["is_error"] = True
    assert parse_claude_stream(_stream(json.dumps(terminal) + "\n")).input_tokens == 16


def test_claude_malformed_top_level_usage_and_non_finite_model_sum_fail_closed() -> None:
    """Top-level fields remain validated even though modelUsage alone supplies totals."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["input_tokens"] = "invalid"
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude usage field"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    for model in terminal["modelUsage"].values():
        model["costUSD"] = 1e308
    with pytest.raises(ClaudeUsageParseError, match="modelUsage costUSD sum"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["modelUsage"]["synthetic-primary"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="modelUsage schema"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))


def test_whole_tree_defaults_false_and_incomplete_usage_rejects_totals() -> None:
    """Records fail closed on unverified tree scope and incomplete accounting."""
    record = _task_record()
    assert record.whole_tree_verified is False
    with pytest.raises(ValidationError, match="incomplete usage"):
        values = _task_record().model_dump()
        values["usage_complete"] = False
        TaskUsageRecord(**values)


def test_estimate_basis_and_closed_record_grammar_reject_invalid_values() -> None:
    """Estimated cost is always labelled and extra/raw fields are prohibited."""
    with pytest.raises(ValidationError, match="CLIENT_SIDE_ESTIMATE"):
        ParsedUsage(estimated_usd=1.0)
    with pytest.raises(ValidationError):
        ParsedUsage.model_validate({"unknown_raw_output": "forbidden"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_estimates_reject_at_model_boundary(value: float) -> None:
    """Closed usage records cannot serialize non-finite estimated costs as null."""
    with pytest.raises(ValidationError):
        ParsedUsage(
            estimated_usd=value,
            estimate_basis=EstimateBasis.CLIENT_SIDE_ESTIMATE,
        )


@pytest.mark.parametrize("field", ["total_cost_usd", "costUSD"])
def test_non_finite_json_estimates_reject_at_parser_boundary(field: str) -> None:
    """JSON non-finite aggregate and per-model estimates fail closed."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    if field == "total_cost_usd":
        terminal[field] = float("inf")
    else:
        terminal["modelUsage"]["synthetic-primary"][field] = float("nan")
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude usage field"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"))


def test_sink_uses_private_modes_and_refuses_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink is append-only, metadata-only, mode-restricted, and no-follow."""
    monkeypatch.chdir(tmp_path)
    sink = tmp_path / ".agent-usage/usage.jsonl"
    sink = Path(".agent-usage/usage/records.jsonl")
    append_record(sink, _task_record())
    append_record(sink, _task_record())
    assert stat.S_IMODE(sink.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(sink.stat().st_mode) == 0o600
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["record_type"] == "TASK_USAGE"

    target = Path(".agent-usage/target.jsonl")
    link = Path(".agent-usage/link.jsonl")
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(target)
    with pytest.raises(CaptureUsageError, match="securely open"):
        append_record(link, _task_record())
    assert not target.exists()


def test_sink_rejects_short_os_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial JSONL write cannot be treated as a complete durable record."""
    monkeypatch.chdir(tmp_path)
    sink = tmp_path / ".agent-usage/usage.jsonl"
    original_write = usage_cli.os.write

    def short_write(_: int, payload: bytes) -> int:
        return len(payload) - 1

    monkeypatch.setattr(usage_cli.os, "write", short_write)
    with pytest.raises(CaptureUsageError, match="could not append usage record"):
        append_record(sink.relative_to(tmp_path), _task_record())
    monkeypatch.setattr(usage_cli.os, "write", original_write)
    append_record(sink.relative_to(tmp_path), _task_record())
    assert len(sink.read_text().splitlines()) == 1


def test_sink_parent_retries_a_secure_open_after_concurrent_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creator winning mkdir's EEXIST race is reopened through the no-follow path."""
    monkeypatch.chdir(tmp_path)
    original_mkdir = usage_cli.os.mkdir
    raced = False

    def create_then_report_race(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            original_mkdir(path, mode, dir_fd=dir_fd)
            raise FileExistsError
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(usage_cli.os, "mkdir", create_then_report_race)
    monkeypatch.setattr(
        usage_cli.os,
        "supports_dir_fd",
        usage_cli.os.supports_dir_fd | {create_then_report_race},
    )
    sink = Path(".agent-usage/raced/usage.jsonl")
    append_record(sink, _task_record())
    assert raced and sink.exists()


def test_sink_rejects_hard_link_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A multiply-linked sink target cannot be modified through capture."""
    monkeypatch.chdir(tmp_path)
    Path(".agent-usage").mkdir()
    Path(".agent-usage/usage.jsonl").write_text("existing\n")
    os.link(".agent-usage/usage.jsonl", ".agent-usage/other.jsonl")
    with pytest.raises(CaptureUsageError, match="could not append usage record"):
        append_record(Path(".agent-usage/usage.jsonl"), _task_record())


def test_sink_rejects_fifo_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nonblocking admission rejects a FIFO without opening a writer stall."""
    monkeypatch.chdir(tmp_path)
    Path(".agent-usage").mkdir()
    os.mkfifo(".agent-usage/usage.jsonl")
    with pytest.raises(CaptureUsageError, match="could not securely open usage sink"):
        append_record(Path(".agent-usage/usage.jsonl"), _task_record())


def test_fifo_inputs_fail_promptly_before_parse_or_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-writer FIFOs cannot block either the sanitized parser or prompt admission."""
    monkeypatch.chdir(tmp_path)
    parse_fifo = Path("sanitized.jsonl")
    prompt_fifo = Path("prompt")
    os.mkfifo(parse_fifo)
    os.mkfifo(prompt_fifo)

    with pytest.raises(SystemExit, match="input file is not an accepted regular input"):
        main(["parse-codex", str(parse_fifo)])

    def fail_which(_name: str) -> str:
        raise AssertionError("FIFO prompt must reject before provider lookup")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="prompt file cannot be safely opened"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                str(prompt_fifo),
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "measurement-pilot",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert not (tmp_path / ".agent-usage").exists()


def test_sink_refuses_nested_symlink_and_accepts_nested_real_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intermediate sink components cannot redirect records outside the cwd."""
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    Path(".agent-usage").mkdir()
    Path(".agent-usage/jump").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CaptureUsageError, match="securely open"):
        append_record(Path(".agent-usage/jump/nested/usage.jsonl"), _task_record())
    assert not (outside / "nested" / "usage.jsonl").exists()

    sink = Path(".agent-usage/real/nested/usage.jsonl")
    append_record(sink, _task_record())
    assert sink.exists()
    assert stat.S_IMODE(sink.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(sink.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(sink.stat().st_mode) == 0o600


def test_capacity_and_outcome_commands_persist_closed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capacity avoids raw percentages and outcome rejects unknown review lenses."""
    monkeypatch.chdir(tmp_path)
    sink = Path(".agent-usage/usage.jsonl")
    assert (
        main(
            [
                "snapshot-capacity",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--family",
                "codex",
                "--status",
                "healthy",
                "--source",
                "operator",
                "--output",
                str(sink),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "annotate-outcome",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--finding-count",
                "SECURITY:MEDIUM:2",
                "--repair-commit-count",
                "1",
                "--final-gate-passed",
                "--output",
                str(sink),
            ]
        )
        == 0
    )
    records = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert records[0] == CapacitySnapshotRecord(
        captured_at=datetime.fromisoformat(records[0]["captured_at"]),
        task_id="811",
        slice_id="capture",
        family=HarnessFamily.CODEX,
        status=CapacityStatus.HEALTHY,
        source=CapacitySource.OPERATOR,
    ).model_dump(mode="json")
    assert records[1]["finding_counts"] == {"SECURITY": {"MEDIUM": 2}}
    assert records[1]["slice_id"] == "capture"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "annotate-outcome",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--finding-count",
                "OTHER:MEDIUM:1",
                "--repair-commit-count",
                "0",
            ]
        )
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("family", "source"),
    [
        (HarnessFamily.CLAUDE, CapacitySource.CLI_STATUS),
        (HarnessFamily.CODEX, CapacitySource.CLI_USAGE),
    ],
)
def test_capacity_snapshot_rejects_impossible_source_family_pairs_without_append(
    family: HarnessFamily,
    source: CapacitySource,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct CLI source labels cannot be attributed to the other harness family."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    with pytest.raises(ValidationError, match="capacity snapshots require"):
        CapacitySnapshotRecord(
            captured_at=now,
            task_id="811",
            slice_id="capture",
            family=family,
            status=CapacityStatus.HEALTHY,
            source=source,
        )

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="metadata input is invalid"):
        main(
            [
                "snapshot-capacity",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--family",
                family.name.lower(),
                "--status",
                "healthy",
                "--source",
                source.name.lower(),
            ]
        )
    assert not (tmp_path / ".agent-usage").exists()


def test_annotate_outcome_rejects_duplicate_finding_counts_without_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lens/severity pair has one closed count and cannot be overwritten."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="duplicate finding count"):
        main(
            [
                "annotate-outcome",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--finding-count",
                "SECURITY:MEDIUM:1",
                "--finding-count",
                "SECURITY:MEDIUM:2",
                "--repair-commit-count",
                "0",
            ]
        )
    assert not (tmp_path / ".agent-usage").exists()


def test_run_uses_fixed_codex_argv_and_keeps_prompt_out_of_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in launcher passes prompt bytes only to fixed harness stdin."""
    monkeypatch.chdir(tmp_path)
    prompt = tmp_path / "prompt.txt"
    sentinel = b"SENTINEL_PROMPT_CONTENT"
    prompt.write_bytes(sentinel)
    observed: dict[str, object] = {}

    class FakeProcess:
        """Minimal fixed harness process with a complete terminal event."""

        class RecordingInput:
            """Minimal writable pipe retaining bytes only for this test assertion."""

            def __init__(self) -> None:
                self.value = b""
                self.closed = False

            def write(self, value: bytes) -> int:
                self.value += value
                return len(value)

            def close(self) -> None:
                self.closed = True

        def __init__(self, output: bytes, exit_code: int = 0) -> None:
            self.stdout = BytesIO(output)
            self.stdin = self.RecordingInput()
            self.exit_code = exit_code

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.exit_code

        def poll(self) -> int:
            return self.exit_code

        def terminate(self) -> None:
            raise AssertionError("completed process must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        if argv[-1] == "--version":
            observed["version_argv"] = argv
            observed["version_kwargs"] = kwargs
            return FakeProcess(b"codex-cli 0.147.0\n")
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        process = FakeProcess(_codex_terminal_event())
        observed["process"] = process
        return process

    def fake_which(_: str) -> str:
        return "/stub/codex"

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    assert (
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                str(prompt),
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--effort",
                "high",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
        == 0
    )
    assert observed["version_argv"] == ["/stub/codex", "--version"]
    assert observed["argv"] == [
        "/stub/codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-terra",
        "-c",
        "agents.enabled=false",
        "-c",
        'model_reasoning_effort="high"',
        "-",
    ]
    process = cast(FakeProcess, observed["process"])
    assert process.stdin.value == sentinel
    assert process.stdin.closed
    assert observed["kwargs"]["shell"] is False  # type: ignore[index]
    record = (tmp_path / ".agent-usage/usage.jsonl").read_text()
    assert sentinel.decode() not in record
    assert "SENTINEL_PROMPT_CONTENT" not in str(observed["argv"])
    assert "SENTINEL_PROMPT_CONTENT" not in str(observed["version_argv"])
    assert json.loads(record)["effort"] == "high"
    parsed_record = json.loads(record)
    assert parsed_record["harness_version"] == "0.147.0"
    assert not parsed_record["whole_tree_verified"]
    assert {
        item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*") if item.is_file()
    } == {"prompt.txt", ".agent-usage/usage.jsonl"}


def test_launch_argv_uses_fixed_claude_flags_and_closed_effort_mapping() -> None:
    """Claude has all installed mandatory flags and accepts no arbitrary effort form."""
    assert usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CLAUDE, "/stub/claude", "opus", "max"
    ) == [
        "/stub/claude",
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
        "opus",
        "--effort",
        "max",
    ]
    assert usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CODEX, "/stub/codex", "terra", None
    ) == [
        "/stub/codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--model",
        "terra",
        "-c",
        "agents.enabled=false",
        "-",
    ]


def test_deadline_callback_marks_exited_leader_with_possible_live_descendants() -> None:
    """A deadline still cleans a group when only its direct leader has exited."""

    class ExitedProcess:
        """Minimal completed process for the deadline boundary."""

        def poll(self) -> int:
            return 0

        pid = 1234

    timed_out = usage_cli.threading.Event()
    with pytest.MonkeyPatch.context() as monkeypatch:
        signals: list[int] = []

        def record_signal(_pid: int, value: signal.Signals) -> None:
            signals.append(int(value))

        monkeypatch.setattr(usage_cli.os, "killpg", record_signal)
        usage_cli._terminate_for_deadline(  # pyright: ignore[reportPrivateUsage]
            cast(subprocess.Popen[bytes], ExitedProcess()), timed_out
        )
    assert timed_out.is_set()
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_deadline_callback_has_one_definition_and_uses_robust_stop() -> None:
    """The timer callback cannot regress to a terminate-only cleanup path."""
    source = inspect.getsource(usage_cli)
    assert source.count("def _terminate_for_deadline(") == 1
    callback = usage_cli._terminate_for_deadline  # pyright: ignore[reportPrivateUsage]
    assert "_stop_process(process)" in inspect.getsource(callback)


def test_task_usage_record_rejects_exit_and_harness_family_mislabeling() -> None:
    """Persisted records cannot claim success or family-specific fields incorrectly."""
    base = _task_record().model_dump()
    with pytest.raises(ValidationError, match="success"):
        TaskUsageRecord.model_validate({**base, "exit_code": 1})
    with pytest.raises(ValidationError, match="Codex"):
        TaskUsageRecord.model_validate(
            {
                **base,
                "claude_model_usage": [
                    {
                        "model": "claude",
                        "input_tokens": 1,
                        "cached_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="whole-tree"):
        TaskUsageRecord.model_validate(
            {
                **base,
                "exit_code": 1,
                "success": False,
                "usage_complete": False,
                "whole_tree_verified": True,
                "input_tokens": None,
                "cached_input_tokens": None,
                "cache_creation_input_tokens": None,
                "output_tokens": None,
                "reasoning_output_tokens": None,
                "estimated_usd": None,
            }
        )


def test_record_builder_preserves_explicit_whole_tree_verification() -> None:
    """The opt-in assertion is false by default and true only when explicitly supplied."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    arguments = argparse.Namespace(
        started_at=now,
        task_id="811",
        slice_id="capture",
        harness=HarnessFamily.CODEX,
        role="measurement-pilot",
        model="gpt-5.6-terra",
        effort=None,
        repository="syamaner/roastpilot-agent",
        branch="feature/811",
        base_sha="2bed7013",
        head_sha="4a3cca6",
        parent_task_id=None,
        whole_tree_verified=True,
    )
    record = usage_cli._record_from_usage(  # pyright: ignore[reportPrivateUsage]
        arguments, "0.147.0", 0, ParsedUsage(input_tokens=1), now, 0
    )
    assert record.whole_tree_verified


def test_record_elapsed_ms_uses_monotonic_value_despite_wall_clock_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit timestamps may move backwards without corrupting monotonic elapsed time."""
    started = datetime(2026, 8, 13, 12, tzinfo=UTC)
    arguments = argparse.Namespace(
        started_at=started,
        task_id="811",
        slice_id="capture",
        harness=HarnessFamily.CODEX,
        role="measurement-pilot",
        model="gpt-5.6-terra",
        effort=None,
        repository="syamaner/roastpilot-agent",
        branch="feature/811",
        base_sha="2bed7013",
        head_sha="4a3cca6",
        parent_task_id=None,
        whole_tree_verified=False,
    )
    record = usage_cli._record_from_usage(  # pyright: ignore[reportPrivateUsage]
        arguments, "0.147.0", 0, ParsedUsage(input_tokens=1), started - timedelta(seconds=5), 250
    )
    assert record.completed_at < record.started_at
    assert record.elapsed_ms == 250


@pytest.mark.parametrize(
    ("query", "result", "message"),
    [
        (["remote", "get-url", "origin"], (0, "https://example.invalid/other.git"), "repository"),
        (["branch", "--show-current"], (0, "wrong-branch"), "branch or head"),
        (["rev-parse", "HEAD"], (0, "deadbeef"), "branch or head"),
        (["rev-parse", "--verify", "4a3cca6^{commit}"], (1, ""), "branch or head"),
        (["rev-parse", "--verify", "2bed7013^{commit}"], (0, "older-base"), "base metadata"),
        (["status", "--porcelain"], (0, " M unsafe"), "worktree is not clean"),
    ],
)
def test_worktree_metadata_admission_rejects_mismatch_before_provider_lookup(
    query: list[str],
    result: tuple[int, str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repository metadata must match a clean current Git worktree before any provider lookup."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/811",
        head_sha="4a3cca6",
        base_sha="2bed7013",
    )
    expected: dict[tuple[str, ...], tuple[int, str]] = {
        ("remote", "get-url", "origin"): (0, "https://github.com/syamaner/roastpilot-agent.git"),
        ("branch", "--show-current"): (0, "feature/811"),
        ("rev-parse", "HEAD"): (0, "4a3cca6"),
        ("rev-parse", "--verify", "4a3cca6^{commit}"): (0, "4a3cca6"),
        ("rev-parse", "--verify", "2bed7013^{commit}"): (0, "2bed7013"),
        ("merge-base", "HEAD", "origin/main"): (0, "2bed7013"),
        ("status", "--porcelain"): (0, ""),
    }
    expected[tuple(query)] = result

    def fake_git_output(command: list[str]) -> tuple[int, str]:
        return expected[tuple(command)]

    def fail_provider_lookup(_name: str) -> str:
        pytest.fail("provider lookup must follow Git admission")

    monkeypatch.setattr(usage_cli, "_git_output", fake_git_output)
    monkeypatch.setattr(
        usage_cli.shutil,
        "which",
        fail_provider_lookup,
    )
    with pytest.raises(CaptureUsageError, match=message):
        _REAL_VALIDATE_WORKTREE_METADATA(arguments)


def test_worktree_metadata_canonicalizes_short_shas_before_record_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted abbreviated input is replaced with resolved full commit identifiers."""
    head = "a" * 40
    base = "b" * 40
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/811",
        head_sha=head[:7],
        base_sha=base[:7],
        started_at=datetime(2026, 8, 13, tzinfo=UTC),
        task_id="811",
        slice_id="capture",
        harness=HarnessFamily.CODEX,
        role="measurement-pilot",
        model="gpt-5.6-terra",
        effort=None,
        parent_task_id=None,
        whole_tree_verified=False,
    )
    expected: dict[tuple[str, ...], tuple[int, str]] = {
        ("remote", "get-url", "origin"): (0, "https://github.com/syamaner/roastpilot-agent.git"),
        ("branch", "--show-current"): (0, "feature/811"),
        ("rev-parse", "HEAD"): (0, head),
        ("rev-parse", "--verify", f"{head[:7]}^{{commit}}"): (0, head),
        ("rev-parse", "--verify", f"{base[:7]}^{{commit}}"): (0, base),
        ("merge-base", "HEAD", "origin/main"): (0, base),
        ("status", "--porcelain"): (0, ""),
    }

    def fake_git_output(command: list[str]) -> tuple[int, str]:
        return expected[tuple(command)]

    monkeypatch.setattr(usage_cli, "_git_output", fake_git_output)

    _REAL_VALIDATE_WORKTREE_METADATA(arguments)
    assert (arguments.head_sha, arguments.base_sha) == (head, base)
    record = usage_cli._record_from_usage(  # pyright: ignore[reportPrivateUsage]
        arguments, "0.147.0", 0, ParsedUsage(input_tokens=1), arguments.started_at, 1
    )
    assert (record.head_sha, record.base_sha) == (head, base)


def test_git_output_runs_fixed_subprocess_and_returns_bounded_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Git admission helper accepts a small, valid fixed-command response."""
    marker = tmp_path / "argv"
    stub = tmp_path / "git"
    stub.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{marker}'\nprintf 'expected metadata\\n'\n"
    )
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    assert usage_cli._git_output(["status", "--porcelain"]) == (  # pyright: ignore[reportPrivateUsage]
        0,
        "expected metadata",
    )
    assert marker.read_text().strip() == "-c core.fsmonitor=false status --porcelain"


def test_git_status_admission_disables_ambient_fsmonitor_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed Git config prevents an inherited fsmonitor command from executing."""
    sentinel = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook))

    status, _ = usage_cli._git_output(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]
    assert status == 0
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("head -c 4097 /dev/zero", "unavailable"),
        ("printf '\\377'", "unavailable"),
    ],
)
def test_git_output_rejects_oversized_or_invalid_utf8_response(
    body: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git admission never retains over-limit or malformed command output."""
    stub = tmp_path / "git"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(CaptureUsageError, match=expected):
        usage_cli._git_output(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]


def test_git_output_timeout_kills_term_ignoring_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung fixed Git stub is killed as a session group and cannot survive admission."""
    marker = tmp_path / "git-stub-pid"
    stub = tmp_path / "git"
    stub.write_text(f"#!/bin/sh\ntrap '' TERM\necho $$ > '{marker}'\nsleep 30\n")
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(usage_cli, "_GIT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(usage_cli, "TERMINATE_GRACE_SECONDS", 0.01)
    real_start_deadline = usage_cli._start_deadline  # pyright: ignore[reportPrivateUsage]

    def start_after_stub_ready(
        process: subprocess.Popen[bytes], seconds: int, timed_out: threading.Event
    ) -> threading.Timer:
        for _ in range(50):
            if marker.exists():
                return real_start_deadline(process, seconds, timed_out)
            time.sleep(0.01)
        pytest.fail("fixed Git stub did not establish the timeout readiness handshake")

    monkeypatch.setattr(usage_cli, "_start_deadline", start_after_stub_ready)

    with pytest.raises(CaptureUsageError, match="Git worktree metadata is unavailable"):
        usage_cli._git_output(["status", "--porcelain"])  # pyright: ignore[reportPrivateUsage]

    pid = int(marker.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        proc_stat = Path(f"/proc/{pid}/stat")
        if not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Git timeout cleanup left a live fixed-command process")


def test_run_failed_exit_outcomes_and_parser_drift_do_not_misrecord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only recognized terminal usage can produce a complete failed-run record."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"PROMPT_SENTINEL")

    class Input:
        """Tiny closed test pipe."""

        closed = False

        def write(self, value: bytes) -> int:
            return len(value)

        def close(self) -> None:
            self.closed = True

    class Process:
        """Stub process returning an exact supplied stream and exit code."""

        def __init__(self, output: bytes, code: int) -> None:
            self.stdin = Input()
            self.stdout = BytesIO(output)
            self.code = code

        def poll(self) -> int:
            return self.code

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.code

        def terminate(self) -> None:
            raise AssertionError("completed stub must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed stub must not kill")

    def run_for(output: bytes, code: int, harness: str = "codex") -> tuple[int | None, str]:
        def fake_popen(argv: list[str], **_: object) -> Process:
            if argv[-1] == "--version":
                version = "0.147.0" if harness == "codex" else "2.1.231"
                return Process(f"{harness} {version}\n".encode(), 0)
            return Process(output, code)

        def fake_which(_: str) -> str:
            return "/stub/codex"

        monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
        monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
        args = [
            "run",
            "--harness",
            harness,
            "--prompt-file",
            "prompt",
            "--task-id",
            "811",
            "--slice-id",
            "capture",
            "--role",
            "validation",
            "--model",
            "gpt-5.6-terra",
            "--repository",
            "syamaner/roastpilot-agent",
            "--branch",
            "feature/811",
            "--base-sha",
            "2bed7013",
            "--head-sha",
            "4a3cca6",
        ]
        try:
            result = main(args)
        except SystemExit as exc:
            result = None
            error = str(exc)
        else:
            error = ""
        sink = tmp_path / ".agent-usage/usage.jsonl"
        return result, sink.read_text() if sink.exists() else error

    result, record = run_for(_codex_terminal_event(), 3)
    assert result == 0
    parsed = json.loads(record)
    assert not parsed["success"] and parsed["usage_complete"]
    assert parsed["input_tokens"] == 1

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    claude_terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    claude_terminal["subtype"] = "error_max_turns"
    claude_terminal["is_error"] = True
    result, record = run_for((json.dumps(claude_terminal) + "\n").encode(), 3, "claude")
    assert result == 0
    parsed = json.loads(record)
    assert not parsed["success"] and parsed["usage_complete"]
    assert parsed["input_tokens"] == 16

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    success_terminal = json.loads((FIXTURES / "claude-2.1.231.jsonl").read_text().splitlines()[-1])
    result, record = run_for((json.dumps(success_terminal) + "\n").encode(), 0, "claude")
    assert result == 0
    parsed = json.loads(record)
    assert parsed["success"] and parsed["usage_complete"]

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    result, error = run_for((json.dumps(success_terminal) + "\n").encode(), 3, "claude")
    assert result is None and "terminal status disagrees" in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    failure_terminal = dict(success_terminal)
    failure_terminal["subtype"] = "error_max_turns"
    failure_terminal["is_error"] = True
    result, error = run_for((json.dumps(failure_terminal) + "\n").encode(), 0, "claude")
    assert result is None and "terminal status disagrees" in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    result, record = run_for(b'{"type":"turn.started"}\n', 3)
    assert result == 0
    parsed = json.loads(record)
    assert not parsed["success"] and not parsed["usage_complete"]
    assert all(parsed[key] is None for key in ("input_tokens", "estimated_usd"))

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    result, error = run_for(b'{"type":"turn.started"}\n', 0)
    assert result is None and "terminal usage" in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    result, error = run_for(b'{"type":"unknown-SENTINEL"}\n', 3)
    assert result is None and "stream is invalid" in error
    assert "SENTINEL" not in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    for output in (_codex_terminal_event(), b'{"type":"turn.started"}\n'):
        validations = 0

        def admit_only_before_launch(_: argparse.Namespace) -> None:
            nonlocal validations
            validations += 1
            if validations == 2:
                raise CaptureUsageError("current worktree is not clean")

        monkeypatch.setattr(usage_cli, "_validate_worktree_metadata", admit_only_before_launch)
        result, error = run_for(output, 3)
        assert result is None and "worktree is not clean" in error
        assert validations == 2
        assert not (tmp_path / ".agent-usage/usage.jsonl").exists()


@pytest.mark.parametrize("output", [_codex_terminal_event(), b'{"type":"turn.started"}\n'])
def test_run_elapsed_snapshot_precedes_post_run_revalidation(
    output: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow post-run admission cannot inflate complete or incomplete harness elapsed time."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")
    ticks = iter((0.0, 0.125, 99.0))
    monkeypatch.setattr(usage_cli.time, "monotonic", lambda: next(ticks))
    validations = 0

    class Input:
        """Minimal successful prompt pipe."""

        closed = False

        def write(self, value: bytes) -> int:
            return len(value)

        def close(self) -> None:
            self.closed = True

    class Process:
        """Completed provider-free process stream."""

        def __init__(self, stream: bytes, code: int) -> None:
            self.stdin = Input()
            self.stdout = BytesIO(stream)
            self.code = code

        def poll(self) -> int:
            return self.code

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.code

        def terminate(self) -> None:
            raise AssertionError("completed process must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed process must not kill")

    def fake_popen(argv: list[str], **_: object) -> Process:
        return Process(b"codex 0.147.0\n", 0) if argv[-1] == "--version" else Process(output, 1)

    def revalidate(_: argparse.Namespace) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            assert next(ticks) == 99.0

    def fake_which(_name: str) -> str:
        return "/stub/codex"

    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli, "_validate_worktree_metadata", revalidate)
    assert (
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "measurement-pilot",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
        == 0
    )
    record = json.loads((tmp_path / ".agent-usage/usage.jsonl").read_text())
    assert record["elapsed_ms"] == 125


@pytest.mark.parametrize(
    ("version_output", "exit_code"),
    [(b"no version\n", 0), (b"x" * 4097, 0), (b"codex 0.147.0\n", 1)],
)
def test_invalid_version_never_spawns_a_run(
    version_output: bytes, exit_code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad, excessive, and nonzero version probes fail before a task launch."""

    class VersionProcess:
        """Completed version-only stub."""

        def __init__(self) -> None:
            self.stdout = BytesIO(version_output)

        def poll(self) -> int:
            return exit_code

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return exit_code

        def terminate(self) -> None:
            raise AssertionError("completed version stub must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed version stub must not kill")

    def fake_popen(*_args: object, **_kwargs: object) -> VersionProcess:
        return VersionProcess()

    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    with pytest.raises(CaptureUsageError, match="version"):
        usage_cli._harness_version("/stub/codex")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("harness", "version"),
    [
        ("codex", "0.146.0"),
        ("codex", "0.148.0"),
        ("claude", "2.1.228"),
        ("claude", "2.1.230"),
        ("claude", "2.1.232"),
    ],
)
def test_run_rejects_unverified_family_version_before_harness_launch(
    harness: str, version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only each frozen family version may proceed from its fixed version probe."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")
    calls = 0

    class VersionProcess:
        """Completed fixed version probe with no task-launch behavior."""

        stdin = None
        stdout = BytesIO(f"{harness} {version}\n".encode())

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed version probe must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed version probe must not kill")

    def fake_popen(*_args: object, **_kwargs: object) -> VersionProcess:
        nonlocal calls
        calls += 1
        return VersionProcess()

    def fake_which(_name: str) -> str:
        return "/stub/harness"

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    with pytest.raises(SystemExit, match="version is not verified"):
        main(
            [
                "run",
                "--harness",
                harness,
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "measurement-pilot",
                "--model",
                "model",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert calls == 1
    assert not (tmp_path / ".agent-usage").exists()


def test_version_probe_timeout_kills_reaps_and_never_reaches_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung version stream is killed and cannot create a task record."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(usage_cli, "VERSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(usage_cli, "TERMINATE_GRACE_SECONDS", 0.001)
    released = threading.Event()
    observed = {"calls": 0, "killed": False, "reaped": False}

    class BlockingOutput:
        """Version stdout that stays open until the child is killed."""

        def read(self, _: int) -> bytes:
            released.wait(timeout=1)
            return b""

        def close(self) -> None:
            return None

    class HungVersion:
        """TERM-ignoring version child that must be killed and reaped."""

        stdin = None
        stdout = BlockingOutput()

        def poll(self) -> int | None:
            return -9 if observed["killed"] else None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            observed["killed"] = True
            released.set()

        def wait(self, timeout: float | None = None) -> int:
            if not observed["killed"] and timeout is not None:
                raise subprocess.TimeoutExpired("stub", timeout)
            observed["reaped"] = True
            return -9

    def fake_popen(*_args: object, **_kwargs: object) -> HungVersion:
        observed["calls"] += 1
        return HungVersion()

    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    with pytest.raises(CaptureUsageError, match="version"):
        usage_cli._harness_version("/stub/codex")  # pyright: ignore[reportPrivateUsage]
    assert observed == {"calls": 1, "killed": True, "reaped": True}
    assert not (tmp_path / ".agent-usage").exists()


def test_harness_version_retains_only_the_first_semver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version metadata freezes the first semantic version from bounded output."""

    class VersionProcess:
        """Completed multi-version output stub."""

        stdin = None
        stdout = BytesIO(b"codex 0.147.0 compatibility 9.9.9\n")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed probe must not terminate")

        def kill(self) -> None:
            raise AssertionError("completed probe must not kill")

    def fake_popen(*_args: object, **_kwargs: object) -> VersionProcess:
        return VersionProcess()

    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    assert usage_cli._harness_version("/stub/codex") == "0.147.0"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("error", [OSError("PROMPT_SENTINEL"), BrokenPipeError()])
def test_writer_failure_is_fixed_and_parent_visible(error: OSError) -> None:
    """A stdin write failure returns a safe state instead of escaping the writer thread."""

    class BrokenInput:
        """Minimal pipe that rejects the prompt write."""

        closed = False

        def write(self, _: bytes) -> int:
            raise error

        def close(self) -> None:
            self.closed = True

    class Process:
        """Minimal process with the failing stdin pipe."""

        stdin = BrokenInput()

    assert not usage_cli._write_prompt(  # pyright: ignore[reportPrivateUsage]
        cast(subprocess.Popen[bytes], Process()), b"PROMPT_SENTINEL"
    )


@pytest.mark.parametrize("value", ["invalid", "2026-08-13T12:00:00"])
def test_captured_at_requires_valid_timezone(value: str) -> None:
    """Outcome timestamps cannot be malformed or timezone-naive."""
    with pytest.raises(argparse.ArgumentTypeError):
        usage_cli._parse_time(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("value", ["SECURITY:MEDIUM:-1", "SECURITY:UNKNOWN:1", "bad"])
def test_finding_count_rejects_malformed_or_negative_values(value: str) -> None:
    """Outcome finding counts remain a closed non-negative grammar."""
    with pytest.raises(argparse.ArgumentTypeError):
        usage_cli._finding_count(value)  # pyright: ignore[reportPrivateUsage]


def test_capture_modules_do_not_load_roaster_control_dependencies() -> None:
    """Usage capture stays import-separated from controller, safety, and MCP code."""
    blocked = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert not blocked.intersection(vars(usage_cli))
    scripts = Path(usage_cli.__file__).parent
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import capture_usage_cli, capture_usage_codex, capture_usage_claude, "
                "capture_usage_models; blocked={'roastpilot_agent.controller',"
                "'roastpilot_agent.safety','roastpilot_agent.mcp_client'}; "
                "raise SystemExit(bool(blocked & set(sys.modules)))"
            ),
        ],
        cwd=scripts,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("--task-id", "bad value"),
        ("--branch", "bad value"),
        ("--base-sha", "not-a-sha"),
        ("--repository", "invalid"),
    ],
)
def test_invalid_cli_metadata_rejects_before_executable_lookup(
    field: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed metadata grammar fails before any selected executable can run."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")
    arguments = [
        "run",
        "--harness",
        "codex",
        "--prompt-file",
        "prompt",
        "--task-id",
        "811",
        "--slice-id",
        "capture",
        "--role",
        "validation",
        "--model",
        "gpt-5.6-terra",
        "--repository",
        "syamaner/roastpilot-agent",
        "--branch",
        "feature/811",
        "--base-sha",
        "2bed7013",
        "--head-sha",
        "4a3cca6",
    ]
    arguments[arguments.index(field) + 1] = value

    def fail_which(_: str) -> str:
        raise AssertionError("invalid metadata must not spawn")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="metadata"):
        main(arguments)


def test_provider_free_executable_integration_captures_only_normalized_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local executable receives stdin and emits a bounded terminal stream only."""
    monkeypatch.chdir(tmp_path)
    prompt = tmp_path / "prompt"
    prompt.write_text("INTEGRATION_PROMPT_SENTINEL")
    stub = tmp_path / "codex"
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        "if '--version' in sys.argv: print('codex 0.147.0'); raise SystemExit()\n"
        "sys.stdin.buffer.read()\n"
        'print(\'{\\"type\\":\\"turn.completed\\",\\"usage\\":{\\"input_tokens\\":1,\\"cached_input_tokens\\":0,\\"cache_write_input_tokens\\":0,\\"output_tokens\\":1,\\"reasoning_output_tokens\\":0}}\')\n'
    )
    stub.chmod(0o700)

    def fake_which(_: str) -> str:
        return str(stub)

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    assert (
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
        == 0
    )
    record = (tmp_path / ".agent-usage/usage.jsonl").read_text()
    assert "INTEGRATION_PROMPT_SENTINEL" not in record
    assert json.loads(record)["input_tokens"] == 1


def test_early_closed_stdin_fails_without_appending_valid_terminal_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider-free child closing stdin early cannot yield a successful capture record."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")

    def large_prompt(_: Path) -> bytes:
        return b"x" * 1_048_576

    monkeypatch.setattr(usage_cli, "_prompt_bytes", large_prompt)
    stub = tmp_path / "codex"
    terminal = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
        separators=(",", ":"),
    )
    started = 'printf \'{"type":"turn.started","pad":"\'; head -c 60000 /dev/zero'
    stub.write_text(
        "#!/bin/sh\n"
        + 'if [ "$1" = "--version" ]; then echo \'codex 0.147.0\'; exit 0; fi\n'
        + started
        + " | tr '\\0' x; printf '\"}\\n'\n"
        + "exec 0<&-\n"
        + f"printf '%s\\n' '{terminal}'\n"
    )
    stub.chmod(0o700)

    def fake_which(_: str) -> str:
        return str(stub)

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    with pytest.raises(SystemExit, match="prompt delivery failed"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert not (tmp_path / ".agent-usage").exists()


def test_real_pipe_backpressure_interleaves_prompt_write_and_stdout_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real pipe requires parsing to start before the child drains prompt stdin.

    The 4 MiB transient test prompt exceeds normal pipe capacity. The child emits a
    valid event then waits for the parser-start marker before reading stdin. Thus a
    synchronous write-before-read parent cannot create the marker and times out,
    while the concurrent production writer/parser exchange completes under two seconds.
    """
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"prompt input is deliberately not persisted")
    marker = tmp_path / "parser-started"

    def large_prompt(_: Path) -> bytes:
        return b"p" * (4 * 1024 * 1024)

    original_parse = usage_cli.parse_codex_stream

    def mark_then_parse(stream: BinaryIO) -> ParsedUsage:
        marker.write_text("started")
        return original_parse(stream)

    monkeypatch.setattr(usage_cli, "_prompt_bytes", large_prompt)
    monkeypatch.setattr(usage_cli, "parse_codex_stream", mark_then_parse)
    stub = tmp_path / "codex"
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import sys, time\n"
        "if '--version' in sys.argv: print('codex 0.147.0'); raise SystemExit()\n"
        'print(\'{\\"type\\":\\"turn.started\\"}\', flush=True)\n'
        "while not __import__('pathlib').Path('parser-started').exists(): time.sleep(0.01)\n"
        "sys.stdin.buffer.read()\n"
        'print(\'{\\"type\\":\\"turn.completed\\",\\"usage\\":{\\"input_tokens\\":1,\\"cached_input_tokens\\":0,\\"cache_write_input_tokens\\":0,\\"output_tokens\\":1,\\"reasoning_output_tokens\\":0}}\')\n'
    )
    stub.chmod(0o700)

    def fake_which(_: str) -> str:
        return str(stub)

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli, "LAUNCH_TIMEOUT_SECONDS", 2)
    assert (
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
        == 0
    )
    assert {
        item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*") if item.is_file()
    } == {"prompt", "codex", "parser-started", ".agent-usage/usage.jsonl"}


def test_run_timeout_kills_and_reaps_term_ignoring_child_without_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end timer kills a TERM-ignoring stream before any append."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")
    monkeypatch.setattr(usage_cli, "LAUNCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(usage_cli, "TERMINATE_GRACE_SECONDS", 0.001)
    released = threading.Event()
    observed: dict[str, bool] = {"killed": False, "reaped": False}

    class BlockingOutput:
        """Pipe that remains open until the fake child is killed."""

        def readline(self, _: int) -> bytes:
            released.wait(timeout=1)
            return b""

        def close(self) -> None:
            return None

    class Input:
        """Writable stdin stub."""

        closed = False

        def write(self, value: bytes) -> int:
            return len(value)

        def close(self) -> None:
            self.closed = True

    class HungProcess:
        """A child that ignores terminate until kill releases its stdout."""

        stdin = Input()
        stdout = BlockingOutput()

        def poll(self) -> int | None:
            return -9 if observed["killed"] else None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            observed["killed"] = True
            released.set()

        def wait(self, timeout: float | None = None) -> int:
            if not observed["killed"] and timeout is not None:
                raise subprocess.TimeoutExpired("stub", timeout)
            observed["reaped"] = True
            return -9

    class VersionProcess:
        """A completed version probe stub."""

        stdin = None
        stdout = BytesIO(b"codex 0.147.0\n")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            raise AssertionError("version must not terminate")

        def kill(self) -> None:
            raise AssertionError("version must not kill")

    calls = 0

    def fake_popen(argv: list[str], **_: object) -> VersionProcess | HungProcess:
        nonlocal calls
        calls += 1
        return VersionProcess() if argv[-1] == "--version" else HungProcess()

    def fake_which(_name: str) -> str:
        return "/stub/codex"

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
    with pytest.raises(SystemExit, match="timed out"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert calls == 2 and observed == {"killed": True, "reaped": True}
    assert not (tmp_path / ".agent-usage").exists()


def test_timeout_kills_real_stdout_inheriting_descendant_without_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process-group deadline kills a TERM-ignoring descendant holding stdout open."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")
    marker = tmp_path / "descendant-pid"
    ready = tmp_path / "descendant-ready"
    stub = tmp_path / "codex"
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import os, signal, sys, time\n"
        "if '--version' in sys.argv: print('codex 0.147.0'); raise SystemExit()\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        " Path('descendant-pid').write_text(str(os.getpid()))\n"
        " Path('descendant-ready').write_text('ready')\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        " time.sleep(30)\n"
        " raise SystemExit()\n"
        "raise SystemExit()\n"
    )
    stub.write_text(
        stub.read_text().replace(
            "import os, signal, sys, time", "from pathlib import Path\nimport os, signal, sys, time"
        )
    )
    stub.chmod(0o700)
    monkeypatch.setattr(usage_cli, "LAUNCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(usage_cli, "TERMINATE_GRACE_SECONDS", 0.01)

    real_start_deadline = usage_cli._start_deadline  # pyright: ignore[reportPrivateUsage]
    deadline_calls = 0

    def start_after_descendant_ready(
        process: subprocess.Popen[bytes], seconds: int, timed_out: threading.Event
    ) -> threading.Timer:
        nonlocal deadline_calls
        deadline_calls += 1
        if deadline_calls == 2:
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.01)
            else:
                pytest.fail("descendant did not establish the timeout readiness handshake")
        return real_start_deadline(process, seconds, timed_out)

    def fake_which(_: str) -> str:
        return str(stub)

    monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
    monkeypatch.setattr(usage_cli, "_start_deadline", start_after_descendant_ready)
    with pytest.raises(SystemExit, match="timed out"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    descendant = int(marker.read_text())
    for _ in range(20):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        proc_stat = Path(f"/proc/{descendant}/stat")
        if not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("process-group cleanup left a live descendant")
    assert not (tmp_path / ".agent-usage").exists()


@pytest.mark.parametrize("option", ["--extra-arg", "--executable", "--cwd", "--env", "--timeout"])
def test_run_rejects_passthrough_options_before_lookup(
    option: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed grammar offers no command, cwd, environment, or timeout escape hatch."""

    def fail_which(_: str) -> str:
        raise AssertionError("unknown CLI option must reject before executable lookup")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit):
        main(["run", option, "unsafe"])


def test_prompt_cap_and_read_failure_reject_before_spawn_without_echoing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt growth and read errors fail closed before any executable lookup."""
    monkeypatch.chdir(tmp_path)
    sentinel = "PROMPT_READ_SENTINEL"
    Path("large").write_bytes(b"x" * (usage_cli.MAX_PROMPT_BYTES + 1))

    def fail_which(_: str) -> str:
        raise AssertionError("unsafe prompt must not spawn")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="prompt file") as oversized:
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "large",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert sentinel not in str(oversized.value)

    Path("read-error").write_text("safe")
    real_fdopen = usage_cli.os.fdopen

    def failing_fdopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError(sentinel)

    monkeypatch.setattr(usage_cli.os, "fdopen", failing_fdopen)
    with pytest.raises(SystemExit, match="prompt file") as failed_read:
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "read-error",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert sentinel not in str(failed_read.value)
    monkeypatch.setattr(usage_cli.os, "fdopen", real_fdopen)


def test_run_rejects_unsupported_effort_before_executable_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only installed-version effort values can influence fixed argv."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")

    def fail_which(_: str) -> str:
        raise AssertionError("executable lookup must not run")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="effort is not supported"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--effort",
                "unsafe",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )


@pytest.mark.parametrize(
    "role",
    [
        "engineer-be",
        "engineer-fe",
        "repair",
        "Engineer-BE",
        "REPAIR",
        "engineer_be",
        "engineer.fe",
        "engineer:be",
        "engineer--be",
        "repair-",
        "ENGINEER:_BE",
    ],
)
def test_run_rejects_protected_implementation_roles_before_lookup(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measurement capture cannot select an implementation or repair role."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")

    def fail_which(_: str) -> str:
        raise AssertionError("protected role must reject before executable lookup")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="role is not permitted"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                "prompt",
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                role,
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )
    assert not (tmp_path / ".agent-usage").exists()


@pytest.mark.parametrize("role", ["measurement-pilot", "repair-audit", "engineer-be-audit"])
def test_run_allows_neutral_or_near_miss_attribution_roles(role: str) -> None:
    """Only normalized exact protected role names are denied."""
    assert (
        usage_cli._validate_run_metadata(  # pyright: ignore[reportPrivateUsage]
            argparse.Namespace(
                task_id="811",
                slice_id="capture",
                harness=HarnessFamily.CODEX,
                role=role,
                model="gpt-5.6-terra",
                effort=None,
                repository="syamaner/roastpilot-agent",
                branch="feature/811",
                base_sha="2bed7013",
                head_sha="4a3cca6",
                whole_tree_verified=False,
                parent_task_id=None,
            )
        )
        is None
    )


@pytest.mark.parametrize("value", [Path("prompt-link"), Path("prompt-dir")])
def test_run_rejects_non_regular_or_symlink_prompt_before_launch(
    value: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt authorization refuses links and non-regular inputs before executable lookup."""
    monkeypatch.chdir(tmp_path)
    if value.name == "prompt-link":
        Path("target").write_text("x")
        value.symlink_to("target")
    else:
        value.mkdir()

    def fail_which(_: str) -> str:
        raise AssertionError("executable lookup must not run")

    monkeypatch.setattr(usage_cli.shutil, "which", fail_which)
    with pytest.raises(SystemExit, match="prompt file"):
        main(
            [
                "run",
                "--harness",
                "codex",
                "--prompt-file",
                str(value),
                "--task-id",
                "811",
                "--slice-id",
                "capture",
                "--role",
                "validation",
                "--model",
                "gpt-5.6-terra",
                "--repository",
                "syamaner/roastpilot-agent",
                "--branch",
                "feature/811",
                "--base-sha",
                "2bed7013",
                "--head-sha",
                "4a3cca6",
            ]
        )


def test_gitignore_contains_exactly_one_agent_usage_rule() -> None:
    """The local sink protection is concrete rather than a documentation claim."""
    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".agent-usage/") == 1


def test_sink_path_rejects_absolute_or_traversal_content() -> None:
    """The CLI exposes no arbitrary output destination option."""
    with pytest.raises(SystemExit):
        main(
            [
                "snapshot-capacity",
                "--family",
                "codex",
                "--status",
                "healthy",
                "--source",
                "operator",
                "--output",
                "/tmp/not-allowed.jsonl",
            ]
        )


@pytest.mark.parametrize(
    "sink",
    [
        Path("src/usage.jsonl"),
        Path("AGENTS.md"),
        Path(".agent-usage"),
        Path(".agent-usage-escape/file"),
        Path("/private/tmp/usage.jsonl"),
        Path("../usage.jsonl"),
    ],
)
def test_append_record_rejects_paths_outside_private_sink(
    sink: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Programmatic capture cannot modify tracked or lookalike path components."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CaptureUsageError, match="confined to .agent-usage"):
        append_record(sink, _task_record())
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / ".agent-usage-escape").exists()
    with pytest.raises(SystemExit):
        main(
            [
                "snapshot-capacity",
                "--family",
                "codex",
                "--status",
                "healthy",
                "--source",
                "operator",
                "--output",
                "../not-allowed.jsonl",
            ]
        )


def test_cli_filesystem_errors_do_not_echo_untrusted_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI filesystem failures report a fixed category without path disclosure."""
    monkeypatch.chdir(tmp_path)
    sentinel = "SENTINEL_PATH_CONTENT"
    with pytest.raises(SystemExit) as error:
        main(["parse-codex", sentinel])
    captured = capsys.readouterr()
    assert sentinel not in str(error.value)
    assert sentinel not in captured.err
    assert "input file cannot be safely opened" in str(error.value)
