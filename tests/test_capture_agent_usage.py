"""Tests for the opt-in, metadata-only agent-usage capture pilot."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NoReturn, cast
from uuid import UUID

import capture_usage_claude as usage_claude
import capture_usage_cli as usage_cli
import capture_usage_codex as usage_codex
import capture_usage_transcript as usage_transcript
import pytest
from capture_usage_claude import (
    CLAUDE_EVENT_TYPES,
    CLAUDE_MODEL_USAGE_KEYS,
    CLAUDE_RESULT_USAGE_KEYS,
    CLAUDE_SYSTEM_SUBTYPES,
    ClaudeAuthorityError,
    ClaudeUsageParseError,
    parse_claude_stream,
)
from capture_usage_cli import CaptureUsageError, append_record, main
from capture_usage_codex import (
    MAX_CODEX_OPAQUE_EVENT_BYTES,
    MAX_CODEX_OPAQUE_TOTAL_BYTES,
    MAX_JSON_NESTING_DEPTH,
    READ_CHUNK_BYTES,
    CodexUsageParseError,
    parse_codex_stream,
)
from capture_usage_models import (
    MAX_EVENT_BYTES,
    MAX_EVENT_COUNT,
    MAX_STREAM_BYTES,
    NATIVE_ROLE_EXCLUSIONS,
    BoundedStreamError,
    CapacitySnapshotRecord,
    CapacitySource,
    CapacityStatus,
    EstimateBasis,
    HarnessFamily,
    NativeClaudeRole,
    NativeWorkerUsageRecord,
    ParsedUsage,
    RoleCapability,
    TaskUsageRecord,
    UsageRecord,
    bounded_jsonl_lines,
)
from capture_usage_models import (
    USAGE_RECORD_ADAPTER as _USAGE_RECORD_ADAPTER,  # pyright: ignore[reportUnknownVariableType]
)
from pydantic import TypeAdapter, ValidationError

_REAL_VALIDATE_WORKTREE_METADATA = usage_cli._validate_worktree_metadata  # pyright: ignore[reportPrivateUsage]
FIXTURES = Path(__file__).parent / "fixtures" / "agent-usage"
USAGE_RECORD_ADAPTER = cast(TypeAdapter[UsageRecord], _USAGE_RECORD_ADAPTER)


def test_owned_transcript_counts_identical_assistant_usage_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A byte-identical duplicate assistant usage is counted once without mutation."""
    session_id = "11111111-1111-4111-8111-111111111233"
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / usage_transcript._project_name(tmp_path)  # pyright: ignore[reportPrivateUsage]
    project.mkdir(parents=True)
    original = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    lines = original.splitlines()
    duplicate = lines[2].replace(
        b'"uuid":"11111111-1111-4111-8111-111111111235"',
        b'"uuid":"11111111-1111-4111-8111-111111111237"',
    )
    assert duplicate != lines[2]
    duplicated = b"\n".join((*lines[:3], duplicate, *lines[3:])) + b"\n"
    transcript = project / f"{session_id}.jsonl"
    transcript.write_bytes(duplicated)
    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)

    usage = usage_transcript.parse_owned_transcript(
        tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
    )

    assert usage.usage_message_count == 1
    assert usage.input_tokens == 2
    assert usage.cached_input_tokens == 0
    assert usage.cache_creation_input_tokens == 36_369
    assert usage.output_tokens == 9
    assert transcript.read_bytes() == duplicated


@pytest.mark.parametrize(
    "fixture",
    ["claude-2.1.231.jsonl", "claude-2.1.231-native.jsonl"],
)
def test_historical_2_1_231_stream_fixtures_reject_current_version(fixture: str) -> None:
    """Current stream admission rejects each retained historical Claude version fixture."""
    content = (FIXTURES / fixture).read_bytes()
    with pytest.raises(ClaudeUsageParseError, match="unverified Claude version") as error:
        parse_claude_stream(BytesIO(content), require_launch_authority=True)
    assert "SYNTHETIC" not in str(error.value)


@pytest.mark.parametrize(
    ("fixture", "role", "session_id"),
    [
        (
            "parent.jsonl",
            NativeClaudeRole.ENGINEER_BE,
            "11111111-1111-4111-8111-111111111111",
        ),
        (
            "engineer-fe.jsonl",
            NativeClaudeRole.ENGINEER_FE,
            "88888888-8888-4888-8888-888888888888",
        ),
    ],
)
def test_historical_2_1_231_transcript_fixtures_reject_current_version(
    fixture: str,
    role: NativeClaudeRole,
    session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current owned-transcript admission rejects both retained historical role fixtures."""
    content = (FIXTURES / "claude-2.1.231-transcript" / fixture).read_bytes()
    _, installed_session = _install_owned_transcript(tmp_path, monkeypatch, content, session_id)
    with pytest.raises(usage_transcript.TranscriptError) as error:
        usage_transcript.parse_owned_transcript(tmp_path, installed_session, role, "high")
    assert str(error.value) == "owned Claude transcript is invalid"
    assert "SYNTHETIC" not in str(error.value)


def test_owned_transcript_accepts_repeated_matching_agent_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude may repeat the same parent-role attestation during one session."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    lines = content.splitlines()
    repeated = b"\n".join((*lines[:2], lines[0], *lines[2:])) + b"\n"
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, repeated)

    usage = usage_transcript.parse_owned_transcript(
        tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
    )

    assert usage.usage_message_count == 1
    assert usage.model == "claude-sonnet-5"
    assert transcript.read_bytes() == repeated


def _install_owned_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    session_id: str = "11111111-1111-4111-8111-111111111233",
) -> tuple[Path, str]:
    """Install a synthetic parent at its one accepted exact location."""
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / usage_transcript._project_name(tmp_path)  # pyright: ignore[reportPrivateUsage]
    project.mkdir(parents=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_bytes(content)
    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
    return transcript, session_id


@pytest.mark.parametrize(
    "old,new",
    [
        (b'"agentSetting":"engineer-be"', b'"agentSetting":"engineer-fe"'),
        (b'"version":"2.1.233"', b'"version":"2.1.999"'),
        (b'"effort":"high"', b'"effort":"low"'),
        (b'"input_tokens":2', b'"input_tokens":true'),
        (b'"input_tokens":2', b'"input_tokens":-1'),
        (b'"output_tokens":9', b'"extra_usage":1,"output_tokens":9'),
    ],
)
def test_owned_transcript_rejects_closed_mutations(
    old: bytes, new: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity and accounting schema drift is rejected without transcript mutation."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    mutated = content.replace(old, new)
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, mutated)
    before = transcript.stat()
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    after = transcript.stat()
    assert (after.st_ino, after.st_mtime_ns, transcript.read_bytes()) == (
        before.st_ino,
        before.st_mtime_ns,
        mutated,
    )


@pytest.mark.parametrize(
    ("fixture", "role", "effort", "model", "totals"),
    [
        (
            "parent.jsonl",
            NativeClaudeRole.ENGINEER_BE,
            "high",
            "claude-sonnet-5",
            (2, 0, 36_369, 9),
        ),
        (
            "engineer-fe.jsonl",
            NativeClaudeRole.ENGINEER_FE,
            "high",
            "claude-sonnet-5",
            (2, 6_289, 29_307, 9),
        ),
        (
            "story-planner.jsonl",
            NativeClaudeRole.STORY_PLANNER,
            "high",
            "claude-opus-5",
            (12, 1_417_065, 288_420, 31_562),
        ),
        (
            "safety-reviewer.jsonl",
            NativeClaudeRole.SAFETY_REVIEWER,
            "xhigh",
            "claude-opus-5",
            (2, 0, 31_282, 9),
        ),
        (
            "security-reviewer.jsonl",
            NativeClaudeRole.SECURITY_REVIEWER,
            "high",
            "claude-sonnet-5",
            (3, 11, 7, 13),
        ),
    ],
)
def test_owned_transcript_parses_every_committed_2_1_233_role_fixture(
    fixture: str,
    role: NativeClaudeRole,
    effort: str,
    model: str,
    totals: tuple[int, int, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every committed 2.1.233 transcript fixture parses to its exact role and totals."""
    content = (FIXTURES / "claude-2.1.233-transcript" / fixture).read_bytes()
    session_id = json.loads(content.splitlines()[0])["sessionId"]
    _, installed_session_id = _install_owned_transcript(tmp_path, monkeypatch, content, session_id)
    usage = usage_transcript.parse_owned_transcript(tmp_path, installed_session_id, role, effort)
    assert usage.model == model
    assert usage.usage_message_count == 1
    assert (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_creation_input_tokens,
        usage.output_tokens,
    ) == totals


def _mode_row_add_extra_key(row: dict[str, object]) -> dict[str, object]:
    """Add an unobserved key to the closed metadata-only ``mode`` row shape."""
    return {**row, "extra": "SENTINEL"}


def _mode_row_drop_mode_key(row: dict[str, object]) -> dict[str, object]:
    """Drop the required ``mode`` key from the row."""
    return {key: value for key, value in row.items() if key != "mode"}


def _mode_row_use_integer_mode(row: dict[str, object]) -> dict[str, object]:
    """Replace the required string ``mode`` value with a non-string value."""
    return {**row, "mode": 1}


def _mode_row_use_null_mode(row: dict[str, object]) -> dict[str, object]:
    """Replace the required string ``mode`` value with ``null``."""
    return {**row, "mode": None}


def _mode_row_use_unobserved_string(row: dict[str, object]) -> dict[str, object]:
    """Replace the exact observed mode value with another string."""
    return {**row, "mode": "plan"}


@pytest.mark.parametrize(
    "mutator",
    [
        _mode_row_add_extra_key,
        _mode_row_drop_mode_key,
        _mode_row_use_integer_mode,
        _mode_row_use_null_mode,
        _mode_row_use_unobserved_string,
    ],
)
def test_owned_transcript_mode_row_admits_exact_shape_only(
    mutator: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed metadata-only ``mode`` row admits only its exact key/value shape."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "story-planner.jsonl").read_bytes()
    lines = content.splitlines()
    mode_index = next(
        index for index, line in enumerate(lines) if json.loads(line)["type"] == "mode"
    )
    mode_row = json.loads(lines[mode_index])
    assert set(mode_row) == {"type", "mode", "sessionId"}
    assert isinstance(mode_row["mode"], str)

    lines[mode_index] = json.dumps(mutator(mode_row)).encode()
    mutated = b"\n".join(lines) + b"\n"
    _, session_id = _install_owned_transcript(tmp_path, monkeypatch, mutated, mode_row["sessionId"])
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.STORY_PLANNER, "high"
        )


def _ai_title_add_extra_key(row: dict[str, object]) -> dict[str, object]:
    """Add an unobserved metadata key to the closed ai-title row."""
    return {**row, "extra": "SENTINEL"}


def _ai_title_drop_title(row: dict[str, object]) -> dict[str, object]:
    """Remove the required metadata title field."""
    return {key: value for key, value in row.items() if key != "aiTitle"}


def _ai_title_use_integer_title(row: dict[str, object]) -> dict[str, object]:
    """Replace the title with a non-string value."""
    return {**row, "aiTitle": 1}


def _ai_title_use_integer_session(row: dict[str, object]) -> dict[str, object]:
    """Replace the session binding with a non-string value."""
    return {**row, "sessionId": 1}


@pytest.mark.parametrize(
    "mutator",
    [
        _ai_title_add_extra_key,
        _ai_title_drop_title,
        _ai_title_use_integer_title,
        _ai_title_use_integer_session,
    ],
)
def test_owned_transcript_ai_title_row_admits_exact_shape_only(
    mutator: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed ``ai-title`` row is metadata-only and closed to its exact shape."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "security-reviewer.jsonl").read_bytes()
    lines = content.splitlines()
    title_index = next(
        index for index, line in enumerate(lines) if json.loads(line)["type"] == "ai-title"
    )
    title_row = json.loads(lines[title_index])
    lines[title_index] = json.dumps(mutator(title_row)).encode()
    mutated = b"\n".join(lines) + b"\n"
    _, session_id = _install_owned_transcript(
        tmp_path, monkeypatch, mutated, title_row["sessionId"]
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.SECURITY_REVIEWER, "high"
        )


def test_native_transcript_binds_model_and_effort_to_the_pinned_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript naming a wrong model family or a wrong effort fails closed."""
    planner_content = (FIXTURES / "claude-2.1.233-transcript" / "story-planner.jsonl").read_bytes()
    planner_pin = usage_cli._native_role_pin(NativeClaudeRole.STORY_PLANNER)  # pyright: ignore[reportPrivateUsage]
    assert planner_pin.model == "claude-opus-5"

    fable_content = planner_content.replace(b'"model":"claude-opus-5"', b'"model":"claude-fable-5"')
    case = tmp_path / "planner"
    case.mkdir()
    _, session_id = _install_owned_transcript(
        case, monkeypatch, fable_content, "33333333-3333-4333-8333-333333333233"
    )
    usage = usage_transcript.parse_owned_transcript(
        case, session_id, NativeClaudeRole.STORY_PLANNER, planner_pin.effort
    )
    assert usage.model == "claude-fable-5" != planner_pin.model

    safety_content = (FIXTURES / "claude-2.1.233-transcript" / "safety-reviewer.jsonl").read_bytes()
    safety_pin = usage_cli._native_role_pin(NativeClaudeRole.SAFETY_REVIEWER)  # pyright: ignore[reportPrivateUsage]
    assert safety_pin.effort == "xhigh"

    downgraded = safety_content.replace(b'"effort":"xhigh"', b'"effort":"high"')
    case = tmp_path / "safety"
    case.mkdir()
    _, session_id = _install_owned_transcript(
        case, monkeypatch, downgraded, "44444444-4444-4444-8444-444444444233"
    )
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            case, session_id, NativeClaudeRole.SAFETY_REVIEWER, safety_pin.effort
        )

    case = tmp_path / "safety-matching"
    case.mkdir()
    _, session_id = _install_owned_transcript(
        case, monkeypatch, safety_content, "44444444-4444-4444-8444-444444444233"
    )
    usage = usage_transcript.parse_owned_transcript(
        case, session_id, NativeClaudeRole.SAFETY_REVIEWER, safety_pin.effort
    )
    assert usage.model == safety_pin.model == "claude-opus-5"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_owned_transcript_rejects_unsafe_exact_file_types(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-tree reading never follows or parses an unsafe final filesystem object."""
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, b"safe\n")
    transcript.unlink()
    if kind == "symlink":
        transcript.symlink_to(tmp_path / "outside")
    elif kind == "hardlink":
        target = tmp_path / "outside"
        target.write_bytes(b"safe\n")
        os.link(target, transcript)
    elif kind == "fifo":
        os.mkfifo(transcript)
    else:
        transcript.mkdir()
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )


