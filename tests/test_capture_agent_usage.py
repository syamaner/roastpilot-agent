"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from capture_usage_claude import ClaudeUsageParseError, parse_claude_stream
from capture_usage_cli import CaptureUsageError, append_record, main
from capture_usage_codex import CodexUsageParseError, parse_codex_stream
from capture_usage_models import (
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
    """The Codex fixture provides only terminal usage, not content retention."""
    with (FIXTURES / "codex-0.147.0.jsonl").open(encoding="utf-8") as stream:
        usage = parse_codex_stream(stream)

    assert usage.input_tokens == 120
    assert usage.cached_input_tokens == 40
    assert usage.cache_creation_input_tokens == 10
    assert usage.output_tokens == 12
    assert usage.reasoning_output_tokens == 3
    serialized = usage.model_dump_json()
    assert "SANITIZED_MESSAGE" not in serialized
    assert "item_0" not in serialized


def test_claude_fixture_uses_whole_tree_terminal_model_usage() -> None:
    """Claude repeated message IDs cannot change result-level whole-tree totals."""
    with (FIXTURES / "claude-2.1.228.jsonl").open(encoding="utf-8") as stream:
        usage = parse_claude_stream(stream)

    assert usage.input_tokens == 16
    assert usage.cached_input_tokens == 20
    assert usage.cache_creation_input_tokens == 30
    assert usage.output_tokens == 20
    assert usage.estimated_usd == 0.123
    assert usage.estimate_basis is EstimateBasis.CLIENT_SIDE_ESTIMATE
    assert usage.claude_model_usage is not None
    assert [(item.model, item.input_tokens) for item in usage.claude_model_usage] == [
        ("synthetic-primary", 5),
        ("synthetic-secondary", 11),
    ]
    assert "SANITIZED_MESSAGE" not in usage.model_dump_json()


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
        parser([event])  # type: ignore[operator]


def test_malformed_or_missing_terminal_usage_fails_closed() -> None:
    """A zero-exit-like stream without required terminal usage cannot normalize."""
    with pytest.raises(CodexUsageParseError, match="terminal"):
        parse_codex_stream(['{"type":"turn.started"}\n'])
    with pytest.raises(ClaudeUsageParseError, match="terminal"):
        parse_claude_stream(['{"type":"assistant"}\n'])
    with pytest.raises(CodexUsageParseError, match="schema"):
        parse_codex_stream(['{"type":"turn.completed","usage":{"input_tokens":1}}\n'])


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


def test_sink_uses_private_modes_and_refuses_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink is append-only, metadata-only, mode-restricted, and no-follow."""
    monkeypatch.chdir(tmp_path)
    sink = Path("usage/records.jsonl")
    append_record(sink, _task_record())
    append_record(sink, _task_record())
    assert stat.S_IMODE(sink.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(sink.stat().st_mode) == 0o600
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["record_type"] == "TASK_USAGE"

    target = Path("target.jsonl")
    link = Path("link.jsonl")
    link.symlink_to(target)
    with pytest.raises(CaptureUsageError, match="securely open"):
        append_record(link, _task_record())
    assert not target.exists()


def test_capacity_and_outcome_commands_persist_closed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capacity avoids raw percentages and outcome rejects unknown review lenses."""
    monkeypatch.chdir(tmp_path)
    sink = Path("usage.jsonl")
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
