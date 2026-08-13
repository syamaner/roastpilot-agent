"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import argparse
import inspect
import json
import stat
import subprocess
import threading
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast

import capture_usage_cli as usage_cli
import pytest
from capture_usage_claude import ClaudeUsageParseError, parse_claude_stream
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

FIXTURES = Path(__file__).parent / "fixtures" / "agent-usage"


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
        role="engineer-be",
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
        family=HarnessFamily.CODEX,
        status=CapacityStatus.HEALTHY,
        source=CapacitySource.OPERATOR,
    ).model_dump(mode="json")
    assert records[1]["finding_counts"] == {"SECURITY": {"MEDIUM": 2}}
    with pytest.raises(SystemExit) as error:
        main(
            [
                "annotate-outcome",
                "--task-id",
                "811",
                "--finding-count",
                "OTHER:MEDIUM:1",
                "--repair-commit-count",
                "0",
            ]
        )
    assert error.value.code == 2


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
                "engineer-be",
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
        "--model",
        "gpt-5.6-terra",
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
        "--model",
        "terra",
        "-",
    ]


def test_deadline_callback_does_not_mark_an_already_exited_process_timed_out() -> None:
    """A timer racing a natural exit cannot suppress a completed record."""

    class ExitedProcess:
        """Minimal completed process for the deadline boundary."""

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("an exited process must not be terminated")

    timed_out = usage_cli.threading.Event()
    usage_cli._terminate_for_deadline(  # pyright: ignore[reportPrivateUsage]
        cast(subprocess.Popen[bytes], ExitedProcess()), timed_out
    )
    assert not timed_out.is_set()


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


def test_record_builder_preserves_explicit_whole_tree_verification() -> None:
    """The opt-in assertion is false by default and true only when explicitly supplied."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    arguments = argparse.Namespace(
        started_at=now,
        task_id="811",
        slice_id="capture",
        harness=HarnessFamily.CODEX,
        role="engineer-be",
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
        arguments, "0.147.0", 0, ParsedUsage(input_tokens=1)
    )
    assert record.whole_tree_verified


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

    def run_for(output: bytes, code: int) -> tuple[int | None, str]:
        def fake_popen(argv: list[str], **_: object) -> Process:
            if argv[-1] == "--version":
                return Process(b"codex 0.147.0\n", 0)
            return Process(output, code)

        def fake_which(_: str) -> str:
            return "/stub/codex"

        monkeypatch.setattr(usage_cli.shutil, "which", fake_which)
        monkeypatch.setattr(usage_cli.subprocess, "Popen", fake_popen)
        args = [
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
            "engineer-be",
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
                "engineer-be",
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
                "engineer-be",
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
                "engineer-be",
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
                "engineer-be",
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
                "engineer-be",
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
    assert "local filesystem operation failed" in str(error.value)