def test_owned_transcript_rejects_subagents_and_lifecycle_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaf authority and the 120-second run binding are mandatory."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, content)
    session_tree = transcript.with_suffix("")
    (session_tree / "subagents").mkdir(parents=True)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    (session_tree / "subagents").rmdir()
    session_tree.rmdir()
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path,
            session_id,
            NativeClaudeRole.ENGINEER_BE,
            "high",
            started_at=datetime(2026, 8, 1, tzinfo=UTC),
            completed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_project_encoding_is_ascii_safe_without_discovery() -> None:
    """Installed path encoding is deterministic and resolver source never scans projects."""
    assert usage_transcript._project_name(Path("/tmp/café spaces")) == "-tmp-caf--spaces"  # pyright: ignore[reportPrivateUsage]
    source = inspect.getsource(usage_transcript)
    for forbidden in ("glob(", "rglob(", "os.walk", "listdir", "scandir"):
        assert forbidden not in source


@pytest.mark.parametrize("name", ["parent.jsonl", "session-directory"])
def test_preexisting_exact_session_rejects_before_launch(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated session cannot be bound to stale Claude-owned state."""
    session_id = "11111111-1111-4111-8111-111111111111"
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / usage_transcript._project_name(tmp_path)  # pyright: ignore[reportPrivateUsage]
    project.mkdir(parents=True)
    if name == "parent.jsonl":
        (project / f"{session_id}.jsonl").write_bytes(b"x\n")
    else:
        (project / session_id).mkdir()
    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.reject_existing_owned_session(tmp_path, session_id)


@pytest.mark.parametrize("component", [".claude", "projects", "project"])
def test_preflight_rejects_unsafe_or_missing_resolver_components(
    component: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a securely absent final project is accepted as first-use state."""
    home = tmp_path / "home"
    if component == ".claude":
        home.mkdir()
        (home / ".claude").symlink_to(tmp_path / "outside")
    else:
        (home / ".claude").mkdir(parents=True)
        if component == "projects":
            (home / ".claude" / "projects").symlink_to(tmp_path / "outside")
        else:
            projects = home / ".claude" / "projects"
            projects.mkdir()
            (projects / usage_transcript._project_name(tmp_path)).symlink_to(tmp_path / "outside")  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.reject_existing_owned_session(
            tmp_path, "11111111-1111-4111-8111-111111111233"
        )


def _native_record_payload() -> dict[str, object]:
    """Build one complete native record payload for boundary validation."""
    now = datetime(2026, 8, 14, tzinfo=UTC)
    return {
        "captured_at": now,
        "task_id": "816",
        "slice_id": "native",
        "native_role": "engineer-be",
        "role_capability": "WRITE",
        "model": "claude-sonnet-5",
        "effort": "high",
        "repository": "syamaner/roastpilot-agent",
        "branch": "feature/816",
        "base_sha": "4c1ac63",
        "final_head_sha": "7d60f41",
        "parent_task_id": "parent",
        "session_id": "11111111-1111-4111-8111-111111111233",
        "subagent_count": 0,
        "usage_message_count": 1,
        "started_at": now,
        "completed_at": now,
        "elapsed_ms": 0,
        "exit_code": 0,
        "success": True,
        "harness_version": "2.1.233",
        "input_tokens": 2,
        "cached_input_tokens": 3,
        "cache_creation_input_tokens": 4,
        "output_tokens": 5,
        "claude_model_usage": [
            {
                "model": "claude-sonnet-5",
                "input_tokens": 2,
                "cached_input_tokens": 3,
                "cache_creation_input_tokens": 4,
                "output_tokens": 5,
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("whole_tree_verified", False),
        ("subagent_count", 1),
        ("usage_message_count", 0),
        ("output_tokens", 6),
        ("model", "other"),
    ],
)
def test_native_record_rejects_incomplete_or_inconsistent_truth(field: str, value: object) -> None:
    """False tree evidence, descendants and inconsistent sole-model sums are unrepresentable."""
    payload = _native_record_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        NativeWorkerUsageRecord.model_validate(payload)


def test_native_record_uses_distinct_schema_v3_and_adapter_roundtrips() -> None:
    """D163 records cannot be mistaken for the generic v1 append-only record shape."""
    record = NativeWorkerUsageRecord.model_validate(_native_record_payload())
    serialized = json.loads(record.model_dump_json())
    assert serialized["schema_version"] == 3
    roundtrip = USAGE_RECORD_ADAPTER.validate_json(record.model_dump_json())
    assert isinstance(roundtrip, NativeWorkerUsageRecord) and roundtrip.schema_version == 3
    prior_shape = dict(serialized)
    prior_shape["schema_version"] = 2
    with pytest.raises(ValidationError):
        NativeWorkerUsageRecord.model_validate(prior_shape)
    missing_capability = dict(serialized)
    del missing_capability["role_capability"]
    with pytest.raises(ValidationError):
        NativeWorkerUsageRecord.model_validate(missing_capability)
    assert _task_record().schema_version == 1


def test_native_record_capability_enforces_read_only_and_write_provenance() -> None:
    """Schema v3 encodes the distinct final-head invariants for each capability."""
    read_only = _native_record_payload()
    read_only["role_capability"] = "READ_ONLY"
    read_only["final_head_sha"] = read_only["base_sha"]
    assert (
        NativeWorkerUsageRecord.model_validate(read_only).role_capability
        is RoleCapability.READ_ONLY
    )
    read_only["final_head_sha"] = "7d60f41"
    with pytest.raises(ValidationError):
        NativeWorkerUsageRecord.model_validate(read_only)

    write = _native_record_payload()
    write["final_head_sha"] = write["base_sha"]
    with pytest.raises(ValidationError):
        NativeWorkerUsageRecord.model_validate(write)


def test_schema_version_cli_reports_generic_and_native_worker_families(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Discovery exposes both append-only record families without changing generic v1."""
    with pytest.raises(SystemExit) as result:
        main(["--schema-version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == "generic=1 native-worker=3\n"


def test_native_cli_exposes_no_caller_session_id() -> None:
    """The parent, never the caller, owns UUID attribution."""
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(
            ["run-native-claude", "--role", "engineer-be", "--session-id", "caller"]
        )
    for override in ("--model", "--effort"):
        with pytest.raises(SystemExit):
            usage_cli.build_parser().parse_args(
                ["run-native-claude", "--role", "engineer-be", override, "caller"]
            )


def _transcript_mutators() -> tuple[Callable[[bytes], bytes], ...]:
    """Return typed JSONL mutations for closed parser boundary checks."""
    return (
        lambda value: value[:-1],
        lambda value: value.replace(b"\n", b"", 1),
        lambda value: value.replace(b'"type":"agent-setting"', b'"type":"unknown"', 1),
        lambda value: value.replace(b'"type":"queue-operation"', b'"type":"ai-title"', 1),
        lambda value: value.replace(b'"sessionId":', b'"sessionId":"wrong", "sessionId":', 1),
        lambda value: value.replace(
            b'"sessionId":"11111111-1111-4111-8111-111111111233"', b'"sessionId":"wrong"', 1
        ),
        lambda value: value.replace(
            b'"effort":"high"', b'"mode":"metadata-only","effort":"high"', 1
        ),
        lambda value: value.replace(
            b'"parentUuid":"11111111-1111-4111-8111-111111111234"', b'"parentUuid":7', 1
        ),
    )


@pytest.mark.parametrize("mutator", _transcript_mutators())
def test_owned_transcript_rejects_jsonl_and_binding_boundaries(
    mutator: Callable[[bytes], bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSONL, duplicate keys, blank rows and malformed parent identity fail closed."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, mutator(content))
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    assert transcript.read_bytes() != b""


def test_owned_transcript_rejects_remaining_identity_and_usage_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact parent binds a setting, session, effort, time and nonempty totals."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    lines = content.splitlines()
    conflicting = list(lines)
    conflicting[2] = conflicting[2].replace(b'"output_tokens":9', b'"output_tokens":8')
    variants = (
        b"\n".join(lines[1:]) + b"\n",
        content.replace(b'"timestamp":"2026-08-18T00:00:01.000Z"', b'"timestamp":"invalid"', 1),
        content.replace(b'"usage":{"input_tokens":2', b'"usage":{}', 1),
        b"\n".join(lines[:2]) + b"\n",
        b"\n".join((*lines, conflicting[2])) + b"\n",
    )
    for index, variant in enumerate(variants):
        case = tmp_path / str(index)
        case.mkdir()
        transcript, session_id = _install_owned_transcript(case, monkeypatch, variant)
        with pytest.raises(usage_transcript.TranscriptError):
            usage_transcript.parse_owned_transcript(
                case, session_id, NativeClaudeRole.ENGINEER_BE, "high"
            )
        assert transcript.read_bytes() == variant
        transcript.unlink()


def test_owned_transcript_rejects_assistant_root_agent_id_without_retaining_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent transcript cannot claim a child-agent identity at the assistant root."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    lines = content.splitlines()
    lines[2] = lines[2].replace(
        b'"version":"2.1.233"',
        b'"agentId":"SENTINEL_CHILD_ID","version":"2.1.233"',
        1,
    )
    transcript, session_id = _install_owned_transcript(
        tmp_path, monkeypatch, b"\n".join(lines) + b"\n"
    )
    with pytest.raises(usage_transcript.TranscriptError) as error:
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    assert str(error.value) == "owned Claude transcript is invalid"
    assert "SENTINEL_CHILD_ID" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert not (tmp_path / ".agent-usage").exists()
    assert b"SENTINEL_CHILD_ID" in transcript.read_bytes()


@pytest.mark.parametrize(
    "content,session_id",
    [
        (
            b'{"type":"assistant","secret":"SENTINEL_TRANSCRIPT_JSON"}\n',
            "11111111-1111-4111-8111-111111111233",
        ),
        (
            b'{"type":"assistant","secret":"SENTINEL_TRANSCRIPT_UTF8\xff"}\n',
            "11111111-1111-4111-8111-111111111233",
        ),
        (b"", "SENTINEL_TRANSCRIPT_PATH"),
    ],
)
def test_owned_transcript_errors_are_fixed_and_content_free(
    content: bytes, session_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed transcript bytes and inaccessible paths never escape through exception state."""
    transcript, expected_session = _install_owned_transcript(tmp_path, monkeypatch, content)
    if session_id != expected_session:
        transcript.unlink()
    with pytest.raises(usage_transcript.TranscriptError) as error:
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    assert str(error.value) in {
        "owned Claude transcript is unavailable",
        "owned Claude transcript is invalid",
    }
    assert "SENTINEL_TRANSCRIPT" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_owned_transcript_reader_never_mutates_its_exact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader has no write, create, delete, rename or copy capability on transcripts."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, content)
    before = transcript.stat()

    def deny_mutation(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("transcript reader must not mutate")

    for name in ("write", "mkdir", "unlink", "rename"):
        monkeypatch.setattr(os, name, deny_mutation)
    monkeypatch.setattr(shutil, "copyfile", deny_mutation)
    assert (
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        ).usage_message_count
        == 1
    )
    after = transcript.stat()
    assert (after.st_ino, after.st_mtime_ns, transcript.read_bytes()) == (
        before.st_ino,
        before.st_mtime_ns,
        content,
    )


def test_owned_transcript_rejects_row_and_file_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact parser independently applies row, file and row-count limits."""
    content = (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl").read_bytes()
    transcript, session_id = _install_owned_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROWS", 1)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROWS", 500_000)
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROW_BYTES", 1)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_ROW_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(usage_transcript, "MAX_TRANSCRIPT_BYTES", 1)
    with pytest.raises(usage_transcript.TranscriptError):
        usage_transcript.parse_owned_transcript(
            tmp_path, session_id, NativeClaudeRole.ENGINEER_BE, "high"
        )
    assert transcript.read_bytes() == content


@pytest.mark.parametrize("role", list(NativeClaudeRole))
def test_native_argv_is_exact_and_generic_measurement_stays_ephemeral(
    role: NativeClaudeRole,
) -> None:
    """D161 session persistence is native-only and no native stdout grammar survives."""
    session_id = "11111111-1111-4111-8111-111111111233"
    pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
    argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude", role, pin.capability, pin.effort, session_id
    )
    assert argv == [
        "claude",
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
        "--permission-mode",
        "auto" if pin.capability is RoleCapability.WRITE else "plan",
        "--effort",
        pin.effort,
    ]
    assert "--no-session-persistence" not in argv and "--output-format" not in argv
    generic = usage_cli._launch_argv(HarnessFamily.CLAUDE, "claude", "model", "high")  # pyright: ignore[reportPrivateUsage]
    assert "--no-session-persistence" in generic
    generic_without_effort = usage_cli._launch_argv(  # pyright: ignore[reportPrivateUsage]
        HarnessFamily.CLAUDE, "claude", "model", None
    )
    assert "--effort" not in generic_without_effort


@pytest.mark.parametrize(
    "role", ["ui-reviewer", "repair", "engineer-be ", "Engineer-BE", "engineer_be", "x"]
)
def test_native_roles_reject_before_provider_lookup(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only byte-exact registered implementation leaves enter native lookup."""

    def no_which(_name: str) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(
        usage_cli.shutil,
        "which",
        no_which,
    )
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(["run-native-claude", "--role", role])


def test_native_role_roster_pins_and_capabilities_match_committed_frontmatter() -> None:
    """D163 admits exactly the committed native roster with derived capabilities."""
    expected = {
        "engineer-be": ("claude-sonnet-5", "high", RoleCapability.WRITE),
        "engineer-fe": ("claude-sonnet-5", "high", RoleCapability.WRITE),
        "mcp-contract-checker": ("claude-sonnet-5", "medium", RoleCapability.READ_ONLY),
        "planning-architect": ("claude-opus-5", "high", RoleCapability.READ_ONLY),
        "pr-triage": ("claude-sonnet-5", "high", RoleCapability.READ_ONLY),
        "product-auditor": ("claude-sonnet-5", "high", RoleCapability.READ_ONLY),
        "qa": ("claude-sonnet-5", "high", RoleCapability.READ_ONLY),
        "safety-reviewer": ("claude-opus-5", "xhigh", RoleCapability.READ_ONLY),
        "security-reviewer": ("claude-sonnet-5", "high", RoleCapability.READ_ONLY),
        "sim-roast-runner": ("claude-sonnet-5", "medium", RoleCapability.READ_ONLY),
        "story-planner": ("claude-opus-5", "high", RoleCapability.READ_ONLY),
    }
    assert tuple(role.value for role in NativeClaudeRole) == tuple(expected)
    for role in NativeClaudeRole:
        pin = usage_cli._native_role_pin(role)  # pyright: ignore[reportPrivateUsage]
        assert (pin.model, pin.effort, pin.capability) == expected[role.value]


def test_native_role_values_union_exclusions_equal_committed_agent_stems() -> None:
    """Every committed `.claude/agents/*.md` role is either native-capable or excluded."""
    agents_dir = Path(__file__).resolve().parents[1] / ".claude" / "agents"
    stems = {path.stem for path in agents_dir.glob("*.md")}
    native_values = {role.value for role in NativeClaudeRole}
    assert native_values.isdisjoint(NATIVE_ROLE_EXCLUSIONS)
    assert native_values | set(NATIVE_ROLE_EXCLUSIONS) == stems
    assert NATIVE_ROLE_EXCLUSIONS == {
        "ui-reviewer": (
            "its Playwright MCP conflicts with the empty-MCP, empty-tools native"
            " capture launch boundary"
        )
    }
    for reason in NATIVE_ROLE_EXCLUSIONS.values():
        assert isinstance(reason, str) and reason.strip()


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        (b"model: claude-sonnet-5", b"model: claude-fable-5"),
        (b"effort: high", b"effort: unsupported"),
        (b"tools: Read, Grep, Glob, Bash, Edit, Write", b"tools: Read, Grep, Glob, Edit, Edit"),
    ],
)
def test_native_role_frontmatter_mutations_fail_closed(
    target: bytes, replacement: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed native role model, effort, or tools never widens capture admission."""
    content = Path(".claude/agents/engineer-be.md").read_bytes().replace(target, replacement)

    def mutated_input(_path: Path) -> bytes:
        return content

    monkeypatch.setattr(usage_cli, "_input_bytes", mutated_input)
    with pytest.raises(CaptureUsageError, match="frontmatter is invalid"):
        usage_cli._native_role_pin(NativeClaudeRole.ENGINEER_BE)  # pyright: ignore[reportPrivateUsage]


class _NativeInput:
    """Minimal writable stdin retaining bytes only for provider-free assertions."""

    def __init__(self, *, fail: bool = False) -> None:
        """Initialize a successful or deliberately broken synthetic pipe."""
        self.value = b""
        self.closed = False
        self._fail = fail

    def write(self, value: bytes) -> int:
        """Retain the bounded prompt or simulate a closed child stdin."""
        if self._fail:
            raise BrokenPipeError
        self.value += value
        return len(value)

    def flush(self) -> None:
        """Model a successful pipe flush."""

    def close(self) -> None:
        """Mark the synthetic stdin as closed."""
        self.closed = True


class _NativeProcess:
    """Completed provider-free native or version process."""

    def __init__(self, output: bytes = b"", code: int = 0, *, prompt_fail: bool = False) -> None:
        """Initialize deterministic process output and exit state."""
        self.stdout = BytesIO(output)
        self.stdin = _NativeInput(fail=prompt_fail)
        self._code = code

    def poll(self) -> int:
        """Return the fixed completed status."""
        return self._code

    def wait(self, timeout: float | None = None) -> int:
        """Return the fixed completed status without blocking."""
        del timeout
        return self._code

    def terminate(self) -> None:
        """Reject stopping an already-complete synthetic process."""
        raise AssertionError("completed process must not terminate")

    def kill(self) -> None:
        """Reject killing an already-complete synthetic process."""
        raise AssertionError("completed process must not be killed")


_NATIVE_SESSION_ID = "11111111-1111-4111-8111-111111111233"


def _native_cli_args(role: str = "engineer-be") -> list[str]:
    """Return closed native command metadata for provider-free tests."""
    return [
        "run-native-claude",
        "--role",
        role,
        "--prompt-file",
        "prompt",
        "--task-id",
        "816",
        "--slice-id",
        "native-1",
        "--parent-task-id",
        "parent-816",
        "--repository",
        "syamaner/roastpilot-agent",
        "--branch",
        "feature/816-native-transcript-usage-1",
        "--base-sha",
        "4c1ac63",
    ]


def _native_transcript_bytes(session_id: str = _NATIVE_SESSION_ID) -> bytes:
    """Return the closed parent fixture bound to one generated session."""
    return (
        (FIXTURES / "claude-2.1.233-transcript" / "parent.jsonl")
        .read_bytes()
        .replace(b"11111111-1111-4111-8111-111111111233", session_id.encode())
    )


def _configure_native_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fixed_session: bool = True
) -> tuple[Path, list[tuple[list[str], dict[str, object]]]]:
    """Install a private Claude parent location and fixed launcher dependencies."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / usage_transcript._project_name(tmp_path)  # pyright: ignore[reportPrivateUsage]
    project.mkdir(parents=True)

    def fixed_executable(_family: HarnessFamily) -> str:
        return "claude"

    def fixed_attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        return "7d60f41" if post_exit else "4c1ac63"

    monkeypatch.setattr(usage_transcript.Path, "home", lambda: home)
    if fixed_session:
        monkeypatch.setattr(usage_cli, "uuid4", lambda: _NATIVE_SESSION_ID)
    agent_dir = Path(__file__).resolve().parents[1] / ".claude" / "agents"
    real_input_bytes = usage_cli._input_bytes  # pyright: ignore[reportPrivateUsage]

    def read_input(path: Path) -> bytes:
        if path.parent == Path(".claude") / "agents":
            return (agent_dir / path.name).read_bytes()
        return real_input_bytes(path)

    monkeypatch.setattr(usage_cli, "_input_bytes", read_input)
    monkeypatch.setattr(usage_cli, "_resolved_executable", fixed_executable)
    monkeypatch.setattr(usage_cli, "_validate_native_worktree", fixed_attestation)
    monkeypatch.setattr(usage_cli, "_utc_now", lambda: datetime(2026, 8, 18, tzinfo=UTC))
    Path("prompt").write_bytes(b"SENTINEL_NATIVE_PROMPT")
    return project, []


def _native_popen(
    project: Path,
    observed: list[tuple[list[str], dict[str, object]]],
    processes: list[_NativeProcess],
    *,
    code: int = 0,
    prompt_fail: bool = False,
    transcript: bytes | None = None,
) -> Callable[..., _NativeProcess]:
    """Return a fake version/worker launcher that creates only one parent transcript."""

    def fake(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            process = _NativeProcess(b"Claude Code 2.1.233\\n")
            processes.append(process)
            return process
        if transcript is not None:
            (project / f"{_NATIVE_SESSION_ID}.jsonl").write_bytes(transcript)
        process = _NativeProcess(code=code, prompt_fail=prompt_fail)
        processes.append(process)
        return process

    return fake


def test_native_command_generates_distinct_real_uuid_sessions_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each native launch owns a new UUIDv4, preventing transcript/preflight aliasing."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch, fixed_session=False)
    preflight_sessions: list[str] = []
    real_preflight = usage_transcript.reject_existing_owned_session

    def spy_preflight(cwd: Path, session_id: str) -> None:
        preflight_sessions.append(session_id)
        real_preflight(cwd, session_id)

    def fake(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            return _NativeProcess(b"Claude Code 2.1.233\n")
        session_id = argv[argv.index("--session-id") + 1]
        (project / f"{session_id}.jsonl").write_bytes(_native_transcript_bytes(session_id))
        return _NativeProcess()

    monkeypatch.setattr(usage_cli, "reject_existing_owned_session", spy_preflight)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake)
    assert main(_native_cli_args()) == 0
    assert main(_native_cli_args()) == 0

    worker_sessions = [
        argv[argv.index("--session-id") + 1] for argv, _ in observed if "--session-id" in argv
    ]
    assert len(worker_sessions) == len(preflight_sessions) == 2
    assert worker_sessions == preflight_sessions and worker_sessions[0] != worker_sessions[1]
    assert all(UUID(session_id).version == 4 for session_id in worker_sessions)
    records = [
        USAGE_RECORD_ADAPTER.validate_json(line)
        for line in Path(".agent-usage/usage.jsonl").read_text().splitlines()
    ]
    assert [
        record.session_id for record in records if isinstance(record, NativeWorkerUsageRecord)
    ] == worker_sessions


def test_native_command_launches_exact_worker_and_records_immutable_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D161 binds a UUID session, fixed argv and stdin to one immutable parent transcript."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    transcript = _native_transcript_bytes()
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=transcript),
    )

    assert main(_native_cli_args()) == 0

    assert len(observed) == 2
    argv, kwargs = observed[1]
    expected_argv = usage_cli._native_claude_argv(  # pyright: ignore[reportPrivateUsage]
        "claude",
        NativeClaudeRole.ENGINEER_BE,
        RoleCapability.WRITE,
        "high",
        _NATIVE_SESSION_ID,
    )
    assert argv == expected_argv
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.PIPE
    assert UUID(_NATIVE_SESSION_ID).version == 4
    assert processes[1].stdin.value == b"SENTINEL_NATIVE_PROMPT" and processes[1].stdin.closed
    stored = project / f"{_NATIVE_SESSION_ID}.jsonl"
    before = stored.stat()
    raw = Path(".agent-usage/usage.jsonl").read_text()
    record = USAGE_RECORD_ADAPTER.validate_json(raw)
    assert isinstance(record, NativeWorkerUsageRecord)
    assert record.success and record.usage_complete and record.final_head_sha == "7d60f41"
    assert (
        record.session_id == _NATIVE_SESSION_ID
        and record.native_role is NativeClaudeRole.ENGINEER_BE
    )
    assert b"SENTINEL_NATIVE_PROMPT" not in raw.encode()
    after = stored.stat()
    assert (after.st_ino, after.st_mtime_ns, stored.read_bytes()) == (
        before.st_ino,
        before.st_mtime_ns,
        transcript,
    )


def test_native_command_records_nonzero_complete_transcript_as_unsuccessful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed owned transcript retains usage even when its worker exits nonzero."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, code=2, transcript=_native_transcript_bytes()),
    )
    assert main(_native_cli_args()) == 0
    record = USAGE_RECORD_ADAPTER.validate_json(Path(".agent-usage/usage.jsonl").read_text())
    assert isinstance(record, NativeWorkerUsageRecord)
    assert not record.success and record.exit_code == 2 and record.usage_complete


def test_native_command_rejects_model_mismatch_without_sink_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete but differently pinned transcript never becomes an attributed record."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    mismatched = _native_transcript_bytes().replace(
        b'"model":"claude-sonnet-5"', b'"model":"claude-opus-5"'
    )
    processes: list[_NativeProcess] = []
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=mismatched),
    )

    with pytest.raises(SystemExit, match="native Claude transcript is invalid"):
        main(_native_cli_args())

    assert len(observed) == 2
    assert not Path(".agent-usage/usage.jsonl").exists()


@pytest.mark.parametrize(
    "failure",
    ["version", "prompt", "timeout", "post-dirty", "post-missing", "post-wrong-base", "transcript"],
)
def test_native_command_fails_closed_at_transcript_launch_boundaries(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Version, delivery, deadline, final provenance and absent transcript append nothing."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)

    def attestation(
        _arguments: argparse.Namespace, _capability: RoleCapability, *, post_exit: bool
    ) -> str:
        if post_exit and failure in {"post-dirty", "post-missing", "post-wrong-base"}:
            raise CaptureUsageError("native worktree attestation failed")
        return "7d60f41" if post_exit else "4c1ac63"

    def fake(argv: list[str], **kwargs: object) -> _NativeProcess:
        observed.append((argv, kwargs))
        if argv[-1] == "--version":
            version = (
                b"Claude Code 2.1.999\\n" if failure == "version" else b"Claude Code 2.1.233\\n"
            )
            return _NativeProcess(version)
        if failure != "transcript":
            (project / f"{_NATIVE_SESSION_ID}.jsonl").write_bytes(_native_transcript_bytes())
        return _NativeProcess(prompt_fail=failure == "prompt")

    monkeypatch.setattr(usage_cli, "_validate_native_worktree", attestation)
    monkeypatch.setattr(usage_cli.subprocess, "Popen", fake)
    if failure == "timeout":
        deadline_calls = 0

        def expire_worker(_process: _NativeProcess, _seconds: int, event: threading.Event) -> None:
            nonlocal deadline_calls
            deadline_calls += 1
            if deadline_calls == 2:
                event.set()

        monkeypatch.setattr(
            usage_cli,
            "_start_deadline",
            expire_worker,
        )
    with pytest.raises(SystemExit) as error:
        main(_native_cli_args())
    assert "SENTINEL_NATIVE_PROMPT" not in str(error.value)
    assert not Path(".agent-usage/usage.jsonl").exists()
    assert len(observed) == (1 if failure == "version" else 2)


@pytest.mark.parametrize("sink_kind", ["symlink", "fifo", "hardlink"])
def test_native_command_refuses_unsafe_sink_after_complete_transcript(
    sink_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript path cannot bypass shared final-sink admission."""
    project, observed = _configure_native_launcher(tmp_path, monkeypatch)
    processes: list[_NativeProcess] = []
    sink_parent = Path(".agent-usage")
    sink_parent.mkdir()
    sink = sink_parent / "usage.jsonl"
    target = Path("SENTINEL_UNSAFE_TARGET")
    if sink_kind == "symlink":
        target.write_text("unchanged\\n")
        sink.symlink_to(target)
    elif sink_kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("platform lacks FIFO support")
        os.mkfifo(sink)
    else:
        target.write_text("unchanged\\n")
        os.link(target, sink)
    monkeypatch.setattr(
        usage_cli.subprocess,
        "Popen",
        _native_popen(project, observed, processes, transcript=_native_transcript_bytes()),
    )
    with pytest.raises(SystemExit) as error:
        main(_native_cli_args())
    assert "SENTINEL_UNSAFE_TARGET" not in str(error.value)
    if sink_kind != "fifo":
        assert target.read_text() == "unchanged\\n"
    if sink_kind == "symlink":
        assert sink.is_symlink()
    elif sink_kind == "fifo":
        assert stat.S_ISFIFO(sink.stat().st_mode)
    else:
        assert sink.stat().st_nlink == 2


def test_native_config_directory_rejects_before_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user Claude configuration directory blocks lookup and worker launch."""
    _configure_native_launcher(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "SENTINEL_CONFIG")

    def no_provider(_family: HarnessFamily) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(
        usage_cli,
        "_resolved_executable",
        no_provider,
    )
    with pytest.raises(SystemExit, match="config directory is not permitted") as error:
        main(_native_cli_args())
    assert "SENTINEL_CONFIG" not in str(error.value)


def test_native_cli_script_suppresses_bytecode_only_when_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied CLI execution is bytecode-clean while import leaves interpreter policy unchanged."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(usage_cli.__file__)
    for name in (
        "capture_usage_cli.py",
        "capture_usage_models.py",
        "capture_usage_claude.py",
        "capture_usage_codex.py",
        "capture_usage_transcript.py",
    ):
        shutil.copy2(source.parent / name, scripts / name)
    executed = subprocess.run(
        [sys.executable, str(scripts / "capture_usage_cli.py"), "--help"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    assert executed.returncode == 0
    assert not list(scripts.rglob("__pycache__"))
    before = sys.dont_write_bytecode
    spec = importlib.util.spec_from_file_location(
        "capture_usage_cli_import_probe", scripts / source.name
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    assert sys.dont_write_bytecode is before
    monkeypatch.delitem(sys.modules, "capture_usage_cli_import_probe", raising=False)


def test_native_precheck_rejects_ignored_bytecode_before_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignored local bytecode is part of native launch admission, not a harmless exception."""
    monkeypatch.chdir(tmp_path)
    Path("prompt").write_bytes(b"safe")

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, "feature/816-native-transcript-usage-1"
        if argv == ["rev-parse", "HEAD"]:
            return 0, "4c1ac63"
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return 0, "4c1ac63"
        if argv == ["status", "--porcelain", "--ignored"]:
            return 0, "!! .claude/__pycache__/worker.pyc"
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)

    def valid_frontmatter(_path: Path) -> bytes:
        return b"---\nmodel: claude-sonnet-5\neffort: high\ntools: Edit\n---\n"

    monkeypatch.setattr(usage_cli, "_input_bytes", valid_frontmatter)

    def no_provider(_family: HarnessFamily) -> str:
        raise AssertionError("provider lookup")

    monkeypatch.setattr(
        usage_cli,
        "_resolved_executable",
        no_provider,
    )
    with pytest.raises(SystemExit, match="native worktree attestation failed"):
        main(_native_cli_args())


def test_native_worktree_attestation_binds_exact_launch_head_not_origin_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feature HEAD serialized ahead of `origin/main`'s merge base still attests.

    Native worktree attestation binds the supplied base to the exact launch
    ``HEAD``, never to ``merge-base(HEAD, origin/main)``: a worktree serialized
    behind an advancing default branch would fail an ``origin/main``-equality
    check even though its exact base is still correctly attested. The fake Git
    double below raises if that call is ever made, so a mutation reintroducing
    the merge-base equality requirement fails this test.
    """
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="7d60f41",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, "7d60f41"
        if argv == ["rev-parse", "--verify", "7d60f41^{commit}"]:
            return 0, "7d60f41"
        if argv in (["status", "--porcelain", "--ignored"], ["status", "--porcelain"]):
            return 0, ""
        if argv == ["merge-base", "HEAD", "origin/main"]:
            raise AssertionError(
                "native worktree attestation must not query the origin/main merge base"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)

    pre_exit_head = usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
        arguments, RoleCapability.READ_ONLY, post_exit=False
    )
    assert pre_exit_head == "7d60f41"
    assert arguments.base_sha == "7d60f41"

    post_exit_head = usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
        arguments, RoleCapability.READ_ONLY, post_exit=True
    )
    assert post_exit_head == "7d60f41"


def test_native_pre_exit_attestation_rejects_base_head_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-exit attestation requires the supplied base to equal the exact launch HEAD."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="4c1ac63",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, "7d60f41"
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return 0, "4c1ac63"
        if argv == ["status", "--porcelain", "--ignored"]:
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    with pytest.raises(CaptureUsageError, match="native worktree attestation failed"):
        usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
            arguments, RoleCapability.READ_ONLY, post_exit=False
        )


@pytest.mark.parametrize("failure", ["dirty", "missing-commit", "invalid-base", "wrong-branch"])
def test_native_post_exit_attestation_rejects_each_final_provenance_break(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final cleanliness, committed advance, a valid base and exact branch independently gate."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="4c1ac63",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, "wrong-branch" if failure == "wrong-branch" else arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, "4c1ac63" if failure == "missing-commit" else "7d60f41"
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return (1, "") if failure == "invalid-base" else (0, "4c1ac63")
        if argv == ["status", "--porcelain"]:
            return 0, " M ignored" if failure == "dirty" else ""
        if argv[:2] == ["merge-base", "--is-ancestor"]:
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    with pytest.raises(CaptureUsageError, match="native worktree attestation failed"):
        usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
            arguments,
            RoleCapability.WRITE,
            post_exit=True,
        )


@pytest.mark.parametrize(
    ("capability", "head", "accepted"),
    [
        (RoleCapability.READ_ONLY, "4c1ac63", True),
        (RoleCapability.READ_ONLY, "7d60f41", False),
        (RoleCapability.WRITE, "7d60f41", True),
        (RoleCapability.WRITE, "4c1ac63", False),
    ],
)
def test_native_post_exit_capability_binds_final_head(
    capability: RoleCapability, head: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only roles retain base HEAD; write roles must commit a base descendant."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="4c1ac63",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, head
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return 0, "4c1ac63"
        if argv in (["status", "--porcelain"], ["status", "--porcelain", "--ignored"]):
            return 0, ""
        if argv[:2] == ["merge-base", "--is-ancestor"]:
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    if accepted:
        assert (
            usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
                arguments, capability, post_exit=True
            )
            == head
        )
    else:
        with pytest.raises(CaptureUsageError, match="native worktree attestation failed"):
            usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
                arguments, capability, post_exit=True
            )


def test_read_only_post_exit_attestation_rejects_ignored_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only workers must leave neither tracked nor ignored worktree artifacts."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="4c1ac63",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, "4c1ac63"
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return 0, "4c1ac63"
        if argv == ["status", "--porcelain", "--ignored"]:
            return 0, "!! .claude/__pycache__/worker.pyc"
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    with pytest.raises(CaptureUsageError, match="native worktree attestation failed"):
        usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
            arguments, RoleCapability.READ_ONLY, post_exit=True
        )


def test_write_post_exit_attestation_keeps_ordinary_clean_tree_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write workers retain the ratified ordinary status check while requiring a descendant."""
    arguments = argparse.Namespace(
        repository="syamaner/roastpilot-agent",
        branch="feature/816-native-transcript-usage-1",
        base_sha="4c1ac63",
    )

    def fake_git(argv: list[str]) -> tuple[int, str]:
        if argv == ["remote", "get-url", "origin"]:
            return 0, "https://github.com/syamaner/roastpilot-agent.git"
        if argv == ["branch", "--show-current"]:
            return 0, arguments.branch
        if argv == ["rev-parse", "HEAD"]:
            return 0, "7d60f41"
        if argv == ["rev-parse", "--verify", "4c1ac63^{commit}"]:
            return 0, "4c1ac63"
        if argv == ["status", "--porcelain"]:
            return 0, ""
        if argv == ["status", "--porcelain", "--ignored"]:
            raise AssertionError("WRITE post-exit must not inspect ignored artifacts")
        if argv == ["merge-base", "--is-ancestor", "4c1ac63", "7d60f41"]:
            return 0, ""
        raise AssertionError(argv)

    monkeypatch.setattr(usage_cli, "_git_output", fake_git)
    assert (
        usage_cli._validate_native_worktree(  # pyright: ignore[reportPrivateUsage]
            arguments, RoleCapability.WRITE, post_exit=True
        )
        == "7d60f41"
    )


@pytest.fixture(autouse=True)
def isolate_run_metadata_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep launcher tests provider-free unless they exercise Git admission directly."""

    def skip_worktree_validation(_arguments: argparse.Namespace) -> None:
        return None

    monkeypatch.setattr(usage_cli, "_validate_worktree_metadata", skip_worktree_validation)


def _stream(*events: str) -> BytesIO:
    """Build a binary JSONL stream matching the future fixed subprocess stdout type."""
    return BytesIO("".join(events).encode("utf-8"))


def _parse_claude_lax(stream: BinaryIO) -> ParsedUsage:
    """Parse a sanitized fixture without asserting a live launch boundary."""
    return parse_claude_stream(stream, require_launch_authority=False)


def _exception_chain(exception: BaseException) -> tuple[BaseException, ...]:
    """Return every distinct exception reachable through cause or context."""
    chain: list[BaseException] = []
    pending = [exception.__cause__, exception.__context__]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend((current.__cause__, current.__context__))
    return tuple(chain)


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


def _opaque_item_event(payload_bytes: int) -> bytes:
    """Return one complete opaque item event with an exact payload byte count."""
    return b'{"payload":"' + (b"x" * payload_bytes) + b'","type":"item.updated"}\n'


class _LazyOpaqueItemStream:
    """Produce a large item event in bounded read responses without full materialization."""

    def __init__(self, payload_bytes: int, trailer: bytes = b"") -> None:
        """Initialize a deterministic payload length and bounded read accounting."""
        self._parts = [
            b'{"nested":{"type":"ignored"},"type":"item.updated","payload":"',
            b'"}\n' + trailer,
        ]
        self._payload_remaining = payload_bytes
        self._part_index = 0
        self._part_offset = 0
        self.requests: list[int] = []

    def read(self, size: int) -> bytes:
        """Return at most the requested bytes while never creating a large payload span."""
        self.requests.append(size)
        if size > READ_CHUNK_BYTES:
            raise AssertionError("parser requested an unbounded read")
        if self._part_index == 0:
            return self._take_prefix(size)
        if self._part_index == 1:
            count = min(size, self._payload_remaining)
            self._payload_remaining -= count
            if self._payload_remaining == 0:
                self._part_index = 2
            return b"x" * count
        if self._part_index == 2:
            return self._take_suffix(size)
        return b""

    def _take_prefix(self, size: int) -> bytes:
        """Return the initial structural bytes, preserving chunk boundaries."""
        prefix = self._parts[0]
        result = prefix[self._part_offset : self._part_offset + size]
        self._part_offset += len(result)
        if self._part_offset == len(prefix):
            self._part_index = 1
            self._part_offset = 0
        return result

    def _take_suffix(self, size: int) -> bytes:
        """Return the terminal string/object/newline bytes after the large payload."""
        suffix = self._parts[1]
        result = suffix[self._part_offset : self._part_offset + size]
        self._part_offset += len(result)
        if self._part_offset == len(suffix):
            self._part_index = 3
        return result


class _LazyTypeFirstStream:
    """Produce a large retained type-first event while recording bounded consumption."""

    def __init__(self, payload_bytes: int) -> None:
        """Initialize a retained type-first event whose payload need not materialize."""
        self._prefix = b'{"type":"turn.started","payload":"'
        self._payload_remaining = payload_bytes
        self._suffix = b'"}\n'
        self._suffix_offset = 0
        self.consumed = 0

    def read(self, size: int) -> bytes:
        """Return only bounded slices until the parser rejects this retained event."""
        if self._prefix:
            result = self._prefix[:size]
            self._prefix = self._prefix[len(result) :]
        elif self._payload_remaining:
            count = min(size, self._payload_remaining)
            self._payload_remaining -= count
            result = b"x" * count
        elif self._suffix_offset < len(self._suffix):
            result = self._suffix[self._suffix_offset : self._suffix_offset + size]
            self._suffix_offset += len(result)
        else:
            result = b""
        self.consumed += len(result)
        return result


class _RepeatedEventStream:
    """Stream repeated small events without materializing their aggregate byte total."""

    def __init__(self, event: bytes, repeats: int, trailer: bytes) -> None:
        """Initialize a finite repeated-event source plus a terminal trailer."""
        self._event = event
        self._repeats = repeats
        self._trailer = trailer
        self._index = 0
        self._offset = 0

    def read(self, size: int) -> bytes:
        """Return bounded fragments from the repeated stream."""
        assert size <= READ_CHUNK_BYTES
        source = self._event if self._index < self._repeats else self._trailer
        if self._index > self._repeats:
            return b""
        result = source[self._offset : self._offset + size]
        self._offset += len(result)
        if self._offset == len(source):
            self._offset = 0
            self._index += 1
        return result


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
    historical = [
        event
        for event in (
            json.loads(line)
            for line in (FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()
        )
        if event.get("type") != "user"
    ]
    init_event = next(event for event in historical if event.get("subtype") == "init")
    init_event["claude_code_version"] = "2.1.233"
    init_event["permissionMode"] = "plan"
    usage = parse_claude_stream(
        _stream("\n".join(json.dumps(event) for event in historical) + "\n"),
        require_launch_authority=False,
    )

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


def test_claude_2_1_233_fixture_matches_frozen_grammar_without_content() -> None:
    """The admitted Claude fixture changes neither grammar nor retained content."""
    fixture = FIXTURES / "claude-2.1.233.jsonl"
    events = [json.loads(line) for line in fixture.read_text().splitlines()]
    with fixture.open("rb") as stream:
        usage = parse_claude_stream(stream, require_launch_authority=True)

    assert {event["type"] for event in events} <= CLAUDE_EVENT_TYPES
    assert frozenset({"system", "assistant", "rate_limit_event", "result"}) == CLAUDE_EVENT_TYPES
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
    assert usage.input_tokens == 2
    assert usage.cached_input_tokens == 3_289
    assert usage.cache_creation_input_tokens == 3_441
    assert usage.output_tokens == 9
    assert usage.estimated_usd == pytest.approx(0.0223677)
    serialized = usage.model_dump_json()
    for sentinel in ("SANITIZED_MESSAGE", "SANITIZED_RESULT", "request_0", "message_0"):
        assert sentinel not in serialized

    terminal["usage"]["SANITIZED_MESSAGE"] = 0
    with pytest.raises(ClaudeUsageParseError) as exc_info:
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)
    assert "SANITIZED_MESSAGE" not in str(exc_info.value)


def test_claude_2_1_233_native_fixture_records_observed_stream_and_authority_rejection() -> None:
    """Native stream evidence parses laxly and proves strict authority rejects pre-init hooks."""
    fixture = FIXTURES / "claude-2.1.233-native.jsonl"
    events = [json.loads(line) for line in fixture.read_text().splitlines()]
    with fixture.open("rb") as stream:
        usage = parse_claude_stream(stream, require_launch_authority=False)

    assert {event["type"] for event in events} == CLAUDE_EVENT_TYPES
    assert {
        event["subtype"] for event in events if event["type"] == "system"
    } == CLAUDE_SYSTEM_SUBTYPES
    terminal = events[-1]
    assert terminal["subtype"] == "success" and terminal["is_error"] is False
    assert set(terminal["usage"]) == CLAUDE_RESULT_USAGE_KEYS
    assert set(terminal["modelUsage"]) == {"synthetic-native"}
    assert usage.claude_terminal_success is True
    assert usage.input_tokens == 2
    assert usage.cached_input_tokens == 50
    assert usage.cache_creation_input_tokens == 100
    assert usage.output_tokens == 9
    assert usage.claude_model_usage is not None
    assert [
        (item.model, item.input_tokens, item.output_tokens) for item in usage.claude_model_usage
    ] == [("synthetic-native", 2, 9)]
    serialized = usage.model_dump_json()
    for sentinel in ("message_synthetic_native", "request_synthetic_native", "00000000-"):
        assert sentinel not in serialized
    with fixture.open("rb") as stream, pytest.raises(ClaudeAuthorityError, match="not attested"):
        parse_claude_stream(stream, require_launch_authority=True)


def test_claude_retired_stream_event_type_fails_closed() -> None:
    """``user`` is retired: no supplied 2.1.233 fixture observes it as a stream event."""
    with pytest.raises(ClaudeUsageParseError, match="unknown Claude event type"):
        parse_claude_stream(_stream('{"type":"user"}\n'), require_launch_authority=False)


def test_claude_retired_default_permission_mode_fails_closed() -> None:
    """``default`` is retired: every supplied 2.1.233 init observes ``plan`` only."""
    init = json.loads((FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()[0])
    init["permissionMode"] = "default"
    for strict in (False, True):
        with pytest.raises(ClaudeAuthorityError, match="init authority is malformed"):
            parse_claude_stream(_stream(json.dumps(init) + "\n"), require_launch_authority=strict)


@pytest.mark.parametrize(
    "subtype",
    [
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_during_execution",
    ],
)
def test_claude_retired_failure_subtypes_fail_closed(subtype: str) -> None:
    """No admitted failure result subtype: none is proven by a supplied 2.1.233 fixture."""
    terminal = json.loads((FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()[-1])
    terminal["subtype"] = subtype
    terminal["is_error"] = True
    with pytest.raises(ClaudeUsageParseError, match="status is invalid"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


def test_claude_launch_authority_attestation_fails_closed() -> None:
    """Only the live Claude path admits one exact empty-tools init before result."""
    events = [
        json.loads(line) for line in (FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()
    ]
    init = events[0]
    terminal = events[-1]
    assert list(init) == [
        "agents",
        "analytics_disabled",
        "apiKeySource",
        "capabilities",
        "claude_code_version",
        "cwd",
        "fast_mode_disabled_reason",
        "fast_mode_state",
        "mcp_servers",
        "messaging_socket_path",
        "model",
        "output_style",
        "permissionMode",
        "plugins",
        "product_feedback_disabled",
        "session_id",
        "skills",
        "slash_commands",
        "subtype",
        "terminal_slash_commands",
        "tools",
        "type",
        "uuid",
    ]
    legacy = (FIXTURES / "claude-2.1.228.jsonl").read_bytes()
    for strict in (False, True):
        with pytest.raises(ClaudeUsageParseError, match="unverified Claude version"):
            parse_claude_stream(BytesIO(legacy), require_launch_authority=strict)

    for field, value in (("tools", ["SENTINEL_TOOL"]), ("mcp_servers", ["SENTINEL_MCP"])):
        drifted = {**init, field: value}
        with pytest.raises(ClaudeAuthorityError) as error:
            parse_claude_stream(
                _stream(json.dumps(drifted) + "\n", json.dumps(terminal) + "\n"),
                require_launch_authority=True,
            )
        assert "SENTINEL" not in str(error.value)

    for field, value in (("permissionMode", "unknown-SENTINEL"), ("permissionMode", 1)):
        malformed = {**init, field: value}
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError) as error:
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    require_launch_authority=strict,
                )
            assert "SENTINEL" not in str(error.value)

    for field, value in (("tools", None), ("mcp_servers", "none"), ("permissionMode", None)):
        malformed = {key: item for key, item in init.items() if key != field}
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError):
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    require_launch_authority=strict,
                )
        malformed[field] = value
        for strict in (False, True):
            with pytest.raises(ClaudeAuthorityError):
                parse_claude_stream(
                    _stream(json.dumps(malformed) + "\n", json.dumps(terminal) + "\n"),
                    require_launch_authority=strict,
                )

    duplicate = _stream(
        json.dumps(init) + "\n", json.dumps(init) + "\n", json.dumps(terminal) + "\n"
    )
    for strict in (False, True):
        with pytest.raises(ClaudeAuthorityError):
            parse_claude_stream(BytesIO(duplicate.getvalue()), require_launch_authority=strict)
    assert (
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n"), require_launch_authority=False
        ).input_tokens
        == 2
    )
    with pytest.raises(ClaudeAuthorityError):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=True)
    with pytest.raises(ClaudeAuthorityError):
        parse_claude_stream(
            _stream(json.dumps(terminal) + "\n", json.dumps(init) + "\n"),
            require_launch_authority=True,
        )

    for pre_init in (
        {"type": "assistant", "message": "PRE_INIT_SENTINEL"},
        {"type": "system", "subtype": "hook_started", "id": "PRE_INIT_SENTINEL"},
    ):
        stream = _stream(
            json.dumps(pre_init) + "\n", json.dumps(init) + "\n", json.dumps(terminal) + "\n"
        )
        with pytest.raises(ClaudeAuthorityError) as error:
            parse_claude_stream(BytesIO(stream.getvalue()), require_launch_authority=True)
        assert str(error.value) == "Claude init authority is not attested"
        assert "PRE_INIT_SENTINEL" not in str(error.value)
        assert (
            parse_claude_stream(
                BytesIO(stream.getvalue()), require_launch_authority=False
            ).input_tokens
            == 2
        )


def test_parse_claude_prints_only_normalized_usage(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inspection command validates lax init grammar without retaining authority data."""
    monkeypatch.chdir(tmp_path)
    stream = tmp_path / "claude.jsonl"
    stream.write_bytes((FIXTURES / "claude-2.1.233.jsonl").read_bytes())
    assert main(["parse-claude", str(stream)]) == 0
    output = capsys.readouterr().out
    assert '"input_tokens":2' in output
    for key in ("tools", "mcp_servers", "permissionMode", "session_id"):
        assert key not in output


@pytest.mark.parametrize(
    ("command", "fixture"),
    [
        ("parse-codex", "codex-0.147.0.jsonl"),
        ("parse-claude", "claude-2.1.233.jsonl"),
    ],
)
def test_detached_parse_commands_cannot_create_native_worker_usage_sink(
    command: str, fixture: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanitized inspection is detached from native-worker record ingestion."""
    monkeypatch.chdir(tmp_path)
    stream = tmp_path / "sanitized.jsonl"
    stream.write_bytes((FIXTURES / fixture).read_bytes())

    assert main([command, str(stream)]) == 0
    assert not (tmp_path / ".agent-usage").exists()
    with pytest.raises(SystemExit):
        usage_cli.build_parser().parse_args(["run-native-codex"])


def test_parse_claude_authority_error_is_fixed_and_creates_no_sink(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifted init exits through the Claude authority boundary without retaining input."""
    monkeypatch.chdir(tmp_path)
    events = [
        json.loads(line) for line in (FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()
    ]
    events[0]["permissionMode"] = "SENTINEL_DRIFT"
    stream = tmp_path / "claude-drifted.jsonl"
    stream.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    with pytest.raises(SystemExit) as error:
        main(["parse-claude", str(stream)])

    assert str(error.value) == "capture-agent-usage: Claude launch authority is not attested"
    assert "SENTINEL_DRIFT" not in str(error.value)
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".agent-usage").exists()


@pytest.mark.parametrize(
    ("parser", "event"),
    [
        (parse_codex_stream, '{"type":"unexpected"}\n'),
        (_parse_claude_lax, '{"type":"unexpected"}\n'),
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
        parse_claude_stream(
            _stream(json.dumps({"type": sentinel}) + "\n"), require_launch_authority=False
        )
    assert sentinel not in str(claude_type_error.value)

    with pytest.raises(ClaudeUsageParseError) as claude_subtype_error:
        parse_claude_stream(
            _stream(json.dumps({"type": "system", "subtype": sentinel}) + "\n"),
            require_launch_authority=False,
        )
    assert sentinel not in str(claude_subtype_error.value)


def test_malformed_or_missing_terminal_usage_fails_closed() -> None:
    """A zero-exit-like stream without required terminal usage cannot normalize."""
    with pytest.raises(CodexUsageParseError, match="terminal"):
        parse_codex_stream(_stream('{"type":"turn.started"}\n'))
    with pytest.raises(ClaudeUsageParseError, match="terminal"):
        parse_claude_stream(_stream('{"type":"assistant"}\n'), require_launch_authority=False)
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
        parse_claude_stream(BytesIO(b"\xff\n"), require_launch_authority=False)
    with pytest.raises(CodexUsageParseError, match="malformed Codex JSON"):
        parse_codex_stream(BytesIO(b"{not-json}\n"))


@pytest.mark.parametrize(
    "stream",
    [
        b'{"type":"item.updated","payload":"CHUNK_SENTINEL\xff"}\n',
        b'{"type":"item.updated","payload":"FLUSH_SENTINEL' + b"\xc3",
    ],
)
def test_codex_utf8_errors_do_not_chain_raw_provider_bytes(stream: bytes) -> None:
    """Chunk and final-flush UTF-8 errors retain no raw provider segment through chaining."""
    with pytest.raises(CodexUsageParseError, match="usage stream contains invalid UTF-8") as error:
        parse_codex_stream(BytesIO(stream))
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    assert "SENTINEL" not in repr(exception.args)


def test_malformed_retained_codex_json_does_not_chain_raw_provider_text() -> None:
    """A retained JSON decoder failure exposes neither sentinel text nor decoder context."""
    sentinel = "RETAINED_JSON_SENTINEL"
    with pytest.raises(CodexUsageParseError, match="malformed Codex JSONL event") as error:
        usage_codex._event_from_line(  # pyright: ignore[reportPrivateUsage]
            f'{{"type":"turn.started","payload":"{sentinel}",}}'
        )
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    assert sentinel not in repr(exception.args)


def _assert_no_exception_chain_or_sentinel(exception: BaseException, sentinel: str) -> None:
    """Assert that an ingestion error retains neither a chain nor provider content."""
    reachable = (exception, *_exception_chain(exception))
    assert len(reachable) == 1
    assert all(sentinel not in repr(item.args) and sentinel not in repr(item) for item in reachable)


def test_bounded_jsonl_utf8_error_does_not_retain_provider_bytes() -> None:
    """The binary reader severs invalid UTF-8 bytes before surfacing its fixed error."""
    sentinel = "UTF8_SENTINEL"
    with pytest.raises(BoundedStreamError, match="usage stream contains invalid UTF-8") as error:
        list(bounded_jsonl_lines(BytesIO(b'"UTF8_SENTINEL"\xff\n')))
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    _assert_no_exception_chain_or_sentinel(exception, sentinel)


def test_malformed_claude_json_does_not_retain_provider_text() -> None:
    """Claude JSON decoder failures expose only the fixed parser error."""
    sentinel = "JSON_SENTINEL"
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude JSONL event") as error:
        usage_claude._event_from_line(  # pyright: ignore[reportPrivateUsage]
            '{"type":"system","payload":"JSON_SENTINEL",}'
        )
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    _assert_no_exception_chain_or_sentinel(exception, sentinel)


def test_claude_utf8_error_does_not_retain_provider_bytes_transitively() -> None:
    """End-to-end Claude decoding retains no raw bytes through nested exception links."""
    sentinel = "E2E_SENTINEL"
    with pytest.raises(ClaudeUsageParseError, match="usage stream contains invalid UTF-8") as error:
        parse_claude_stream(BytesIO(b'{"k":"E2E_SENTINEL"\xff\n'), require_launch_authority=False)
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    _assert_no_exception_chain_or_sentinel(exception, sentinel)


def test_malformed_claude_stream_does_not_retain_provider_text() -> None:
    """End-to-end Claude JSON errors do not expose the malformed provider record."""
    sentinel = "MALFORMED_SENTINEL"
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude JSONL event") as error:
        parse_claude_stream(
            BytesIO(b'{"type" "MALFORMED_SENTINEL"}\n'), require_launch_authority=False
        )
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    _assert_no_exception_chain_or_sentinel(exception, sentinel)


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (b'{"type":"user"}', "usage stream contains a partial event"),
        (b"x" * (MAX_EVENT_BYTES + 1) + b"\n", "usage stream event exceeds size limit"),
    ],
)
def test_claude_bounded_stream_errors_preserve_fixed_messages(stream: bytes, message: str) -> None:
    """Claude translates every bounded-reader failure without changing its fixed text."""
    with pytest.raises(ClaudeUsageParseError, match=message) as error:
        parse_claude_stream(BytesIO(stream), require_launch_authority=False)
    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert exception.__suppress_context__
    assert _exception_chain(exception) == ()


def test_claude_duplicate_key_error_remains_outside_json_decode_translation() -> None:
    """Claude duplicate-key rejection retains its specific fixed parser error."""
    with pytest.raises(
        ClaudeUsageParseError, match="Claude event contains duplicate JSON keys"
    ) as error:
        usage_claude._event_from_line(  # pyright: ignore[reportPrivateUsage]
            '{"type":"system","type":"system"}'
        )
    assert _exception_chain(error.value) == ()


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


def test_codex_streams_a_five_mebibyte_opaque_item_with_bounded_reads() -> None:
    """A large nested/type-after-payload item is discarded before terminal usage retention."""
    stream = _LazyOpaqueItemStream(5 * 1024 * 1024, _codex_terminal_event())
    usage = parse_codex_stream(stream)
    assert usage.input_tokens == 1
    assert max(stream.requests) == READ_CHUNK_BYTES
    assert len(stream.requests) > 50
    assert "ignored" not in usage.model_dump_json()


def test_codex_rejects_type_first_retained_event_before_multi_mebibyte_consumption() -> None:
    """A known retained root type cannot consume the opaque eight-mebibyte allowance."""
    stream = _LazyTypeFirstStream(2 * 1024 * 1024)
    with pytest.raises(CodexUsageParseError, match="Codex retained event exceeds size limit"):
        parse_codex_stream(stream)
    assert MAX_EVENT_BYTES < stream.consumed <= MAX_EVENT_BYTES + READ_CHUNK_BYTES


def test_codex_accepts_late_discriminator_opaque_item_within_fixed_budget() -> None:
    """An item discriminator after a large payload retains its deliberate opaque allowance."""
    stream = _LazyOpaqueItemStream(2 * 1024 * 1024, _codex_terminal_event())
    assert parse_codex_stream(stream).input_tokens == 1


def test_codex_pipe_read1_rejects_before_writer_eof() -> None:
    """Buffered pipe parsing handles a type-first oversized event without awaiting writer EOF."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer_may_close = threading.Event()
    writer_started = threading.Event()

    def write_open_event() -> None:
        """Write a retained oversized event then deliberately retain the pipe's write end."""
        payload = b'{"type":"turn.started","payload":"' + (b"x" * (MAX_EVENT_BYTES + 1))
        writer_started.set()
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(write_fd, payload[offset:])
            writer_may_close.wait(timeout=2)
        except BrokenPipeError:
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write_open_event, daemon=True)
    writer.start()
    assert writer_started.wait(timeout=1)
    try:
        with pytest.raises(CodexUsageParseError, match="Codex retained event exceeds size limit"):
            parse_codex_stream(reader)
    finally:
        reader.close()
        writer_may_close.set()
        writer.join(timeout=2)
    assert not writer.is_alive()


def test_codex_rejects_blank_jsonl_event() -> None:
    """Whitespace-only Codex JSONL events do not pass scanner completion."""
    with pytest.raises(CodexUsageParseError, match="blank Codex JSONL event"):
        parse_codex_stream(BytesIO(b" \t\r\n"))


def test_codex_counts_small_items_as_opaque_not_retained_budget() -> None:
    """Many valid sub-64-KiB item events can exceed the retained budget before a terminal."""
    item = _opaque_item_event(32_000)
    usage = parse_codex_stream(BytesIO(item * 40 + _codex_terminal_event()))
    assert usage.output_tokens == 1


@pytest.mark.stress
def test_codex_opaque_event_boundary_is_exact() -> None:
    """The real opaque item event byte limit accepts exact and rejects one over."""
    prefix = b'{"type":"item.updated","payload":"'
    suffix = b'"}\n'
    exact_payload = MAX_CODEX_OPAQUE_EVENT_BYTES - len(prefix) - len(suffix)
    exact = prefix + (b"x" * exact_payload) + suffix
    assert parse_codex_stream(BytesIO(exact + _codex_terminal_event())).input_tokens == 1
    with pytest.raises(CodexUsageParseError, match="opaque event exceeds size limit"):
        parse_codex_stream(BytesIO(prefix + (b"x" * (exact_payload + 1)) + suffix))


def _opaque_total_boundary_parts() -> tuple[bytes, int]:
    """Build one real aggregate-limit event and its exact repeat count."""
    prefix = b'{"type":"item.updated","payload":"'
    suffix = b'"}\n'
    event_bytes = 128 * 1024
    medium = prefix + (b"x" * (event_bytes - len(prefix) - len(suffix))) + suffix
    assert len(medium) == event_bytes
    repeats = MAX_CODEX_OPAQUE_TOTAL_BYTES // len(medium)
    assert repeats * len(medium) == MAX_CODEX_OPAQUE_TOTAL_BYTES
    return medium, repeats


@pytest.mark.stress
def test_codex_opaque_total_boundary_accepts_exact_limit() -> None:
    """The real aggregate opaque stream byte limit accepts its exact boundary."""
    medium, repeats = _opaque_total_boundary_parts()
    assert (
        parse_codex_stream(
            _RepeatedEventStream(medium, repeats, _codex_terminal_event())
        ).input_tokens
        == 1
    )


@pytest.mark.stress
def test_codex_opaque_total_boundary_rejects_one_over() -> None:
    """The real aggregate opaque stream byte limit rejects one event over."""
    medium, repeats = _opaque_total_boundary_parts()
    with pytest.raises(CodexUsageParseError, match="opaque stream exceeds total byte limit"):
        parse_codex_stream(_RepeatedEventStream(medium, repeats + 1, _codex_terminal_event()))


def test_codex_opaque_event_boundary_is_fast_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reduced valid opaque event bound preserves exact comparison behaviour."""
    event_limit = 4 * MAX_EVENT_BYTES
    monkeypatch.setattr(usage_codex, "MAX_CODEX_OPAQUE_EVENT_BYTES", event_limit)
    prefix = b'{"type":"item.updated","payload":"'
    suffix = b'"}\n'
    exact_payload = event_limit - len(prefix) - len(suffix)
    exact = prefix + (b"x" * exact_payload) + suffix
    assert parse_codex_stream(BytesIO(exact + _codex_terminal_event())).input_tokens == 1
    with pytest.raises(CodexUsageParseError, match="opaque event exceeds size limit"):
        parse_codex_stream(BytesIO(prefix + (b"x" * (exact_payload + 1)) + suffix))


def test_codex_opaque_total_boundary_is_fast_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reduced valid opaque total bound preserves exact comparison behaviour."""
    medium = _opaque_item_event(80_000)
    event_limit = 4 * MAX_EVENT_BYTES
    total_limit = 4 * len(medium)
    monkeypatch.setattr(usage_codex, "MAX_CODEX_OPAQUE_EVENT_BYTES", event_limit)
    monkeypatch.setattr(usage_codex, "MAX_CODEX_OPAQUE_TOTAL_BYTES", total_limit)
    assert event_limit <= total_limit
    assert (
        parse_codex_stream(
            _RepeatedEventStream(medium, total_limit // len(medium), _codex_terminal_event())
        ).input_tokens
        == 1
    )
    with pytest.raises(CodexUsageParseError, match="opaque stream exceeds total byte limit"):
        parse_codex_stream(
            _RepeatedEventStream(medium, (total_limit // len(medium)) + 1, _codex_terminal_event())
        )


@pytest.mark.parametrize(
    "event",
    [
        b'{"type":"turn.completed","payload":"' + (b"x" * MAX_EVENT_BYTES) + b'"}\n',
        b'{"type":"turn.failed","payload":"' + (b"x" * MAX_EVENT_BYTES) + b'"}\n',
        b'{"type":"unexpected","payload":"' + (b"x" * MAX_EVENT_BYTES) + b'"}\n',
        b'{"payload":"' + (b"x" * MAX_EVENT_BYTES) + b'"}\n',
        b'{"type":7,"payload":"' + (b"x" * MAX_EVENT_BYTES) + b'"}\n',
    ],
)
def test_oversized_non_item_events_fail_closed(event: bytes) -> None:
    """Terminal, unknown, missing, and non-string discriminators never enter discard mode."""
    with pytest.raises(CodexUsageParseError) as error:
        parse_codex_stream(BytesIO(event))
    assert "x" * 20 not in str(error.value)


@pytest.mark.parametrize(
    "event",
    [
        b'{"type":"item.updated","type":"item.updated","payload":"x"}\n',
        b'{"payload":"x","type":"item.updated","type":"item.updated"}\n',
        b'{"type":"item.updated","payload":"x"} {}\n',
        b'{"type":"item.updated","payload":"unterminated}\n',
        b'{"type":"item.updated","payload":"x"}',
        b'{"type":"item.updated","payload":"\xff"}\n',
    ],
)
def test_opaque_scanner_rejects_ambiguous_or_incomplete_json(event: bytes) -> None:
    """Discard requires one unambiguous root type and complete strict JSON through newline."""
    with pytest.raises(CodexUsageParseError) as error:
        parse_codex_stream(BytesIO(event))
    assert "payload" not in str(error.value)


def test_opaque_scanner_ignores_nested_type_and_counts_discarded_events() -> None:
    """Only root type counts, while discarded events still consume the global event budget."""
    item = b'{"nested":{"type":"turn.completed"},"type":"item.updated"}\n'
    assert parse_codex_stream(BytesIO(item + _codex_terminal_event())).input_tokens == 1
    with pytest.raises(CodexUsageParseError, match="event count limit"):
        parse_codex_stream(BytesIO(item * (MAX_EVENT_COUNT + 1)))


@pytest.mark.parametrize(
    "number", [b"0", b"-0", b"1", b"-1.5", b"1e2", b"1e+2", b"1e-2", b"-1.5E+3"]
)
def test_opaque_scanner_accepts_rfc_json_number_forms(number: bytes) -> None:
    """Opaque numeric values follow stdlib JSON's ASCII signed-exponent grammar."""
    event = b'{"type":"item.updated","value":' + number + b"}\n"
    assert parse_codex_stream(BytesIO(event + _codex_terminal_event())).input_tokens == 1


def test_opaque_scanner_handles_large_signed_exponents_and_rejects_unicode_digits() -> None:
    """Oversized opaque values retain RFC numeric grammar without leaking rejected payload bytes."""
    payload = b"x" * MAX_EVENT_BYTES
    valid = b'{"type":"item.updated","payload":"' + payload + b'","value":1e-2}\n'
    assert parse_codex_stream(BytesIO(valid + _codex_terminal_event())).input_tokens == 1

    unicode_digit = "١".encode()
    invalid = (
        b'{"type":"item.updated","payload":"' + payload + b'","value":' + unicode_digit + b"}\n"
    )
    with pytest.raises(CodexUsageParseError, match="malformed Codex JSON event") as error:
        parse_codex_stream(BytesIO(invalid))
    assert unicode_digit.decode() not in str(error.value)


def test_opaque_scanner_enforces_json_nesting_depth_boundary() -> None:
    """Opaque item structures accept the fixed shallow depth and reject one level deeper."""
    prefix = b'{"type":"item.updated","payload":'
    suffix = b"}\n"
    exact_nesting = MAX_JSON_NESTING_DEPTH - 1
    exact = prefix + (b"[" * exact_nesting) + b"0" + (b"]" * exact_nesting) + suffix
    assert parse_codex_stream(BytesIO(exact + _codex_terminal_event())).input_tokens == 1

    one_over = prefix + (b"[" * (exact_nesting + 1)) + b"0" + (b"]" * (exact_nesting + 1)) + suffix
    with pytest.raises(CodexUsageParseError, match="malformed Codex JSON event"):
        parse_codex_stream(BytesIO(one_over))


@pytest.mark.parametrize(
    "event",
    [
        b'{"before":"split\\u003a\\"escape","nested":{"type":"ignored"},"type":"item.completed"}\n',
        b'{"type":"item.updated",}\n',
        b'{"type":"item.updated","values":[1,]}\n',
    ],
)
def test_opaque_scanner_handles_chunk_split_escapes_and_rejects_trailing_commas(
    event: bytes,
) -> None:
    """Scanner state crosses one-byte reads without accepting malformed JSON punctuation."""

    class OneByteStream:
        """Expose exactly one byte per bounded reader request."""

        def __init__(self, content: bytes) -> None:
            self._content = content
            self._offset = 0

        def read(self, size: int) -> bytes:
            assert size <= READ_CHUNK_BYTES
            if self._offset == len(self._content):
                return b""
            result = self._content[self._offset : self._offset + 1]
            self._offset += 1
            return result

    if event.endswith(b'"}\n'):
        assert parse_codex_stream(OneByteStream(event + _codex_terminal_event())).input_tokens == 1
    else:
        with pytest.raises(CodexUsageParseError, match="malformed Codex JSON"):
            parse_codex_stream(OneByteStream(event))


def test_parse_codex_streams_large_secure_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI parses a disk file above the old materialization cap without retaining it."""
    monkeypatch.chdir(tmp_path)
    path = Path("opaque.jsonl")
    path.write_bytes(_opaque_item_event((2 * 1024 * 1024) - 64) + _codex_terminal_event())
    assert main(["parse-codex", str(path)]) == 0


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
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


def test_claude_terminal_usage_extra_or_model_usage_drift_fails_closed() -> None:
    """Unknown terminal or per-model schema keys cannot be silently accepted."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="usage schema"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


def test_claude_duplicate_json_keys_fail_closed() -> None:
    """Duplicate result or model-usage keys cannot select a retained value silently."""
    duplicate_result = b'{"type":"result","type":"result"}\n'
    duplicate_model = (
        b'{"type":"result","subtype":"success","is_error":false,"usage":{},'
        b'"modelUsage":{"model":{"inputTokens":1,"inputTokens":2}}}\n'
    )
    for stream in (duplicate_result, duplicate_model):
        with pytest.raises(ClaudeUsageParseError, match="duplicate JSON keys"):
            parse_claude_stream(BytesIO(stream), require_launch_authority=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [("subtype", "error"), ("subtype", "unknown"), ("is_error", True), ("is_error", "false")],
)
def test_claude_terminal_requires_observed_success_status(field: str, value: object) -> None:
    """Only the observed success/false result status may supply terminal usage."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal[field] = value
    with pytest.raises(ClaudeUsageParseError, match="status is invalid"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


@pytest.mark.parametrize(
    "subtype",
    [
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_during_execution",
    ],
)
def test_claude_2_1_228_failure_statuses_are_retired_rejection_evidence(subtype: str) -> None:
    """The 2.1.228 failure subtypes are retired: no 2.1.233 fixture proves one is admitted."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["subtype"] = subtype
    terminal["is_error"] = True
    with pytest.raises(ClaudeUsageParseError, match="status is invalid"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


def test_claude_malformed_top_level_usage_and_non_finite_model_sum_fail_closed() -> None:
    """Top-level fields remain validated even though modelUsage alone supplies totals."""
    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["usage"]["input_tokens"] = "invalid"
    with pytest.raises(ClaudeUsageParseError, match="malformed Claude usage field"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    for model in terminal["modelUsage"].values():
        model["costUSD"] = 1e308
    with pytest.raises(ClaudeUsageParseError, match="modelUsage costUSD sum"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)

    terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    terminal["modelUsage"]["synthetic-primary"]["unknown"] = 0
    with pytest.raises(ClaudeUsageParseError, match="modelUsage schema"):
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


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
        parse_claude_stream(_stream(json.dumps(terminal) + "\n"), require_launch_authority=False)


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


def test_parse_codex_rejects_final_symlink_without_leaking_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sanitized parse command refuses a final symlink before reading its target."""
    monkeypatch.chdir(tmp_path)
    target = Path("TARGET_PATH_SECRET.jsonl")
    target.write_text("TARGET_CONTENT_SENTINEL\n")
    link = Path("SYMLINK_PATH_SECRET.jsonl")
    link.symlink_to(target)

    with pytest.raises(SystemExit, match="input file cannot be safely opened") as error:
        main(["parse-codex", str(link)])
    captured = capsys.readouterr()
    output = f"{error.value}{captured.out}{captured.err}"
    assert target.read_text() == "TARGET_CONTENT_SENTINEL\n"
    assert "TARGET_CONTENT_SENTINEL" not in output
    assert target.name not in output
    assert link.name not in output


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
                version = "0.147.0" if harness == "codex" else "2.1.233"
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
    claude_init = (FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()[0]
    claude_terminal = json.loads((FIXTURES / "claude-2.1.228.jsonl").read_text().splitlines()[-1])
    claude_terminal["subtype"] = "error_max_turns"
    claude_terminal["is_error"] = True
    result, error = run_for(
        (claude_init + "\n" + json.dumps(claude_terminal) + "\n").encode(), 3, "claude"
    )
    assert result is None and "stream is invalid" in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    success_terminal = json.loads((FIXTURES / "claude-2.1.233.jsonl").read_text().splitlines()[-1])
    result, record = run_for(
        (claude_init + "\n" + json.dumps(success_terminal) + "\n").encode(), 0, "claude"
    )
    assert result == 0
    parsed = json.loads(record)
    assert parsed["success"] and parsed["usage_complete"]

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    drifted_init = json.loads(claude_init)
    drifted_init["tools"] = ["SENTINEL_TOOL"]
    result, error = run_for(
        (json.dumps(drifted_init) + "\n" + json.dumps(success_terminal) + "\n").encode(),
        0,
        "claude",
    )
    assert (
        result is None and error == "capture-agent-usage: Claude launch authority is not attested"
    )
    assert "SENTINEL_TOOL" not in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    for pre_init in (
        {"type": "assistant", "message": "PRE_INIT_SENTINEL"},
        {"type": "system", "subtype": "hook_response", "id": "PRE_INIT_SENTINEL"},
    ):
        result, error = run_for(
            (
                json.dumps(pre_init)
                + "\n"
                + claude_init
                + "\n"
                + json.dumps(success_terminal)
                + "\n"
            ).encode(),
            0,
            "claude",
        )
        assert result is None
        assert error == "capture-agent-usage: Claude launch authority is not attested"
        assert "PRE_INIT_SENTINEL" not in error
        assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    result, record = run_for(b'{"type":"assistant"}\n', 3, "claude")
    assert result == 0
    parsed = json.loads(record)
    assert not parsed["success"] and not parsed["usage_complete"]
    assert parsed["input_tokens"] is None and parsed["estimated_usd"] is None

    (tmp_path / ".agent-usage/usage.jsonl").unlink()
    result, error = run_for(
        (claude_init + "\n" + json.dumps(success_terminal) + "\n").encode(), 3, "claude"
    )
    assert result is None and "terminal status disagrees" in error
    assert not (tmp_path / ".agent-usage/usage.jsonl").exists()

    failure_terminal = dict(success_terminal)
    failure_terminal["subtype"] = "error_max_turns"
    failure_terminal["is_error"] = True
    result, error = run_for(
        (claude_init + "\n" + json.dumps(failure_terminal) + "\n").encode(), 0, "claude"
    )
    assert result is None and "stream is invalid" in error
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
        ("claude", "2.1.231"),
        ("claude", "2.1.232"),
        ("claude", "2.1.234"),
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

        def read(self, _: int) -> bytes:
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
        *(role.value for role in NativeClaudeRole),
        "repair",
        "Engineer-BE",
        "REPAIR",
        "engineer_be",
        "engineer.fe",
        "engineer:be",
        "engineer--be",
        "repair-",
        "ENGINEER:_BE",
        "Safety-Reviewer",
        "SAFETY_REVIEWER",
        "story_planner",
        "Story:Planner",
        "mcp.contract.checker",
        "MCP-CONTRACT-CHECKER",
        "Sim-Roast-Runner",
        "PR_TRIAGE",
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


@pytest.mark.parametrize(
    "role",
    [
        "measurement-pilot",
        "repair-audit",
        "engineer-be-audit",
        "qa-adjacent",
        "story-planner-lite",
        "safety-reviewer-summary",
        "audit-mcp-contract-checker",
    ],
)
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


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (CodexUsageParseError, "Codex usage stream is invalid"),
        (ClaudeAuthorityError, "Claude launch authority is not attested"),
        (ClaudeUsageParseError, "Claude usage stream is invalid"),
        (OSError, "local filesystem operation failed"),
        (ValueError, "metadata input is invalid"),
    ],
)
def test_cli_fixed_error_mapping_severs_every_exception_chain(
    error_type: type[Exception], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-level categories never retain the raw exception that selected them."""

    def fail(_arguments: argparse.Namespace) -> int:
        raise error_type("SENTINEL_MAIN_ERROR")

    class StubParser:
        def parse_args(self, _argv: object) -> argparse.Namespace:
            return argparse.Namespace(handler=fail)

    monkeypatch.setattr(usage_cli, "build_parser", StubParser)
    with pytest.raises(SystemExit, match=message) as error:
        main([])
    _assert_no_exception_chain_or_sentinel(error.value, "SENTINEL_MAIN_ERROR")
